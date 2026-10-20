"""Оркестрация переноса входящих документов Saby в 1С."""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.clients.http_json import HttpClientError
from app.clients.saby import SabyClient
from app.config import Settings
from app.schemas.one_s import AdditionalExpenseItem, SupplierOrderItem
from app.schemas.saby import SabyDocument
from app.schemas.sync import MatchCandidate, SyncItemResult, SyncRunRequest, SyncRunResponse
from app.services.matching import MatchConfig, match_additional_expenses
from app.services.one_s_repository import OneSRepository, OrderLookup
from app.services.reporting import ReportService
from app.services.confirmations import ConfirmationStore, PendingItem, hash_code
from app.services.sync_state import SyncStateStore
from app.services.validation import ValidationContext, validate_before_write
from app.utils.dates import default_window
from app.utils.normalization import clean_text

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {"technical_error"}



def _candidates_of(outcome) -> list[MatchCandidate]:
    """Кандидаты в виде, пригодном для показа человеку и для хранения.

    `MatchOutcome` держит доменные модели 1С целиком; в результат прогона нужна
    та их часть, по которой сотрудник опознаёт документ, плюс счёт и сигналы —
    чтобы было видно, на чём основано предложение.
    """
    return [
        MatchCandidate(
            ref=entry.item.ref,
            number=entry.item.number,
            date=entry.item.date.date() if isinstance(entry.item.date, datetime) else entry.item.date,
            amount=entry.item.amount,
            counterparty_name=entry.item.counterparty.name,
            supplier_order_number=entry.item.supplier_order.number,
            warehouse_name=entry.item.warehouse.name if entry.item.warehouse else None,
            additional_expense_type=entry.item.additional_expense_type,
            score=entry.score,
            signals=list(entry.signals),
        )
        for entry in outcome.scored
    ]


