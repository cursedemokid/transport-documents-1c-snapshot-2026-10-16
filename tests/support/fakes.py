"""Фабрики доменных объектов и заглушки внешних систем для тестов.

Шов проходит по `OneSRepository`: сервисы синхронизации работают только с ним,
поэтому подменять достаточно его, а не транспорт.
"""

from datetime import date, datetime
from decimal import Decimal

from app.clients.http_json import HttpClientError
from app.schemas.one_s import (
    AdditionalExpenseItem,
    IncomingRequisites,
    Party,
    SupplierOrderItem,
    SupplierOrderLink,
    Warehouse,
)
from app.schemas.one_s_wire import (
    AdditionalExpenseRow,
    SupplierOrderAttributeRow,
    SupplierOrderRow,
)
from app.schemas.saby import SabyDocument
from app.services.one_s_repository import OrderLookup, WriteOutcome
from app.services.reporting import ReportDelivery
from app.utils.normalization import normalize_doc_number
from tests.support.fixtures import load_one_s_rows

#: Отличает «поле не задано» от явно переданного None.
_UNSET = object()

DEFAULT_INCOMING_NUMBER = "4726385"
DEFAULT_ORDER_NUMBER = "ТД000001405"
DEFAULT_WAREHOUSE = "Центральный ОСНОВНОЙ"
DEFAULT_DATE = date(2026, 8, 17)


def saby_doc(
    incoming_number: str = DEFAULT_INCOMING_NUMBER,
    order_number: str | None = DEFAULT_ORDER_NUMBER,
    amount: Decimal | str = "100.00",
    incoming_date: date = DEFAULT_DATE,
    counterparty_name: str = "ВЕКТОР ООО",
    **overrides,
) -> SabyDocument:
    return SabyDocument(
        id=f"saby-{incoming_number}",
        incoming_number=incoming_number,
        incoming_date=incoming_date,
        amount=Decimal(str(amount)),
        organization_name='ООО «Пример»',
        counterparty_name=counterparty_name,
        supplier_order_number=order_number,
        **overrides,
    )


def expense(
    amount: Decimal | str = "100.00",
    expense_type: str = "ЖД РФ",
    incoming_requisites: IncomingRequisites | None = None,
    incoming_number: str = DEFAULT_INCOMING_NUMBER,
    order_number: str | None = DEFAULT_ORDER_NUMBER,
    ref: str = "7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
    number: str = "ТД000000840",
    document_date: datetime | None = None,
    counterparty_name: str = "ВЕКТОР ООО (сверен на 31.12.2025)",
    comment: str | None | object = _UNSET,
    deletion_mark: bool = False,
    #: Обмен отдаёт признак проведения не всегда, поэтому None — законное
    #: значение и от True отличается: «неизвестно» не равно «распроведён».
    posted: bool | None = True,
    **overrides,
) -> AdditionalExpenseItem:
    return AdditionalExpenseItem(
        ref=ref,
        number=number,
        date=document_date or datetime(2026, 8, 17, 12, 0, 0),
        posted=posted,
        deletion_mark=deletion_mark,
        amount=Decimal(str(amount)),
        organization=Party(name='ООО «Пример»'),
        counterparty=Party(name=counterparty_name),
        incoming_requisites=incoming_requisites or IncomingRequisites(),
        comment=f"УПД {incoming_number}" if comment is _UNSET else comment,
        additional_expense_type=expense_type,
        supplier_order=SupplierOrderLink(
            number=order_number,
            date=date(2026, 4, 16),
            presentation=(
                f"Заказ поставщику {order_number} от 16.04.2026 0:00:00" if order_number else None
            ),
        ),
        **overrides,
    )


def order(
    warehouse_name: str = DEFAULT_WAREHOUSE,
    order_number: str = DEFAULT_ORDER_NUMBER,
    auto_pickup_fact_date: date | None = DEFAULT_DATE,
    **overrides,
) -> SupplierOrderItem:
    return SupplierOrderItem(
        ref="7e9061fe-d89a-55c4-a8c8-495cf7b0050b",
        number=order_number,
        date=datetime(2026, 4, 16, 0, 0, 0),
        posted=True,
        deletion_mark=False,
        warehouse=Warehouse(name=warehouse_name),
        organization=Party(name='ООО «Пример»'),
        auto_pickup_fact_date=auto_pickup_fact_date,
        **overrides,
    )


class SabyStub:
    def __init__(self, documents: list[SabyDocument], error: Exception | None = None):
        self._documents = documents
        self._error = error
        self.calls = 0

    def list_incoming_documents(self, date_from: date, date_to: date) -> list[SabyDocument]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._documents


class InMemoryStateStore:
    """Состояние синхронизации без обращения к диску."""

    def __init__(self, last_processed_date: date | None = None):
        self.last_processed_date = last_processed_date
        self.saves: list[date] = []

    def load_last_processed_date(self) -> date | None:
        return self.last_processed_date

    def save_last_processed_date(self, value: date) -> None:
        self.saves.append(value)
        if self.last_processed_date is None or value > self.last_processed_date:
            self.last_processed_date = value


class RecordingReportService:
    def __init__(self, delivery: ReportDelivery | None = None):
        self.sent = 0
        self.saved = 0
        self._delivery = delivery

    def save(self, response):
        self.saved += 1
        return None

    def send(self, response) -> ReportDelivery:
        self.sent += 1
        return self._delivery or ReportDelivery(
            sent=True, backend="test", reason="доставлено в тесте", path=None
        )


