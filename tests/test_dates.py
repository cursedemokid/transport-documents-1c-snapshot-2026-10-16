"""Разбор дат.

`parse_date` принимает всё, что приходит и от Saby, и от API: строку, дату,
дату-время. Ошибка здесь тихо сдвинула бы окно выборки.
"""

import unittest
from datetime import date, datetime

from app.utils.dates import default_window, normalize_stored_date, parse_date
from tests.support.no_network import NetworkGuardTestCase


class ParseDateTests(NetworkGuardTestCase):
    def test_accepts_the_shapes_that_actually_arrive(self):
        cases = {
            "2026-08-17": date(2026, 8, 17),
            "2026-08-17T12:30:00": date(2026, 8, 17),
            "2026-08-17 12:30:00+03:00": date(2026, 8, 17),
            date(2026, 8, 17): date(2026, 8, 17),
            datetime(2026, 8, 17, 23, 59, 59): date(2026, 8, 17),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, parse_date(value))

    def test_empty_values_are_none(self):
        for value in (None, "", 0, [], object()):
            with self.subTest(value=value):
                self.assertIsNone(parse_date(value))

    def test_unparsable_string_raises(self):
        """Молчаливый None здесь означал бы документ без даты в выборке."""
        with self.assertRaises(ValueError):
            parse_date("вчера")


class StoredDateTests(NetworkGuardTestCase):
    def test_1c_placeholder_is_empty(self):
        """01.01.0001 в реквизитах превратил бы пустой документ в вечный конфликт."""
        self.assertIsNone(normalize_stored_date(date(1, 1, 1)))
        self.assertIsNone(normalize_stored_date(datetime(1, 1, 1, 0, 0, 0)))

    def test_real_dates_survive(self):
        self.assertEqual(date(2026, 8, 17), normalize_stored_date(date(2026, 8, 17)))
        self.assertEqual(date(2026, 8, 17), normalize_stored_date(datetime(2026, 8, 17, 12, 0)))

    def test_non_dates_are_none(self):
        for value in (None, "2026-08-17", 17):
            with self.subTest(value=value):
                self.assertIsNone(normalize_stored_date(value))


class WindowTests(NetworkGuardTestCase):
    def test_window_ends_today_and_spans_the_requested_days(self):
        start, end = default_window(14)

        self.assertEqual(date.today(), end)
        self.assertEqual(14, (end - start).days)


if __name__ == "__main__":
    unittest.main()
