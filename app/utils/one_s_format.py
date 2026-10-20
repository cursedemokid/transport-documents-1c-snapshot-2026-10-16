"""Разбор значений в форматах, которые использует обмен с 1С.

Обмен отдаёт все значения строками, причём в представлении для человека, а не для
машины: суммы с неразрывным пробелом между разрядами и запятой вместо точки,
булевы как «Да»/«Нет», а пустая дата — как 01.01.0001.

Это форматы конкретной чужой системы, поэтому они живут отдельно от универсальных
хелперов в `normalization.py` и `dates.py`.
"""

import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime as DateTime
from decimal import Decimal, InvalidOperation

ONE_S_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"
ONE_S_DATE_FORMAT = "%d.%m.%Y"
ONE_S_PERIOD_FORMAT = "%Y%m%d"

#: 1С присылает такую дату вместо пустой.
ONE_S_EMPTY_DATE = Date(1, 1, 1)

ONE_S_TRUE = "Да"
ONE_S_FALSE = "Нет"

#: Разряды в суммах разделяются неразрывным пробелом, но встречаются и обычные.
_THOUSANDS_SEPARATORS = (" ", " ", " ", " ")

#: "Заказ поставщику ТД000001075 от 16.04.2026 0:00:00"
#: Ленивый вид документа корректно отрабатывает скобки: у документа-основания
#: представление выглядит как "Заказ поставщику (спецификация) 1938870207 от ...".
_PRESENTATION_RE = re.compile(
    r"^(?P<kind>.*?)\s+(?P<number>\S+)\s+от\s+(?P<date>\d{2}\.\d{2}\.\d{4})"
)


@dataclass(frozen=True)
class DocumentPresentation:
    """Разобранное представление ссылки на документ 1С."""

    kind: str
    number: str
    date: Date | None
    raw: str


def parse_one_s_text(value: object) -> str | None:
    """Пустая строка в обмене означает отсутствие значения."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_one_s_datetime(value: object) -> DateTime | None:
    if value is None or value == "":
        return None
    if isinstance(value, DateTime):
        return None if value.date() == ONE_S_EMPTY_DATE else value
    if isinstance(value, Date):
        return None if value == ONE_S_EMPTY_DATE else DateTime.combine(value, DateTime.min.time())

    text = str(value).strip()
    if not text:
        return None
    for pattern in (ONE_S_DATETIME_FORMAT, ONE_S_DATE_FORMAT):
        try:
            parsed = DateTime.strptime(text, pattern)
        except ValueError:
            continue
        return None if parsed.date() == ONE_S_EMPTY_DATE else parsed
    raise ValueError(f"Не удалось разобрать дату 1С: {value!r}")


def parse_one_s_date(value: object) -> Date | None:
    parsed = parse_one_s_datetime(value)
    return parsed.date() if parsed is not None else None


def parse_one_s_amount(value: object) -> Decimal | None:
    """'1 191,06' -> Decimal('1191.06'). Квантование оставлено вызывающему коду."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    for separator in _THOUSANDS_SEPARATORS:
        text = text.replace(separator, "")
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"Не удалось разобрать сумму 1С: {value!r}") from error


def parse_one_s_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value

    text = str(value).strip().casefold()
    if text == ONE_S_TRUE.casefold():
        return True
    if text == ONE_S_FALSE.casefold():
        return False
    raise ValueError(f"Не удалось разобрать булево значение 1С: {value!r}")


def format_one_s_period(value: Date) -> str:
    """Формат periodBegin/periodEnd и incomingdocdate: YYYYMMDD."""
    return value.strftime(ONE_S_PERIOD_FORMAT)


def parse_document_presentation(value: object) -> DocumentPresentation | None:
    """Разбирает строку представления документа 1С.

    Представление может измениться при перенастройке 1С, поэтому разбор всегда
    возвращает исходную строку в `raw`, а вызывающий код обязан быть готов к `None`.
    """
    text = parse_one_s_text(value)
    if text is None:
        return None
    match = _PRESENTATION_RE.match(text)
    if not match:
        return None
    try:
        parsed_date = parse_one_s_date(match.group("date"))
    except ValueError:
        parsed_date = None
    return DocumentPresentation(
        kind=match.group("kind").strip(),
        number=match.group("number"),
        date=parsed_date,
        raw=text,
    )
