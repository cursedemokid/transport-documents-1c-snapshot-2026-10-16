import unittest
from datetime import date, datetime

from app.utils.dates import normalize_stored_date
from app.utils.normalization import normalize_party_name, party_name_similarity, same_doc_number
from tests.support.no_network import NetworkGuardTestCase


class PartyNameTests(NetworkGuardTestCase):
    def test_strips_service_note_and_legal_form(self):
        """Реальный случай: в 1С к наименованию дописана пометка о сверке."""
        self.assertEqual(normalize_party_name("ВЕКТОР ООО (сверен на 31.12.2025)"), "вектор")
        self.assertEqual(normalize_party_name('ООО «Вектор»'), "вектор")

    def test_dirty_and_clean_names_match_exactly(self):
        similarity = party_name_similarity(
            "ВЕКТОР ООО (сверен на 31.12.2025)",
            'ООО «Вектор»',
        )

        self.assertEqual(similarity, 1.0)

    def test_different_counterparties_score_low(self):
        similarity = party_name_similarity("ВЕКТОР ООО", "Дельта логистик ООО")

        self.assertLess(similarity, 0.85)

    def test_multiword_name_survives_punctuation(self):
        self.assertEqual(
            normalize_party_name("Дельта логистик ООО"),
            normalize_party_name('ООО «Дельта логистик»'),
        )

    def test_empty_names_score_zero(self):
        self.assertEqual(party_name_similarity(None, "ВЕКТОР"), 0.0)
        self.assertEqual(party_name_similarity("ВЕКТОР", None), 0.0)


class StoredDateTests(NetworkGuardTestCase):
    def test_sentinel_is_none(self):
        self.assertIsNone(normalize_stored_date(date(1, 1, 1)))
        self.assertIsNone(normalize_stored_date(datetime(1, 1, 1, 0, 0, 0)))

    def test_real_date_passes_through(self):
        self.assertEqual(normalize_stored_date(date(2026, 8, 6)), date(2026, 8, 6))
        self.assertEqual(normalize_stored_date(datetime(2026, 8, 6, 23, 59, 59)), date(2026, 8, 6))

    def test_none_stays_none(self):
        self.assertIsNone(normalize_stored_date(None))



class SameDocNumberTests(NetworkGuardTestCase):
    """Saby набивает номер нулями, 1С — нет.

    На бэкфилле за восемь месяцев это давало семь ложных конфликтов из девяти:
    программа сообщала «в 1С другой входящий документ» про документ, заведённый
    ровно из этого УПД. Конфликт — стоп-условие при раскатке.
    """

    def test_leading_zeros_are_ignored_for_numeric_numbers(self):
        self.assertTrue(same_doc_number("0004100200", "4100200"))
        self.assertTrue(same_doc_number("4100200", "0004100200"))
        self.assertTrue(same_doc_number("00010000", "10000"))

    def test_different_numbers_stay_different(self):
        self.assertFalse(same_doc_number("2200111", "3300111"))
        self.assertFalse(same_doc_number("4582541781", "4100201"))

    def test_prefix_is_never_stripped(self):
        """У «УТ000001908» ведущая часть значима — трогать её нельзя."""
        self.assertFalse(same_doc_number("УТ000001908", "УТ1908"))
        self.assertTrue(same_doc_number("УТ000001908", "ут000001908"))

    def test_padding_is_stripped_only_from_purely_numeric_numbers(self):
        """Набивка нулями — особенность числовых номеров Saby.

        У буквенно-цифрового номера ведущие нули могут быть частью самого
        номера, и срезать их — значит склеить два разных документа.
        """
        self.assertFalse(same_doc_number("0012АБ", "12АБ"))
        self.assertFalse(same_doc_number("00A1", "A1"))

    def test_spacing_and_dashes_still_ignored(self):
        self.assertTrue(same_doc_number(" 0004100200 ", "4100-200"))

    def test_empty_never_matches(self):
        for left, right in ((None, "1"), ("1", None), ("", "1"), ("   ", "1")):
            with self.subTest(left=left, right=right):
                self.assertFalse(same_doc_number(left, right))

    def test_all_zeros_do_not_collapse_to_empty(self):
        self.assertTrue(same_doc_number("000", "0"))


if __name__ == "__main__":
    unittest.main()
