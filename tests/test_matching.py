import unittest
from datetime import date, datetime
from decimal import Decimal

from app.services.matching import MatchConfig, match_additional_expenses
from app.utils.normalization import clean_text
from tests.support.fakes import expense, saby_doc
from tests.support.no_network import NetworkGuardTestCase

ALLOWED = frozenset({clean_text("Фрахт"), clean_text("ЖД РФ")})


def _config(**overrides) -> MatchConfig:
    params = {
        "amount_tolerance": Decimal("0.01"),
        "allowed_expense_types": ALLOWED,
    }
    params.update(overrides)
    return MatchConfig(**params)


class HardFilterTests(NetworkGuardTestCase):
    def test_amount_beyond_tolerance_is_rejected(self):
        outcome = match_additional_expenses(saby_doc(), [expense(amount="100.02")], _config())

        self.assertIsNone(outcome.best)
        self.assertEqual(outcome.hard_passed, 0)

    def test_amount_within_tolerance_passes(self):
        outcome = match_additional_expenses(saby_doc(), [expense(amount="100.01")], _config())

        self.assertIsNotNone(outcome.best)

    def test_expense_type_outside_allowed_is_rejected(self):
        outcome = match_additional_expenses(
            saby_doc(), [expense(expense_type="Автовывоз")], _config()
        )

        self.assertEqual(outcome.hard_passed, 0)

    def test_saby_document_type_does_not_affect_matching(self):
        """Регрессия: в документе Saby это поле — тип документа СБИС, а не вид
        доп. расхода, и раньше оно отбрасывало все корректные кандидаты."""
        document = saby_doc(additional_expense_type="Счет-фактура")

        outcome = match_additional_expenses(document, [expense(expense_type="Фрахт")], _config())

        self.assertIsNotNone(outcome.best)

    def test_deleted_candidate_is_rejected(self):
        outcome = match_additional_expenses(saby_doc(), [expense(deletion_mark=True)], _config())

        self.assertEqual(outcome.hard_passed, 0)

    def test_candidate_far_in_time_is_rejected(self):
        far = expense(document_date=datetime(2026, 12, 1, 0, 0, 0))

        outcome = match_additional_expenses(saby_doc(), [far], _config())

        self.assertEqual(outcome.hard_passed, 0)


class SignalTests(NetworkGuardTestCase):
    def test_incoming_number_in_comment_is_enough(self):
        outcome = match_additional_expenses(saby_doc(), [expense()], _config())

        self.assertIsNotNone(outcome.best)
        self.assertIn("номер УПД в комментарии", outcome.scored[0].signals)

    def test_amount_alone_is_not_enough(self):
        """Сумма — необходимое условие, но никогда не достаточное."""
        weak = expense(comment=None, order_number=None, counterparty_name="Совсем другой контрагент")

        outcome = match_additional_expenses(saby_doc(order_number=None), [weak], _config())

        self.assertIsNone(outcome.best)
        self.assertEqual(outcome.hard_passed, 1)
        self.assertIn("Просмотрено", outcome.explanation)

    def test_amount_and_counterparty_are_not_enough(self):
        weak = expense(comment=None, order_number=None)

        outcome = match_additional_expenses(saby_doc(order_number=None), [weak], _config())

        self.assertIsNone(outcome.best)

    def test_dirty_counterparty_name_still_matches(self):
        """«ВЕКТОР ООО (сверен на 31.12.2025)» и «ООО «Пример»» — один контрагент."""
        document = saby_doc(counterparty_name='ООО «Пример»')

        outcome = match_additional_expenses(document, [expense()], _config())

        self.assertIn("контрагент", outcome.scored[0].signals)

    def test_different_order_number_disqualifies(self):
        other = expense(order_number="ТД000009999")

        outcome = match_additional_expenses(saby_doc(), [other], _config())

        self.assertIsNone(outcome.best)

    def test_order_number_guessed_from_text_does_not_disqualify(self):
        r"""Регрессия: номер заказа, выдранный регуляркой из комментария.

        В шаблон `[А-Я]{1,4}\d{6,12}` попадают счета-фактуры, номера контейнеров
        и договоров. Такой «номер заказа» почти никогда не совпадёт с настоящим, а
        штраф −100 снимал верного кандидата — документ уходил в `not_found`, и по
        отчёту это читалось как слабое сопоставление.
        """
        document = saby_doc(order_number="СФ000999999", supplier_order_source="text")

        outcome = match_additional_expenses(document, [expense()], _config())

        self.assertIsNotNone(outcome.best)
        self.assertNotIn("номер заказа не совпадает", outcome.scored[0].signals)

    def test_matching_order_number_from_text_still_scores(self):
        """Совпадение случайным не бывает — засчитываем независимо от источника."""
        document = saby_doc(supplier_order_source="text")

        outcome = match_additional_expenses(document, [expense(comment=None)], _config())

        self.assertIn("номер заказа поставщику", outcome.scored[0].signals)

    def test_order_number_from_field_still_disqualifies(self):
        document = saby_doc(order_number="ТД000009999", supplier_order_source="field")

        outcome = match_additional_expenses(document, [expense()], _config())

        self.assertIsNone(outcome.best)

    def test_missing_order_number_on_candidate_is_not_fatal(self):
        """Пустой заказ у кандидата — не повод его выбрасывать, если есть номер УПД."""
        outcome = match_additional_expenses(saby_doc(), [expense(order_number=None)], _config())

        self.assertIsNotNone(outcome.best)


