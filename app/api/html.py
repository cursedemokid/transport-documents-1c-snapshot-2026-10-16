"""Сборка страниц формы подтверждения.

Шаблонизатора в проекте нет и заводить его не за чем: экранов три, а проект
принципиально живёт на стандартной библиотеке — как `urllib` вместо `requests` и
самодельный XLSX вместо `openpyxl`.

Единственное правило: **всё, что пришло снаружи, проходит через `esc`**. В
страницу попадают наименования контрагентов из 1С и адреса, введённые в форму,
то есть чужой текст.
"""

from __future__ import annotations

from html import escape

#: Стиль держим здесь, а не в отдельном файле: он маленький, а лишний запрос к
#: серверу за статикой — лишняя точка отказа.
_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px;
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: #f6f7f9; color: #1a1d21;
}
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 8px; }
p.lead { color: #55606b; margin: 0 0 24px; }
.card {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 12px;
  padding: 20px; margin-bottom: 16px;
}
.doc { font-weight: 600; margin-bottom: 4px; }
.muted { color: #6b7580; font-size: 14px; }
label.choice {
  display: block; padding: 10px 12px; margin-top: 8px;
  border: 1px solid #e3e6ea; border-radius: 8px; cursor: pointer;
}
label.choice:hover { border-color: #9aa4b0; background: #fafbfc; }
input[type=text], input[type=email] {
  font: inherit; padding: 10px 12px; border: 1px solid #c8cfd6;
  border-radius: 8px; width: 100%; max-width: 360px;
}
input.code { font-family: ui-monospace, monospace; letter-spacing: 3px; text-transform: uppercase; }
button {
  font: inherit; font-weight: 600; padding: 11px 20px; border-radius: 8px;
  border: 1px solid transparent; background: #1f6feb; color: #fff; cursor: pointer;
}
button.secondary { background: #fff; color: #1a1d21; border-color: #c8cfd6; }
button:hover { filter: brightness(1.05); }
.note { background: #eef4ff; border: 1px solid #cfe0ff; border-radius: 8px; padding: 12px 14px; margin: 16px 0; }
.warn { background: #fff6e6; border-color: #ffd699; }
.bad  { background: #fdecec; border-color: #f6bcbc; }
.good { background: #e9f7ec; border-color: #b7e2c1; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 20px; }
table { border-collapse: collapse; width: 100%; font-size: 15px; }
td, th { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f3; vertical-align: top; }
@media (prefers-color-scheme: dark) {
  body { background: #15181c; color: #e6e9ec; }
  .card { background: #1c2026; border-color: #2c333b; }
  label.choice { border-color: #2c333b; }
  label.choice:hover { background: #222831; border-color: #47505b; }
  input[type=text], input[type=email] { background: #12151a; color: #e6e9ec; border-color: #3a424c; }
  button.secondary { background: #1c2026; color: #e6e9ec; border-color: #3a424c; }
  .note { background: #16233a; border-color: #23385c; }
  .warn { background: #2e2413; border-color: #5a4415; }
  .bad  { background: #331a1a; border-color: #6b2b2b; }
  .good { background: #142b1b; border-color: #285c38; }
  .muted, p.lead { color: #9aa4b0; }
  td, th { border-color: #262c34; }
}
"""


def esc(value: object) -> str:
    """Экранирование с приличным видом пустого значения."""
    if value is None or value == "":
        return "—"
    return escape(str(value), quote=True)


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def note(text: str, kind: str = "") -> str:
    return f'<div class="note {kind}">{text}</div>'
