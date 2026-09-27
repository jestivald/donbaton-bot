"""Политика Wi-Fi/LTE поверх общего набора полей — шов ДонБатона.

У обычной подписки то, что уезжает в панель, определяет тариф: его лимит
трафика, его сквады, его стратегия сброса. У подписки категории Wi-Fi/LTE — нет:
там лимит панели ВСЕГДА 0/NO_RESET, а доступ к LTE-сквадам выдаётся и отбирается
собственным леджером (`app/services/lte_quota_service.py`), который считает
логические байты сам и не доверяет счётчику панели как лимитеру.

До 4.8.0 это накладывалось семью вызовами ``apply_panel_policy`` в
``subscription_service`` — по одному на каждую копию кода «отправить подписку в
панель». Апстрим схлопнул эти копии в ``panel_sync.push_subscription``, и
политике осталась ровно одна точка входа: здесь. Всё, что пишет подписку в
панель, проходит через ``push_subscription``, а он через ``apply_managed_policy``
— значит, тарифные сквады и настоящий лимит трафика мимо квоты уехать не могут.

Импорты леджера — поздние, внутри функций: ``lte_quota_service`` сам пишет в
панель через ``panel_sync.writer``, и модульный импорт замкнул бы круг.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import structlog

from app.services.panel_sync.identity import PanelIdentity
from app.services.panel_sync.payload import POLICY_FIELDS, PanelPayload


logger = structlog.get_logger(__name__)


async def apply_managed_policy(
    db,
    subscription,
    payload: PanelPayload,
    *,
    api=None,
    identity: PanelIdentity | None = None,
    panel_user=None,
    now: datetime | None = None,
) -> PanelPayload:
    """Наложить политику Wi-Fi/LTE на набор полей. Для прочих подписок — no-op.

    ``panel_user`` нужен леджеру не для записи, а для СНЯТИЯ ПРОБЫ счётчика:
    из него берётся ``lifetimeUsedTrafficBytes``, и без пробы квота остаётся без
    базовой точки, а подписка — без LTE-сквадов до следующего прохода воркера.
    Поэтому, если аккаунт панели ещё не прочитан, но его адрес известен, читаем
    его здесь: массовый проход намеренно экономит этот GET (``verify_recorded_id
    =False``), а управляемой подписке он необходим.
    """
    from app.services.lte_quota_service import LtePolicyError, apply_panel_policy, is_managed

    if payload.managed:
        # Политику уже наложил вызывающий (см. subscription_service.update_remnawave_user,
        # где тот же набор полей нужен ещё и грейсу). Второй проход снял бы
        # пробу счётчика повторно и заново взял блокировку квоты.
        return payload

    if not is_managed(subscription):
        # Тот же сторож, что и до 4.8.0: у тарифа категория Wi-Fi/LTE, а квоты
        # у подписки нет — молча уехать в панель тарифными сквадами и настоящим
        # лимитом значит продать безлимитный LTE.
        await apply_panel_policy(db, subscription, {}, now=now)
        return payload

    if db is None:
        raise LtePolicyError('Политике Wi-Fi/LTE нужна сессия базы: без неё квоту не прочитать')

    if panel_user is None and identity is not None:
        panel_user = identity.panel_user
        if panel_user is None and identity.known_id is not None and api is not None:
            panel_user = await api.get_user_by_id(identity.known_id)

    return await _project(db, subscription, payload, panel_user=panel_user, now=now)


async def reproject_after_create(
    db,
    subscription,
    payload: PanelPayload,
    panel_user,
    *,
    update,
    now: datetime | None = None,
):
    """Досэмплировать счётчик только что заведённого аккаунта и досыпать LTE.

    У нового аккаунта панели пробы ещё не было, поэтому первая проекция знает
    только Wi-Fi: ``accounting_state`` у квоты не 'ready', и LTE-сквады политика
    не выдаёт. Ответ на ``create_user`` — первая возможность снять базовую точку;
    сняв её, политику надо посчитать заново и дослать результат, иначе только что
    оплаченный Wi-Fi + LTE остаётся без LTE до следующего прохода воркера квот.

    Ровно это делали обе копии create-пути до 4.8.0 вторым ``update_user``.
    """
    from app.services.lte_quota_service import is_managed

    if not is_managed(subscription):
        return panel_user
    panel_user_id = getattr(panel_user, 'id', None)
    if panel_user_id is None:
        return panel_user

    # `managed=False` — просим посчитать политику заново, теперь уже с пробой.
    projected = await _project(db, subscription, replace(payload, managed=False), panel_user=panel_user, now=now)
    reprojected = await update(
        **projected.update_kwargs(user_id=panel_user_id, now=now, only_fields=set(POLICY_FIELDS))
    )
    return reprojected or panel_user


async def _project(db, subscription, payload: PanelPayload, *, panel_user, now) -> PanelPayload:
    """Переложить набор полей в словарь леджера и обратно.

    Леджер говорит на языке kwargs ``RemnaWaveAPI`` — так его и покрывают тесты
    ДонБатона. Здесь только перевод, никакой своей логики: любое правило про то,
    что уходит в панель, обязано остаться в одном месте — в ``apply_panel_policy``.
    """
    from app.services.lte_quota_service import apply_panel_policy

    request = {
        'status': payload.status,
        'traffic_limit_bytes': payload.traffic_limit_bytes,
        'traffic_limit_strategy': payload.traffic_limit_strategy,
        'active_internal_squads': list(payload.active_internal_squads),
    }
    if payload.hwid_device_limit is not None:
        request['hwid_device_limit'] = payload.hwid_device_limit

    projected = await apply_panel_policy(db, subscription, request, panel_user=panel_user, now=now)

    return replace(
        payload,
        status=projected.get('status', payload.status),
        traffic_limit_bytes=projected['traffic_limit_bytes'],
        traffic_limit_strategy=projected['traffic_limit_strategy'],
        active_internal_squads=tuple(projected['active_internal_squads']),
        hwid_device_limit=projected.get('hwid_device_limit', payload.hwid_device_limit),
        managed=True,
        # Дату политика называет только окну оплаты (конец окна).
        payment_window_until=projected.get('expire_at'),
    )