class RankingTests(NetworkGuardTestCase):
    def test_stronger_candidate_wins_by_margin(self):
        strong = expense(ref="uuid-strong", number="ТД000000001")
        weak = expense(
            ref="uuid-weak",
            number="ТД000000002",
            comment=None,
            incoming_requisites=None,
        )

        outcome = match_additional_expenses(saby_doc(), [weak, strong], _config())

        self.assertIsNotNone(outcome.best)
        self.assertEqual(outcome.best.ref, "uuid-strong")

    def test_equal_candidates_are_ambiguous(self):
        first = expense(ref="uuid-1", number="ТД000000001")
        second = expense(ref="uuid-2", number="ТД000000002")

        outcome = match_additional_expenses(saby_doc(), [first, second], _config())

        self.assertIsNone(outcome.best)
        self.assertTrue(outcome.ambiguous)
        self.assertIn("слишком близки по весу", outcome.explanation)

    def test_explanation_names_every_close_candidate(self):
        """Неоднозначность — это разрыв меньше запаса, а не равенство счетов.

        В текст попадали только равные по счёту, поэтому при счетах 50 и 40
        отчёт называл одного кандидата — а конкурент, из-за которого документ и
        не записан, не упоминался вовсе.
        """
        leader = expense(ref="uuid-leader", number="ТД000000001",
                         document_date=datetime(2026, 8, 17, 12, 0, 0))
        # Дата дальше семи дней: минус сигнал «близкая дата», отставание на 10.
        trailing = expense(ref="uuid-trailing", number="ТД000000002",
                           document_date=datetime(2026, 7, 10, 12, 0, 0))

        outcome = match_additional_expenses(saby_doc(), [leader, trailing], _config())

        self.assertTrue(outcome.ambiguous)
        scores = {entry.item.number: entry.score for entry in outcome.scored}
        self.assertNotEqual(*scores.values(), "счета должны отличаться, иначе тест ни о чём")
        self.assertIn("ТД000000001", outcome.explanation)
        self.assertIn("ТД000000002", outcome.explanation)

    def test_empty_expense_type_list_means_every_type(self):
        """Пустой список — «все виды», а не «ни одного».

        Через `or default` выключить фильтр было нечем: пустое значение молча
        возвращало умолчание. А пустой набор в самой проверке отсекал бы вообще
        всех кандидатов — то есть выключение фильтра ломало бы сопоставление.
        """
        exotic = expense(expense_type="Портовые сборы")
        without_filter = _config(allowed_expense_types=frozenset())

        self.assertIsNotNone(
            match_additional_expenses(saby_doc(), [exotic], without_filter).best
        )
        # А со списком тот же кандидат по-прежнему отсекается.
        self.assertIsNone(
            match_additional_expenses(saby_doc(), [exotic], _config()).best
        )

    def test_explanation_is_actionable_when_nothing_found(self):
        outcome = match_additional_expenses(saby_doc(), [], _config())

        self.assertIsNone(outcome.best)
        self.assertFalse(outcome.ambiguous)
        self.assertIn("4726385", outcome.explanation)


if __name__ == "__main__":
    unittest.main()
