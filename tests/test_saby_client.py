import json
import socket
import tempfile
import unittest
from pathlib import Path
from datetime import date
from decimal import Decimal

from app.clients.saby import (
    INCOMING_EVENTS_METHOD,
    SabyClient,
    build_incoming_events_payload,
)
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import default_settings


class SabyClientTests(NetworkGuardTestCase):
    def test_build_incoming_events_payload_uses_method_dates_and_limit(self):
        payload = build_incoming_events_payload(
            limit=21,
            date_from=date(2026, 8, 17),
            date_to=date(2026, 8, 18),
        )

        self.assertEqual(INCOMING_EVENTS_METHOD, payload["method"])
        self.assertEqual(7, payload["protocol"])
        self.assertEqual(21, payload["params"]["Навигация"]["d"][2])
        self.assertEqual("18.08.2026", payload["params"]["Фильтр"]["d"][11])
        self.assertEqual("17.08.2026", payload["params"]["Фильтр"]["d"][12])

    def test_library_client_maps_record_events_and_paginates(self):
        manager = _FakeSabyManager(
            [
                {
                    "result": {
                        "События": [
                            _event(
                                {
                                    "@Событие": 1,
                                    "Номер": "4726385",
                                    "Дата": "17.08.2026",
                                    "Сумма": "100.00",
                                    "Контрагент": _record(
                                        {
                                            "Название": "Mediterranean",
                                            "ИНН": "5228245906",
                                            "КПП": "222222222",
                                        }
                                    ),
                                    "НашаОрганизация": _record(
                                        {
                                            "Название": 'ООО «Пример»',
                                            "ИНН": "3703528406",
                                        }
                                    ),
                                    "Комментарий": "УПД 4726385 заказ ТД000001405",
                                }
                            )
                        ],
                        "Навигация": _record(
                            {
                                "HasMore": True,
                                "Position": _record(
                                    {
                                        "@Событие": 1,
                                        "StartPosition": "1786689636.276610",
                                    }
                                ),
                            }
                        ),
                    }
                },
                {
                    "result": {
                        "События": [
                            {
                                "@Событие": 2,
                                "Номер": "4726386",
                                "Дата": "18.08.2026",
                                "Сумма": "200.00",
                                "ТипДокумента.ТипДокумента": WANTED_TYPE,
                            }
                        ],
                        "Навигация": {"HasMore": False},
                    }
                },
            ]
        )
        client = SabyClient(_settings(saby_page_size=1), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(["4726385", "4726386"], [item.incoming_number for item in documents])
        self.assertEqual(date(2026, 8, 17), documents[0].incoming_date)
        self.assertEqual(Decimal("100.00"), documents[0].amount)
        self.assertEqual("5228245906", documents[0].counterparty_inn)
        self.assertEqual("ТД000001405", documents[0].supplier_order_number)
        self.assertEqual(2, len(manager.calls))
        self.assertEqual("1786689636.276610", manager.calls[1][1]["Навигация"]["d"][3]["d"][1])

    def test_library_client_sends_full_payload_and_refreshes_session_after_unauthorized(self):
        manager = _FakeSabyManager(
            [
                (401, {"error": {"message": "unauthorized"}}),
                {
                    "result": {
                        "items": [
                            {
                                "id": "saby-1",
                                "incoming_number": "4726385",
                                "incoming_date": "17.08.2026",
                                "amount": "100.00",
                                "ТипДокумента.ТипДокумента": WANTED_TYPE,
                            }
                        ],
                        "navigation": {"HasMore": False},
                    }
                },
            ]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(["4726385"], [item.incoming_number for item in documents])
        self.assertEqual(2, len(manager.raw_calls))
        self.assertEqual(1, manager.auth_calls)
        self.assertEqual("https://online.sbis.ru/service/", manager.raw_calls[0][0])
        self.assertEqual(7, manager.raw_calls[0][1]["protocol"])
        self.assertEqual(INCOMING_EVENTS_METHOD, manager.raw_calls[0][1]["method"])
        self.assertEqual("session-1", manager.raw_calls[0][2]["X-SBISSessionID"])
        self.assertEqual("session-2", manager.raw_calls[1][2]["X-SBISSessionID"])


class SabyPeriodTests(NetworkGuardTestCase):
    """Реестр СБИС фильтр по датам игнорирует, поэтому период отсекается у нас."""

    def _page(self, events, cursor=None):
        page = {"result": {"События": events}}
        if cursor is not None:
            # Реальная Saby отдаёт курсор списком [StartPosition, @Событие];
            # на каждой странице он свой, иначе сработает защита от зацикливания.
            page["result"]["Навигация"] = _record(
                {"HasMore": True, "Position": [f"1788182532.{cursor}", cursor]}
            )
        return page

    def _event(self, number, day):
        return _event(  # noqa: F821 — модульная функция, не метод
            {
                "@Событие": number,
                "Номер": number,
                "Дата": day,
                "ДокСумма": "100.00",
                "Лицо1.Название": "Контрагент",
            }
        )

    def test_documents_outside_period_are_dropped(self):
        manager = _FakeSabyManager(
            [self._page([self._event("внутри", "10.09.2026"), self._event("снаружи", "01.08.2026")])]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 9, 8), date(2026, 9, 15))

        self.assertEqual(["внутри"], [d.incoming_number for d in documents])

    def test_boundary_dates_are_kept(self):
        manager = _FakeSabyManager(
            [self._page([self._event("с", "08.09.2026"), self._event("по", "15.09.2026")])]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 9, 8), date(2026, 9, 15))

        self.assertEqual({"с", "по"}, {d.incoming_number for d in documents})

    def test_pagination_is_bounded(self):
        """Без предела выборка листала бы реестр целиком и прогон не заканчивался."""
        pages = [self._page([self._event(f"n{i}", "10.09.2026")], cursor=i) for i in range(20)]
        manager = _FakeSabyManager(pages)
        client = SabyClient(_settings(saby_max_pages=3), manager=manager)

        documents = client.list_incoming_documents(date(2026, 9, 8), date(2026, 9, 15))

        self.assertEqual(3, len(manager.raw_calls))
        self.assertEqual(3, len(documents))


