from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SyncItemStatus = Literal[
    "written",
    # Проверки пройдены, но записи не будет, пока её не подтвердит человек.
    # Отличается от dry_run_ready намеренно: там «писать запрещено настройкой»,
    # здесь «ждём решения сотрудника», и в отчёте это разные строки.
    "awaiting_confirmation",
    "dry_run_ready",
    "already_filled",
    "not_found",
    "ambiguous",
    "business_error",
    "manual",
    "conflict",
    "technical_error",
]


class SyncModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SyncRunRequest(SyncModel):
    date_from: Date | None = None
    date_to: Date | None = None
    dry_run: bool = True
    send_report: bool = True
    max_documents: int | None = None
    #: Запретить запоминание периода: режим «посмотреть, не двигая состояние».
    advance_state: bool | None = None


class MatchCandidate(SyncModel):
    """Кандидат на сопоставление — то, из чего выбирает человек.

    До появления подтверждений структурированный список терялся: в результат
    попадала только фраза из `MatchOutcome.explanation`, а по фразе выбор не
    сделаешь.
    """

    ref: str
    number: str | None = None
    date: Date | None = None
    amount: Decimal | None = None
    counterparty_name: str | None = None
    supplier_order_number: str | None = None
    warehouse_name: str | None = None
    additional_expense_type: str | None = None
    score: int = 0
    signals: list[str] = Field(default_factory=list)


class SyncItemResult(SyncModel):
    status: SyncItemStatus
    attempt: int = 1
    saby_id: str | None = None
    saby_number: str | None = None
    saby_date: Date | None = None
    counterparty_name: str | None = None
    counterparty_inn: str | None = None
    amount: Decimal | None = None
    additional_expense_ref: str | None = None
    additional_expense_number: str | None = None
    supplier_order_ref: str | None = None
    supplier_order_number: str | None = None
    warehouse_name: str | None = None
    warehouse_group: str | None = None
    additional_expense_type: str | None = None
    message: str
    #: Повторять имеет смысл только то, что сорвалось на сети или на стороне 1С.
    #: Отказ по настройке или по данным от повтора не изменится.
    retryable: bool = False
    #: Из чего выбирать: один кандидат у awaiting_confirmation, несколько у
    #: ambiguous. Пусто у всех остальных статусов.
    candidates: list[MatchCandidate] = Field(default_factory=list)


class SyncRunResponse(SyncModel):
    request_id: str
    #: Итоговый режим прогона: запись пропускается и при запросе с dry_run,
    #: и при выключенной настройке ONE_S_WRITES_ENABLED.
    dry_run: bool
    date_from: Date
    date_to: Date
    #: Сколько документов вернул Saby до обрезки max_documents.
    total_saby_documents: int
    #: Сколько из них реально обработано.
    processed_documents: int = 0
    #: События Saby, которые не удалось разобрать: они не попали ни в выборку,
    #: ни в статусы, поэтому суммы по отчёту без них не сходятся.
    skipped_saby_events: int = 0
    #: Реестр СБИС прочитан не полностью (упёрлись в SABY_MAX_PAGES). Он не
    #: отсортирован по дате документа, поэтому обрезка означает дыру в выборке,
    #: а не просто «не дочитали старое».
    saby_registry_truncated: bool = False
    results: list[SyncItemResult] = Field(default_factory=list)
    report_sent: bool = False
    #: Куда сохранён файл отчёта и почему письмо не ушло — чтобы причина была
    #: видна прямо в ответе API, без похода в логи.
    report_path: str | None = None
    report_reason: str | None = None
    #: Разрешена ли запись в 1С — чтобы по отчёту было видно, был ли прогон боевым.
    writes_enabled: bool = False
    #: Код подтверждения живёт ровно до отправки письма и в ответе API
    #: всегда пуст: он предназначен получателю рассылки, а не вызывающему.
    confirmation_code: str | None = Field(default=None, exclude=True)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.results:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["counts"] = self.counts
        return data
