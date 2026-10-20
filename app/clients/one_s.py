"""Транспорт до HTTP-сервисов 1С.

Клиент знает только про пути, обёртку ответа и запрет записи. Превращение строк
обмена в доменные модели живёт в `app/services/one_s_mapping.py`, композиция
запросов — в `app/services/one_s_repository.py`.
"""

from typing import Any, TypeVar

from app.clients.http_json import HttpClientError, JsonHttpClient
from app.config import Settings
from app.schemas.one_s_wire import (
    AdditionalExpenseQuery,
    AdditionalExpenseRow,
    AdditionalExpenseTypeRow,
    OneSWireModel,
    OrganizationRow,
    SupplierOrderAttributeRow,
    SupplierOrderAttributesQuery,
    SupplierOrderQuery,
    SupplierOrderRow,
    UpdateIncomingRequisitesRequest,
    UpdateIncomingRequisitesResult,
    WarehouseRow,
)

ONE_S_PATHS = {
    "organization": "/data1C/organization",
    "addexpensetype": "/data1C/addexpensetype",
    "warehouse": "/data1C/warehouse",
    "docaddexpenses": "/data1C/docaddexpenses",
    "ordertosupplier": "/data1C/ordertosupplier",
    "ordertosupplierattributes": "/data1C/ordertosupplierattributes",
    "updatedocaddexpenses": "/data1C/updatedocaddexpenses",
}

ONE_S_OK_CODE = 200

WRITES_FORBIDDEN_MESSAGE = (
    "Запись в 1С запрещена настройкой ONE_S_WRITES_ENABLED. HTTP POST не выполнялся."
)

WireT = TypeVar("WireT", bound=OneSWireModel)


def _as_int(value: Any) -> int | None:
    """Код ответа может прийти и числом, и строкой — в обмене всё остальное строки."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def unwrap_one_s_response(path: str, response: Any) -> list[dict[str, Any]]:
    """Разбирает обёртку {"code": ..., "data": ...}.

    Форм ответа три, и все они наблюдались вживую:

    * успех — ``{"code": 200, "data": [...]}``;
    * исключение в обработчике — ``{"code": 500, "data": "<текст исключения>"}``,
      то есть тип ``data`` меняется со списка на строку;
    * ошибка маршрутизации — ``{"code": 404, "error": "Некорректный путь."}``,
      где текст лежит уже в другом ключе.

    Текст ошибки извлекается раньше проверки кода, чтобы сообщение 1С доехало до
    колонки «Описание» в отчёте, а не потерялось за безликим «код 404».
    """
    if not isinstance(response, dict):
        raise HttpClientError(f"1С вернула неожиданный ответ на {path}", payload=response)

    code = _as_int(response.get("code"))
    data = response.get("data")
    error = response.get("error")

    if isinstance(error, str) and error.strip():
        raise HttpClientError(
            f"1С вернула ошибку на {path}: {error}", status_code=code, payload=response
        )
    if isinstance(data, str):
        raise HttpClientError(
            f"1С вернула ошибку на {path}: {data}", status_code=code, payload=response
        )
    if code is not None and code != ONE_S_OK_CODE:
        raise HttpClientError(f"1С вернула код {code} на {path}", status_code=code, payload=response)
    if data is None:
        return []
    if not isinstance(data, list):
        raise HttpClientError(
            f"1С вернула не список в data на {path}", status_code=code, payload=response
        )
    return [row for row in data if isinstance(row, dict)]


class OneSClient:
    def __init__(self, settings: Settings):
        self._writes_enabled = settings.one_s_writes_enabled
        self._bulk_timeout_seconds = settings.one_s_bulk_timeout_seconds
        self._http = JsonHttpClient(
            base_url=settings.one_s_base_url,
            timeout_seconds=settings.request_timeout_seconds,
            username=settings.one_s_username,
            REDACTED=settings.one_s_REDACTED,
        )

    def _fetch(
        self,
        key: str,
        model: type[WireT],
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> list[WireT]:
        path = ONE_S_PATHS[key]
        response = self._http.get_json(path, payload, timeout_seconds=timeout_seconds)
        return [model.model_validate(row) for row in unwrap_one_s_response(path, response)]

    # --- Справочники -------------------------------------------------------

    def list_organizations(self) -> list[OrganizationRow]:
        return self._fetch("organization", OrganizationRow)

    def list_additional_expense_types(self) -> list[AdditionalExpenseTypeRow]:
        return self._fetch("addexpensetype", AdditionalExpenseTypeRow)

    def list_warehouses(self) -> list[WarehouseRow]:
        return self._fetch("warehouse", WarehouseRow)

    # --- Документы ---------------------------------------------------------

    def list_additional_expenses(self, query: AdditionalExpenseQuery) -> list[AdditionalExpenseRow]:
        return self._fetch(
            "docaddexpenses",
            AdditionalExpenseRow,
            query.to_payload(),
            timeout_seconds=self._bulk_timeout_seconds,
        )

    def list_supplier_orders(self, query: SupplierOrderQuery) -> list[SupplierOrderRow]:
        return self._fetch(
            "ordertosupplier",
            SupplierOrderRow,
            query.to_payload(),
            timeout_seconds=self._bulk_timeout_seconds,
        )

    def list_supplier_order_attributes(self, document_id: str) -> list[SupplierOrderAttributeRow]:
        """Свойства одного заказа, включая уплощённую табличную часть «Даты»."""
        return self._fetch(
            "ordertosupplierattributes",
            SupplierOrderAttributeRow,
            SupplierOrderAttributesQuery(document_id=document_id).to_payload(),
        )

    # --- Запись ------------------------------------------------------------

    def update_incoming_requisites(
        self, request: UpdateIncomingRequisitesRequest
    ) -> UpdateIncomingRequisitesResult:
        # Запрет проверяется до формирования тела и до любого обращения к сети:
        # это последний рубеж перед продуктивной базой.
        if not self._writes_enabled:
            raise RuntimeError(WRITES_FORBIDDEN_MESSAGE)
        response = self._http.post(ONE_S_PATHS["updatedocaddexpenses"], request.to_payload())
        return UpdateIncomingRequisitesResult.from_response(response)