class SabyAmountTests(NetworkGuardTestCase):
    """Разбор сумм реестра.

    `Decimal("1 234,56")` бросает `InvalidOperation`, а она наследуется от
    `ArithmeticError`, не от `ValueError`. Одна такая сумма раньше выносила весь
    прогон в единственную техническую ошибку.
    """

    def _amount(self, raw_amount):
        manager = _FakeSabyManager(
            [
                {
                    "result": {
                        "События": [
                            _event(
                                {
                                    "@Событие": 1,
                                    "Номер": "4726385",
                                    "Дата": "17.08.2026",
                                    "Сумма": raw_amount,
                                }
                            )
                        ],
                        "Навигация": {"HasMore": False},
                    }
                }
            ]
        )
        client = SabyClient(_settings(), manager=manager)
        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))
        return documents, client

    def test_amount_formats_are_parsed(self):
        cases = {
            "100.00": Decimal("100.00"),
            "1 191,06": Decimal("1191.06"),
            "1\u00a0191,06": Decimal("1191.06"),  # неразрывный пробел — так отдаёт 1С
            "1 234 567,89": Decimal("1234567.89"),
            "-500,50": Decimal("-500.50"),
            "100": Decimal("100"),
            1191.06: Decimal("1191.06"),
        }
        for raw_amount, expected in cases.items():
            with self.subTest(amount=raw_amount):
                documents, _ = self._amount(raw_amount)

                self.assertEqual(1, len(documents))
                self.assertEqual(expected, documents[0].amount)

    def test_unparsable_amount_skips_only_its_event(self):
        documents, client = self._amount("договорная")

        self.assertEqual([], documents)
        self.assertEqual(1, client.skipped_events)

    def test_broken_event_does_not_drop_the_rest_of_the_page(self):
        manager = _FakeSabyManager(
            [
                {
                    "result": {
                        "События": [
                            _event({"@Событие": 1, "Номер": "битый", "Дата": "17.08.2026"}),
                            _event({"@Событие": 2, "Номер": "целый", "Дата": "17.08.2026", "Сумма": "100.00"}),
                            _event({"@Событие": 3, "Номер": "без даты", "Сумма": "100.00"}),
                        ],
                        "Навигация": {"HasMore": False},
                    }
                }
            ]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(["целый"], [item.incoming_number for item in documents])
        self.assertEqual(2, client.skipped_events)

    def test_skipped_counter_resets_between_runs(self):
        """Счётчик попадает в отчёт: накопленный с прошлого прогона — ложь."""
        _, client = self._amount("договорная")
        client._manager._responses = [{"result": {"События": [], "Навигация": {"HasMore": False}}}]

        client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(0, client.skipped_events)


class RegistryTypeFilterTests(NetworkGuardTestCase):
    """В реестре лежат не только УПД.

    Замер на живом реестре: 2739 событий, из них нашего типа 1653. Остальные —
    договоры, акты сверки, корректировки, счета, транспортные накладные — суммы
    в событии не имеют по природе и раньше попадали в счётчик «не удалось
    разобрать», то есть счётчик в отчёте просто врал.
    """

    def _run(self, events, **overrides):
        manager = _FakeSabyManager(
            [{"result": {"События": events, "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(**overrides), manager=manager)
        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))
        return documents, client

    def test_foreign_types_are_dropped_without_counting_as_failures(self):
        events = [
            _event({"@Событие": 1, "Номер": "упд", "Дата": "17.08.2026", "Сумма": "100.00"}),
            _record({"@Событие": 2, "Номер": "договор", "Дата": "17.08.2026",
                     "ТипДокумента.ТипДокумента": "ДоговорДок"}),
            _record({"@Событие": 3, "Номер": "акт", "Дата": "17.08.2026",
                     "ТипДокумента.ТипДокумента": "АктСверкиДок"}),
        ]

        documents, client = self._run(events)

        self.assertEqual(["упд"], [item.incoming_number for item in documents])
        self.assertEqual(0, client.skipped_events)
        self.assertEqual(2, client.wrong_type_events)

    def test_empty_setting_disables_the_filter(self):
        events = [
            _record({"@Событие": 1, "Номер": "чужой", "Дата": "17.08.2026", "Сумма": "1",
                     "ТипДокумента.ТипДокумента": "ДоговорДок"}),
        ]

        documents, client = self._run(events, saby_document_types=frozenset())

        self.assertEqual(["чужой"], [item.incoming_number for item in documents])
        self.assertEqual(0, client.wrong_type_events)


class SkippedDocumentTests(NetworkGuardTestCase):
    """Документ нашего типа без суммы должен быть виден, а не исчезать.

    На живых данных это 117 из 1653 входящих УПД: сканы, у которых
    машиночитаемой суммы нет ни в событии, ни в самом документе.
    """

    def _run(self, events, date_from=date(2026, 8, 17), date_to=date(2026, 8, 18)):
        manager = _FakeSabyManager(
            [{"result": {"События": events, "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(), manager=manager)
        client.list_incoming_documents(date_from, date_to)
        return client

    def test_unparsed_document_is_remembered_with_its_identity(self):
        client = self._run(
            [
                _event(
                    {
                        "@Событие": 77,
                        "Номер": 500100,
                        "Дата": "17.08.2026",
                        "Лицо1.Название": "Вектор, ООО",
                    }
                )
            ]
        )

        self.assertEqual(1, len(client.skipped_documents))
        skipped = client.skipped_documents[0]
        # Реестр отдаёт идентификатор и номер числами — на живом прогоне это
        # роняло сборку строки отчёта.
        self.assertEqual("77", skipped.id)
        self.assertEqual("500100", skipped.number)
        self.assertEqual(date(2026, 8, 17), skipped.date)
        self.assertEqual("Вектор, ООО", skipped.counterparty_name)
        self.assertIn("суммы", skipped.reason)

    def test_unparsed_document_outside_the_period_is_not_reported(self):
        """Реестр отдаёт полтора месяца на запрос за неделю — чужое в отчёт не идёт."""
        client = self._run(
            [_event({"@Событие": 1, "Номер": "старый", "Дата": "01.01.2026"})]
        )

        self.assertEqual(1, client.skipped_events)
        self.assertEqual([], client.skipped_documents)

    def test_truncated_registry_is_flagged(self):
        """Реестр отсортирован по времени события: обрезка — это дыра в выборке."""
        pages = [
            {
                "result": {
                    "События": [_event({"@Событие": i, "Номер": f"n{i}",
                                        "Дата": "17.08.2026", "Сумма": "1"})],
                    "Навигация": _record(
                        {"HasMore": True, "Position": [f"178818253{i}.0", i]}
                    ),
                }
            }
            for i in range(5)
        ]
        manager = _FakeSabyManager(pages)
        client = SabyClient(_settings(saby_max_pages=2), manager=manager)

        client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertTrue(client.registry_truncated)

    def test_complete_registry_is_not_flagged(self):
        client = self._run(
            [_event({"@Событие": 1, "Номер": "1", "Дата": "17.08.2026", "Сумма": "1"})]
        )

        self.assertFalse(client.registry_truncated)


class SocketTimeoutTests(NetworkGuardTestCase):
    """pysaby зовёт urlopen без таймаута.

    Общий предел выставляется при старте сервиса, но прогон можно запустить и
    мимо него — из скрипта или из чужого процесса. Тогда зависшее соединение
    держит поток вечно: на живом прогоне выборка простояла одиннадцать минут на
    recv и падать не собиралась.
    """

    def setUp(self):
        super().setUp()
        # Исходный уровень задаём явно: другой тест мог оставить в процессе
        # ровно то значение, которое мы проверяем, и тест ничего бы не различал.
        self.addCleanup(socket.setdefaulttimeout, socket.getdefaulttimeout())
        socket.setdefaulttimeout(None)

    def _run_with_timeout(self, seconds):
        seen = {}

        class _Manager(_FlakySabyManager):
            def _send_json_request(self, url, payload, headers):
                seen["timeout"] = socket.getdefaulttimeout()
                return super()._send_json_request(url, payload, headers)

        manager = _Manager([{"result": {"События": [], "Навигация": {"HasMore": False}}}])
        client = SabyClient(_settings(request_timeout_seconds=seconds), manager=manager)
        client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))
        return seen.get("timeout")

    def test_timeout_is_set_for_the_request(self):
        self.assertEqual(30, self._run_with_timeout(30))

    def test_previous_timeout_is_restored(self):
        socket.setdefaulttimeout(11)

        self._run_with_timeout(30)

        self.assertEqual(11, socket.getdefaulttimeout())

    def test_zero_leaves_the_default_alone(self):
        socket.setdefaulttimeout(7)

        self.assertEqual(7, self._run_with_timeout(0))


class SessionFileTests(NetworkGuardTestCase):
    """Файл сессии pysaby не должен жить внутри site-packages.

    Там он теряется при переустановке зависимостей, а в окружении только для
    чтения — типовом для развёрнутой службы — конструктор библиотеки просто
    падает, и весь прогон превращается в одну техническую ошибку.
    """

    class _FakeManager:
        """Повторяет поведение SABYManager: создаёт базу прямо в конструкторе."""

        def __init__(self, login, REDACTED):
            self.login = login
            self.db_file = "/несуществующий/каталог/saby_manager.db"
            self._init_db()

        def _init_db(self):
            # Как sqlite3.connect: пишет по указанному пути и падает, если
            # каталога нет.
            Path(self.db_file).write_text("таблица токенов", encoding="utf-8")

    def test_session_file_goes_to_the_configured_path(self):
        from app.clients.saby import _build_manager

        directory = self.enterContext(tempfile.TemporaryDirectory())
        target = f"{directory}/вложенный/session.db"

        manager = _build_manager(self._FakeManager, _settings(saby_session_path=target))

        self.assertEqual(target, manager.db_file)
        self.assertEqual("таблица токенов", Path(target).read_text(encoding="utf-8"))

    def test_constructor_never_touches_the_library_directory(self):
        """Регрессия: конструктор создавал базу до того, как путь подменялся."""
        from app.clients.saby import _build_manager

        directory = self.enterContext(tempfile.TemporaryDirectory())
        _build_manager(self._FakeManager, _settings(saby_session_path=f"{directory}/s.db"))

        # Конструктор фейка упал бы на mkdir(exist_ok=False), если бы его
        # вызвали с исходным путём.
        self.assertFalse(Path("/несуществующий").exists())

    def test_original_init_is_restored(self):
        """Подмена класса временная: следующий вызов должен получить его целым."""
        from app.clients.saby import _build_manager

        original = self._FakeManager._init_db
        directory = self.enterContext(tempfile.TemporaryDirectory())
        _build_manager(self._FakeManager, _settings(saby_session_path=f"{directory}/s.db"))

        self.assertIs(original, self._FakeManager._init_db)


class PageRetryTests(NetworkGuardTestCase):
    """Обрыв TLS на одной странице из тридцати не должен валить прогон."""

    def _page(self, number):
        return {
            "result": {
                "События": [
                    _event({"@Событие": number, "Номер": str(number),
                            "Дата": "17.08.2026", "Сумма": "100.00"})
                ],
                "Навигация": {"HasMore": False},
            }
        }

    def test_page_is_retried(self):
        manager = _FlakySabyManager([OSError("обрыв TLS"), self._page(1)])
        client = SabyClient(_settings(saby_page_retries=3, saby_retry_delay_seconds=0), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(["1"], [item.incoming_number for item in documents])
        self.assertEqual(2, manager.attempts)

    def test_retries_are_bounded(self):
        manager = _FlakySabyManager([OSError("обрыв")] * 10)
        client = SabyClient(_settings(saby_page_retries=2, saby_retry_delay_seconds=0), manager=manager)

        with self.assertRaises(OSError):
            client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(2, manager.attempts)


class DuplicateDocumentTests(NetworkGuardTestCase):
    """Реестр отдаёт один документ на нескольких страницах.

    Замер на живой выборке: 7 лишних строк из 2017. Без дедупликации документ
    обрабатывается дважды и дважды попадает в отчёт.
    """

    def _doc(self, guid, number="4726385", edition="28100"):
        return _event(
            {
                "@Событие": 1,
                # Идентификатор редакции намеренно один и тот же: в реестре он
                # совпадает у разных документов и личностью служить не может.
                "@Документ": edition,
                "ИдРедакции": edition,
                "ИдентификаторДокумента": guid,
                "Номер": number,
                "Дата": "17.08.2026",
                "Сумма": "100.00",
            }
        )

    def test_repeated_document_is_counted_once(self):
        guid = "7e9061fe-d89a-55c4-a8c8-495cf7b0050b"
        manager = _FakeSabyManager(
            [{"result": {"События": [self._doc(guid), self._doc(guid)],
                         "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(1, len(documents))
        self.assertEqual(1, client.duplicate_events)

    def test_different_documents_sharing_an_event_id_both_survive(self):
        """Идентификатор события в реестре не уникален: 1980 против 2010 по GUID."""
        manager = _FakeSabyManager(
            [{"result": {
                "События": [
                    self._doc("7e9061fe-d89a-55c4-a8c8-495cf7b0050b", "первый"),
                    self._doc("7e9061fe-d89a-55c4-a8c8-495cf7b0050b", "второй"),
                ],
                "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual({"первый", "второй"}, {d.incoming_number for d in documents})
        self.assertEqual(0, client.duplicate_events)

    def test_guid_is_preferred_over_the_event_id(self):
        guid = "7e9061fe-d89a-55c4-a8c8-495cf7b0050b"
        manager = _FakeSabyManager(
            [{"result": {"События": [self._doc(guid)], "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(), manager=manager)

        documents = client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))

        self.assertEqual(guid, documents[0].id)


class SabyOrderNumberSourceTests(NetworkGuardTestCase):
    """Номер заказа из реквизита и выдранный из текста — разного качества."""

    def _document(self, values):
        manager = _FakeSabyManager(
            [{"result": {"События": [_event(values)], "Навигация": {"HasMore": False}}}]
        )
        client = SabyClient(_settings(), manager=manager)
        return client.list_incoming_documents(date(2026, 8, 17), date(2026, 8, 18))[0]

    def test_number_from_field_is_marked_as_field(self):
        document = self._document(
            {
                "@Событие": 1,
                "Номер": "4726385",
                "Дата": "17.08.2026",
                "Сумма": "100.00",
                "НомерЗаказа": "ТД000001405",
                "Комментарий": "счёт-фактура СФ000999999",
            }
        )

        self.assertEqual("ТД000001405", document.supplier_order_number)
        self.assertEqual("field", document.supplier_order_source)

    def test_number_from_comment_is_marked_as_text(self):
        document = self._document(
            {
                "@Событие": 1,
                "Номер": "4726385",
                "Дата": "17.08.2026",
                "Сумма": "100.00",
                "Комментарий": "УПД 4726385 заказ ТД000001405",
            }
        )

        self.assertEqual("ТД000001405", document.supplier_order_number)
        self.assertEqual("text", document.supplier_order_source)

    def test_absent_number_has_no_source(self):
        document = self._document(
            {"@Событие": 1, "Номер": "4726385", "Дата": "17.08.2026", "Сумма": "100.00"}
        )

        self.assertIsNone(document.supplier_order_number)
        self.assertIsNone(document.supplier_order_source)


def _settings(**overrides):
    return default_settings(
        saby_base_url="https://online.sbis.ru",
        saby_login="login",
        saby_REDACTED="REDACTED",
        report_backend="none",
        **overrides,
    )


#: Тип входящего УПД. Реестр отдаёт ещё договоры, акты сверки, корректировки и
#: счета — они отсеиваются по настройке, поэтому событие без типа до разбора
#: не доходит.
WANTED_TYPE = "ДокОтгрВх"


def _event(values: dict):
    """Событие реестра нужного типа."""
    return _record({"ТипДокумента.ТипДокумента": WANTED_TYPE, **values})


def _record(values: dict):
    return {
        "d": list(values.values()),
        "s": [{"t": "Строка", "n": key} for key in values],
        "_type": "record",
        "f": 0,
    }


class _FlakySabyManager:
    """Отдаёт по очереди: исключение — это сбой сети, словарь — нормальный ответ."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.attempts = 0
        self.base_url = "https://online.sbis.ru"
        self.headers = {}

    def _get_sid(self):
        return "session-1"

    def _send_json_request(self, url, payload, headers):
        self.attempts += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return 200, json.dumps(response)


class _FakeSabyManager:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []
        self.raw_calls = []
        self.auth_calls = 0
        self.base_url = "https://online.sbis.ru"
        self.headers = {
            "Content-Type": "application/json-rpc; charset=utf-8",
            "Accept": "application/json-rpc",
        }

    def _get_sid(self):
        return "session-1"

    def _auth(self):
        self.auth_calls += 1
        return "session-2"

    def _send_json_request(self, url, payload, headers):
        self.calls.append((payload["method"], payload["params"]))
        self.raw_calls.append((url, payload, dict(headers)))
        response = self._responses.pop(0)
        if isinstance(response, tuple):
            status_code, body = response
        else:
            status_code, body = 200, response
        return status_code, json.dumps(body)


if __name__ == "__main__":
    unittest.main()
