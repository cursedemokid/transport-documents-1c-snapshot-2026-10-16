"""Доступ к данным 1С на уровне доменных моделей.

Здесь собрано всё, чего не умеет обмен: выборка за период кэшируется на прогон,
заказы поставщику ищутся локальным индексом (фильтра по номеру у обмена нет), а
оптимистическая блокировка эмулируется перечитыванием документа.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.clients.http_json import HttpClientError
from app.clients.one_s import OneSClient
from app.config import Settings
from app.schemas.one_s import AdditionalExpenseItem, SupplierOrderItem
from app.schemas.one_s_wire import (
    AdditionalExpenseQuery,
    SupplierOrderQuery,
    UpdateIncomingRequisitesRequest,
)
from app.services.one_s_mapping import (
    map_additional_expenses,
    map_date_rows,
    map_supplier_orders,
)
from app.services.one_s_reference import OneSReferenceCache
from app.utils.dates import normalize_stored_date
from app.utils.normalization import normalize_doc_number

logger = logging.getLogger(__name__)

OrderLookupStatus = Literal["found", "ambiguous", "not_in_period", "no_number"]
WriteStatus = Literal["written", "conflict", "unknown"]


@dataclass(frozen=True)
class OrderLookup:
    """Результат поиска заказа поставщику.

    Причины отсутствия различаются намеренно: «номер не разобран» — дефект
    данных, повтор не поможет; «нет в выборке» — возможно, стоит расширить период.
    """

    status: OrderLookupStatus
    order: SupplierOrderItem | None = None
    candidates: tuple[SupplierOrderItem, ...] = ()


@dataclass(frozen=True)
class WriteOutcome:
    status: WriteStatus
    message: str
    raw_response: object = None


@dataclass
class _OrderIndex:
    by_number: dict[str, list[SupplierOrderItem]] = field(default_factory=dict)
    #: Загруженные периоды — только для дедупликации точечных запросов.
    covered_periods: list[tuple[date, date]] = field(default_factory=list)
    #: Слепой перебор периодов назад закончен. Точечные запросы по дате из
    #: представления на это не влияют: их число ограничено числом документов.
    exhausted: bool = False
    walk_requests: int = 0
    targeted_requests: int = 0
    #: Граница перебора назад. Двигается ТОЛЬКО перебором: если брать минимум по
    #: всем загруженным периодам, первый же точечный запрос за старым заказом
    #: уводил перебор в прошлое, и свежие периоды не загружались никогда.
    walk_cursor: date | None = None


def _as_date(value: datetime | date | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


class OneSRepository:
    """Живёт один прогон синхронизации: кэши намеренно не переживают его."""

    def __init__(self, settings: Settings, client: OneSClient | None = None):
        self._settings = settings
        self._client = client or OneSClient(settings)
        self.reference = OneSReferenceCache(self._client)
        self._expenses: dict[tuple[date, date, str | None], list[AdditionalExpenseItem]] = {}
        self._expense_failures: dict[tuple[date, date, str | None], tuple[int, HttpClientError]] = {}
        self._orders = _OrderIndex()
        self._attributes: dict[str, list] = {}

    # --- Поступления доп. расходов ----------------------------------------

    def list_additional_expenses(
        self,
        date_from: date,
        date_to: date,
        organization_name: str | None = None,
    ) -> list[AdditionalExpenseItem]:
        """Выборка за период, мемоизированная на прогон — включая отказ.

        Серверных фильтров по контрагенту и сумме у обмена нет, поэтому без
        мемоизации каждый документ Saby заново выкачивал бы весь период.

        Отказ запоминается тоже. Повторить имеет смысл несколько раз — сбой
        бывает разовым, — но дальше запрос бесполезен и очень дорог: при
        таймауте массовой выборки в 120 с полусотня документов по две попытки
        каждый превращает отказ в многочасовое зависание вместо мгновенного.
        """
        organization_code = (
            self.reference.organization_code(organization_name) if organization_name else None
        )
        key = (date_from, date_to, organization_code)
        if key in self._expenses:
            return self._expenses[key]

        attempts, cached_error = self._expense_failures.get(key, (0, None))
        if cached_error is not None and attempts >= max(1, self._settings.sync_max_attempts):
            logger.warning(
                "Выборка доп. расходов за %s — %s уже не удалась %d раз, повтор пропущен.",
                date_from,
                date_to,
                attempts,
            )
            raise cached_error

        try:
            rows = self._client.list_additional_expenses(
                AdditionalExpenseQuery(
                    period_begin=date_from,
                    period_end=date_to,
                    organization_code=organization_code,
                )
            )
        except HttpClientError as error:
            self._expense_failures[key] = (attempts + 1, error)
            raise

        self._expense_failures.pop(key, None)
        items = [item for item in map_additional_expenses(rows) if self._is_usable(item)]
        self._expenses[key] = items
        return items

    @staticmethod
    def _is_usable(item: AdditionalExpenseItem) -> bool:
        """Защитный постфильтр на случай, если серверные параметры перестанут работать."""
        return not item.deletion_mark and item.posted is not False

    def reload_additional_expense(
        self, expense: AdditionalExpenseItem
    ) -> AdditionalExpenseItem | None:
        """Перечитывает один документ непосредственно перед записью.

        Фильтра по идентификатору у обмена нет, поэтому запрашивается день
        документа и нужная строка отбирается локально. Окно расширено на сутки:
        включительность границ периода в 1С не подтверждена, а документы бывают
        проведены в 23:59:59.
        """
        document_date = _as_date(expense.date)
        if document_date is None:
            logger.warning("У документа %s нет даты — перечитать его нельзя.", expense.ref)
            return None

        slack = timedelta(days=max(0, self._settings.one_s_order_date_slack_days))
        rows = self._client.list_additional_expenses(
            AdditionalExpenseQuery(
                period_begin=document_date - slack,
                period_end=document_date + slack,
            )
        )
        for item in map_additional_expenses(rows):
            if item.ref == expense.ref:
                return item
        return None

    # --- Заказы поставщику -------------------------------------------------

    def find_supplier_order(self, expense: AdditionalExpenseItem) -> OrderLookup:
        number = expense.supplier_order.number
        if not number:
            return OrderLookup(status="no_number")

        key = normalize_doc_number(number)
        hint = expense.supplier_order.date

        while key not in self._orders.by_number and self._load_next_order_chunk(hint):
            pass

        found = self._orders.by_number.get(key, [])
        if not found:
            return OrderLookup(status="not_in_period")
        if len(found) == 1:
            return OrderLookup(status="found", order=self._with_dates(found[0]))

        # Номера заказов повторяются по годам, поэтому одного номера мало:
        # различает дата, которую мы берём из представления ссылки.
        if hint is not None:
            exact = [order for order in found if _as_date(order.date) == hint]
            if len(exact) == 1:
                return OrderLookup(status="found", order=self._with_dates(exact[0]))
        return OrderLookup(status="ambiguous", candidates=tuple(found))

    def _with_dates(self, order: SupplierOrderItem) -> SupplierOrderItem:
        """Дополняет заказ табличной частью «Даты».

        Она приходит отдельным методом по идентификатору заказа, поэтому
        запрашивается только для тех заказов, которые действительно понадобились,
        и кэшируется на прогон: один заказ обычно делят несколько доп. расходов.
        """
        if not order.ref:
            return order
        if order.ref not in self._attributes:
            try:
                self._attributes[order.ref] = self._client.list_supplier_order_attributes(order.ref)
            except HttpClientError as error:
                # Без дат документ уйдёт в ручную обработку, но остальные
                # проверки должны отработать.
                logger.warning("Не удалось получить свойства заказа %s: %s", order.number, error)
                self._attributes[order.ref] = []
        date_rows = map_date_rows(self._attributes[order.ref])
        return order.model_copy(update={"date_rows": date_rows}) if date_rows else order

    def _load_next_order_chunk(self, hint_date: date | None) -> bool:
        """Догружает очередной кусок заказов. Возвращает False, когда грузить нечего.

        Точечный запрос по дате из представления и слепой перебор периодов назад
        считаются раздельно. Лимит ONE_S_BULK_MAX_CHUNKS ограничивает только
        перебор: иначе после нескольких документов индекс объявлялся бы
        исчерпанным, и остальные заказы переставали находиться, хотя каждый из
        них достаётся одним точным запросом.
        """
        targeted = self._targeted_period(hint_date)
        if targeted is None:
            if self._orders.exhausted:
                return False
            if self._orders.walk_requests >= max(1, self._settings.one_s_bulk_max_chunks):
                logger.warning(
                    "Достигнут предел ONE_S_BULK_MAX_CHUNKS при переборе периодов заказов."
                )
                self._orders.exhausted = True
                return False
            period = self._walk_period()
            if period is None:
                self._orders.exhausted = True
                return False
            self._orders.walk_requests += 1
            self._orders.walk_cursor = period[0]
        else:
            period = targeted
            self._orders.targeted_requests += 1

        period_from, period_to = period
        rows = self._client.list_supplier_orders(
            SupplierOrderQuery(period_begin=period_from, period_end=period_to)
        )
        self._orders.covered_periods.append(period)
        for order in map_supplier_orders(rows, self._settings.one_s_auto_pickup_fields):
            index_key = normalize_doc_number(order.number)
            if index_key:
                self._orders.by_number.setdefault(index_key, []).append(order)
        return True

    def _targeted_period(self, hint_date: date | None) -> tuple[date, date] | None:
        """Узкое окно вокруг даты заказа из представления — типовой случай."""
        if hint_date is None or self._is_loaded(hint_date):
            return None
        slack = timedelta(days=max(0, self._settings.one_s_order_date_slack_days))
        # Заказ может быть старше SYNC_START_DATE: раз дата известна точно,
        # ограничивать её нижней границей окна синхронизации незачем.
        return hint_date - slack, hint_date + slack

    def _walk_period(self) -> tuple[date, date] | None:
        """Следующий кусок слепого перебора назад.

        Отсчитывается от собственного курсора, а не от минимума загруженных
        периодов: точечные запросы кладут в загруженные и старые даты, и минимум
        по ним уводил перебор в прошлое, оставляя свежие периоды непрочитанными.
        """
        start = self._settings.sync_start_date
        chunk = max(1, self._settings.one_s_bulk_chunk_days)

        if self._orders.walk_cursor is None:
            today = date.today()
            return max(today - timedelta(days=chunk - 1), start), today
        if self._orders.walk_cursor <= start:
            return None
        current_to = self._orders.walk_cursor - timedelta(days=1)
        return max(current_to - timedelta(days=chunk - 1), start), current_to

    def _is_loaded(self, day: date) -> bool:
        return any(period[0] <= day <= period[1] for period in self._orders.covered_periods)

    # --- Запись ------------------------------------------------------------

    def write_incoming_requisites(
        self,
        expense: AdditionalExpenseItem,
        incoming_number: str,
        incoming_date: date,
    ) -> WriteOutcome:
        """Записывает входящие реквизиты и подтверждает результат чтением.

        Полей оптимистической блокировки обмен не принимает, поэтому документ
        перечитывается до записи, а успех определяется не телом ответа (его
        формат не задокументирован), а состоянием документа после неё.
        """
        result = self._client.update_incoming_requisites(
            UpdateIncomingRequisitesRequest(
                document_id=expense.ref,
                incoming_number=incoming_number,
                incoming_date=incoming_date,
            )
        )
        if result.explicit_error:
            return WriteOutcome(
                status="unknown",
                message=f"1С отклонила запись: {result.message}",
                raw_response=result.raw,
            )

        written = self.reload_additional_expense(expense)
        if written is None:
            return WriteOutcome(
                status="unknown",
                message=(
                    "1С приняла запрос, но перечитать документ не удалось. "
                    "Результат записи неизвестен, проверьте вручную."
                ),
                raw_response=result.raw,
            )

        actual = written.incoming_requisites
        actual_date = normalize_stored_date(actual.date)
        if (
            normalize_doc_number(actual.number) == normalize_doc_number(incoming_number)
            and actual_date == incoming_date
        ):
            return WriteOutcome(
                status="written",
                message="Входящие реквизиты записаны в 1С.",
                raw_response=result.raw,
            )
        if not actual.number and actual_date is None:
            return WriteOutcome(
                status="unknown",
                message="1С приняла запрос, но реквизиты не изменились.",
                raw_response=result.raw,
            )
        return WriteOutcome(
            status="conflict",
            message=f"После записи в 1С другие реквизиты: {actual.number!r} от {actual_date}.",
            raw_response=result.raw,
        )
