"""Доступ к фикстурам с ответами 1С.

Фикстуры повторяют форму реальных ответов обмена, включая неудобные места:
неразрывный пробел в суммах, дату-заглушку 01.01.0001, строку представления
вместо ссылки на заказ и «грязные» наименования контрагентов.
"""

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
ONE_S_DIR = FIXTURES_DIR / "one_s"


def load_fixture(name: str) -> Any:
    """Читает фикстуру по имени относительно tests/fixtures."""
    path = FIXTURES_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def load_one_s_rows(name: str) -> list[dict[str, Any]]:
    """Возвращает строки из ответа 1С, отбрасывая служебную обёртку."""
    payload = load_fixture(f"one_s/{name}.json")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"Фикстура {name} содержит ответ с ошибкой, а не список строк")
    return rows
