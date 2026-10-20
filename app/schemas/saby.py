from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SabyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SabyDocument(SabyModel):
    id: str
    title: str | None = None
    incoming_number: str
    incoming_date: Date
    amount: Decimal
    organization_name: str | None = None
    organization_inn: str | None = None
    organization_kpp: str | None = None
    counterparty_name: str | None = None
    counterparty_inn: str | None = None
    counterparty_kpp: str | None = None
    supplier_order_number: str | None = None
    #: Откуда взят номер заказа: "field" — из реквизита документа, "text" — выдран
    #: регуляркой из комментария или названия. Во втором случае номер ненадёжен:
    #: в тот же шаблон попадают номера счетов-фактур, контейнеров и договоров.
    supplier_order_source: Literal["field", "text"] | None = None
    additional_expense_type: str | None = None
    comment: str | None = None
    source_url: str | None = None
    raw: dict | None = None
