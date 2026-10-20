from __future__ import annotations

import csv
import logging
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from app.schemas.sync import SyncItemResult, SyncRunResponse
from app.services.report_labels import status_group, status_label

logger = logging.getLogger(__name__)


def _writes_label(report: SyncRunResponse) -> str:
    """Тестовый режим и запрет записи — разные вещи, и в отчёте их надо различать."""
    return "разрешена" if report.writes_enabled else "запрещена настройкой"


#: Реестр СБИС не отсортирован по дате документа, поэтому обрезка — это дыра в
#: выборке, а не «не дочитали старое». Молчать о ней нельзя.
_TRUNCATED_WARNING = (
    "Реестр СБИС прочитан не полностью (SABY_MAX_PAGES): выборка неполна."
)

HEADERS = [
    "Статус",
    "Попытка",
    "ID Saby",
    "Номер Saby",
    "Дата Saby",
    "Контрагент",
    "ИНН контрагента",
    "Сумма",
    "Ссылка документа 1С",
    "Номер документа 1С",
    "Ссылка заказа поставщику",
    "Номер заказа поставщику",
    "Склад",
    "Группа склада",
    "Вид доп. расхода",
    "Описание",
    "Группа отчета",
]


#: Имена файлов отчётов: по ним же отбираются кандидаты на удаление, чтобы
#: ротация не тронула ничего постороннего, положенного в тот же каталог.
REPORT_FILE_PREFIX = "saby_1c_report_"


def prune_reports(report_dir: str, keep: int) -> list[Path]:
    """Оставляет в каталоге последние `keep` отчётов, остальные удаляет.

    Каталог растёт на файл за прогон и никем не чистится: за полгода ежедневных
    прогонов это сотни файлов, среди которых не найти нужный. Удаляются только
    файлы с нашим префиксом — чужое в каталоге не трогаем.
    """
    if keep <= 0:
        return []
    directory = Path(report_dir)
    if not directory.is_dir():
        return []

    files = sorted(
        (path for path in directory.glob(f"{REPORT_FILE_PREFIX}*") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    removed = []
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError as error:
            # Чистка не должна ронять прогон: отчёт уже сохранён и отправлен.
            logger.warning("Не удалось удалить старый отчёт %s: %s", path, error)
        else:
            removed.append(path)
    return removed


def save_report_file(report: SyncRunResponse, report_dir: str, report_format: str) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe_request_id = "".join(ch if ch.isalnum() or ch == "-" else "_" for ch in report.request_id)
    extension = "xlsx" if report_format == "xlsx" else "csv"
    path = (
        directory
        / f"{REPORT_FILE_PREFIX}{report.date_from}_{report.date_to}_{safe_request_id}.{extension}"
    )

    if extension == "xlsx":
        _write_xlsx(path, report)
    else:
        _write_csv(path, report)

    return path


def _row(item: SyncItemResult) -> list[str]:
    return [
        status_label(item.status),
        str(item.attempt),
        item.saby_id or "",
        item.saby_number or "",
        item.saby_date.isoformat() if item.saby_date else "",
        item.counterparty_name or "",
        item.counterparty_inn or "",
        str(item.amount) if item.amount is not None else "",
        item.additional_expense_ref or "",
        item.additional_expense_number or "",
        item.supplier_order_ref or "",
        item.supplier_order_number or "",
        item.warehouse_name or "",
        item.warehouse_group or "",
        item.additional_expense_type or "",
        item.message,
        status_group(item.status),
    ]


def _write_csv(path: Path, report: SyncRunResponse) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["ID запуска", report.request_id])
        writer.writerow(["Период", f"{report.date_from} - {report.date_to}"])
        writer.writerow(["Тестовый режим", "Да" if report.dry_run else "Нет"])
        writer.writerow(["Запись в 1С", _writes_label(report)])
        if report.saby_registry_truncated:
            writer.writerow(["ВНИМАНИЕ", _TRUNCATED_WARNING])
        writer.writerow([])
        writer.writerow(HEADERS)
        for item in report.results:
            writer.writerow(_row(item))


def _write_xlsx(path: Path, report: SyncRunResponse) -> None:
    rows = [
        ["ID запуска", report.request_id],
        ["Период", f"{report.date_from} - {report.date_to}"],
        ["Тестовый режим", "Да" if report.dry_run else "Нет"],
        ["Запись в 1С", _writes_label(report)],
        *([["ВНИМАНИЕ", _TRUNCATED_WARNING]] if report.saby_registry_truncated else []),
        [],
        HEADERS,
        *[_row(item) for item in report.results],
    ]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml())
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(rows))


#: Excel не принимает в ячейке больше 32767 символов.
MAX_CELL_LENGTH = 32767

#: XML 1.0 не допускает управляющие символы, кроме табуляции и переводов строки.
_FORBIDDEN_XML_CHARS = dict.fromkeys(
    [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF]
)


def sanitize_xml_text(value: object) -> str:
    """Готовит значение к вставке в ячейку XLSX.

    В колонку «Описание» попадает сырой текст исключения 1С, а он может содержать
    управляющие символы. `escape` их не трогает, и Excel отказывался открывать
    такой файл — узнать об этом можно было только от получателя отчёта.
    """
    text = str(value).translate(_FORBIDDEN_XML_CHARS)
    if len(text) > MAX_CELL_LENGTH:
        text = text[: MAX_CELL_LENGTH - 1] + "…"
    return text


def _worksheet_xml(rows: list[list[str]]) -> str:
    row_xml = []
    for row_index, values in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(values, start=1):
            ref = f"{_column_name(col_index)}{row_index}"
            cells.append(
                # xml:space="preserve" — иначе Excel срезает пробелы по краям.
                f'<c r="{ref}" t="inlineStr"><is>'
                f'<t xml:space="preserve">{escape(sanitize_xml_text(value))}</t>'
                f"</is></c>"
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://internal.internal.internal.example.invalid/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://internal.internal.internal.example.invalid/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://internal.internal.internal.example.invalid/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://internal.internal.internal.example.invalid/spreadsheetml/2006/main" '
        'xmlns:r="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships">'
        '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def _workbook_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://internal.internal.internal.example.invalid/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )


def attachment_mime_type(path: Path) -> str:
    if path.suffix.lower() == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "text/csv"
