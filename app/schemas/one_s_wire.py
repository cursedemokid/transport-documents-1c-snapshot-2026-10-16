"""Модели обмена с 1С «как есть»: русские ключи, значения строками.

Это исполняемое описание реального контракта: какие ключи присылает 1С и в каком
виде. Доменные модели из `one_s.py` остаются языком сервисов, а превращение одних
в другие живёт в `app/services/one_s_mapping.py`.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime as DateTime
from decimal import Decimal
from typing import Annotated, Any, Iterable

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.utils.one_s_format import (
    format_one_s_period,
    parse_one_s_amount,
    parse_one_s_bool,
    parse_one_s_date,
    parse_one_s_datetime,
    parse_one_s_text,
)

OneSText = Annotated[str | None, BeforeValidator(parse_one_s_text)]
OneSDate = Annotated[Date | None, BeforeValidator(parse_one_s_date)]
OneSDateTime = Annotated[DateTime | None, BeforeValidator(parse_one_s_datetime)]
OneSAmount = Annotated[Decimal | None, BeforeValidator(parse_one_s_amount)]
OneSBool = Annotated[bool | None, BeforeValidator(parse_one_s_bool)]


class OneSWireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def compact_one_s_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Убирает из тела запроса значения, которых 1С не ждёт.

    Пустая ссылка роняет сервер: `ordertosupplier: ""` возвращает HTTP 500
    «Ошибка при вызове конструктора (УникальныйИдентификатор)». Поэтому пустые
    значения не отправляются вовсе. `False` при этом сохраняется — `confirmed` и
    `deletemark` фильтруют по точному совпадению, и потеря `False` изменила бы
    выборку.
    """
    return {key: value for key, value in payload.items() if value is not None and value != ""}


# --- Справочники -----------------------------------------------------------


class OrganizationRow(OneSWireModel):
    """Коды организаций в 1С НЕ уникальны, ИНН обмен не отдаёт."""

    name: OneSText = Field(default=None, alias="Организация")
    code: OneSText = Field(default=None, alias="Код")


class AdditionalExpenseTypeRow(OneSWireModel):
    name: OneSText = Field(default=None, alias="ВидДопРасхода")
    code: OneSText = Field(default=None, alias="Код")
    #: Группировка по сверке с НБ, к складам отношения не имеет.
    group_name: OneSText = Field(default=None, alias="Группа")


class WarehouseRow(OneSWireModel):
    """Единственный источник группы склада: в документах её нет."""

    name: OneSText = Field(default=None, alias="Склад")
    code: OneSText = Field(default=None, alias="Код")
    group_name: OneSText = Field(default=None, alias="Группа")
    accounting_warehouse: OneSText = Field(default=None, alias="СкладБухгалтерия")


# --- Документы -------------------------------------------------------------


class AdditionalExpenseRow(OneSWireModel):
    """Поступление дополнительных расходов. Склада и ИНН в ответе нет."""

    document_id: OneSText = Field(default=None, alias="ИдентификаторДокумента")
    number: OneSText = Field(default=None, alias="Номер")
    date: OneSDateTime = Field(default=None, alias="Дата")
    posted: OneSBool = Field(default=None, alias="Проведен")
    deletion_mark: OneSBool = Field(default=None, alias="ПометкаУдаления")
    amount: OneSAmount = Field(default=None, alias="СуммаДокумента")
    organization: OneSText = Field(default=None, alias="Организация")
    counterparty: OneSText = Field(default=None, alias="Контрагент")
    distribution_method: OneSText = Field(default=None, alias="СпособРаспределения")
    incoming_number: OneSText = Field(default=None, alias="НомерВходящегоДокумента")
    incoming_date: OneSDate = Field(default=None, alias="ДатаВходящегоДокумента")
    department: OneSText = Field(default=None, alias="Подразделение")
    responsible: OneSText = Field(default=None, alias="Ответственный")
    comment: OneSText = Field(default=None, alias="Комментарий")
    additional_expense_type: OneSText = Field(default=None, alias="ВидДопРасхода")
    #: Строка представления вида «Заказ поставщику ТД000001075 от 16.04.2026 0:00:00»,
    #: а не ссылка: UUID заказа обмен не отдаёт.
    supplier_order: OneSText = Field(default=None, alias="ЗаказПоставщику")


