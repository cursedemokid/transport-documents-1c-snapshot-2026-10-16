"""Превращение строк обмена 1С в доменные модели приложения.

Граница между чужим контрактом и языком сервисов: `matching`, `validation` и
отчёты продолжают работать с `AdditionalExpenseItem` и `SupplierOrderItem`, не
зная ни про русские ключи, ни про строковые суммы.
"""

import logging
from typing import Iterable, Sequence

from app.schemas.one_s import (
    AdditionalExpenseItem,
    DateRow,
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
from app.utils.normalization import extract_order_number
from app.utils.one_s_format import parse_document_presentation, parse_one_s_date

logger = logging.getLogger(__name__)


#: Табличная часть «Даты» приходит уплощённой: префикс свойства задаёт колонку.
ATTRIBUTE_COLUMN_PREFIXES = {
    "ПЛАН ": "plan",
    "ФАКТ ": "fact",
    "НОРМ ": "normative",
}


def map_date_rows(attributes: list[SupplierOrderAttributeRow]) -> list[DateRow]:
    """Собирает строки дат из плоского списка свойств заказа.

    «ФАКТ Дата автовывоза со станции/порта = Пост.» и её соседи с префиксами
    ПЛАН и НОРМ — это одна строка табличной части с тремя колонками. Свойства без
    известного префикса (судно, партия, порт выхода) строками дат не являются и
    пропускаются.
    """
    rows: dict[str, dict[str, object]] = {}
    for attribute in attributes:
        name = attribute.name
        if not name:
            continue
        for prefix, column in ATTRIBUTE_COLUMN_PREFIXES.items():
            if name.startswith(prefix):
                row_name = name[len(prefix):].strip()
                if not row_name:
                    break
                try:
                    parsed = parse_one_s_date(attribute.value)
                except ValueError:
                    # Свойство с таким префиксом может оказаться не датой —
                    # терять из-за него весь заказ нельзя.
                    logger.warning("Свойство %r не разобралось как дата: %r", name, attribute.value)
                    break
                rows.setdefault(row_name, {"name": row_name})[column] = parsed
                break
    return [DateRow(**values) for values in rows.values()]


def map_supplier_order_link(presentation: str | None) -> SupplierOrderLink:
    """Разбирает представление заказа поставщику.

    Представление может измениться при перенастройке 1С, поэтому исходная строка
    сохраняется всегда, а номер извлекается с запасным вариантом. Если номер не
    вытащить, сопоставление честно не найдёт заказ и документ уйдёт на ручную
    обработку — записать не тот документ такой сбой не может.
    """
    if not presentation:
        return SupplierOrderLink()

    parsed = parse_document_presentation(presentation)
    if parsed is not None:
        return SupplierOrderLink(number=parsed.number, date=parsed.date, presentation=presentation)

    number = extract_order_number(presentation)
    if number is None:
        logger.warning("Не удалось разобрать представление заказа поставщику: %r", presentation)
    return SupplierOrderLink(number=number, presentation=presentation)


def map_additional_expense(row: AdditionalExpenseRow) -> AdditionalExpenseItem | None:
    """Строка без идентификатора бесполезна: по ней нельзя ни писать, ни ссылаться."""
    if not row.document_id:
        logger.warning("Поступление доп. расходов без идентификатора пропущено: %r", row.number)
        return None

    return AdditionalExpenseItem(
        ref=row.document_id,
        number=row.number,
        date=row.date,
        posted=row.posted,
        deletion_mark=row.deletion_mark,
        amount=row.amount,
        organization=Party(name=row.organization),
        counterparty=Party(name=row.counterparty),
        distribution_method=row.distribution_method,
        # Склада в поступлении доп. расходов нет — он берётся из заказа.
        warehouse=Warehouse(),
        incoming_requisites=IncomingRequisites(number=row.incoming_number, date=row.incoming_date),
        department=row.department,
        responsible=row.responsible,
        comment=row.comment,
        additional_expense_type=row.additional_expense_type,
        supplier_order=map_supplier_order_link(row.supplier_order),
    )


def map_supplier_order(
    row: SupplierOrderRow,
    auto_pickup_fields: Iterable[str] = (),
) -> SupplierOrderItem | None:
    if not row.document_id:
        logger.warning("Заказ поставщику без идентификатора пропущен: %r", row.number)
        return None

    return SupplierOrderItem(
        ref=row.document_id,
        number=row.number,
        date=row.date,
        posted=row.posted,
        deletion_mark=row.deletion_mark,
        amount=row.amount,
        currency=row.currency,
        counterparty=Party(name=row.counterparty),
        payment_date=row.payment_date,
        receipt_date=row.receipt_date,
        # Группа склада проставляется отдельно, по справочнику складов.
        warehouse=Warehouse(name=row.warehouse),
        department=row.department,
        organization=Party(name=row.organization),
        additional_expenses_flag=row.additional_expense_flag,
        responsible=row.responsible,
        comment=row.comment,
        base_document=map_supplier_order_link(row.base_document),
        container_number=row.container_number,
        departure_month=row.departure_month,
        auto_pickup_fact_date=row.auto_pickup_fact_date(auto_pickup_fields),
    )


def map_additional_expenses(rows: list[AdditionalExpenseRow]) -> list[AdditionalExpenseItem]:
    return [item for item in map(map_additional_expense, rows) if item is not None]


def map_supplier_orders(
    rows: list[SupplierOrderRow],
    auto_pickup_fields: Sequence[str] = (),
) -> list[SupplierOrderItem]:
    return [
        item
        for item in (map_supplier_order(row, auto_pickup_fields) for row in rows)
        if item is not None
    ]
