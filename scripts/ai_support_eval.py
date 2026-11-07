"""Оценка золотого набора через ядро ИИ; явный запуск делает платные запросы провайдеру."""

import argparse
import asyncio
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest
from app.services.ai_support import api
from app.services.system_settings_service import bot_configuration_service


async def evaluate(cases, model=None):
    await bot_configuration_service.initialize(sync_web_api_token=False)
    previous = settings.AI_SUPPORT_MODEL
    if model:
        settings.AI_SUPPORT_MODEL = model
    passed, total_cost, total_tokens = 0, 0.0, 0
    try:
        await api.startup()
        print('id | исход | передача | ожидание | must/must_not | токены | $')
        for case in cases:
            result = await api.answer(
                case['question'],
                [],
                api.AiRequestContext(
                    channel=api.AiChannel.ADMIN_TEST,
                    user_id=None,
                    allow_account_tools=False,
                ),
            )
            text = (result.reply_text or '').casefold()
            missing = [v for v in case.get('must', []) if v.casefold() not in text]
            forbidden = [v for v in case.get('must_not', []) if v.casefold() in text]
            expect = case['expect']
            correct = (
                result.outcome == api.AiOutcome.HANDOFF
                if expect == 'handoff'
                else result.outcome == api.AiOutcome.ANSWERED and expect == 'answer'
            )
            # answer_account без инструментов считается непроверенным, не успешным.
            match = 'не проверено: нужны инструменты' if expect == 'answer_account' else 'да' if correct else 'нет'
            passed += bool(correct and not missing and not forbidden)
            tokens, cost = 0, 0.0
            if result.request_id is not None:
                async with AsyncSessionLocal() as db:
                    record = await db.get(AiSupportRequest, result.request_id)
                    if record:
                        tokens = record.prompt_tokens + record.completion_tokens
                        cost = float(record.cost_usd)
            total_cost += cost
            total_tokens += tokens
            violations = '; '.join(
                [
                    'нет ' + ', '.join(missing) if missing else '',
                    'запрещено ' + ', '.join(forbidden) if forbidden else '',
                ]
            ).strip('; ')
            print(
                f'{case["id"]} | {result.outcome.value} | {result.handoff_reason or "-"} | {match} | {violations or "-"} | {tokens} | {cost:.6f}'
            )
        print(f'Итог: {passed}/{len(cases)}; токены {total_tokens}; стоимость ${total_cost:.6f}')
        return passed == len(cases)
    finally:
        settings.AI_SUPPORT_MODEL = previous
        await api.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--model')
    args = parser.parse_args()
    cases = json.loads(args.file.read_text(encoding='utf-8'))
    raise SystemExit(0 if asyncio.run(evaluate(cases, args.model)) else 1)


if __name__ == '__main__':
    main()
