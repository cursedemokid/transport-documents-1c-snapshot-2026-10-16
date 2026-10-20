from datetime import date, datetime, timedelta


def today() -> date:
    return date.today()


def default_window(window_days: int) -> tuple[date, date]:
    end = today()
    return end - timedelta(days=window_days), end


def parse_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


#: 1С отдаёт такую дату вместо пустой; см. app/utils/one_s_format.py.
_EMPTY_STORED_DATE = date(1, 1, 1)


def normalize_stored_date(value: object) -> date | None:
    """Считает дату-заглушку 1С пустым значением.

    Страховка на случай, если 01.01.0001 просочится мимо парсера обмена: такая
    дата в реквизитах превратила бы пустой документ в вечный конфликт и
    заблокировала бы его навсегда.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return None
    return None if value <= _EMPTY_STORED_DATE else value

