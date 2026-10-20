"""Форма подтверждения разноски.

Программа не записывает в 1С сама. Она готовит предложения, рассылает их вместе
с кодом доступа, а запись выполняет здесь — после того как человек выбрал
вариант и подтвердил его дважды.

Устройство доступа без формы входа:

* **Код** — ворота. Приходит в письме с отчётом, живёт свой срок и срабатывает
  один раз: открыл сессию — погас. Саму сессию это не выбрасывает, перезагрузка
  страницы ничего не ломает.
* **Адрес почты** — атрибуция. Не пускает и не защищает, но отвечает на вопрос
  «кто разрешил запись в бухгалтерию». Проверяется по списку рассылки.

Ограничения от перебора: счётчик неудачных попыток по адресу браузера и пауза
после исчерпания; повторная отправка кода не чаще заданного интервала и только
на адрес, который и так есть в рассылке.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.html import esc, note, page
from app.config import Settings
from app.services.confirmations import (
    ConfirmationStore,
    PendingBatch,
    hash_code,
    now,
)
from app.services.report_labels import status_label
from app.services.reporting import ReportService
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["confirm"])

#: Исходы, после которых решать больше нечего: запись сделана, реквизиты уже
#: стояли или в 1С указан другой документ. Только такие позиции уходят из
#: партии. При отказе и технической ошибке предложение остаётся — иначе решение
#: человека пропадало вместе с предложением, и повторить было нечего.
SETTLED_STATUSES = frozenset({"written", "already_filled", "conflict"})

SESSION_COOKIE = "confirm_session"
#: Ключ к запомненному адресу того, кто запрашивал код. В куке только он —
#: сам адрес лежит на сервере, иначе его подменил бы кто угодно.
IDENTITY_COOKIE = "confirm_identity"


@dataclass
class _Session:
    expires_at: datetime
    code_hash: str
    email: str = ""
    #: Адрес известен из запроса кода, а не назван на глаз. Код одноразовый и
    #: уходит только на адрес из рассылки, поэтому вошедший с ним почти наверняка
    #: читает тот ящик — это свидетельство, а не самодекларация.
    email_verified: bool = False


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: datetime | None = None


@dataclass
class _Gate:
    """Сессии и счётчики попыток.

    Хранятся в памяти процесса намеренно: при перезапуске сотрудник вводит код
    заново, а он у него в открытом письме. Файл на диске дал бы ровно ту же
    надёжность ценой ещё одного состояния, которое надо чистить.
    """

    sessions: dict[str, _Session] = field(default_factory=dict)
    attempts: dict[str, _Attempts] = field(default_factory=dict)
    last_resend: dict[str, datetime] = field(default_factory=dict)
    #: Кому уходил код: ключ из куки → адрес. Живёт столько же, сколько код.
    identities: dict[str, tuple[str, datetime]] = field(default_factory=dict)

    def session(self, token: str | None, minutes: int) -> _Session | None:
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        if now() >= session.expires_at:
            self.sessions.pop(token, None)
            return None
        # Продлеваем при каждом действии: человек может думать долго.
        session.expires_at = now() + timedelta(minutes=minutes)
        return session

    def remember_identity(self, email: str, hours: int) -> str:
        token = secrets.token_urlsafe(24)
        self.identities[token] = (email, now() + timedelta(hours=max(1, hours)))
        return token

    def identity(self, token: str | None) -> str:
        if not token:
            return ""
        remembered = self.identities.get(token)
        if remembered is None:
            return ""
        email, expires_at = remembered
        if now() >= expires_at:
            self.identities.pop(token, None)
            return ""
        return email

    def open_session(self, code_hash: str, minutes: int) -> str:
        token = secrets.token_urlsafe(24)
        self.sessions[token] = _Session(
            expires_at=now() + timedelta(minutes=minutes), code_hash=code_hash
        )
        return token



class _Form:
    """Разбор тела формы без внешних зависимостей.

    `request.form()` в Starlette требует `python-multipart` даже для обычного
    urlencoded, а загрузки файлов здесь нет и не будет. Значения бывают
    повторяющимися — например, отмеченные строки, — поэтому храним пары.
    """

    def __init__(self, pairs: list[tuple[str, str]]):
        self._pairs = pairs

    @classmethod
    async def read(cls, request: Request) -> "_Form":
        body = (await request.body()).decode("utf-8", errors="replace")
        return cls(parse_qsl(body, keep_blank_values=True))

    def get(self, name: str, default: str = "") -> str:
        for key, value in self._pairs:
            if key == name:
                return value
        return default

    def all(self, name: str) -> list[str]:
        return [value for key, value in self._pairs if key == name]

    def pairs(self) -> list[tuple[str, str]]:
        return list(self._pairs)


_GATE = _Gate()


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else Settings.from_env()


def _store(settings: Settings) -> ConfirmationStore:
    return ConfirmationStore(settings.confirm_pending_path, settings.confirm_journal_path)


def _disabled(settings: Settings) -> HTMLResponse | None:
    """Отказ, если подтверждение выключено настройкой.

    Зовётся первой строкой в КАЖДОМ обработчике. Раньше проверка стояла только
    на входной странице, и выключатель гасил вывеску, а не механизм: POST на
    /confirm/login с верным кодом всё так же открывал сессию, а за ней и запись.
    """
    if settings.confirm_enabled:
        return None
    return HTMLResponse(page("Подтверждение", note("Подтверждение отключено настройкой.", "warn")))


def _client(request: Request) -> str:
    return request.client.host if request.client else "неизвестно"


def _recipients(settings: Settings) -> list[str]:
    return [address.strip().lower() for address in settings.smtp_to if address.strip()]



def _nothing_to_confirm(settings: Settings, batch: PendingBatch | None) -> str:
    """Почему подтверждать нечего и что с этим делать.

    Раньше здесь была одна фраза на все случаи: «либо всё уже обработано, либо
    срок кода истёк, дождитесь следующего отчёта». Она умалчивала о самом частом
    случае — прогон ещё не выполнялся, — а совет ждать отчёта прямо вводил в
    заблуждение при выключенном планировщике: отчёт сам не придёт никогда.
    """
    if batch is not None and batch.is_expired():
        reason = "Срок кода истёк — предложения из того письма больше не подтвердить."
    else:
        reason = "Подтверждать нечего: либо всё уже обработано, либо прогон ещё не выполнялся."

    if settings.daily_sync_enabled:
        advice = f"Следующий отчёт придёт по расписанию, в {settings.daily_sync_time}."
    else:
        advice = ("Ежедневные прогоны выключены, поэтому отчёт сам не придёт — "
                  "синхронизацию нужно запустить.")
    return note(f"{reason} {advice}", "warn")


# --- Экран ввода кода ------------------------------------------------------


def _code_screen(settings: Settings, message: str = "") -> str:
    body = f"""
      <h1>Подтверждение разноски документов</h1>
      <p class=lead>Введите код из письма с отчётом — он в конце письма.</p>
      {message}
      <form class=card method=post action="/confirm/login">
        <label>Код доступа<br>
          <input class=code type=text name=code autocomplete=off autofocus
                 maxlength=16 placeholder="XXXXXXXXXX"></label>
        <div class=row><button type=submit>Продолжить</button></div>
      </form>
      {note("Код одноразовый — он сработает один раз. Если страница закроется "
            "или вы отвлечётесь надолго, запросите новый: письмо придёт сразу.")}
      <form class=card method=post action="/confirm/resend">
        <p class=muted>Не нашли письмо? Введите адрес почты, куда отправить код.</p>
        <label><input type=email name=email placeholder="developer@example.com"></label>
        <div class=row><button class=secondary type=submit>Выслать код</button></div>
      </form>
    """
    return page("Подтверждение разноски", body)


@router.get("/confirm", response_class=HTMLResponse)
def confirm_start(request: Request):
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    if _GATE.session(request.cookies.get(SESSION_COOKIE), settings.confirm_session_minutes):
        return RedirectResponse("/confirm/list", status_code=303)
    return HTMLResponse(_code_screen(settings))


@router.post("/confirm/login", response_class=HTMLResponse)
async def confirm_login(request: Request):
    # Разбираем тело сами: объявление Form(...) потянуло бы python-multipart,
    # а формы здесь обычные urlencoded, без загрузки файлов.
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    code = (await _Form.read(request)).get("code")
    store = _store(settings)
    client = _client(request)

    attempts = _GATE.attempts.setdefault(client, _Attempts())
    if attempts.locked_until and now() < attempts.locked_until:
        left = int((attempts.locked_until - now()).total_seconds() // 60) + 1
        return HTMLResponse(
            _code_screen(settings, note(
                f"Слишком много попыток. Попробуйте через {left} мин.", "bad")),
            status_code=429,
        )

    batch = store.load()
    if batch is None or batch.is_expired():
        return HTMLResponse(_code_screen(settings, _nothing_to_confirm(settings, batch)))

    if batch.code_used_at is not None and secrets.compare_digest(
        hash_code(code), batch.code_hash
    ):
        # Код верный, но уже сработал. Говорим прямо и показываем выход: новый
        # код запрашивается тут же, на этом экране.
        store.record("код уже использован", адрес_браузера=client)
        return HTMLResponse(
            _code_screen(settings, note(
                "Этот код уже использован — он одноразовый. Запросите новый на почту.",
                "warn")),
            status_code=401,
        )

    if not secrets.compare_digest(hash_code(code), batch.code_hash):
        attempts.failures += 1
        if attempts.failures >= max(1, settings.confirm_max_attempts):
            attempts.locked_until = now() + timedelta(minutes=settings.confirm_lockout_minutes)
            attempts.failures = 0
        store.record("неверный код", адрес_браузера=client)
        left = max(0, settings.confirm_max_attempts - attempts.failures)
        return HTMLResponse(
            _code_screen(settings, note(
                f"Код не подошёл. Осталось попыток: {left}.", "bad")),
            status_code=401,
        )

    attempts.failures = 0
    token = _GATE.open_session(batch.code_hash, settings.confirm_session_minutes)
    known = _GATE.identity(request.cookies.get(IDENTITY_COOKIE))
    if known:
        session = _GATE.sessions[token]
        session.email = known
        session.email_verified = True
    store.mark_code_used()
    store.record("вход", код=batch.code_hash, адрес_браузера=client)
    response = RedirectResponse("/confirm/list", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/confirm")
    return response


@router.post("/confirm/resend", response_class=HTMLResponse)
async def confirm_resend(request: Request):
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    email = (await _Form.read(request)).get("email")
    store = _store(settings)
    address = email.strip().lower()
    client = _client(request)

    # Пауза считается и по адресу, и по браузеру, и засекается ДО проверки
    # списка рассылки. Форма отвечает разборчиво — отправлено или нет, — а это
    # значит, что без паузы на отказах она превращается в справочник: чужие
    # адреса можно было перебирать подряд и узнать состав рассылки.
    interval = timedelta(minutes=max(1, settings.confirm_resend_interval_minutes))
    last = max(
        (moment for moment in (_GATE.last_resend.get(address), _GATE.last_resend.get(client))
         if moment is not None),
        default=None,
    )
    if last is not None and now() - last < interval:
        store.record("повторная отправка отклонена", адрес=address,
                     адрес_браузера=client, причина="слишком часто")
        left = int((interval - (now() - last)).total_seconds() // 60) + 1
        return HTMLResponse(_code_screen(settings, note(
            f"Не отправлено: запрос уже был. Повторить можно через {left} мин.", "warn")))

    _GATE.last_resend[client] = now()

    if address not in _recipients(settings):
        store.record("повторная отправка отклонена", адрес=address,
                     адрес_браузера=client, причина="адреса нет в рассылке")
        logger.warning("Код не выслан: адреса %r нет в рассылке, браузер %s", address, client)
        return HTMLResponse(_code_screen(settings, note(
            "Не отправлено: такой адрес не получает отчёты.", "bad")))

    # Прежний код восстановить нельзя — хранится только хэш. Поэтому выдаём
    # новый; старый с этого момента не работает.
    code = store.reissue_code()
    if code is None:
        return HTMLResponse(_code_screen(settings, note(
            "Не отправлено: подтверждать сейчас нечего. Дождитесь следующего отчёта.",
            "warn")))

    delivery = ReportService(settings).send_confirmation_code(code, [address])
    _GATE.last_resend[address] = now()
    store.record(
        "повторная отправка",
        адрес=address,
        адрес_браузера=client,
        код=hash_code(code),
        итог="отправлено" if delivery.sent else delivery.reason,
    )
    if delivery.sent:
        response = HTMLResponse(_code_screen(settings, note(
            f"Отправлено на {esc(address)}. Прежний код больше не действует.", "good")))
        # Код ушёл именно на этот адрес и сработает один раз: вошедший с ним
        # почти наверняка читает тот ящик. Запоминаем — потом не придётся
        # спрашивать, кто подтверждает.
        response.set_cookie(
            IDENTITY_COOKIE,
            _GATE.remember_identity(address, settings.confirm_code_ttl_hours),
            httponly=True, samesite="lax", path="/confirm",
        )
        return response
    return HTMLResponse(_code_screen(settings, note(
        f"Не отправлено: {esc(delivery.reason)}", "bad")))


# --- Список и подтверждение ------------------------------------------------


def _candidate_row(key: str, index: int, raw: dict, single: bool) -> str:
    control = (
        f'<input type=hidden name="choice_{key}" value="{esc(raw.get("ref"))}">'
        if single
        else f'<input type=radio name="choice_{key}" value="{esc(raw.get("ref"))}"'
             f'{" checked" if index == 0 else ""}>'
    )
    order = raw.get("supplier_order") or {}
    return (
        f"<label class=choice>{control} <b>{esc(raw.get('number'))}</b> "
        f"от {esc(_short_date(raw.get('date')))}, {esc(raw.get('amount'))}"
        f"<br><span class=muted>заказ {esc(order.get('number'))}, "
        f"{esc(raw.get('additional_expense_type'))}</span></label>"
    )


def _ru_date(value: object) -> str:
    """Дата в том виде, в каком её читает бухгалтер: 08.09.2026."""
    text = str(value or "")[:10]
    parts = text.split("-")
    return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else text


def _short_date(value: object) -> str:
    text = str(value or "")
    return text[:10] if text else ""


def _item_card(item) -> str:
    document = item.saby_document
    single = len(item.candidates) == 1
    heading = (
        "Программа уверена в выборе — проверьте и подтвердите."
        if single
        else "Программа не смогла выбрать сама — укажите нужный документ."
    )
    choices = "".join(
        _candidate_row(item.key, index, raw, single)
        for index, raw in enumerate(item.candidates)
    )
    return f"""
      <div class=card>
        <div class=doc>УПД {esc(document.get('incoming_number'))} от
             {esc(_short_date(document.get('incoming_date')))},
             {esc(document.get('counterparty_name'))}, {esc(document.get('amount'))}</div>
        <p class=muted>{esc(heading)}</p>
        <label class=choice><input type=checkbox name=selected value="{esc(item.key)}"
               checked> Подтвердить этот документ</label>
        {choices}
      </div>
    """


def _identity_block(session: "_Session") -> str:
    """Поле «кто подтверждает» — или строка с уже известным адресом.

    Показываем адрес как факт, а не как поле только для чтения: `readonly` в
    HTML правится в отладчике за секунду, и вид защищённости был бы обманом.
    Настоящая защита в том, что сервер берёт адрес из своей памяти и присланное
    значение не читает вовсе.
    """
    if session.email_verified:
        return f"""
        <div class=card>
          <p class=muted>Подтверждает: <b>{esc(session.email)}</b></p>
        </div>"""
    return """
        <div class=card>
          <label>Ваша почта — чтобы в журнале осталось, кто подтвердил:<br>
            <input type=email name=email required placeholder="developer@example.com"></label>
        </div>"""


def _stale_session(session: "_Session", batch: PendingBatch) -> bool:
    """Сессия открыта другой партией.

    Прогон заменяет партию целиком, а ключи позиций в ней — `000`, `001` — те же
    самые. Без этой проверки открытая сессия продолжала работать с новым набором
    документов, и от записи не в тот документ спасало лишь то, что `ref` не
    совпадёт. Держать защиту на совпадении нельзя.
    """
    return session.code_hash != batch.code_hash


def _list_screen(
    settings: Settings, batch: PendingBatch, message: str = "", session: "_Session | None" = None
) -> str:
    if not batch.items:
        return page("Подтверждение", note("Всё подтверждено. Спасибо.", "good"))

    cards = "".join(_item_card(item) for item in batch.items)
    writes_warning = (
        ""
        if settings.one_s_writes_enabled
        else note("Запись в 1С сейчас запрещена настройкой. Подтверждение будет "
                  "принято, но документы останутся неизменными.", "warn")
    )
    body = f"""
      <h1>Документы, ожидающие решения</h1>
      <p class=lead>Подтверждение заполнит в выбранном документе 1С номер и дату
         входящего документа — больше ничего. Остальные документы отчёта в
         приложенном к письму файле, их подтверждать не нужно.</p>
      {message}{writes_warning}
      <form method=post action="/confirm/preview">
        {cards}
        {_identity_block(session) if session else ""}
        {note("Пока вы не нажмёте «Подтвердить» и не согласитесь во втором окне, "
              "в 1С ничего не изменится.")}
        <div class=row><button type=submit>Подтвердить</button></div>
      </form>
    """
    return page("Документы к подтверждению", body)


@router.get("/confirm/list", response_class=HTMLResponse)
def confirm_list(request: Request):
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    session = _GATE.session(request.cookies.get(SESSION_COOKIE), settings.confirm_session_minutes)
    if session is None:
        return HTMLResponse(_code_screen(settings, note(
            "Сессия закрылась. Прежний код уже использован — запросите новый на почту.")))

    batch = _store(settings).load()
    if batch is None or batch.is_expired():
        return HTMLResponse(page("Подтверждение", _nothing_to_confirm(settings, batch)))
    if _stale_session(session, batch):
        return HTMLResponse(_code_screen(settings, note(
            "Список успел обновиться — это уже другая партия документов. "
            "Введите код из последнего письма.", "warn")))
    return HTMLResponse(_list_screen(settings, batch, session=session))


@router.post("/confirm/preview", response_class=HTMLResponse)
async def confirm_preview(request: Request):
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    session = _GATE.session(request.cookies.get(SESSION_COOKIE), settings.confirm_session_minutes)
    if session is None:
        return HTMLResponse(_code_screen(settings, note("Сессия закрылась. Введите код ещё раз.")))

    form = await _Form.read(request)
    store = _store(settings)
    batch = store.load()
    if batch is None:
        return HTMLResponse(page("Подтверждение", _nothing_to_confirm(settings, batch)))
    if _stale_session(session, batch):
        return HTMLResponse(_code_screen(settings, note(
            "Список успел обновиться — это уже другая партия документов. "
            "Введите код из последнего письма.", "warn")))

    if session.email_verified:
        # Адрес из памяти сервера: присланное значение не читаем вовсе, иначе
        # вся затея свелась бы к полю, которое правится в отладчике.
        email = session.email
    else:
        email = form.get("email").strip().lower()
    if email not in _recipients(settings):
        # Сообщение нейтральное: по нему нельзя понять, состоит адрес в
        # рассылке или нет. Сама попытка при этом не теряется — она попадает и
        # в журнал, и в лог, чтобы подбор чужих адресов был заметен.
        store.record("адрес отклонён", адрес=email, адрес_браузера=_client(request))
        logger.warning(
            "Подтверждение отклонено: адрес %r не в списке рассылки, браузер %s",
            email,
            _client(request),
        )
        return HTMLResponse(_list_screen(settings, batch, note(
            "Введите адрес почты.", "bad"), session=session))

    chosen = _selected(form, batch)
    if not chosen:
        return HTMLResponse(_list_screen(settings, batch, note(
            "Ни один документ не отмечен.", "warn"), session=session))

    session.email = email
    # Показываем именно два поля, которые уйдут в 1С, и по отдельности: человек
    # подтверждает запись конкретных значений в конкретные реквизиты, а не
    # абстрактную «связь документов».
    rows = "".join(
        f"<tr><td>{esc(_candidate_number(item, ref))}</td>"
        f"<td>{esc(item.saby_document.get('incoming_number'))}</td>"
        f"<td>{esc(_ru_date(item.saby_document.get('incoming_date')))}</td></tr>"
        for item, ref in chosen
    )
    hidden = "".join(
        f'<input type=hidden name="apply_{esc(item.key)}" value="{esc(ref)}">'
        for item, ref in chosen
    )
    body = f"""
      <h1>Подтвердите запись</h1>
      <p class=lead>В 1С (УТ), в поступлении доп. расходов, будут заполнены
         два поля — «Номер входящего документа» и «Дата входящего документа»:</p>
      <div class=card><table>
        <tr><th>Поступление доп. расходов</th>
            <th>Номер входящего документа</th>
            <th>Дата входящего документа</th></tr>
        {rows}
      </table></div>
      {note("Больше запрос ничего не содержит: в нём только ссылка на документ "
            "и эти два значения. После записи программа перечитает документ "
            "из 1С и покажет, что в нём получилось.")}
      <form method=post action="/confirm/apply">
        {hidden}
        <div class=row>
          <button type=submit>Записать номер и дату в 1С</button>
          <a href="/confirm/list"><button class=secondary type=button>Вернуться</button></a>
        </div>
      </form>
    """
    return HTMLResponse(page("Подтвердите запись", body))


@router.post("/confirm/apply", response_class=HTMLResponse)
async def confirm_apply(request: Request):
    settings = _settings(request)
    if (refusal := _disabled(settings)) is not None:
        return refusal
    session = _GATE.session(request.cookies.get(SESSION_COOKIE), settings.confirm_session_minutes)
    if session is None:
        return HTMLResponse(_code_screen(settings, note("Сессия закрылась. Введите код ещё раз.")))

    form = await _Form.read(request)
    store = _store(settings)
    batch = store.load()
    if batch is None:
        return HTMLResponse(page("Подтверждение", _nothing_to_confirm(settings, batch)))
    if _stale_session(session, batch):
        return HTMLResponse(_code_screen(settings, note(
            "Список успел обновиться — это уже другая партия документов. "
            "Введите код из последнего письма.", "warn")))

    service = SyncService(settings, confirmations=store)
    rows = []
    for name, value in form.pairs():
        if not name.startswith("apply_"):
            continue
        item = batch.item(name[len("apply_"):])
        if item is None:
            continue

        result = service.apply_confirmation(item, str(value))
        if result.status in SETTLED_STATUSES:
            store.remove_item(item.key)
        store.record(
            "подтверждение",
            код=batch.code_hash,
            кому_ушёл_код=session.email,
            # Откуда взялся адрес. «Код уходил на него» — свидетельство:
            # письмо пришло в тот ящик, и код одноразовый. «Назван в форме» —
            # слова человека, и в журнале это должно выглядеть иначе.
            адрес_подтверждён="код уходил на него" if session.email_verified else "назван в форме",
            адрес_браузера=_client(request),
            saby={"id": item.saby_document.get("id"),
                  "номер": item.saby_document.get("incoming_number")},
            выбран_1с={"ref": str(value), "номер": result.additional_expense_number},
            итог=result.status,
            осталось_в_списке=result.status not in SETTLED_STATUSES,
        )
        rows.append((result, item))

    return HTMLResponse(page("Результат", _result_screen(rows)))


def _result_screen(rows: list) -> str:
    if not rows:
        return note("Ничего не записано: ни один документ не выбран.", "warn")

    #: Формулировки исходов: «уже заполнено» — это нормальный ход дела, а не сбой.
    tone = {
        "written": ("good", "записано"),
        "already_filled": ("warn", "пока вы выбирали, реквизиты заполнили вручную. Ничего не изменено"),
        "conflict": ("warn", "в 1С указан другой входящий документ. Ничего не изменено, посмотрите вручную"),
        "manual": ("warn", "запись не выполнена"),
    }
    lines = []
    for result, item in rows:
        kind, text = tone.get(result.status, ("bad", status_label(result.status).lower()))
        # Если позиция осталась в партии, об этом надо сказать: иначе человек не
        # понимает, пропало его решение или попытку можно повторить.
        again = (
            ""
            if result.status in SETTLED_STATUSES
            else " Предложение осталось в списке — попытку можно повторить."
        )
        lines.append(
            f'<div class="note {kind}"><b>УПД '
            f'{esc(item.saby_document.get("incoming_number"))}</b> → '
            f'{esc(result.additional_expense_number)} — {esc(text)}.{again}'
            f'<br><span class=muted>{esc(result.message)}</span></div>'
        )
    return (
        "<h1>Результат</h1>" + "".join(lines) +
        '<div class=row><a href="/confirm/list"><button class=secondary type=button>'
        "К списку</button></a></div>"
    )


def _selected(form, batch: PendingBatch) -> list:
    keys = form.all("selected")
    chosen = []
    for key in keys:
        item = batch.item(key)
        if item is None:
            continue
        ref = form.get(f"choice_{key}").strip()
        if ref:
            chosen.append((item, ref))
    return chosen


def _candidate_number(item, ref: str) -> str:
    for raw in item.candidates:
        if raw.get("ref") == ref:
            return str(raw.get("number") or ref)
    return ref
