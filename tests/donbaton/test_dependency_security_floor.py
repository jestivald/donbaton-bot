"""Нижние границы зависимостей с закрытыми бюллетенями (аудит безопасности 01.10.2026, §5).

urllib3 2.7.0 (и в апстриме v5.0.0) — GHSA-8988-9cw3-xx77, GHSA-gh4c-6fx4-qh6g,
GHSA-vxq7-64xx-v4gw, исправлено в 2.8.0; pyjwt < 2.15 — бюллетени проверки ключей.
Сторож ловит откат версии в uv.lock при следующем слиянии апстрима.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.version import Version


LOCK = Path(__file__).resolve().parents[2] / 'uv.lock'
FLOORS = {'urllib3': '2.8.0', 'pyjwt': '2.15.1'}


def locked_versions() -> dict[str, str]:
    data = tomllib.loads(LOCK.read_text(encoding='utf-8'))
    return {package['name']: package['version'] for package in data['package']}


@pytest.mark.parametrize(('name', 'floor'), sorted(FLOORS.items()))
def test_lock_meets_security_floor(name, floor):
    assert Version(locked_versions()[name]) >= Version(floor)
