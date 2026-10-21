import re
from decimal import Decimal
from difflib import SequenceMatcher


ORDER_NUMBER_RE = re.compile(r"\b[А-ЯЁA-Z]{1,4}\d{6,12}\b", re.IGNORECASE)

#: Организационно-правовые формы, которые пишут по-разному с обеих сторон обмена.
LEGAL_FORMS = frozenset({
    "ооо", "оао", "зао", "пао", "ао", "ип", "нко", "гуп", "муп", "чп",
    "llc", "ltd", "co", "inc", "gmbh", "plc",
    "общество", "с", "ограниченной", "ответственностью",
})


_BRACKETS_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[\"'«»`.,;:\-–—/\\&]+")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().casefold().split())


def normalize_doc_number(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s\-_/]+", "", value).casefold()


def same_doc_number(left: str | None, right: str | None) -> bool:
    """Сравнивает номера документов, не придираясь к ведущим нулям.

    Saby отдаёт номер УПД с набивкой нулями («2858374843»), а в 1С тот же номер
    записан без неё («4100200»). На бэкфилле за восемь месяцев это давало семь
    ложных конфликтов из девяти — то есть программа сообщала «в 1С другой
    входящий документ» про документ, который бухгалтер завёл именно из этого УПД.
    Конфликт — стоп-условие при раскатке, так что цена ошибки тут высокая.

    Нули срезаются только когда после них остаются одни цифры: у номеров вида
    «УТ000001908» ведущая часть значима, и трогать её нельзя.
    """
    left_key = normalize_doc_number(left)
    right_key = normalize_doc_number(right)
    if not left_key or not right_key:
        return False
    return _strip_padding(left_key) == _strip_padding(right_key)


def _strip_padding(key: str) -> str:
    # Пустой ключ сюда не доходит: same_doc_number отсекает его раньше.
    return key.lstrip("0") if key.isdigit() else key


def money(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def money_equal(
    left: Decimal | int | float | str | None,
    right: Decimal | int | float | str | None,
    tolerance: Decimal | int | float | str,
) -> bool:
    left_money = money(left)
    right_money = money(right)
    if left_money is None or right_money is None:
        return False
    return abs(left_money - right_money) <= Decimal(str(tolerance))


def extract_order_number(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = ORDER_NUMBER_RE.search(value)
        if match:
            return match.group(0)
    return None


def normalize_party_name(value: str | None) -> str:
    """Приводит наименование контрагента к сравнимому виду.

    В 1С наименования «грязные»: со служебными пометками в скобках, кавычками и
    организационно-правовой формой в произвольном месте. Из-за этого строгое
    сравнение с наименованием из Saby почти никогда не срабатывает, а ИНН, по
    которому можно было бы сверяться надёжно, обмен с 1С не отдаёт.

    'ВЕКТОР ООО (сверен на 31.12.2025)' и 'ООО «Пример»' -> 'вектор'
    """
    text = clean_text(value)
    if not text:
        return ""
    text = _BRACKETS_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    tokens = [token for token in text.split() if token not in LEGAL_FORMS]
    return " ".join(tokens)


def party_name_similarity(left: str | None, right: str | None) -> float:
    """Оценка похожести наименований от 0.0 до 1.0."""
    left_norm = normalize_party_name(left)
    right_norm = normalize_party_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return max(jaccard, SequenceMatcher(None, left_norm, right_norm).ratio())

