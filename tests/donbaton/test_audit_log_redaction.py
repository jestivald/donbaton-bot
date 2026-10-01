"""Аудит-лог админки не хранит секреты из тел запросов (аудит безопасности 01.10.2026, §2.7).

``require_permission`` пишет в ``admin_audit_log.details`` тело каждого успешного
POST/PUT/PATCH/DELETE. Новое значение ключа платёжки, SMTP или OAuth уезжало туда
открытым текстом — его читают роли с ``audit_log:read`` и оно попадает в дампы БД.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from app.cabinet import dependencies
from app.cabinet.dependencies import AUDIT_SECRET_MASK, redact_audit_payload, require_permission
from app.services.permission_service import PermissionService


def admin_request(method: str, path: str, body: dict, *, path_params=None, query: bytes = b'') -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {'type': 'http.request', 'body': raw, 'more_body': False}

    scope = {
        'type': 'http',
        'method': method,
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'scheme': 'https',
        'query_string': query,
        'headers': [(b'user-agent', b'pytest'), (b'content-type', b'application/json')],
        'client': ('203.0.113.7', 4242),
        'server': ('lk.example', 443),
        'path_params': path_params or {},
    }
    return Request(scope, receive)


async def audited_details(monkeypatch, request: Request, permission: str = 'settings:edit') -> dict:
    log_action = AsyncMock()
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(True, 'ok')))
    monkeypatch.setattr(PermissionService, 'log_action', log_action)
    monkeypatch.setattr(dependencies, 'get_client_ip', MagicMock(return_value='203.0.113.7'))
    dependency = require_permission(permission)
    await dependency(request=request, user=SimpleNamespace(id=1), db=AsyncMock())
    success = [call for call in log_action.await_args_list if call.kwargs.get('status') == 'success']
    assert len(success) == 1
    return success[0].kwargs['details']


async def test_secret_setting_value_from_path_key_is_masked(monkeypatch):
    request = admin_request(
        'PUT',
        '/cabinet/admin/settings/YOOKASSA_SECRET_KEY',
        {'value': 'live_secret_123'},
        path_params={'key': 'YOOKASSA_SECRET_KEY'},
    )
    details = await audited_details(monkeypatch, request)
    assert details['request_body'] == {'value': AUDIT_SECRET_MASK}
    assert 'live_secret_123' not in json.dumps(details)
    assert details['path'] == '/cabinet/admin/settings/YOOKASSA_SECRET_KEY'  # что меняли — видно


async def test_ordinary_setting_value_stays_readable(monkeypatch):
    request = admin_request(
        'PUT',
        '/cabinet/admin/settings/SUPPORT_USERNAME',
        {'value': '@support'},
        path_params={'key': 'SUPPORT_USERNAME'},
    )
    details = await audited_details(monkeypatch, request)
    assert details['request_body'] == {'value': '@support'}


async def test_nested_password_and_keys_are_masked(monkeypatch):
    body = {
        'name': 'Ops',
        'smtp': {'host': 'smtp.example', 'password': 'p@ss', 'port': 587},
        'providers': [{'title': 'Google', 'client_secret': 's3cr3t'}, {'title': 'X', 'apiKey': 'k1', 'api_key': 'k2'}],
        'bot_token': '123:ABC',
        'private_key_pem': '-----BEGIN',
    }
    request = admin_request('POST', '/cabinet/admin/integrations', body, query=b'access_token=qq&page=2')
    details = await audited_details(monkeypatch, request, permission='settings:edit')
    redacted = details['request_body']
    assert redacted['name'] == 'Ops'
    assert redacted['smtp'] == {'host': 'smtp.example', 'password': AUDIT_SECRET_MASK, 'port': 587}
    assert redacted['providers'][0] == {'title': 'Google', 'client_secret': AUDIT_SECRET_MASK}
    assert redacted['providers'][1] == {'title': 'X', 'apiKey': AUDIT_SECRET_MASK, 'api_key': AUDIT_SECRET_MASK}
    assert redacted['bot_token'] == AUDIT_SECRET_MASK and redacted['private_key_pem'] == AUDIT_SECRET_MASK
    assert details['query_params'] == {'access_token': AUDIT_SECRET_MASK, 'page': '2'}
    dumped = json.dumps(details)
    for secret in ('p@ss', 's3cr3t', 'k1', 'k2', '123:ABC', 'BEGIN', 'qq'):
        assert secret not in dumped


@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        ({'key': 'CASHERA_API_SECRET', 'value': 'sk_x'}, {'key': 'CASHERA_API_SECRET', 'value': AUDIT_SECRET_MASK}),
        ({'key': 'SUPPORT_USERNAME', 'value': '@s'}, {'key': 'SUPPORT_USERNAME', 'value': '@s'}),
        ({'SMTP_PASSWORD': 'x', 'SMTP_HOST': 'h'}, {'SMTP_PASSWORD': AUDIT_SECRET_MASK, 'SMTP_HOST': 'h'}),
        ([{'token': 't'}, 'plain', 7], [{'token': AUDIT_SECRET_MASK}, 'plain', 7]),
    ],
    ids=['setting_pair_secret', 'setting_pair_plain', 'setting_names_as_keys', 'list'],
)
def test_redaction_rules(payload, expected):
    assert redact_audit_payload(payload) == expected


def test_secret_context_masks_everything_but_keeps_shape():
    assert redact_audit_payload({'value': 'x', 'meta': {'a': 1}, 'list': [1, 'b']}, secret=True) == {
        'value': AUDIT_SECRET_MASK,
        'meta': AUDIT_SECRET_MASK,
        'list': AUDIT_SECRET_MASK,
    }
