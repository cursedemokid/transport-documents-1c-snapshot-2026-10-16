import json
import logging
import socket
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.schemas.saby import SabyDocument
from app.utils.dates import parse_date
from app.utils.one_s_format import parse_one_s_amount
from app.utils.normalization import extract_order_number


INCOMING_EVENTS_METHOD = "РеестрВходящих.СписокСобытий"
logger = logging.getLogger(__name__)


@contextmanager
def _socket_timeout(seconds: float):
    """Предел ожидания на время запроса к СБИС.

    pysaby зовёт `urlopen` без таймаута. Общий предел выставляется при старте
    сервиса, но прогон можно запустить и мимо него — из скрипта, из планировщика
    чужого процесса. Тогда зависшее соединение держит поток вечно: проверено,
    выборка простояла одиннадцать минут на живом `recv` и не собиралась падать.
    Поэтому предел ставится здесь, рядом с единственным вызовом без таймаута,
    и снимается сразу после.
    """
    if not seconds or seconds <= 0:
        yield
        return
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


@dataclass(frozen=True)
class SkippedDocument:
    """Документ нужного типа, который не удалось разобрать.

    Такие документы обязаны попадать в отчёт. Замеры на живом реестре: из 1653
    входящих УПД за две недели у 117 (7%) нет суммы — это сканы, у которых
    машиночитаемой суммы нет ни в реестре, ни в самом документе. Сопоставить их
    нечем, но молча терять нельзя: снаружи это неотличимо от «документа не было».
    """

    id: str | None
    number: str | None
    date: date | None
    counterparty_name: str | None
    reason: str


