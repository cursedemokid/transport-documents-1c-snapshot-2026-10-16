"""Бизнес-проверки перед записью входящих реквизитов в 1С."""

from dataclasses import dataclass
from datetime import date as Date
from typing import Callable

from app.schemas.one_s import AdditionalExpenseItem, SupplierOrderItem
from app.schemas.saby import SabyDocument
from app.schemas.sync import SyncItemResult
from app.services.one_s_repository import OrderLookup
from app.utils.dates import normalize_stored_date
from app.utils.normalization import clean_text, same_doc_number


@dataclass(frozen=True)
class ValidationContext:
    #: Нормализованные значения: сравнение идёт через clean_text.
    #: Разрешённые виды доп. расходов. Пустой набор — все.
    allowed_expense_types: frozenset[str]
    allowed_warehouse_groups: frozenset[str]
    #: Группа склада берётся из справочника: в документах её нет.
    warehouse_group_resolver: Callable[[str | None], str | None]
    auto_pickup_row_name: str
    #: Дозаполнять документ, если в 1С проставлен только номер или только дата.
    allow_completing_partial: bool = True


def resolve_auto_pickup_fact(order: SupplierOrderItem, row_name: str) -> Date | None:
    """Ищет фактическую дату автовывоза во всех известных источниках.

    Источников два, и они в разном состоянии. Плоского поля в обмене нет — его
    имя не названо, кандидаты задаются `ONE_S_AUTO_PICKUP_FIELDS` и по умолчанию
    список пуст. А табличная часть «Даты» приходит через
    `ordertosupplierattributes` и работает: на живом прогоне документы эту
    проверку проходят. Поэтому пустой результат означает не «обмен не умеет», а
    «у этого заказа даты нет».
    """
    if order.auto_pickup_fact_date is not None:
        return normalize_stored_date(order.auto_pickup_fact_date)

    target = clean_text(row_name)
    rows = [*order.date_rows, *(row for container in order.containers for row in container.date_rows)]
    facts = [
        normalized
        for row in rows
        if clean_text(row.name) == target and row.fact is not None
        # Берётся именно факт: плановая и нормативная даты не подходят.
        for normalized in (normalize_stored_date(row.fact),)
        if normalized is not None
    ]
    return min(facts) if facts else None


def _result_context(
    saby_doc: SabyDocument,
    expense: AdditionalExpenseItem,
    order: SupplierOrderItem | None,
    warehouse_group: str | None = None,
) -> dict:
    warehouse = order.warehouse if order else expense.warehouse
    return {
        "saby_id": saby_doc.id,
        "saby_number": saby_doc.incoming_number,
        "saby_date": saby_doc.incoming_date,
        "counterparty_name": saby_doc.counterparty_name or expense.counterparty.name,
        "counterparty_inn": saby_doc.counterparty_inn or expense.counterparty.inn,
        "amount": saby_doc.amount,
        "additional_expense_ref": expense.ref,
        "additional_expense_number": expense.number,
        "supplier_order_ref": order.ref if order else expense.supplier_order.ref,
        "supplier_order_number": order.number if order else expense.supplier_order.number,
        "warehouse_name": warehouse.name,
        "warehouse_group": warehouse_group or warehouse.group_name,
        "additional_expense_type": expense.additional_expense_type,
    }


def _same_incoming_number(saby_doc: SabyDocument, expense: AdditionalExpenseItem) -> bool:
    return same_doc_number(expense.incoming_requisites.number, saby_doc.incoming_number)