class SupplierOrderRow(OneSWireModel):
    """Заказ поставщику. Табличных частей обмен не отдаёт вовсе.

    Неизвестные ключи намеренно сохраняются: техническое имя поля с фактической
    датой автовывоза 1С ещё не назвала, и когда оно появится, его нужно будет
    прочитать по имени из настройки, а не по зашитому в код предположению.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    document_id: OneSText = Field(default=None, alias="ИдентификаторДокумента")
    number: OneSText = Field(default=None, alias="Номер")
    date: OneSDateTime = Field(default=None, alias="Дата")
    posted: OneSBool = Field(default=None, alias="Проведен")
    deletion_mark: OneSBool = Field(default=None, alias="ПометкаУдаления")
    amount: OneSAmount = Field(default=None, alias="СуммаДокумента")
    currency: OneSText = Field(default=None, alias="ВалютаДокумента")
    counterparty: OneSText = Field(default=None, alias="Контрагент")
    payment_date: OneSDate = Field(default=None, alias="ДатаОплаты")
    receipt_date: OneSDate = Field(default=None, alias="ДатаПоступления")
    warehouse: OneSText = Field(default=None, alias="Склад")
    department: OneSText = Field(default=None, alias="Подразделение")
    organization: OneSText = Field(default=None, alias="Организация")
    additional_expense_flag: OneSBool = Field(default=None, alias="ДопРасход")
    responsible: OneSText = Field(default=None, alias="Ответственный")
    comment: OneSText = Field(default=None, alias="Комментарий")
    base_document: OneSText = Field(default=None, alias="ДокументОснование")
    container_number: OneSText = Field(default=None, alias="НомерКонтейнера")
    departure_month: OneSDate = Field(default=None, alias="МесяцВыхода")

    def auto_pickup_fact_date(self, field_names: Iterable[str]) -> Date | None:
        """Фактическая дата автовывоза из ПЛОСКОГО поля заказа, если оно есть.

        Имени такого поля в обмене пока нет: угадывать его в коде нельзя,
        поэтому кандидаты задаются настройкой ONE_S_AUTO_PICKUP_FIELDS, и пока
        она пуста, метод всегда возвращает None. Это не значит, что даты нет
        вовсе: основной источник — табличная часть «Даты» из
        `ordertosupplierattributes`, и разбирает её `map_date_rows`.
        """
        extra = self.model_extra or {}
        for name in field_names:
            if name in extra:
                return parse_one_s_date(extra[name])
        return None


class SupplierOrderAttributeRow(OneSWireModel):
    """Одно свойство заказа из /data1C/ordertosupplierattributes.

    Табличная часть «Даты» приходит уплощённой: имя свойства начинается с
    «ПЛАН », «ФАКТ » или «НОРМ », а дальше идёт название строки. Свойства без
    префикса — обычные реквизиты заказа (судно, партия, порт выхода).
    """

    name: OneSText = Field(default=None, alias="Свойство")
    value: OneSText = Field(default=None, alias="Значение")


# --- Запросы ---------------------------------------------------------------


class AdditionalExpenseQuery(OneSWireModel):
    """Параметры GET /data1C/docaddexpenses."""

    period_begin: Date
    period_end: Date
    posted: bool | None = True
    deletion_mark: bool | None = False
    organization_code: str | None = None
    additional_expense_type_code: str | None = None
    #: UUID заказа. Пустую строку сюда передавать нельзя — 1С падает.
    supplier_order_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return compact_one_s_payload(
            {
                "periodBegin": format_one_s_period(self.period_begin),
                "periodEnd": format_one_s_period(self.period_end),
                "confirmed": self.posted,
                "deletemark": self.deletion_mark,
                "organization": self.organization_code,
                "addexpensetype": self.additional_expense_type_code,
                "ordertosupplier": self.supplier_order_id,
            }
        )


class SupplierOrderQuery(OneSWireModel):
    """Параметры GET /data1C/ordertosupplier.

    Фильтра по номеру или UUID заказа обмен не поддерживает: лишние параметры
    молча игнорируются и возвращается вся выборка за период.
    """

    period_begin: Date
    period_end: Date
    posted: bool | None = True
    deletion_mark: bool | None = False
    organization_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return compact_one_s_payload(
            {
                "periodBegin": format_one_s_period(self.period_begin),
                "periodEnd": format_one_s_period(self.period_end),
                "confirmed": self.posted,
                "deletemark": self.deletion_mark,
                "organization": self.organization_code,
            }
        )


class SupplierOrderAttributesQuery(OneSWireModel):
    """Параметры GET /data1C/ordertosupplierattributes."""

    document_id: str

    def to_payload(self) -> dict[str, Any]:
        return {"docid": self.document_id}


class UpdateIncomingRequisitesRequest(OneSWireModel):
    """Тело POST /data1C/updatedocaddexpenses.

    Полей оптимистической блокировки обмен не принимает, поэтому защита от
    перезаписи чужих изменений строится на перечитывании документа.
    """

    document_id: str
    incoming_number: str
    incoming_date: Date

    def to_payload(self) -> dict[str, Any]:
        return {
            "docID": self.document_id,
            # Номер пишем ровно в том виде, в каком он пришёл из Saby:
            # нормализация нужна только для сравнения, но не для записи.
            "incomingdocnumber": self.incoming_number,
            "incomingdocdate": format_one_s_period(self.incoming_date),
        }


#: Ключи, по которым 1С сообщает об ошибке в разных обработчиках.
_ERROR_KEYS = ("error", "Ошибка", "ОписаниеОшибки")
_STATUS_KEYS = ("success", "Успех", "Результат", "status")
_FAILED_STATUSES = {"false", "ошибка", "error", "fail", "failed", "нет"}


class UpdateIncomingRequisitesResult(OneSWireModel):
    """Ответ на запись. Формат обмена не задокументирован, поэтому разбор
    никогда не падает на форме ответа.

    Признать запись успешной по телу ответа нельзя — это делает контрольное
    перечитывание документа. Здесь распознаётся только явный отказ, чтобы не
    выполнять лишнее чтение и показать причину в отчёте.
    """

    explicit_error: bool = False
    code: int | None = None
    message: str | None = None
    raw: Any = None

    @classmethod
    def from_response(cls, response: Any) -> "UpdateIncomingRequisitesResult":
        if not isinstance(response, dict):
            return cls(raw=response, message=None if response in (None, {}) else str(response))

        code = response.get("code")
        code = code if isinstance(code, int) else None

        for key in _ERROR_KEYS:
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return cls(explicit_error=True, code=code, message=value.strip(), raw=response)

        for key in _STATUS_KEYS:
            value = response.get(key)
            if value is False or (isinstance(value, str) and value.strip().casefold() in _FAILED_STATUSES):
                return cls(
                    explicit_error=True,
                    code=code,
                    message=f"1С вернула {key}={value!r}",
                    raw=response,
                )

        data = response.get("data")
        message = data.strip() if isinstance(data, str) and data.strip() else None
        return cls(code=code, message=message, raw=response)