class SabyClient:
    def __init__(self, settings: Settings, manager: Any | None = None):
        self._settings = settings
        self._manager = manager
        #: События, которые не удалось превратить в документ. Тихий пропуск —
        #: это тот же потерянный документ, только без следа.
        self.skipped_events = 0
        #: Из них те, что попали в период: они идут в отчёт отдельными строками.
        self.skipped_documents: list[SkippedDocument] = []
        #: Реестр прочитан не полностью — выборка заведомо неполна.
        self.registry_truncated = False
        #: События не нашего типа: договоры, акты сверки, корректировки, счета.
        self.wrong_type_events = 0
        #: Повторы одного документа на разных страницах реестра.
        self.duplicate_events = 0

    def list_incoming_documents(self, date_from: date, date_to: date) -> list[SabyDocument]:
        """Входящие документы нужного типа за период.

        Реестр СБИС фильтр по датам из запроса игнорирует: на запрос за неделю
        приходят документы за полтора месяца. Сортирован он по времени события,
        а не по дате документа, поэтому остановиться «дойдя до начала периода»
        нельзя — реестр читается целиком, а период отсекается здесь.

        Типы фильтруются тоже: в реестре лежат договоры, акты сверки,
        корректировки и счета, у которых суммы в событии нет по природе. Без
        фильтра они попадали в счётчик «не удалось разобрать» — на живых данных
        599 из 2739 событий, то есть счётчик в отчёте просто врал.
        """
        documents: list[SabyDocument] = []
        seen_documents: set[str] = set()
        self.duplicate_events = 0
        self.skipped_events = 0
        self.skipped_documents = []
        self.registry_truncated = False
        self.wrong_type_events = 0
        position: dict[str, Any] | None = None
        seen_positions: set[str] = set()
        max_pages = max(1, self._settings.saby_max_pages)
        pages = 0
        outside_period = 0

        while pages < max_pages:
            payload = build_incoming_events_payload(
                limit=self._settings.saby_page_size,
                date_from=date_from,
                date_to=date_to,
                position=position,
            )
            response = self._send_page(payload, pages + 1)
            pages += 1
            result = response.get("result") or response
            normalized = _record_to_dict(result)

            for item in _extract_event_items(normalized):
                flat = _normalize_recordset_item(_record_to_dict(item))
                if not self._is_wanted_type(flat):
                    self.wrong_type_events += 1
                    continue

                document = _try_map_saby_document(item)
                if document is None:
                    self.skipped_events += 1
                    self._remember_skipped(flat)
                    continue
                # Реестр отдаёт один и тот же документ на нескольких страницах:
                # на живой выборке это 7 лишних строк из 2017. Без дедупликации
                # документ обрабатывается дважды и дважды попадает в отчёт.
                if document.id in seen_documents:
                    self.duplicate_events += 1
                    continue
                seen_documents.add(document.id)

                if date_from <= document.incoming_date <= date_to:
                    documents.append(document)
                else:
                    outside_period += 1

            navigation = _pick(normalized, "Навигация", "navigation") or {}
            has_more = _pick(navigation, "HasMore", "has_more")
            next_position = _pick(navigation, "Position", "position")
            if not has_more or not next_position:
                break

            position_key = repr(next_position)
            if position_key in seen_positions:
                break
            seen_positions.add(position_key)
            position = next_position
        else:
            # Реестр не отсортирован по дате документа, поэтому обрезка означает
            # именно «часть периода не прочитана», а не «прочитано меньше старого».
            self.registry_truncated = True
            logger.error(
                "Достигнут предел SABY_MAX_PAGES (%s): реестр прочитан не полностью, "
                "выборка неполна. Увеличьте SABY_MAX_PAGES.",
                max_pages,
            )

        # В отчёт идут только пропуски внутри периода: остальные к делу не относятся.
        self.skipped_documents = [
            item for item in self.skipped_documents
            if item.date is None or date_from <= item.date <= date_to
        ]
        if self.skipped_events:
            logger.warning(
                "Не удалось разобрать событий Saby: %s, из них внутри периода %s.",
                self.skipped_events,
                len(self.skipped_documents),
            )
        if self.wrong_type_events:
            logger.info("Отсеяно событий не нашего типа: %s.", self.wrong_type_events)
        if self.duplicate_events:
            logger.info("Повторов одного документа в реестре: %s.", self.duplicate_events)
        if outside_period:
            logger.info(
                "Отсеяно документов вне периода %s..%s: %s. Реестр СБИС фильтр по датам не применяет.",
                date_from,
                date_to,
                outside_period,
            )
        return documents

    def _is_wanted_type(self, flat: dict[str, Any]) -> bool:
        wanted = self._settings.saby_document_types
        if not wanted:
            return True
        kind = _pick(flat, "ТипДокумента.ТипДокумента", "additional_expense_type")
        return str(kind or "") in wanted

    def _remember_skipped(self, flat: dict[str, Any]) -> None:
        # Идентификатор и номер в реестре бывают числами, а не строками.
        identifier = _pick(flat, *_ID_KEYS)
        number = _pick(flat, "incoming_number", "number", "Номер")
        self.skipped_documents.append(
            SkippedDocument(
                id=str(identifier) if identifier is not None else None,
                number=str(number) if number is not None else None,
                date=_parse_saby_date(_pick(flat, "incoming_date", "date", "Дата")),
                counterparty_name=_pick(flat, "counterparty_name", "Лицо1.Название"),
                reason="в Saby нет суммы документа — сопоставить не по чему",
            )
        )

    def _send_page(self, payload: dict[str, Any], page_number: int) -> dict[str, Any]:
        """Страница реестра с повтором.

        Чтение реестра идемпотентно, а обрыв TLS на одной странице из тридцати
        случается регулярно и раньше валил весь прогон в одну техническую
        ошибку — при том что остальные страницы читались без проблем.
        """
        attempts = max(1, self._settings.saby_page_retries)
        for attempt in range(1, attempts + 1):
            try:
                return self._send_payload(payload)
            except Exception as error:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "Страница реестра %s не прочитана (попытка %s из %s): %s",
                    page_number,
                    attempt,
                    attempts,
                    error,
                )
                time.sleep(self._settings.saby_retry_delay_seconds * attempt)
        raise AssertionError("недостижимо")

    def _send_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._send_raw_payload(payload)
        if not isinstance(response, dict):
            raise ValueError("Saby library returned non-dict response")
        return response

    def _send_raw_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        manager = self._get_manager()
        with _socket_timeout(self._settings.request_timeout_seconds):
            return self._send_through(manager, payload)

    def _send_through(self, manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(manager, "_get_sid") or not hasattr(manager, "_send_json_request"):
            raise ValueError("pysaby manager does not expose raw request methods")

        token = manager._get_sid()
        if token is None:
            raise ValueError("Saby login/REDACTED auth did not return session id")

        headers = dict(getattr(manager, "headers", {}))
        headers["X-SBISSessionID"] = token
        url = f"{getattr(manager, 'base_url', self._settings.saby_base_url)}/service/"
        status_code, response_text = manager._send_json_request(url, payload, headers)

        if status_code == 401 and hasattr(manager, "_auth"):
            token = manager._auth()
            if token is None:
                raise ValueError("Saby session refresh failed")
            headers["X-SBISSessionID"] = token
            status_code, response_text = manager._send_json_request(url, payload, headers)

        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise ValueError(f"Saby library returned invalid JSON: {response_text}") from error

        error_detail = response_data.get("error") if isinstance(response_data, dict) else None
        if error_detail:
            raise AttributeError(f"Ошибка сервера при запросе {INCOMING_EVENTS_METHOD}: {error_detail}")
        if status_code != 200:
            raise AttributeError(f"Ошибка сервера при запросе {INCOMING_EVENTS_METHOD}: HTTP {status_code}")

        return response_data.get("result") or response_data

    def _get_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        if not self._settings.saby_login or not self._settings.saby_REDACTED:
            raise ValueError("Saby login/REDACTED auth is not configured")
        try:
            from pysaby import SABYManager
        except ImportError as error:
            raise ValueError("pysaby is not installed. Install it with: pip install pysaby") from error
        self._manager = _build_manager(SABYManager, self._settings)
        return self._manager



#: Конструирование менеджера подменяет класс на пару тактов — делаем это под
#: замком, чтобы два потока не столкнулись.
_MANAGER_LOCK = threading.Lock()


def _build_manager(manager_class: Any, settings: Settings) -> Any:
    """Создаёт менеджер pysaby с файлом сессии в каталоге состояния сервиса.

    Библиотека держит сессию в SQLite рядом с собой, внутри `site-packages`, и
    создаёт файл прямо в конструкторе. Это плохо дважды: файл теряется при
    переустановке зависимостей, а в окружении только для чтения — типовом для
    развёрнутой службы — конструктор просто падает, и весь прогон превращается
    в одну техническую ошибку.

    Подменить путь после создания недостаточно: таблица к тому моменту уже
    создана не там. Поэтому на время конструктора отключаем инициализацию базы,
    а потом выполняем её по нужному пути.
    """
    session_path = Path(settings.saby_session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    with _MANAGER_LOCK:
        original_init_db = manager_class._init_db
        manager_class._init_db = lambda self: None
        try:
            manager = manager_class(
                login=settings.saby_login,
                REDACTED=settings.saby_REDACTED,
            )
        finally:
            manager_class._init_db = original_init_db

    manager.db_file = str(session_path)
    manager._init_db()
    return manager


def build_incoming_events_payload(
    limit: int,
    date_from: date | None = None,
    date_to: date | None = None,
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    filter_data = [
        True,
        True,
        True,
        False,
        True,
        [],
        True,
        [],
        None,
        True,
        "Все",
        _format_saby_date(date_to),
        _format_saby_date(date_from),
        "Нет",
        None,
        [],
        {"selected": [], "excluded": []},
        None,
        False,
        False,
        True,
        False,
        [],
        None,
        False,
        "",
        [],
        [],
        "-1",
        False,
        {
            "continueIterative": True,
            "skipPreselect": True,
            "СписокИдОПоМаске": None,
            "СписокИдОПоМаскеОбработан": None,
        },
    ]
    return {
        "jsonrpc": "2.0",
        "protocol": 7,
        "method": INCOMING_EVENTS_METHOD,
        "params": {
            "Фильтр": {
                "d": filter_data,
                "s": [
                    {"t": "Логическое", "n": "iterative_list"},
                    {"t": "Логическое", "n": "new_navigation"},
                    {"t": "Логическое", "n": "position_on_today"},
                    {"t": "Логическое", "n": "translit_search"},
                    {"t": "Логическое", "n": "ws4_registry"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ИдРегламента"},
                    {"t": "Логическое", "n": "НеОбнулятьНашуКомпанию"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ТипДокументаВД"},
                    {"t": "Строка", "n": "ТипДокументаСВложением"},
                    {"t": "Логическое", "n": "УчитыватьИерархиюНО"},
                    {"t": "Строка", "n": "ФильтрВладелец"},
                    {"t": "Строка", "n": "ФильтрДатаП"},
                    {"t": "Строка", "n": "ФильтрДатаС"},
                    {"t": "Строка", "n": "ФильтрИзСписка"},
                    {"t": "Строка", "n": "ФильтрИсполнитель"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ФильтрЛицо1"},
                    {"t": "JSON-объект", "n": "ФильтрЛицо1ВклИскл"},
                    {"t": "Строка", "n": "ФильтрМастерСостояние"},
                    {"t": "Логическое", "n": "ФильтрНезакрепленные"},
                    {"t": "Логическое", "n": "ФильтрНепрочитанные"},
                    {"t": "Логическое", "n": "ФильтрНовыйРПДокумент"},
                    {"t": "Логическое", "n": "ФильтрНовыйРПОИ"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ФильтрОрганизацияФилиал"},
                    {"t": "Строка", "n": "ФильтрОтветственноеЛицо"},
                    {"t": "Логическое", "n": "ФильтрОтветыКонтрагентов"},
                    {"t": "Строка", "n": "ФильтрПоМаске"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ФильтрПометкиВкл"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "ФильтрПометкиИскл"},
                    {"t": "Строка", "n": "ФильтрСостояние_1"},
                    {"t": "Логическое", "n": "ФильтрУдаленные"},
                    {"t": "JSON-объект", "n": "searchSettings"},
                ],
                "_type": "record",
                "f": 0,
            },
            "Сортировка": None,
            "Навигация": {
                "d": [
                    "forward",
                    True,
                    limit,
                    _position_record(position),
                ],
                "s": [
                    {"t": "Строка", "n": "Direction"},
                    {"t": "Логическое", "n": "HasMore"},
                    {"t": "Число целое", "n": "Limit"},
                    {"t": "Запись", "n": "Position"},
                ],
                "_type": "record",
                "f": 0,
            },
            "ДопПоля": [],
        },
        "id": 1,
    }


def _format_saby_date(value: date | None) -> str | None:
    return value.strftime("%d.%m.%Y") if value else None


def _position_record(position: dict[str, Any] | None) -> dict[str, Any] | None:
    if not position:
        return None
    if isinstance(position, (list, tuple)) and len(position) >= 2:
        return {
            "d": [
                position[1],
                position[0],
            ],
            "s": [
                {"t": "Число целое", "n": "@Событие"},
                {"t": "Строка", "n": "StartPosition"},
            ],
            "_type": "record",
            "f": 1,
        }
    if position.get("_type") == "record":
        return position
    return {
        "d": [
            position.get("@Событие"),
            position.get("StartPosition"),
        ],
        "s": [
            {"t": "Число целое", "n": "@Событие"},
            {"t": "Строка", "n": "StartPosition"},
        ],
        "_type": "record",
        "f": 1,
    }


def _record_to_dict(value: Any) -> Any:
    if isinstance(value, list):
        return [_record_to_dict(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("_type") == "recordset" and isinstance(value.get("d"), list) and isinstance(value.get("s"), list):
        items: list[dict[str, Any]] = []
        schema = value["s"]
        for row in value["d"]:
            if not isinstance(row, list):
                continue
            item: dict[str, Any] = {}
            for field, field_value in zip(schema, row):
                name = field.get("n") if isinstance(field, dict) else None
                if name:
                    item[name] = _record_to_dict(field_value)
            items.append(item)
        metadata = _record_to_dict(value.get("m"))
        return {
            "items": items,
            "navigation": {
                "HasMore": bool(value.get("n")),
                "Position": metadata.get("nextPosition") if isinstance(metadata, dict) else None,
            },
            "metadata": metadata,
        }
    if value.get("_type") == "record" and isinstance(value.get("d"), list) and isinstance(value.get("s"), list):
        result: dict[str, Any] = {}
        for field, item in zip(value["s"], value["d"]):
            name = field.get("n") if isinstance(field, dict) else None
            if name:
                result[name] = _record_to_dict(item)
        return result
    return {key: _record_to_dict(item) for key, item in value.items()}


def _extract_event_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    direct_items = _pick(
        response,
        "События",
        "СписокСобытий",
        "Документы",
        "documents",
        "events",
        "items",
    )
    if isinstance(direct_items, list):
        return [item for item in direct_items if isinstance(item, dict)]

    for value in response.values():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    return []


def _parse_saby_date(value: Any):
    if isinstance(value, str) and len(value) >= 10 and value[2] == "." and value[5] == ".":
        return date(int(value[6:10]), int(value[3:5]), int(value[0:2]))
    return parse_date(value)


def _pick(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
        current: Any = source
        found = True
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found and current not in (None, ""):
            return current
    return None


def _normalize_recordset_item(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    aliases = {
        # GUID первым: @Документ и ИдРедакции — идентификаторы редакции, и на
        # живой выборке они совпадают у разных документов (1980 уникальных
        # против 2010 по GUID), то есть личностью документа служить не могут.
        "id": ("ИдентификаторДокумента", "@Документ", "ИдРедакции"),
        "incoming_number": ("Номер",),
        "incoming_date": ("Дата",),
        "amount": ("ДокСумма", "ДокументРасширение.$Сумма"),
        "title": ("ТипДокумента.ТипДокумента", "Лицо1.Название"),
        "additional_expense_type": ("ТипДокумента.ТипДокумента",),
        "organization_name": ("ДокументНашаОрганизация.Контрагент.Название", "Лицо3.Название"),
        "counterparty_name": ("Лицо1.Название", "Лицо2.Название"),
        "counterparty_inn": ("Контрагент.ИНН",),
        "counterparty_kpp": ("Контрагент.КПП",),
        "comment": ("ЗначимыйКомментарий",),
    }
    for target, source_keys in aliases.items():
        if normalized.get(target) not in (None, ""):
            continue
        value = _pick(raw, *source_keys)
        if value not in (None, ""):
            normalized[target] = value
    return normalized


def _try_map_saby_document(raw: dict[str, Any]) -> SabyDocument | None:
    """Разбирает событие реестра, не роняя из-за него всю выборку.

    Ловится и ArithmeticError: `decimal.InvalidOperation` наследуется от него, а
    не от ValueError, поэтому одна сумма в неожиданном формате выносила весь
    прогон в единственную техническую ошибку.
    """
    try:
        return _map_saby_document(raw)
    except (ValueError, ArithmeticError, TypeError) as error:
        logger.warning("Событие Saby %s пропущено: %s", _describe_event(raw), error)
        return None


#: Ключи идентификатора документа, от устойчивых к запасным.
#:
#: ИдентификаторДокумента — GUID документа в СБИС, единственный устойчивый ключ.
#: @Документ и @Событие — идентификаторы редакции и события: замер на живой
#: выборке (2017 строк) показал, что они СОВПАДАЮТ у разных документов — 1980
#: уникальных против 2010 по GUID. Использовать их как личность документа нельзя.
_ID_KEYS = (
    "ИдентификаторДокумента", "id", "doc_id", "document_id", "Ид", "Идентификатор",
    "Документ.Идентификатор", "Документ.Ид", "@Документ", "@Событие",
)


def _describe_event(raw: dict[str, Any]) -> str:
    """Идентификатор пропущенного события для лога.

    Разбор до `_record_to_dict` ничего не находил: в сыром рекорде значения
    лежат в списке `d`, и в логе оставалось «без идентификатора» — найти
    пропавший документ по такой записи невозможно.
    """
    try:
        flat = _normalize_recordset_item(_record_to_dict(raw))
    except Exception:  # noqa: BLE001 — лог не должен падать сам
        return "без идентификатора"
    identifier = _pick(flat, *_ID_KEYS)
    number = _pick(flat, "incoming_number", "number", "Номер", "Документ.Номер")
    if identifier and number:
        return f"{identifier} (номер {number})"
    return str(identifier or number or "без идентификатора")


def _map_saby_document(raw: dict[str, Any]) -> SabyDocument:
    raw = _normalize_recordset_item(_record_to_dict(raw))
    document_id = str(_pick(raw, *_ID_KEYS))
    incoming_number = str(
        _pick(
            raw,
            "incoming_number",
            "number",
            "document_number",
            "Номер",
            "Документ.Номер",
            "Документ.РегНомер",
            "РегНомер",
        )
    )
    incoming_date = _parse_saby_date(
        _pick(
            raw,
            "incoming_date",
            "date",
            "document_date",
            "Дата",
            "Документ.Дата",
            "Документ.ДатаВремя",
            "ДатаВремя",
        )
    )
    amount = _pick(raw, "amount", "sum", "total", "Сумма", "Документ.Сумма")

    if incoming_date is None:
        raise ValueError(f"Saby document {document_id} has no incoming date")
    if amount is None:
        raise ValueError(f"Saby document {document_id} has no amount")

    comment = _pick(raw, "comment", "description", "Комментарий", "Описание", "Документ.Комментарий")
    supplier_order_number = _pick(raw, "supplier_order_number", "order_number", "НомерЗаказа", "Документ.НомерЗаказа")
    supplier_order_source = "field" if supplier_order_number else None
    if not supplier_order_number:
        supplier_order_number = extract_order_number(str(comment or ""), str(_pick(raw, "title", "name") or ""))
        supplier_order_source = "text" if supplier_order_number else None

    return SabyDocument(
        id=document_id,
        title=_pick(raw, "title", "name", "Название", "Документ.Название", "Документ.Тип"),
        incoming_number=incoming_number,
        incoming_date=incoming_date,
        amount=parse_one_s_amount(amount),
        organization_name=_pick(raw, "organization.name", "our_company.name", "organization_name", "НашаОрганизация.Название"),
        organization_inn=_pick(raw, "organization.inn", "our_company.inn", "organization_inn", "НашаОрганизация.ИНН"),
        organization_kpp=_pick(raw, "organization.kpp", "our_company.kpp", "organization_kpp", "НашаОрганизация.КПП"),
        counterparty_name=_pick(raw, "counterparty.name", "contractor.name", "counterparty_name", "Контрагент.Название"),
        counterparty_inn=_pick(raw, "counterparty.inn", "contractor.inn", "counterparty_inn", "Контрагент.ИНН"),
        counterparty_kpp=_pick(raw, "counterparty.kpp", "contractor.kpp", "counterparty_kpp", "Контрагент.КПП"),
        supplier_order_number=supplier_order_number,
        supplier_order_source=supplier_order_source,
        additional_expense_type=_pick(raw, "additional_expense_type", "expense_type", "ТипДокумента", "Документ.ТипДокумента"),
        comment=comment,
        source_url=_pick(raw, "url", "source_url", "Ссылка", "Документ.Ссылка"),
        raw=raw,
    )