def validate_before_write(
    saby_doc: SabyDocument,
    expense: AdditionalExpenseItem,
    lookup: OrderLookup,
    context: ValidationContext,
) -> SyncItemResult | None:
    """Возвращает None, если документ можно записывать."""
    # Пометка удаления и проведённость проверяются здесь, а не только в выборке.
    # Между предложением и подтверждением проходят часы: сотрудник читает письмо
    # не сразу. За это время документ успевают пометить на удаление или
    # распровести, и перечитывание перед записью вернёт его как ни в чём не
    # бывало — постфильтра выборки на этом пути нет.
    if expense.deletion_mark or expense.posted is False:
        state = "помечен на удаление" if expense.deletion_mark else "не проведён"
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, None),
            message=f"Документ 1С {state} — запись не выполнялась.",
        )

    stored_number = expense.incoming_requisites.number
    stored_date = normalize_stored_date(expense.incoming_requisites.date)
    number_matches = _same_incoming_number(saby_doc, expense)
    date_matches = stored_date == saby_doc.incoming_date

    if number_matches and date_matches:
        return SyncItemResult(
            status="already_filled",
            **_result_context(saby_doc, expense, None),
            message="Входящие реквизиты уже заполнены теми же номером и датой.",
        )

    if stored_number and not number_matches:
        return SyncItemResult(
            status="conflict",
            **_result_context(saby_doc, expense, None),
            message=(
                f"В 1С указан другой входящий документ: {stored_number!r} от {stored_date}. "
                "Документ автоматически не изменялся."
            ),
        )
    if stored_date is not None and not date_matches:
        return SyncItemResult(
            status="conflict",
            **_result_context(saby_doc, expense, None),
            message=(
                f"В 1С другая дата входящего документа: {stored_date} вместо "
                f"{saby_doc.incoming_date}. Документ автоматически не изменялся."
            ),
        )
    # Сюда доходим либо с пустыми реквизитами, либо когда заполнена ровно одна
    # половина и она совпадает с нашей — такой документ можно дозаполнить.
    partially_filled = bool(stored_number) or stored_date is not None
    if partially_filled and not context.allow_completing_partial:
        return SyncItemResult(
            status="conflict",
            **_result_context(saby_doc, expense, None),
            message="Входящие реквизиты заполнены частично, дозаполнение запрещено настройкой.",
        )

    if lookup.status == "no_number":
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, None),
            message=(
                "В поступлении доп. расходов не указан заказ поставщику "
                f"({expense.supplier_order.presentation!r}). Требуется ручная обработка."
            ),
        )
    if lookup.status == "ambiguous":
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, None),
            message=(
                f"Найдено несколько заказов с номером {expense.supplier_order.number}: "
                f"{len(lookup.candidates)}. Выбрать автоматически невозможно."
            ),
        )
    if lookup.order is None:
        return SyncItemResult(
            status="business_error",
            **_result_context(saby_doc, expense, None),
            message=f"Заказ поставщику {expense.supplier_order.number} не найден в выборке 1С.",
        )

    order = lookup.order

    if (
        context.allowed_expense_types
        and clean_text(expense.additional_expense_type) not in context.allowed_expense_types
    ):
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, order),
            message=f"Неподдерживаемый вид доп. расхода: {expense.additional_expense_type!r}.",
        )

    warehouse_group = context.warehouse_group_resolver(order.warehouse.name)
    if warehouse_group is None:
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, order),
            message=(
                f"Склад {order.warehouse.name!r} из заказа не найден в справочнике 1С "
                "однозначно — группу определить не удалось."
            ),
        )
    if clean_text(warehouse_group) not in context.allowed_warehouse_groups:
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, order, warehouse_group),
            message=(
                f"Группа склада {warehouse_group!r} не входит в разрешённые. "
                "Требуется ручная обработка."
            ),
        )

    fact_date = resolve_auto_pickup_fact(order, context.auto_pickup_row_name)
    if fact_date is None:
        return SyncItemResult(
            status="manual",
            **_result_context(saby_doc, expense, order, warehouse_group),
            message=(
                "Не удалось получить фактическую дату автовывоза со станции/порта по заказу "
                f"{order.number}: 1С не передаёт это поле. Требуется ручная обработка."
            ),
        )

    if saby_doc.incoming_date > fact_date:
        return SyncItemResult(
            status="business_error",
            **_result_context(saby_doc, expense, order, warehouse_group),
            message=(
                f"Дата УПД {saby_doc.incoming_date:%d.%m.%Y} позже фактического автовывоза "
                f"{fact_date:%d.%m.%Y}."
            ),
        )

    return None
