"""Форма подтверждения: доступ, выбор, запись.

Главное, что здесь проверяется, — форма не становится обходом предохранителей.
Запись идёт тем же путём, что и прогон, а при запрещённой записи не происходит
вовсе.
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from app.services.confirmations import (
    ConfirmationStore,
    PendingBatch,
    PendingItem,
    generate_code,
    hash_code,
    now,
)
from tests.support.fakes import FakeOneSRepository, expense, saby_doc
from tests.support.no_network import NetworkGuardTestCase
from tests.support.settings import test_settings

RECIPIENT = "developer@example.com"


def _item(key="000", candidates=None, status="awaiting_confirmation") -> PendingItem:
    documents = candidates if candidates is not None else [expense()]
    return PendingItem(
        key=key,
        saby_document=saby_doc().model_dump(mode="json"),
        candidates=[item.model_dump(mode="json") for item in documents],
        status=status,
        message="",
    )


class ConfirmFormTests(NetworkGuardTestCase):
    def setUp(self):
        super().setUp()
        from app.api import confirm

        # Сессии и счётчики живут в памяти процесса — между тестами их чистим.
        confirm._GATE.sessions.clear()
        confirm._GATE.attempts.clear()
        confirm._GATE.last_resend.clear()
        self.confirm = confirm

        self.directory = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.pending = f"{self.directory}/pending.json"
        self.journal = f"{self.directory}/journal.jsonl"
        self.store = ConfirmationStore(self.pending, self.journal)

    def _settings(self, **overrides):
        params = {
            "confirm_enabled": True,
            "smtp_to": [RECIPIENT],
            "confirm_pending_path": self.pending,
            "confirm_journal_path": self.journal,
            # При включённой записи валидация требует ключ на POST /sync/run.
            "sync_api_key": "ключ-для-теста",
        }
        params.update(overrides)
        return test_settings(**params)

    def _client(self, settings=None, repository=None) -> TestClient:
        settings = settings or self._settings()
        patcher = mock.patch("app.config.Settings.from_env", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        if repository is not None:
            service = mock.patch(
                "app.api.confirm.SyncService",
                side_effect=lambda s, **kw: __import__(
                    "app.services.sync_service", fromlist=["SyncService"]
                ).SyncService(s, repository=repository, **kw),
            )
            service.start()
            self.addCleanup(service.stop)
        import main

        return TestClient(main.app)

    def _open_batch(self, items=None, ttl_hours=48) -> str:
        code = generate_code()
        self.store.save(PendingBatch(
            code_hash=hash_code(code),
            created_at=now(),
            expires_at=now() + timedelta(hours=ttl_hours),
            request_id="test",
            items=items if items is not None else [_item()],
        ))
        return code

    def _reissue(self) -> str:
        """Действующий код партии: повторная отправка его сменила."""
        from app.services.confirmations import generate_code, hash_code

        batch = self.store.load()
        code = generate_code()
        batch.code_hash = hash_code(code)
        batch.code_used_at = None
        self.store.save(batch)
        return code

    def _journal(self) -> list[str]:
        from pathlib import Path

        path = Path(self.journal)
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class AccessTests(ConfirmFormTests):
    def test_correct_code_opens_the_list(self):
        code = self._open_batch()
        with self._client() as client:
            response = client.post("/confirm/login", data={"code": code}, follow_redirects=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("ожидающие решения", response.text)

    def test_code_is_case_insensitive_and_trimmed(self):
        """Код переписывают с экрана руками — регистр и пробелы прощаем."""
        code = self._open_batch()
        with self._client() as client:
            response = client.post(
                "/confirm/login", data={"code": f"  {code.lower()} "}, follow_redirects=True
            )

        self.assertIn("ожидающие решения", response.text)

    def test_wrong_code_is_refused(self):
        self._open_batch()
        with self._client() as client:
            response = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})

        self.assertEqual(401, response.status_code)

    def test_attempts_are_limited(self):
        self._open_batch()
        settings = self._settings(confirm_max_attempts=3)
        with self._client(settings) as client:
            for _ in range(3):
                client.post("/confirm/login", data={"code": "AAAAAAAAAA"})
            blocked = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})

        self.assertEqual(429, blocked.status_code)
        self.assertIn("Слишком много попыток", blocked.text)

    def test_expired_code_opens_nothing(self):
        code = self._open_batch(ttl_hours=-1)
        with self._client() as client:
            response = client.post("/confirm/login", data={"code": code})

        self.assertNotIn("ожидающие решения", response.text)
        self.assertIn("Срок кода истёк", response.text)

    def test_empty_state_does_not_blame_an_expired_code(self):
        """Чаще всего партии нет потому, что прогон ещё не выполнялся."""
        with self._client() as client:
            response = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})

        self.assertIn("прогон ещё не выполнялся", response.text)
        self.assertNotIn("Срок кода истёк", response.text)

    def test_advice_matches_the_scheduler(self):
        """Совет «дождитесь отчёта» лжив, когда ежедневные прогоны выключены."""
        with self._client(self._settings(daily_sync_enabled=False)) as client:
            off = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})
        with self._client(self._settings(daily_sync_enabled=True,
                                         daily_sync_time="17:00")) as client:
            on = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})

        self.assertIn("сам не придёт", off.text)
        self.assertIn("17:00", on.text)

    def test_session_survives_page_reload(self):
        """Человек может думать долго; перезагрузка не должна ничего ломать."""
        code = self._open_batch()
        with self._client() as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            again = client.get("/confirm/list")

        self.assertIn("ожидающие решения", again.text)

    def test_expired_session_asks_for_the_code_again(self):
        code = self._open_batch()
        with self._client(self._settings(confirm_session_minutes=0)) as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            later = client.get("/confirm/list")

        self.assertIn("name=code", later.text)
        self.assertIn("запросите новый", later.text.lower())

    def test_code_burns_after_one_use(self):
        """Код одноразовый: открыл сессию — погас."""
        code = self._open_batch()
        with self._client() as client:
            first = client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.cookies.clear()
            second = client.post("/confirm/login", data={"code": code})

        self.assertIn("ожидающие решения", first.text)
        self.assertEqual(401, second.status_code)
        self.assertIn("уже использован", second.text)

    def test_used_code_is_told_apart_from_a_wrong_one(self):
        """«Код неверный» и «код уже сработал» — разные беды с разным выходом."""
        code = self._open_batch()
        with self._client() as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.cookies.clear()
            used = client.post("/confirm/login", data={"code": code})
            wrong = client.post("/confirm/login", data={"code": "AAAAAAAAAA"})

        self.assertIn("запросите новый", used.text.lower())
        self.assertIn("Осталось попыток", wrong.text)

    def test_open_session_survives_the_code_burning(self):
        """Погасший код не должен выбрасывать того, кто уже вошёл."""
        code = self._open_batch()
        with self._client() as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            still_there = client.get("/confirm/list")

        self.assertIn("ожидающие решения", still_there.text)

    def test_resend_revives_the_batch(self):
        """Погасил код — запросил новый, и он работает."""
        code = self._open_batch()
        with self._client(self._settings(report_backend="console")) as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.cookies.clear()
            client.post("/confirm/resend", data={"email": RECIPIENT})
            fresh = self.store.load()

        self.assertIsNone(fresh.code_used_at)


class KillSwitchTests(ConfirmFormTests):
    """CONFIRM_ENABLED должен выключать механизм, а не вывеску.

    Раньше проверка стояла только на входной странице: она честно писала
    «отключено», а POST на /confirm/login с верным кодом всё так же открывал
    сессию, и за ней были доступны список, предпросмотр и запись.
    """

    def _off(self):
        return self._settings(confirm_enabled=False, one_s_writes_enabled=True)

    def test_valid_code_opens_nothing(self):
        code = self._open_batch()
        with self._client(self._off()) as client:
            response = client.post("/confirm/login", data={"code": code}, follow_redirects=False)

        self.assertIn("отключено", response.text)
        self.assertNotIn("confirm_session", response.headers.get("set-cookie", ""))

    def test_apply_writes_nothing(self):
        repository = FakeOneSRepository(allow_writes=True)  # любая запись провалит тест
        code = self._open_batch()
        with self._client(self._off(), repository=repository) as client:
            client.post("/confirm/login", data={"code": code})
            response = client.post("/confirm/apply", data={"apply_000": expense().ref})

        self.assertEqual(0, repository.write_calls)
        self.assertIn("отключено", response.text)
        self.assertIsNotNone(self.store.load(), "партия не должна была тронуться")

    def test_code_is_not_reissued(self):
        self._open_batch()
        with self._client(self._off()) as client:
            response = client.post("/confirm/resend", data={"email": RECIPIENT})

        self.assertIn("отключено", response.text)


class StaleSessionTests(ConfirmFormTests):
    """Сессия действительна только для той партии, которой открывалась.

    Прогон заменяет партию целиком, а ключи позиций в ней те же — `000`, `001`.
    Без этой проверки открытая сессия продолжала работать с новым набором
    документов, и от записи не в тот документ спасало лишь несовпадение `ref`.
    """

    def _reopen_batch(self) -> None:
        """Прогон заменил партию: другой код, другие документы."""
        self.store.save(PendingBatch(
            code_hash=hash_code(generate_code()),
            created_at=now(),
            expires_at=now() + timedelta(hours=48),
            request_id="следующий прогон",
            items=[_item(key="000")],
        ))

    def test_list_asks_for_the_new_code(self):
        code = self._open_batch()
        with self._client() as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            self._reopen_batch()
            response = client.get("/confirm/list")

        self.assertIn("другая партия", response.text)
        self.assertIn("name=code", response.text)

    def test_apply_writes_nothing(self):
        repository = FakeOneSRepository(allow_writes=True)
        code = self._open_batch()
        with self._client(self._settings(one_s_writes_enabled=True),
                          repository=repository) as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            self._reopen_batch()
            response = client.post("/confirm/apply", data={"apply_000": expense().ref})

        self.assertEqual(0, repository.write_calls)
        self.assertIn("другая партия", response.text)


class ResendTests(ConfirmFormTests):
    """Повторная отправка кода.

    Прежний код восстановить нельзя — хранится только хэш, — поэтому «выслать
    код» означает выдать новый. Старый с этого момента не работает, и это
    скорее плюс: утёкший код гаснет сам.
    """

    def _settings(self, **overrides):
        # Консольный backend: письмо формируется целиком, но никуда не уходит.
        overrides.setdefault("report_backend", "console")
        return super()._settings(**overrides)

    def test_known_address_gets_a_new_code(self):
        first = self._open_batch()
        with self._client() as client:
            response = client.post("/confirm/resend", data={"email": RECIPIENT})

        self.assertIn("Отправлено", response.text)
        self.assertNotEqual(hash_code(first), self.store.load().code_hash)

    def test_old_code_stops_working_after_resend(self):
        old = self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            response = client.post("/confirm/login", data={"code": old})

        self.assertEqual(401, response.status_code)

    def test_unknown_address_is_told_plainly(self):
        self._open_batch()
        with self._client() as client:
            response = client.post("/confirm/resend", data={"email": "developer@example.com"})

        self.assertIn("Не отправлено", response.text)
        self.assertEqual([], [line for line in self._journal() if '"повторная отправка"' in line])

    def test_resend_is_rate_limited(self):
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            second = client.post("/confirm/resend", data={"email": RECIPIENT})

        self.assertIn("Не отправлено", second.text)
        self.assertIn("запрос уже был", second.text)

    def test_rejected_address_also_starts_the_pause(self):
        """Иначе форма становится справочником по составу рассылки.

        Отвечает она разборчиво — «отправлено» или «такой адрес не получает
        отчёты», это решение осознанное. Но пауза раньше засекалась только после
        удачной отправки, и чужие адреса можно было перебирать подряд без
        всякого ограничения.
        """
        self._open_batch()
        with self._client() as client:
            first = client.post("/confirm/resend", data={"email": "developer@example.com"})
            second = client.post("/confirm/resend", data={"email": "developer@example.com"})

        self.assertIn("не получает отчёты", first.text)
        self.assertIn("запрос уже был", second.text)

    def test_nothing_to_confirm_is_reported(self):
        with self._client() as client:
            response = client.post("/confirm/resend", data={"email": RECIPIENT})

        self.assertIn("подтверждать сейчас нечего", response.text)

    def test_failed_delivery_is_reported_as_not_sent(self):
        """Молчаливый провал отправки был бы хуже отказа: человек ждал бы письма."""
        from app.services.reporting import ReportService

        class _BrokenMail(ReportService):
            def _send_smtp(self, message, recipients):
                raise OSError("почтовый сервер недоступен")

        self._open_batch()
        settings = self._settings(report_backend="smtp", smtp_host="mail.internal.internal.example.invalid")
        patcher = mock.patch("app.api.confirm.ReportService", _BrokenMail)
        patcher.start()
        self.addCleanup(patcher.stop)

        with self._client(settings) as client:
            response = client.post("/confirm/resend", data={"email": RECIPIENT})

        self.assertIn("Не отправлено", response.text)
        self.assertIn("недоступен", response.text)

    def test_journal_records_the_outcome(self):
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})

        entries = [line for line in self._journal() if '"повторная отправка"' in line]
        self.assertEqual(1, len(entries))
        self.assertIn(RECIPIENT, entries[0])
        self.assertIn("отправлено", entries[0])
        self.assertIn("sha256:", entries[0])


class KnownRequesterTests(ConfirmFormTests):
    """Кто запрашивал код, того и не нужно спрашивать.

    Код одноразовый и уходит только на адрес из рассылки, поэтому вошедший с
    ним почти наверняка читает тот ящик. Это свидетельство, а не слова, и
    держать его надо на сервере: кука хранит лишь непрозрачный ключ.
    """

    def _settings(self, **overrides):
        overrides.setdefault("report_backend", "console")
        return super()._settings(**overrides)

    def test_address_is_not_asked_again(self):
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            # Код сменился при отправке — берём действующий.
            code = self._reissue()
            page = client.post("/confirm/login", data={"code": code}, follow_redirects=True)

        self.assertIn("Подтверждает:", page.text)
        self.assertIn(RECIPIENT, page.text)
        self.assertNotIn('name=email', page.text.split("Подтверждает:")[1][:400])

    def test_confirmation_works_without_the_field(self):
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            code = self._reissue()
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            response = client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref},
            )

        # Второе окно обязано называть поля, которые будут заполнены: человек
        # подтверждает запись конкретных значений, а не «связь документов».
        self.assertIn("Номер входящего документа", response.text)
        self.assertIn("Дата входящего документа", response.text)

    def test_submitted_address_cannot_override_the_known_one(self):
        """Иначе вся затея свелась бы к полю, которое правится в отладчике."""
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            code = self._reissue()
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref,
                      "email": "developer@example.com"},
            )
            client.post("/confirm/apply", data={"apply_000": expense().ref})

        entry = [line for line in self._journal() if '"подтверждение"' in line][0]
        self.assertIn(RECIPIENT, entry)
        self.assertNotIn("developer@example.com", entry)

    def test_journal_says_how_the_address_was_established(self):
        self._open_batch()
        with self._client() as client:
            client.post("/confirm/resend", data={"email": RECIPIENT})
            code = self._reissue()
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.post("/confirm/preview",
                        data={"selected": "000", "choice_000": expense().ref})
            client.post("/confirm/apply", data={"apply_000": expense().ref})

        entry = [line for line in self._journal() if '"подтверждение"' in line][0]
        self.assertIn("код уходил на него", entry)

    def test_without_resend_the_field_is_still_asked(self):
        """Обычный путь — код из общей рассылки: кто именно вошёл, неизвестно."""
        code = self._open_batch()
        with self._client() as client:
            page = client.post("/confirm/login", data={"code": code}, follow_redirects=True)

        self.assertIn("name=email", page.text)
        self.assertNotIn("Подтверждает:", page.text)

    def test_self_declared_address_is_marked_as_such(self):
        code = self._open_batch()
        with self._client() as client:
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.post("/confirm/preview",
                        data={"selected": "000", "choice_000": expense().ref,
                              "email": RECIPIENT})
            client.post("/confirm/apply", data={"apply_000": expense().ref})

        entry = [line for line in self._journal() if '"подтверждение"' in line][0]
        self.assertIn("назван в форме", entry)


class AttributionTests(ConfirmFormTests):
    def _logged_in(self, client):
        code = self._open_batch()
        client.post("/confirm/login", data={"code": code}, follow_redirects=True)

    def test_confirmation_requires_an_address_from_the_list(self):
        with self._client() as client:
            self._logged_in(client)
            response = client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref, "email": "developer@example.com"},
            )

        self.assertIn("Введите адрес почты", response.text)
        self.assertNotIn("Будет записано", response.text)

    def test_rejected_address_does_not_hint_at_the_list(self):
        """По сообщению нельзя понять, состоит адрес в рассылке или нет."""
        with self._client() as client:
            self._logged_in(client)
            response = client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref, "email": "developer@example.com"},
            )

        for hint in ("рассылк", "списк", "отчёт приходит"):
            self.assertNotIn(hint, response.text.split("Введите адрес")[0][-400:])

    def test_rejected_address_is_recorded(self):
        """Подбор чужих адресов должен быть заметен, а не пройти бесследно."""
        with self._client() as client:
            self._logged_in(client)
            client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref, "email": "developer@example.com"},
            )

        entries = [line for line in self._journal() if '"адрес отклонён"' in line]
        self.assertEqual(1, len(entries))
        self.assertIn("developer@example.com", entries[0])

    def test_known_address_reaches_the_preview(self):
        with self._client() as client:
            self._logged_in(client)
            response = client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": expense().ref, "email": RECIPIENT},
            )

        # Второе окно обязано называть поля, которые будут заполнены: человек
        # подтверждает запись конкретных значений, а не «связь документов».
        self.assertIn("Номер входящего документа", response.text)
        self.assertIn("Дата входящего документа", response.text)
        # Раньше здесь проверялось обещание «документ не перепроводится». Мы
        # этого не знаем: что обработчик 1С делает с документом после записи —
        # открытый вопрос к разработчику 1С. Форма теперь говорит только о том,
        # что отправляет сама, и это единственное, за что можно ручаться.
        self.assertNotIn("не перепроводится", response.text)
        self.assertIn("Больше запрос ничего не содержит", response.text)


class ApplyTests(ConfirmFormTests):
    def _apply(self, settings=None, repository=None, ref=None):
        repository = repository if repository is not None else FakeOneSRepository(allow_writes=True)
        with self._client(settings or self._settings(one_s_writes_enabled=True),
                          repository=repository) as client:
            code = self._open_batch()
            client.post("/confirm/login", data={"code": code}, follow_redirects=True)
            client.post(
                "/confirm/preview",
                data={"selected": "000", "choice_000": ref or expense().ref, "email": RECIPIENT},
            )
            response = client.post("/confirm/apply", data={"apply_000": ref or expense().ref})
        return response, repository

    def test_confirmed_document_is_written(self):
        response, repository = self._apply()

        self.assertIn("записано", response.text)
        self.assertEqual(1, repository.write_calls)

    def test_nothing_is_written_while_writes_are_forbidden(self):
        """Форма не отменяет главный рубильник."""
        repository = FakeOneSRepository()  # любая запись провалит тест
        response, _ = self._apply(
            settings=self._settings(one_s_writes_enabled=False), repository=repository
        )

        self.assertEqual(0, repository.write_calls)
        self.assertIn("ONE_S_WRITES_ENABLED", response.text)

    def test_document_filled_meanwhile_is_not_overwritten(self):
        """Между показом списка и нажатием реквизиты могли заполнить руками."""
        from app.schemas.one_s import IncomingRequisites

        filled = expense(
            incoming_requisites=IncomingRequisites(
                number="4726385", date=date(2026, 8, 17)
            )
        )
        repository = FakeOneSRepository(expenses=[filled], allow_writes=True)
        response, _ = self._apply(repository=repository, ref=filled.ref)

        self.assertEqual(0, repository.write_calls)
        self.assertIn("заполнили вручную", response.text)

    def test_foreign_ref_is_refused(self):
        response, repository = self._apply(ref="7e9061fe-d89a-55c4-a8c8-495cf7b0050b")

        self.assertEqual(0, repository.write_calls)
        self.assertIn("не относится к этому предложению", response.text)

    def test_confirmed_item_leaves_the_batch(self):
        self._apply()

        batch = self.store.load()
        self.assertIsNone(batch)

    def test_unwritten_item_stays_in_the_batch(self):
        """Не записали — предложение должно остаться.

        Раньше позиция уходила из партии при любом исходе. При запрещённой
        записи это выглядело так: человек жмёт «Записать», в 1С ничего не
        меняется, а предложение исчезает вместе с его решением — повторить
        нечего. Та же беда ждала и обрыв связи с 1С.
        """
        repository = FakeOneSRepository()  # запись запрещена — ничего не уйдёт
        response, _ = self._apply(
            settings=self._settings(one_s_writes_enabled=False), repository=repository
        )

        batch = self.store.load()
        self.assertIsNotNone(batch, "партия исчезла после неудачной записи")
        self.assertEqual(["000"], [item.key for item in batch.items])
        self.assertIn("осталось в списке", response.text)

    def test_journal_says_whether_the_item_survived(self):
        """Разбирающему журнал важно, ждёт документ повторной попытки или нет."""
        self._apply(settings=self._settings(one_s_writes_enabled=False),
                    repository=FakeOneSRepository())

        entries = [line for line in self._journal() if '"подтверждение"' in line]
        self.assertIn('"осталось_в_списке": true', entries[0])

    def test_journal_records_who_confirmed_and_the_outcome(self):
        self._apply()

        entries = [line for line in self._journal() if '"подтверждение"' in line]
        self.assertEqual(1, len(entries))
        self.assertIn(RECIPIENT, entries[0])
        self.assertIn("written", entries[0])
        # Код в журнале только хэшем: иначе журнал становится связкой ключей.
        self.assertIn("sha256:", entries[0])


class BatchStoreTests(NetworkGuardTestCase):
    def test_batch_survives_a_restart(self):
        import tempfile

        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = ConfirmationStore(f"{directory}/pending.json", f"{directory}/journal.jsonl")
        code, batch = store.open_batch([_item()], "run-1", ttl_hours=48)

        restored = ConfirmationStore(
            f"{directory}/pending.json", f"{directory}/journal.jsonl"
        ).load()

        self.assertEqual(batch.code_hash, restored.code_hash)
        self.assertEqual(hash_code(code), restored.code_hash)
        self.assertEqual(1, len(restored.items))

    def test_broken_file_does_not_raise(self):
        import tempfile
        from pathlib import Path

        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "pending.json"
        path.write_text("{не json", encoding="utf-8")

        self.assertIsNone(ConfirmationStore(path, Path(directory) / "j.jsonl").load())

    def test_empty_batch_is_not_created(self):
        import tempfile

        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = ConfirmationStore(f"{directory}/pending.json", f"{directory}/journal.jsonl")

        self.assertEqual((None, None), store.open_batch([], "run-1", 48))


if __name__ == "__main__":
    unittest.main()