class SyncService:
    def __init__(
        self,
        settings: Settings,
        saby_client: SabyClient | None = None,
        one_s_client=None,
        report_service: ReportService | None = None,
        repository: OneSRepository | None = None,
        state_store: SyncStateStore | None = None,
        confirmations: ConfirmationStore | None = None,
    ):
        self._settings = settings
        self._saby = saby_client or SabyClient(settings)
        self._one_s = repository or OneSRepository(settings, client=one_s_client)
        self._reporting = report_service or ReportService(settings)
        self._state = state_store or SyncStateStore(settings.sync_state_path)
        self._confirmations = confirmations or ConfirmationStore(
            settings.confirm_pending_path, settings.confirm_journal_path
        )
        #: Доменные объекты для позиций, ожидающих решения. В SyncItemResult
        #: попадают только поля для отчёта, а для записи нужны они целиком.
        self._confirmable: dict[int, tuple[SabyDocument, list[AdditionalExpenseItem]]] = {}

    # --- Конфигурация проверок ---------------------------------------------

    def _match_config(self) -> MatchConfig:
        return MatchConfig(
            amount_tolerance=Decimal(str(self._settings.amount_tolerance)),
            allowed_expense_types=self._allowed_expense_types(),
            date_window_days=self._settings.match_date_window_days,
            min_score=self._settings.match_min_score,
            score_margin=self._settings.match_score_margin,
            name_similarity=self._settings.match_name_similarity,
        )

    def _validation_context(self) -> ValidationContext:
        return ValidationContext(
            allowed_expense_types=self._allowed_expense_types(),
            allowed_warehouse_groups=frozenset(
                clean_text(group) for group in self._settings.one_s_allowed_warehouse_groups
            ),
            warehouse_group_resolver=self._one_s.reference.warehouse_group,
            auto_pickup_row_name=self._settings.one_s_auto_pickup_row_name,
            allow_completing_partial=self._settings.allow_completing_partial_requisites,
        )

    def _allowed_expense_types(self) -> frozenset[str]:
        return frozenset(
            clean_text(name) for name in self._settings.one_s_allowed_expense_types
        )

    # --- Прогон -------------------------------------------------------------

    def run(self, request: SyncRunRequest) -> SyncRunResponse:
        request_id = str(uuid4())
        date_from, date_to = self._resolve_period(request)
        writes_enabled = self._settings.one_s_writes_enabled
        # Третий, независимый предохранитель: при запрещённой записи прогон идёт
        # как dry-run, и до RuntimeError в клиенте дело просто не доходит —
        # значит, нечего и ретраить.
        effective_dry_run = request.dry_run or not writes_enabled

        def build_response(
            total: int, processed: int, results: list[SyncItemResult]
        ) -> SyncRunResponse:
            return SyncRunResponse(
                request_id=request_id,
                dry_run=effective_dry_run,
                writes_enabled=writes_enabled,
                date_from=date_from,
                date_to=date_to,
                total_saby_documents=total,
                processed_documents=processed,
                skipped_saby_events=getattr(self._saby, "skipped_events", 0),
                saby_registry_truncated=getattr(self._saby, "registry_truncated", False),
                results=results,
            )

        try:
            saby_documents = self._saby.list_incoming_documents(date_from, date_to)
        except Exception as error:
            failure = SyncItemResult(
                status="technical_error",
                message=f"Не удалось получить документы Saby: {error}",
                retryable=True,
            )
            return self._finalize(request, build_response(0, 0, [failure]))

        # Документы нужного типа, которые не удалось разобрать, идут в отчёт
        # отдельными строками. На живом реестре это 7% входящих УПД: сканы, у
        # которых машиночитаемой суммы нет ни в событии, ни в самом документе.
        # Сопоставить их нечем, но исчезать они не должны — снаружи пропажа
        # неотличима от «документа не было».
        unparsed = [
            self._unparsed_result(item)
            for item in getattr(self._saby, "skipped_documents", [])
        ]

        # Считаем до обрезки: иначе отчёт показывал бы обрезанное число как
        # полный размер выборки, и пропущенный хвост был бы невидим.
        total_documents = len(saby_documents) + len(unparsed)
        if request.max_documents is not None:
            saby_documents = saby_documents[: request.max_documents]

        # Ключ — позиция в выборке: идентификаторы Saby не гарантированно уникальны,
        # а позиция уникальна всегда и сохраняет исходный порядок в отчёте.
        results: dict[int, SyncItemResult] = {}
        retry_items: list[tuple[int, SabyDocument]] = []
        max_attempts = max(1, self._settings.sync_max_attempts)

        for key, item in enumerate(saby_documents):
            result = self._process_saby_document(request, item, date_from, date_to, effective_dry_run)
            result.attempt = 1
            results[key] = result
            if self._is_retryable(result) and max_attempts > 1:
                retry_items.append((key, item))

        for attempt in range(2, max_attempts + 1):
            if not retry_items:
                break
            next_retry_items: list[tuple[int, SabyDocument]] = []
            for key, item in retry_items:
                result = self._process_saby_document(
                    request, item, date_from, date_to, effective_dry_run
                )
                result.attempt = attempt
                results[key] = result
                if self._is_retryable(result) and attempt < max_attempts:
                    next_retry_items.append((key, item))
            retry_items = next_retry_items

        outcomes = [*results.values(), *unparsed]
        if self._should_advance_state(request, outcomes, date_to, total_documents):
            self._state.save_last_processed_date(date_to)
        return self._finalize(
            request,
            build_response(total_documents, len(saby_documents) + len(unparsed), outcomes),
        )

    @staticmethod
    def _unparsed_result(skipped) -> SyncItemResult:
        return SyncItemResult(
            status="manual",
            saby_id=skipped.id,
            saby_number=skipped.number,
            saby_date=skipped.date,
            counterparty_name=skipped.counterparty_name,
            message=f"Документ не разобран: {skipped.reason}. Требуется ручная обработка.",
        )

    def _should_advance_state(
        self,
        request: SyncRunRequest,
        results: list[SyncItemResult],
        date_to: date,
        total_documents: int,
    ) -> bool:
        """Можно ли считать период пройденным.

        Правило намеренно строгое: окно умеет только расширяться назад, повторная
        обработка идемпотентна (документ распознаётся как уже заполненный), а
        глубина ограничена SYNC_MAX_LOOKBACK_DAYS. Цена строгости — лишний запрос
        к 1С, цена мягкости — документ, к которому никто больше не вернётся.
        """
        if request.advance_state is False:
            logger.info("Период %s не запоминается: запрошено advance_state=false", date_to)
            return False
        if request.max_documents is not None and request.max_documents < total_documents:
            logger.info(
                "Период %s не запоминается: выборка обрезана max_documents=%s из %s",
                date_to, request.max_documents, total_documents,
            )
            return False
        if date_to > date.today():
            logger.info("Период %s не запоминается: конец периода в будущем", date_to)
            return False
        failed = [item for item in results if item.status == "technical_error"]
        if failed:
            logger.warning(
                "Период %s не запоминается: %s документов завершились технической ошибкой. "
                "Следующий прогон вернётся к этому периоду.",
                date_to, len(failed),
            )
            return False
        return True

    @staticmethod
    def _is_retryable(result: SyncItemResult) -> bool:
        return result.status in RETRYABLE_STATUSES and result.retryable

    def _finalize(self, request: SyncRunRequest, response: SyncRunResponse) -> SyncRunResponse:
        """Единственное место, где отправляется отчёт — чтобы ни одна ветка его не забыла.

        Партия на подтверждение открывается здесь же: код нужен письму, а письмо
        уходит отсюда. Открывать её раньше значило бы выдать код, который может
        не дойти ни до кого.
        """
        response.confirmation_code = self._open_confirmation_batch(response.request_id)
        response.report_sent = self._send_report_if_needed(request, response)
        # Код в ответе API не нужен: он предназначен получателю письма.
        response.confirmation_code = None
        return response

    def _resolve_period(self, request: SyncRunRequest) -> tuple[date, date]:
        window = max(1, self._settings.sync_window_days)
        if request.date_from and request.date_to:
            date_from, date_to = request.date_from, request.date_to
        elif request.date_from:
            date_from, date_to = request.date_from, max(request.date_from, date.today())
        elif request.date_to:
            # Одна лишь конечная дата задаёт окно обычной длины, а не один день.
            date_from, date_to = request.date_to - timedelta(days=window), request.date_to
        else:
            date_from, date_to = default_window(window)
            date_from = self._extend_to_last_processed(date_from)
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        return date_from, date_to

    def _extend_to_last_processed(self, date_from: date) -> date:
        """Подтягивает начало окна к последнему обработанному периоду.

        Только расширяет окно назад и никогда не сдвигает вперёд: пропуск запусков
        не должен оставлять дыру, а лишняя перепроверка безопасна — такие документы
        распознаются как уже заполненные. Глубина ограничена SYNC_MAX_LOOKBACK_DAYS
        и SYNC_START_DATE, чтобы после долгого простоя не поднимать всю базу.
        """
        last_processed = self._state.load_last_processed_date()
        if last_processed is None or last_processed >= date_from:
            return date_from

        oldest_allowed = max(
            date.today() - timedelta(days=max(0, self._settings.sync_max_lookback_days)),
            self._settings.sync_start_date,
        )
        extended = max(last_processed + timedelta(days=1), oldest_allowed)
        if extended < date_from:
            logger.info(
                "Предыдущий прогон закончился на %s — окно расширено с %s до %s.",
                last_processed,
                date_from,
                extended,
            )
            return extended
        return date_from

    def _expense_period(self, date_from: date, date_to: date) -> tuple[date, date]:
        """Поступление доп. расходов может быть проведено и раньше, и позже УПД."""
        widened_from = date_from - timedelta(days=max(0, self._settings.expense_lookback_days))
        widened_to = date_to + timedelta(days=max(0, self._settings.expense_lookahead_days))
        return max(widened_from, self._settings.sync_start_date), widened_to

    def _result_context(
        self,
        saby_doc: SabyDocument,
        expense: AdditionalExpenseItem | None = None,
        order: SupplierOrderItem | None = None,
    ) -> dict:
        warehouse = order.warehouse if order else expense.warehouse if expense else None
        return {
            "saby_id": saby_doc.id,
            "saby_number": saby_doc.incoming_number,
            "saby_date": saby_doc.incoming_date,
            "counterparty_name": saby_doc.counterparty_name or (expense.counterparty.name if expense else None),
            "counterparty_inn": saby_doc.counterparty_inn or (expense.counterparty.inn if expense else None),
            "amount": saby_doc.amount,
            "additional_expense_ref": expense.ref if expense else None,
            "additional_expense_number": expense.number if expense else None,
            "supplier_order_ref": order.ref if order else expense.supplier_order.ref if expense else None,
            "supplier_order_number": order.number if order else expense.supplier_order.number if expense else None,
            "warehouse_name": warehouse.name if warehouse else None,
            "warehouse_group": warehouse.group_name if warehouse else None,
            "additional_expense_type": expense.additional_expense_type if expense else None,
        }

    # --- Обработка одного документа ----------------------------------------

    def _process_saby_document(
        self,
        request: SyncRunRequest,
        saby_doc: SabyDocument,
        date_from: date,
        date_to: date,
        effective_dry_run: bool,
    ) -> SyncItemResult:
        try:
            expenses_from, expenses_to = self._expense_period(date_from, date_to)
            candidates = self._one_s.list_additional_expenses(
                expenses_from,
                expenses_to,
                organization_name=saby_doc.organization_name
                or self._settings.default_organization_name,
            )
            outcome = match_additional_expenses(saby_doc, candidates, self._match_config())

            if outcome.best is None:
                if outcome.ambiguous:
                    self._remember_confirmable(
                        saby_doc, [entry.item for entry in outcome.scored]
                    )
                return SyncItemResult(
                    status="ambiguous" if outcome.ambiguous else "not_found",
                    **self._result_context(saby_doc),
                    message=outcome.explanation,
                    # Спорному документу нужен человек, а выбирать он будет из
                    # кандидатов, а не из фразы с объяснением.
                    candidates=_candidates_of(outcome),
                )

            expense = outcome.best
            lookup = self._one_s.find_supplier_order(expense)
            validation_error = validate_before_write(
                saby_doc, expense, lookup, self._validation_context()
            )
            if validation_error:
                return validation_error

            if effective_dry_run:
                # Партию готовим и здесь: иначе при запрещённой записи форме
                # нечего показывать, и убедиться в её работе до включения
                # записи было бы нечем. Подтверждение в этом режиме честно
                # ответит, что запись запрещена настройкой.
                self._remember_confirmable(saby_doc, [expense])
                reason = (
                    "запись пропущена из-за dry_run."
                    if request.dry_run
                    else "запись пропущена: ONE_S_WRITES_ENABLED=false."
                )
                return SyncItemResult(
                    status="dry_run_ready",
                    **self._result_context(saby_doc, expense, lookup.order),
                    message=f"Проверки пройдены, {reason}",
                    candidates=_candidates_of(outcome),
                )

            if not self._settings.one_s_auto_write_enabled:
                self._remember_confirmable(saby_doc, [expense])
                # Самостоятельных решений программа не принимает: даже когда
                # кандидат единственный и все проверки пройдены, запись делает
                # человек через форму подтверждения.
                return SyncItemResult(
                    status="awaiting_confirmation",
                    **self._result_context(saby_doc, expense, lookup.order),
                    message=(
                        "Проверки пройдены. Запись выполняется только после "
                        "подтверждения сотрудником."
                    ),
                    candidates=_candidates_of(outcome),
                )

            return self._write_incoming_requisites(saby_doc, expense, lookup)
        except HttpClientError as error:
            # Единственный случай, который имеет смысл повторять.
            return SyncItemResult(
                status="technical_error",
                **self._result_context(saby_doc),
                message=f"HTTP-ошибка: {error}",
                retryable=True,
            )
        except RuntimeError as error:
            # Запрет записи и прочие отказы по настройке повтором не лечатся.
            return SyncItemResult(
                status="manual",
                **self._result_context(saby_doc),
                message=f"Документ требует ручной обработки: {error}",
            )
        except Exception as error:
            logger.exception("Не удалось обработать документ Saby %s", saby_doc.id)
            return SyncItemResult(
                status="technical_error",
                **self._result_context(saby_doc),
                message=f"Техническая ошибка: {error}",
            )

    # --- Подтверждение сотрудником -----------------------------------------

    def _remember_confirmable(
        self, saby_doc: SabyDocument, candidates: list[AdditionalExpenseItem]
    ) -> None:
        """Откладывает доменные объекты до сборки партии.

        Ключ — идентификатор документа Saby: он уникален в пределах прогона
        после дедупликации реестра.
        """
        self._confirmable[saby_doc.id] = (saby_doc, candidates)

    def _open_confirmation_batch(self, request_id: str) -> str | None:
        """Складывает отложенное в партию. Возвращает код для письма."""
        if not self._settings.confirm_enabled or not self._confirmable:
            return None

        items = [
            PendingItem(
                key=f"{index:03d}",
                saby_document=document.model_dump(mode="json"),
                candidates=[item.model_dump(mode="json") for item in candidates],
                status="ambiguous" if len(candidates) > 1 else "awaiting_confirmation",
                message="",
            )
            for index, (document, candidates) in enumerate(self._confirmable.values())
        ]
        code, batch = self._confirmations.open_batch(
            items, request_id, self._settings.confirm_code_ttl_hours
        )
        if code is not None:
            self._confirmations.record(
                "код выдан",
                код=hash_code(code),
                позиций=len(items),
                действует_до=batch.expires_at,
                прогон=request_id,
            )
        return code

    def apply_confirmation(
        self, item: PendingItem, chosen_ref: str
    ) -> SyncItemResult:
        """Записывает подтверждённый сотрудником документ.

        Идёт ровно тем же путём, что и прогон: перечитывание, проверки,
        запись, проверка результата. Форма не должна становиться обходом
        предохранителей, поэтому своей логики записи здесь нет.
        """
        saby_doc = item.document()
        expense = item.candidate(chosen_ref)
        if expense is None:
            return SyncItemResult(
                status="business_error",
                **self._result_context(saby_doc),
                message="Выбранный документ 1С не относится к этому предложению.",
            )

        if not self._settings.one_s_writes_enabled:
            return SyncItemResult(
                status="manual",
                **self._result_context(saby_doc, expense),
                message="Запись в 1С запрещена настройкой ONE_S_WRITES_ENABLED.",
            )

        try:
            lookup = self._one_s.find_supplier_order(expense)
            return self._write_incoming_requisites(saby_doc, expense, lookup)
        except HttpClientError as error:
            return SyncItemResult(
                status="technical_error",
                **self._result_context(saby_doc, expense),
                message=f"HTTP-ошибка: {error}",
                retryable=True,
            )

    # --- Запись -------------------------------------------------------------

    def _write_incoming_requisites(
        self,
        saby_doc: SabyDocument,
        expense: AdditionalExpenseItem,
        lookup: OrderLookup,
    ) -> SyncItemResult:
        """Перечитать → сверить → записать → подтвердить чтением.

        Полей оптимистической блокировки обмен не принимает, поэтому документ
        перечитывается непосредственно перед POST: это сокращает окно гонки до
        секунд, хотя и не закрывает его полностью.
        """
        fresh = self._one_s.reload_additional_expense(expense)
        if fresh is None:
            return SyncItemResult(
                status="technical_error",
                **self._result_context(saby_doc, expense, lookup.order),
                message="Документ 1С не найден при перечитывании перед записью.",
                retryable=True,
            )

        guard = validate_before_write(saby_doc, fresh, lookup, self._validation_context())
        if guard is not None:
            return guard

        outcome = self._one_s.write_incoming_requisites(
            fresh, saby_doc.incoming_number, saby_doc.incoming_date
        )
        status = {
            "written": "written",
            "conflict": "conflict",
            # Результат неизвестен: повторять запись вслепую нельзя.
            "unknown": "technical_error",
        }[outcome.status]
        return SyncItemResult(
            status=status,
            **self._result_context(saby_doc, fresh, lookup.order),
            message=outcome.message,
        )

    def _send_report_if_needed(self, request: SyncRunRequest, response: SyncRunResponse) -> bool:
        if not request.send_report:
            # Файл отчёта нужен и без отправки письма, иначе результат прогона
            # не остаётся нигде.
            try:
                self._reporting.save(response)
            except Exception:
                logger.exception("Не удалось сохранить файл отчёта")
            return False
        try:
            delivery = self._reporting.send(response)
        except Exception:
            # Без записи в лог невозможно отличить «SMTP не настроен» от «упал коннект».
            logger.exception("Не удалось отправить отчёт синхронизации")
            return False
        response.report_path = str(delivery.path) if delivery.path else None
        response.report_reason = delivery.reason
        if not delivery.sent:
            logger.warning("Отчёт не доставлен: %s", delivery.reason)
        return delivery.sent
