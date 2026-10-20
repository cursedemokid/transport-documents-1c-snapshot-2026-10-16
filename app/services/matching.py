"""Сопоставление входящих документов Saby с поступлениями доп. расходов 1С.

Обмен с 1С не отдаёт ИНН, а наименования контрагентов там «грязные», поэтому
главный риск — не ложные совпадения, а ложные отказы. Отсюда устройство: сумма и
вид расхода отсекают заведомо чужое, а решение принимается по сумме сигналов.

Сильного сигнала при этом НЕ требуется, и это осознанный компромисс. Оба сильных
признака в живых данных недоступны: номер УПД в реквизитах 1С — ровно то, что мы
вписываем, а номер заказа поставщику Saby не отдаёт ни у одного документа. При
пороге 60 на эталоне из 46 пар не находилось ничего; при рабочем
`MATCH_MIN_SCORE=40` проходит связка контрагент (30) плюс организация (10).
Замеры и таблица порогов — в READINESS.md. От ошибки защищает не порог, а запас
`MATCH_SCORE_MARGIN`: спорные случаи уходят в «неоднозначно», а не в неверную
запись.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from app.schemas.one_s import AdditionalExpenseItem
from app.schemas.saby import SabyDocument
from app.utils.normalization import (
    clean_text,
    money_equal,
    normalize_doc_number,
    party_name_similarity,
    same_doc_number,
)

#: Сильный сигнал: сам по себе достаточен, чтобы признать кандидата.
STRONG_SIGNAL_SCORE = 60


@dataclass(frozen=True)
class MatchConfig:
    amount_tolerance: Decimal
    allowed_expense_types: frozenset[str]
    date_window_days: int = 45
    min_score: int = STRONG_SIGNAL_SCORE
    score_margin: int = 20
    name_similarity: float = 0.85


@dataclass(frozen=True)
class ScoredCandidate:
    item: AdditionalExpenseItem
    score: int
    signals: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.item.number} (счёт {self.score}: {', '.join(self.signals) or 'нет сигналов'})"


@dataclass(frozen=True)
class MatchOutcome:
    best: AdditionalExpenseItem | None = None
    scored: tuple[ScoredCandidate, ...] = ()
    hard_passed: int = 0
    total_examined: int = 0
    explanation: str = ""
    ambiguous: bool = False


def _document_date(item: AdditionalExpenseItem) -> date | None:
    if isinstance(item.date, datetime):
        return item.date.date()
    return item.date


def _passes_hard_filters(
    saby_doc: SabyDocument, candidate: AdditionalExpenseItem, config: MatchConfig
) -> bool:
    if candidate.deletion_mark:
        return False
    # Вид расхода проверяется ТОЛЬКО по стороне 1С: в документе Saby это поле
    # заполняется типом документа СБИС и к видам доп. расходов отношения не имеет.
    # Пустой набор — осознанное «берём все виды», а не «не берём ни одного».
    if (
        config.allowed_expense_types
        and clean_text(candidate.additional_expense_type) not in config.allowed_expense_types
    ):
        return False
    if not money_equal(saby_doc.amount, candidate.amount, config.amount_tolerance):
        return False
    candidate_date = _document_date(candidate)
    if candidate_date is not None:
        if abs((candidate_date - saby_doc.incoming_date).days) > config.date_window_days:
            return False
    return True


def _score(
    saby_doc: SabyDocument, candidate: AdditionalExpenseItem, config: MatchConfig
) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []
    incoming = normalize_doc_number(saby_doc.incoming_number)

    # Набивка нулями у Saby и её отсутствие в 1С — один и тот же номер.
    if same_doc_number(candidate.incoming_requisites.number, saby_doc.incoming_number):
        score += 100
        signals.append("номер УПД в реквизитах 1С")
    if incoming and incoming in normalize_doc_number(candidate.comment):
        score += 60
        signals.append("номер УПД в комментарии")

    order_number = normalize_doc_number(saby_doc.supplier_order_number)
    candidate_order = normalize_doc_number(candidate.supplier_order.number)
    if order_number and candidate_order:
        if same_doc_number(saby_doc.supplier_order_number, candidate.supplier_order.number):
            score += 60
            signals.append("номер заказа поставщику")
        elif saby_doc.supplier_order_source == "text":
            # Номер выдран регуляркой из свободного комментария, а в тот же шаблон
            # попадают номера счетов-фактур, контейнеров и договоров. Несовпадение
            # такого номера ничего не доказывает, а штраф снял бы верного кандидата.
            signals.append("номер заказа из текста не совпал — не учитываем")
        else:
            # Заказ разобран из реквизита и явно другой — это дисквалификация.
            score -= 100
            signals.append("номер заказа не совпадает")

    if party_name_similarity(saby_doc.counterparty_name, candidate.counterparty.name) >= config.name_similarity:
        score += 30
        signals.append("контрагент")
    if incoming and incoming in normalize_doc_number(candidate.number):
        score += 15
        signals.append("номер УПД в номере документа 1С")
    if party_name_similarity(saby_doc.organization_name, candidate.organization.name) >= config.name_similarity:
        score += 10
        signals.append("организация")

    candidate_date = _document_date(candidate)
    if candidate_date is not None and abs((candidate_date - saby_doc.incoming_date).days) <= 7:
        score += 10
        signals.append("близкая дата")

    return score, signals


def match_additional_expenses(
    saby_doc: SabyDocument,
    candidates: Iterable[AdditionalExpenseItem],
    config: MatchConfig,
) -> MatchOutcome:
    total = 0
    hard_passed = 0
    scored: list[ScoredCandidate] = []

    for candidate in candidates:
        total += 1
        if not _passes_hard_filters(saby_doc, candidate, config):
            continue
        hard_passed += 1
        score, signals = _score(saby_doc, candidate, config)
        if score >= config.min_score:
            scored.append(ScoredCandidate(item=candidate, score=score, signals=tuple(signals)))

    scored.sort(key=lambda entry: (-entry.score, entry.item.number or ""))

    if not scored:
        return MatchOutcome(
            scored=(),
            hard_passed=hard_passed,
            total_examined=total,
            explanation=(
                f"Просмотрено {total} поступлений доп. расходов, по сумме и виду расхода "
                f"подошло {hard_passed}, но ни у одного нет номера УПД "
                f"{saby_doc.incoming_number!r} или совпадающего номера заказа."
            ),
        )

    if len(scored) == 1 or scored[0].score - scored[1].score >= config.score_margin:
        best = scored[0]
        return MatchOutcome(
            best=best.item,
            scored=tuple(scored),
            hard_passed=hard_passed,
            total_examined=total,
            explanation=f"Выбран {best.describe()}",
        )

    # Именно те, из-за кого выбор невозможен: лидер и все, кто отстал от него
    # меньше чем на запас. Равенства счёта тут нет и не требуется — раньше в
    # текст попадали только равные, и при счетах 50 и 40 отчёт называл одного
    # кандидата под заголовком про равный вес, а конкурент не упоминался вовсе.
    close = [entry for entry in scored if scored[0].score - entry.score < config.score_margin]
    return MatchOutcome(
        scored=tuple(scored),
        hard_passed=hard_passed,
        total_examined=total,
        ambiguous=True,
        explanation=(
            "Кандидаты слишком близки по весу, выбрать автоматически нельзя: "
            + "; ".join(entry.describe() for entry in close)
        ),
    )
