from __future__ import annotations

from datetime import date as Date, datetime as DateTime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class OneSModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Party(OneSModel):
    name: str | None = None
    #: Обмен с 1С ИНН и КПП не отдаёт — для документов 1С эти поля всегда пустые,
    #: сопоставление контрагентов идёт по наименованию.
    inn: str | None = None
    kpp: str | None = None


class Warehouse(OneSModel):
    name: str | None = None
    group_name: str | None = None


class IncomingRequisites(OneSModel):
    number: str | None = None
    date: Date | None = None


class SupplierOrderLink(OneSModel):
    #: UUID заказа обмен не отдаёт: в документе лежит только представление.
    ref: str | None = None
    number: str | None = None
    #: Исходная строка «Заказ поставщику ТД000001075 от 16.04.2026 0:00:00».
    presentation: str | None = None
    #: Дата из представления. Заказы нельзя фильтровать по номеру, поэтому она
    #: подсказывает, какой период запрашивать.
    date: Date | None = None


class DateRow(OneSModel):
    name: str
    plan: Date | None = None
    fact: Date | None = None
    normative: Date | None = None
    comment: str | None = None


class ContainerDates(OneSModel):
    number: str | None = None
    party: str | None = None
    date_rows: list[DateRow] = Field(default_factory=list)


class AdditionalExpenseItem(OneSModel):
    ref: str
    date: DateTime | Date | None = None
    number: str | None = None
    posted: bool | None = None
    deletion_mark: bool | None = None
    amount: Decimal | None = None
    organization: Party = Field(default_factory=Party)
    counterparty: Party = Field(default_factory=Party)
    distribution_method: str | None = None
    warehouse: Warehouse = Field(default_factory=Warehouse)
    incoming_requisites: IncomingRequisites = Field(default_factory=IncomingRequisites)
    department: str | None = None
    responsible: str | None = None
    comment: str | None = None
    additional_expense_type: str | None = None
    supplier_order: SupplierOrderLink = Field(default_factory=SupplierOrderLink)


class SupplierOrderItem(OneSModel):
    ref: str
    date: DateTime | Date | None = None
    number: str | None = None
    posted: bool | None = None
    deletion_mark: bool | None = None
    amount: Decimal | None = None
    currency: str | None = None
    counterparty: Party = Field(default_factory=Party)
    payment_date: Date | None = None
    receipt_date: Date | None = None
    warehouse: Warehouse = Field(default_factory=Warehouse)
    department: str | None = None
    organization: Party = Field(default_factory=Party)
    additional_expenses_flag: bool | None = None
    responsible: str | None = None
    comment: str | None = None
    base_document: SupplierOrderLink = Field(default_factory=SupplierOrderLink)
    container_number: str | None = None
    departure_month: Date | str | None = None
    #: Табличных частей обмен не отдаёт, поэтому оба списка всегда пусты.
    #: Оставлены до тех пор, пока 1С не решит, в каком виде передавать даты.
    containers: list[ContainerDates] = Field(default_factory=list)
    date_rows: list[DateRow] = Field(default_factory=list)
    #: Фактическая дата автовывоза со станции/порта. Пока 1С её не передаёт,
    #: поле остаётся пустым и документ уходит в ручную обработку.
    auto_pickup_fact_date: Date | None = None