class _ReferenceStub:
    """Группы складов из фикстуры справочника."""

    def __init__(self, groups: dict[str, str] | None = None):
        if groups is None:
            groups = {row["Склад"]: row["Группа"] for row in load_one_s_rows("warehouse")}
        self._groups = groups
        self.calls = 0

    def warehouse_group(self, name: str | None) -> str | None:
        self.calls += 1
        return self._groups.get(name) if name else None

    def organization_code(self, name: str | None) -> str | None:
        return None

    def expense_type_code(self, name: str | None) -> str | None:
        return None


class FakeOneSRepository:
    """Заглушка репозитория. Запись по умолчанию проваливает тест."""

    def __init__(
        self,
        expenses: list[AdditionalExpenseItem] | None = None,
        orders: list[SupplierOrderItem] | None = None,
        warehouse_groups: dict[str, str] | None = None,
        write_outcome: WriteOutcome | None = None,
        expense_error: Exception | None = None,
        allow_writes: bool = False,
    ):
        self._expenses = expenses if expenses is not None else [expense()]
        self._orders = orders if orders is not None else [order()]
        self._write_outcome = write_outcome
        self._expense_error = expense_error
        self._allow_writes = allow_writes
        self.reference = _ReferenceStub(warehouse_groups)
        self.expense_calls = 0
        self.order_calls = 0
        self.reload_calls = 0
        self.write_calls = 0

    def list_additional_expenses(self, date_from, date_to, organization_name=None):
        self.expense_calls += 1
        if self._expense_error is not None:
            error, self._expense_error = self._expense_error, None
            raise error
        return self._expenses

    def find_supplier_order(self, item: AdditionalExpenseItem) -> OrderLookup:
        self.order_calls += 1
        number = item.supplier_order.number
        if not number:
            return OrderLookup(status="no_number")
        key = normalize_doc_number(number)
        found = [row for row in self._orders if normalize_doc_number(row.number) == key]
        if not found:
            return OrderLookup(status="not_in_period")
        if len(found) > 1:
            return OrderLookup(status="ambiguous", candidates=tuple(found))
        return OrderLookup(status="found", order=found[0])

    def reload_additional_expense(self, item: AdditionalExpenseItem):
        self.reload_calls += 1
        for candidate in self._expenses:
            if candidate.ref == item.ref:
                return candidate
        return None

    def write_incoming_requisites(self, item, incoming_number, incoming_date) -> WriteOutcome:
        self.write_calls += 1
        if not self._allow_writes:
            raise AssertionError("HTTP POST в 1С не должен выполняться")
        return self._write_outcome or WriteOutcome(
            status="written", message="Входящие реквизиты записаны в 1С."
        )


class FakeOneSClient:
    """Транспортная заглушка: отдаёт строки обмена из фикстур."""

    def __init__(
        self,
        expense_rows: list[AdditionalExpenseRow] | None = None,
        order_rows: list[SupplierOrderRow] | None = None,
        expense_error: Exception | None = None,
        attribute_rows: list[SupplierOrderAttributeRow] | None = None,
    ):
        self.attribute_rows = (
            attribute_rows
            if attribute_rows is not None
            else [
                SupplierOrderAttributeRow.model_validate(row)
                for row in load_one_s_rows("ordertosupplierattributes")
            ]
        )
        self.attribute_calls: list[str] = []
        self.expense_rows = (
            expense_rows
            if expense_rows is not None
            else [
                AdditionalExpenseRow.model_validate(row)
                for row in load_one_s_rows("docaddexpenses_2026-08")
            ]
        )
        self.order_rows = (
            order_rows
            if order_rows is not None
            else [
                SupplierOrderRow.model_validate(row)
                for row in load_one_s_rows("ordertosupplier_2026-04")
            ]
        )
        self._expense_error = expense_error
        self.expense_queries: list[object] = []
        self.order_queries: list[object] = []

    def list_additional_expenses(self, query):
        self.expense_queries.append(query)
        if self._expense_error is not None:
            error, self._expense_error = self._expense_error, None
            raise error
        return self.expense_rows

    def list_supplier_orders(self, query):
        """Отдаёт только заказы внутри запрошенного периода — как настоящая 1С.

        Без этого точечный запрос по дате находил бы любой заказ, слепой перебор
        никогда не задействовался, и тесты на него ничего бы не проверяли.
        """
        self.order_queries.append(query)
        return [
            row
            for row in self.order_rows
            if row.date is None
            or query.period_begin <= row.date.date() <= query.period_end
        ]

    def list_supplier_order_attributes(self, document_id: str):
        self.attribute_calls.append(document_id)
        return self.attribute_rows

    def list_organizations(self):
        from app.schemas.one_s_wire import OrganizationRow

        return [OrganizationRow.model_validate(row) for row in load_one_s_rows("organization")]

    def list_additional_expense_types(self):
        from app.schemas.one_s_wire import AdditionalExpenseTypeRow

        return [
            AdditionalExpenseTypeRow.model_validate(row) for row in load_one_s_rows("addexpensetype")
        ]

    def list_warehouses(self):
        from app.schemas.one_s_wire import WarehouseRow

        return [WarehouseRow.model_validate(row) for row in load_one_s_rows("warehouse")]

    def update_incoming_requisites(self, request):
        raise AssertionError("HTTP POST в 1С не должен выполняться")


def temporary_http_error() -> HttpClientError:
    return HttpClientError("временная ошибка 1С")
