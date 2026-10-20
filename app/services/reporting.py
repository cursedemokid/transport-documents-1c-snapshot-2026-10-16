import logging
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app.config import Settings
from app.schemas.sync import SyncRunResponse
from app.services.report_file import attachment_mime_type, prune_reports, save_report_file
from app.services.report_labels import REPORT_GROUP_ORDER, status_group, status_label

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportDelivery:
    """Результат доставки отчёта.

    Раньше `send()` возвращал голый bool, и «не настроено» было неотличимо от
    «отправлено». Теперь причина видна и в ответе API, и в логе.
    """

    sent: bool
    backend: str
    reason: str
    path: Path | None = None


class ReportService:
    def __init__(self, settings: Settings):
        self._settings = settings

    def save(self, report: SyncRunResponse) -> Path:
        """Файл отчёта пишется всегда, независимо от того, уходит ли письмо."""
        path = save_report_file(
            report,
            self._settings.report_dir,
            self._settings.report_format,
        )
        removed = prune_reports(self._settings.report_dir, self._settings.report_keep_files)
        if removed:
            logger.info("Удалено старых отчётов: %d.", len(removed))
        return path

    def send(self, report: SyncRunResponse) -> ReportDelivery:
        report_path = self.save(report)

        backend = self._settings.report_backend
        if backend == "none":
            # Раньше здесь возвращался True, и в ответе API стояло
            # report_sent=true, хотя не отправлялось ничего.
            reason = "отправка отключена настройкой REPORT_BACKEND=none"
            logger.info("Отчёт сохранён в %s, письмо не отправляется: %s", report_path, reason)
            return ReportDelivery(sent=False, backend=backend, reason=reason, path=report_path)

        message = EmailMessage()
        message["Subject"] = (
            f"{self._settings.smtp_subject_prefix} отчет синхронизации "
            f"{report.date_from} - {report.date_to}"
        )
        message["From"] = self._settings.smtp_from or self._settings.smtp_username or ""
        message["To"] = ", ".join(self._settings.smtp_to)
        if self._settings.smtp_cc:
            message["Cc"] = ", ".join(self._settings.smtp_cc)
        # Решение о вложении принимается до сборки текста: если файл не влезает,
        # об этом должно быть сказано в самом письме. Молчаливый пропуск оставлял
        # получателя с письмом без файла и без единого намёка почему.
        refusal = self._attachment_refusal(report_path)
        message.set_content(self._build_text(report, attachment_refusal=refusal))
        if self._settings.smtp_attach_report and refusal is None:
            self._attach_report(message, report_path)

        return self._deliver(message, report_path)

    def send_test_message(self, recipients: list[str] | None = None) -> ReportDelivery:
        """Проверочное письмо — тем же путём, которым уходит отчёт.

        Раньше это делал отдельный скрипт со своей копией разбора настроек, и
        копия расходилась с боевым путём на краях: `smtp+console` она резолвила
        в `both` и письмо отправляла, а продуктив — в `console` и не отправлял.
        То есть проверка проходила ровно тогда, когда боевой путь был сломан.
        """
        to = recipients if recipients else list(self._settings.smtp_to)
        backend = self._settings.report_backend

        message = EmailMessage()
        message["Subject"] = f"{self._settings.smtp_subject_prefix} проверка доставки отчётов"
        message["From"] = self._settings.smtp_from or self._settings.smtp_username or ""
        message["To"] = ", ".join(to)
        message.set_content(
            "Проверочное письмо сервиса переноса транспортных документов Saby → 1С.\n"
            "Если оно пришло, отчёты о синхронизации будут доходить тем же путём.\n"
        )

        if backend == "none":
            reason = "отправка отключена настройкой REPORT_BACKEND=none"
            logger.info("Проверочное письмо не отправляется: %s", reason)
            return ReportDelivery(sent=False, backend=backend, reason=reason)

        return self._deliver(message, None, recipients=to)

    def send_confirmation_code(self, code: str, recipients: list[str]) -> ReportDelivery:
        """Высылает код доступа к форме подтверждения.

        Отдельное короткое письмо: сотрудник запросил код, значит прежнее письмо
        с отчётом он не нашёл, и присылать отчёт заново незачем.
        """
        message = EmailMessage()
        message["Subject"] = f"{self._settings.smtp_subject_prefix} код для подтверждения разноски"
        message["From"] = self._settings.smtp_from or self._settings.smtp_username or ""
        message["To"] = ", ".join(recipients)
        lines = [
            f"Код доступа: {code}",
            "",
            f"Действует {self._settings.confirm_code_ttl_hours} ч и срабатывает один "
            "раз: как только он откроет страницу, он погаснет.",
            "Если страница закроется — запросите на форме новый код, письмо придёт сразу.",
            "Прежний код, если он у вас был, больше не работает.",
        ]
        if self._settings.confirm_base_url:
            lines += ["", f"Форма: {self._settings.confirm_base_url}/confirm"]
        message.set_content("\n".join(lines))

        if self._settings.report_backend == "none":
            reason = "отправка отключена настройкой REPORT_BACKEND=none"
            logger.info("Код не отправлен: %s", reason)
            return ReportDelivery(sent=False, backend="none", reason=reason)

        return self._deliver(message, None, recipients=recipients)

    def _deliver(
        self,
        message: EmailMessage,
        report_path: Path | None,
        recipients: list[str] | None = None,
    ) -> ReportDelivery:
        backend = self._settings.report_backend
        to = recipients if recipients is not None else list(self._settings.smtp_to)
        # Исходы считаются раздельно. Раньше печать в консоль ставила общий
        # признак доставки, и при REPORT_BACKEND=both упавший SMTP отчитывался
        # как «отправлено» — то есть проверка врала ровно там, где проверяют.
        printed = False
        smtp_sent = False
        reasons: list[str] = []

        if backend in {"console", "both"}:
            self._send_console(message, report_path)
            printed = True
            reasons.append("напечатано в консоль")

        if backend in {"smtp", "both"}:
            missing = self._missing_smtp_settings(to)
            if missing:
                # Молчаливый отказ здесь означал, что об отсутствии отчёта
                # никто не узнает: ни в ответе API, ни в логе следа не было.
                reason = "SMTP не настроен: " + ", ".join(missing)
                logger.error("Письмо не отправлено. %s", reason)
                reasons.append(reason)
            else:
                try:
                    self._send_smtp(message, to)
                except (OSError, smtplib.SMTPException) as error:
                    # Сетевой сбой — такой же «не доставлено», как и
                    # ненастроенный SMTP. Причина должна быть видна в ответе API,
                    # а не только в трейсбеке.
                    reason = f"ошибка отправки на {self._settings.smtp_host}: {error}"
                    logger.error("Письмо не отправлено. %s", reason)
                    reasons.append(reason)
                else:
                    smtp_sent = True
                    reasons.append(f"отправлено на {', '.join(to)}")

        # Просили письмо — значит доставку решает только SMTP.
        return ReportDelivery(
            sent=smtp_sent if backend in {"smtp", "both"} else printed,
            backend=backend,
            reason="; ".join(reasons),
            path=report_path,
        )

    def _missing_smtp_settings(self, recipients: list[str]) -> list[str]:
        missing = []
        if not self._settings.smtp_host:
            missing.append("не задан SMTP_HOST")
        if not recipients:
            missing.append("не заданы получатели (ERROR_EMAIL_TO / SMTP_TO)")
        return missing

    def _send_smtp(self, message: EmailMessage, recipients: list[str]) -> None:
        smtp_class = smtplib.SMTP_SSL if self._settings.smtp_use_ssl else smtplib.SMTP
        # Без таймаута зависший SMTP держит поток планировщика, а поток
        # asyncio.to_thread не отменяется — процесс перестаёт гаситься вообще.
        with smtp_class(
            self._settings.smtp_host,
            self._settings.smtp_port,
            timeout=self._settings.smtp_timeout_seconds,
        ) as smtp:
            if self._settings.smtp_use_tls and not self._settings.smtp_use_ssl:
                smtp.starttls()
            if self._settings.smtp_username and self._settings.smtp_REDACTED:
                smtp.login(self._settings.smtp_username, self._settings.smtp_REDACTED)
            smtp.send_message(message, to_addrs=self._all_recipients(recipients))

    def _send_console(self, message: EmailMessage, report_path: Path | None) -> None:
        print("=" * 78, file=sys.stdout)
        print("Console report backend: outgoing email was not sent.", file=sys.stdout)
        print(f"Subject: {message['Subject']}", file=sys.stdout)
        print(f"From: {message['From']}", file=sys.stdout)
        print(f"To: {message['To']}", file=sys.stdout)
        if report_path is not None:
            print(f"Report file: {report_path}", file=sys.stdout)
        print("-" * 78, file=sys.stdout)
        body = message.get_body(preferencelist=("plain",))
        print(body.get_content() if body else "", file=sys.stdout)
        print("=" * 78, file=sys.stdout)

    def _all_recipients(self, recipients: list[str]) -> list[str]:
        return [
            *recipients,
            *self._settings.smtp_cc,
            *self._settings.smtp_bcc,
        ]

    def _attachment_refusal(self, report_path: Path) -> str | None:
        """Почему файл не попадёт в письмо. None — попадёт.

        Отдельный метод, потому что ответ нужен раньше сборки текста: причина
        должна быть в самом письме, а не только в логе.
        """
        if not self._settings.smtp_attach_report:
            return None
        limit_mb = self._settings.smtp_max_attachment_mb
        size = report_path.stat().st_size
        if size <= limit_mb * 1024 * 1024:
            return None
        reason = (
            f"Файл отчёта не вложен: {size / 1024 / 1024:.1f} МБ больше предела "
            f"SMTP_MAX_ATTACHMENT_MB={limit_mb}. Отчёт лежит в {report_path}."
        )
        logger.warning("%s", reason)
        return reason

    def _attach_report(self, message: EmailMessage, report_path: Path) -> None:
        maintype, subtype = attachment_mime_type(report_path).split("/", 1)
        message.add_attachment(
            report_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=report_path.name,
        )

    def _confirmation_block(self, code: str) -> str:
        """Раздел письма с кодом доступа к форме подтверждения.

        Программа ничего не записывает сама, поэтому без этого раздела отчёт
        становится списком того, что никогда не произойдёт.
        """
        lines = [
            "",
            "Часть документов ждёт вашего решения.",
            f"Код доступа: {code}",
            f"Код действует {self._settings.confirm_code_ttl_hours} ч и срабатывает "
            "один раз: как только он откроет страницу, он погаснет.",
            "Если страница закроется — запросите на форме новый код, он придёт сразу.",
        ]
        if self._settings.confirm_base_url:
            lines.append(f"Форма подтверждения: {self._settings.confirm_base_url}/confirm")
        return "\n".join(lines)

    def _build_text(self, report: SyncRunResponse, attachment_refusal: str | None = None) -> str:
        lines = [
            "Отчет синхронизации Saby/1C",
            f"ID запуска: {report.request_id}",
            f"Период: {report.date_from} - {report.date_to}",
            f"Тестовый режим: {'Да' if report.dry_run else 'Нет'}",
            f"Запись в 1С: {'разрешена' if report.writes_enabled else 'запрещена настройкой'}",
            f"Документов Saby: {report.total_saby_documents}"
            + (f" (обработано {report.processed_documents})"
               if report.processed_documents != report.total_saby_documents else ""),
            "",
            "Итоги по статусам:",
        ]
        if report.skipped_saby_events:
            lines.insert(
                -2,
                f"Не удалось разобрать событий Saby: {report.skipped_saby_events}",
            )
        if report.confirmation_code:
            lines.insert(-2, self._confirmation_block(report.confirmation_code))
        if attachment_refusal:
            lines.insert(-2, attachment_refusal)
        if report.saby_registry_truncated:
            lines.insert(
                -2,
                "ВНИМАНИЕ: реестр СБИС прочитан не полностью (упёрлись в SABY_MAX_PAGES). "
                "Выборка неполна, часть документов периода не увидена.",
            )
        # Группировка есть в файле отчёта (колонка «Группа отчета»), но в письме
        # её не было: получатель видел только плоские счётчики по статусам.
        grouped: dict[str, list[tuple[str, int]]] = {}
        for status, count in sorted(report.counts.items()):
            grouped.setdefault(status_group(status), []).append((status, count))

        if not grouped:
            lines.append("- документов не обработано")
        for group in REPORT_GROUP_ORDER:
            rows = grouped.pop(group, [])
            if not rows:
                continue
            lines.append("")
            lines.append(f"{group}: {sum(count for _, count in rows)}")
            for status, count in rows:
                lines.append(f"  - {status_label(status)}: {count}")
        for group, rows in grouped.items():
            lines.append("")
            lines.append(f"{group}: {sum(count for _, count in rows)}")
            for status, count in rows:
                lines.append(f"  - {status_label(status)}: {count}")

        return "\n".join(lines)
