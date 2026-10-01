"""Неверная подпись вебхука CryptoBot не выдаёт в лог ожидаемые HMAC (аудит безопасности 01.10.2026, §3.2).

Раньше запись об ошибке несла ``expected_raw``/``expected_reserialized`` и первые
символы токена — любой, кто читает ``payments.log``, получал готовую подпись для
подделанного тела.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from structlog.testing import capture_logs

from app.external.cryptobot import CryptoBotService


TOKEN = '12345:AAABBBCCCDDDEEEFFF'


def expected_signatures(body: str) -> list[str]:
    key = hashlib.sha256(TOKEN.encode()).digest()
    parsed = json.loads(body)
    variants = [
        body,
        json.dumps(parsed, separators=(',', ':'), ensure_ascii=False),
        json.dumps(parsed, separators=(',', ':'), ensure_ascii=True),
    ]
    return [hmac.new(key, variant.encode('utf-8'), hashlib.sha256).hexdigest() for variant in variants]


def test_bad_signature_log_has_no_expected_hmac_or_token(monkeypatch):
    service = CryptoBotService()
    monkeypatch.setattr(service, 'api_token', TOKEN)
    body = '{"update_type":"invoice_paid","payload":{"invoice_id":1,"amount":"10","comment":"тест"}}'
    forged = 'f' * 64

    with capture_logs() as records:
        assert service.verify_webhook_signature(body, forged) is False

    failures = [record for record in records if record.get('event') == 'Неверная подпись CryptoBot webhook']
    assert len(failures) == 1
    record = failures[0]
    assert record['received_signature'] == forged[:8]
    assert record['body_length'] == len(body)
    dumped = json.dumps(records, ensure_ascii=False, default=str)
    for signature in expected_signatures(body):
        assert signature not in dumped
        assert signature[:16] not in dumped
    assert TOKEN[:4] not in dumped
    assert not {'expected_raw', 'expected_reserialized', 'token_prefix'} & set(record)


def test_valid_signature_still_accepted(monkeypatch):
    service = CryptoBotService()
    monkeypatch.setattr(service, 'api_token', TOKEN)
    body = '{"update_type":"invoice_paid","payload":{"invoice_id":1}}'
    assert service.verify_webhook_signature(body, expected_signatures(body)[0]) is True
