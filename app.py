"""WB Docs Updater — дашборд автоматической привязки разрешительных документов
(деклараций и сертификатов соответствия) к существующим карточкам товаров
Wildberries через Content API v2 с сохранением всех текущих данных карточек.

Запуск:  streamlit run app.py
"""

from __future__ import annotations

import html
import io
import json
import threading
import time
from datetime import datetime
from typing import Any, Iterable, Mapping

import pandas as pd
import streamlit as st

import sample_generator as sg
import wb_client as wb

# ---------------------------------------------------------------------------
# Константы интерфейса
# ---------------------------------------------------------------------------

APP_TITLE = "WB Docs Updater"
APP_SUBTITLE = 'Привязка деклараций и сертификатов к карточкам Wildberries без потери данных'

MODE_DEMO = "demo"
MODE_REAL = "real"
MODE_LABELS = {
    MODE_REAL: 'Боевой режим (WB API Token)',
    MODE_DEMO: 'Демо-режим (Песочница для теста)',
}

STATUS_ICON = {
    wb.STATUS_UPDATED: '✅',
    wb.STATUS_UNCHANGED: '🟦',
    wb.STATUS_NEEDS_REVIEW: '🟡',
    wb.STATUS_MISMATCH: '🟠',
    wb.STATUS_NOT_FOUND: '🔎',
    wb.STATUS_NO_DATA: '⚪',
    wb.STATUS_INVALID: '⛔',
    wb.STATUS_DUPLICATE: '♻️',
    wb.STATUS_TOKEN: '🔑',
    wb.STATUS_STOPPED: '⏹️',
    wb.STATUS_ERROR: '❌',
}

METRIC_TILES = [
    ("total", 'Всего артикулов в файле', "#6366F1", '🧾'),
    ("found", 'Найдено в WB', "#0EA5E9", '🔍'),
    ("updated", 'Успешно обновлено', "#10B981", '✅'),
    ("errors", 'Ошибки / пропущено', "#EF4444", '⚠️'),
]

# Характеристики документов из справочника WB по subjectID 105 (одежда):
# именно эти charcID приходят в diff, когда WB хранит имя характеристики пустым.
DOCUMENT_CHARC_LABELS = {
    15001135: wb.DOC_CHARACTERISTIC_NAMES["declaration"],
    15001138: wb.DOC_CHARACTERISTIC_NAMES["expiry"],
    15001136: wb.DOC_CHARACTERISTIC_NAMES["certificate"],
}
# Если документ целиком выведен выше одной строкой «Документ», его
# характеристики в «Добавлено» только повторяют тот же номер — прячем.
DOCUMENT_CHARC_NAMES = {name.lower() for name in wb.DOC_CHARACTERISTIC_NAMES.values()}

# Поля карточки WB → понятные селлеру подписи (для построчного diff).
DIFF_FIELD_LABELS = {
    "nmID": 'Номер карточки в WB (nmID)',
    "imtID": 'Номер группы карточек (imtID)',
    "nmUUID": 'Внутренний код карточки (nmUUID)',
    "vendorCode": 'Артикул продавца',
    "subjectID": 'ID предмета',
    "subjectName": 'Предмет',
    "title": 'Название карточки',
    "description": 'Описание',
    "brand": 'Бренд',
    "dimensions": 'Габариты, см',
    "sizes": 'Размеры и штрихкоды',
    "photos": 'Фотографии',
    "video": 'Видео',
    "tags": 'Теги',
    "needKiz": 'Маркировка «Честный знак»',
    "characteristics": 'Характеристики',
    "documents": 'Документы (декларации и сертификаты)',
    "createdAt": 'Дата создания карточки',
    'chrtID «': 'Размер · chrtID «',
}

# Служебные поля, о которых селлеру знать незачем.
DIFF_HIDDEN_FIELDS = {"updatedAt"}

# Типы документов WB (поле `type` в documents.items).
DOCUMENT_KIND_LABELS = {
    1: 'Сертификат соответствия',
    2: 'Декларация соответствия',
    3: 'Отказное письмо',
}

LOG_ICONS = {"info": '•', "success": '✔', "warning": '⚠', "error": '✖', "muted": '·'}
LOG_COLORS = {
    "info": "#CBD5E1",
    "success": "#34D399",
    "warning": "#FBBF24",
    "error": "#F87171",
    "muted": "#64748B",
}

CSS = """
<style>
    :root { color-scheme: dark; }
    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1320px; }
    .hero {
        background: linear-gradient(120deg, #4C1D95 0%, #6D28D9 45%, #0F766E 100%);
        border-radius: 22px; padding: 26px 32px; margin-bottom: 22px;
        box-shadow: 0 18px 40px rgba(76, 29, 149, .28);
        border: 1px solid rgba(255, 255, 255, .12);
    }
    .hero h1 { margin: 0; font-size: 2.05rem; font-weight: 800; color: #fff; letter-spacing: -.02em; }
    .hero p { margin: 8px 0 0; color: rgba(255, 255, 255, .82); font-size: 1rem; }
    .hero .pills { margin-top: 16px; }
    .pill {
        display: inline-block; padding: 4px 12px; margin: 0 8px 6px 0; border-radius: 999px;
        background: rgba(255, 255, 255, .14); color: #fff; font-size: .78rem; font-weight: 600;
        border: 1px solid rgba(255, 255, 255, .18);
    }
    .metric-tile {
        border-radius: 18px; padding: 18px 20px; height: 100%;
        background: rgba(148, 163, 184, .10);
        border: 1px solid rgba(148, 163, 184, .22);
        box-shadow: 0 10px 24px rgba(2, 6, 23, .22);
    }
    .metric-tile .mt-top { display: flex; align-items: center; justify-content: space-between; }
    .metric-tile .mt-label { font-size: .82rem; color: #94A3B8; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; }
    .metric-tile .mt-icon {
        width: 34px; height: 34px; border-radius: 12px; display: grid; place-items: center;
        font-size: 1rem; background: rgba(255, 255, 255, .08);
    }
    .metric-tile .mt-value { font-size: 2.15rem; font-weight: 800; margin-top: 8px; line-height: 1.1; }
    .metric-tile .mt-sub { font-size: .8rem; color: #94A3B8; margin-top: 4px; }
    .diff-chip {
        display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .72rem;
        font-weight: 700; margin-right: 6px; letter-spacing: .02em;
    }
    .chip-added { background: rgba(16, 185, 129, .18); color: #6EE7B7; border: 1px solid rgba(16, 185, 129, .35); }
    .chip-changed { background: rgba(245, 158, 11, .18); color: #FCD34D; border: 1px solid rgba(245, 158, 11, .35); }
    .chip-removed { background: rgba(239, 68, 68, .18); color: #FCA5A5; border: 1px solid rgba(239, 68, 68, .35); }
    .chip-kept { background: rgba(59, 130, 246, .18); color: #93C5FD; border: 1px solid rgba(59, 130, 246, .35); }
    .diff-row {
        padding: 10px 12px; border-radius: 12px; margin-bottom: 8px;
        background: rgba(148, 163, 184, .07); border: 1px solid rgba(148, 163, 184, .16);
        font-size: .88rem;
    }
    .diff-row .dr-field { color: #E2E8F0; font-weight: 600; }
    .diff-row .dr-val { color: #94A3B8; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem; word-break: break-word; }
    .diff-row .dr-val2 { color: #E2E8F0; font-family: inherit; font-size: .86rem; word-break: break-word; }
    .diff-chip.chip-neutral { background: rgba(148, 163, 184, .18); color: #CBD5E1; border: 1px solid rgba(148, 163, 184, .35); }
    .diff-chip.chip-ok { background: rgba(16, 185, 129, .18); color: #6EE7B7; border: 1px solid rgba(16, 185, 129, .4); }
    .diff-group { margin: 4px 0 14px; line-height: 1.9; }
    .diff-group .dg-note { color: #94A3B8; font-size: .84rem; margin: 2px 0 8px; }
    .log-box {
        background: #0B1220; border-radius: 14px; padding: 12px 14px; max-height: 340px; overflow-y: auto;
        border: 1px solid rgba(148, 163, 184, .2); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: .8rem; line-height: 1.5;
    }
    .log-box .lt { color: #64748B; margin-right: 8px; }
    .safety-card {
        border-radius: 16px; padding: 16px 18px; background: rgba(16, 185, 129, .08);
        border: 1px solid rgba(16, 185, 129, .28);
    }
    .section-title { font-size: 1.15rem; font-weight: 700; margin: 6px 0 2px; }
    .section-sub { color: #94A3B8; font-size: .86rem; margin-bottom: 10px; }
    .empty-hint {
        border: 1px dashed rgba(148, 163, 184, .35); border-radius: 16px; padding: 22px;
        text-align: center; color: #94A3B8;
    }
</style>
"""


# ---------------------------------------------------------------------------
# Кэш генераторов
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _sample_excel_bytes(rows: int = 1000, clean: bool = False) -> bytes:
    'Тестовый прайс на ``rows`` артикулов в виде байтов .xlsx.'
    frame = sg.generate_sample_dataframe(rows=rows, clean=clean)
    display = frame.copy()
    display["expiry_date"] = [
        "" if (value is None or (isinstance(value, float) and pd.isna(value))) else value
        for value in display["expiry_date"]
    ]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        display.to_excel(writer, index=False, sheet_name='Документы')
        ws = writer.book['Документы']
        from openpyxl.styles import Alignment, Font, PatternFill

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="4F46E5")
            cell.font = Font(color="FFFFFF", bold=True, size=11)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column, width in {"A": 22, "B": 40, "C": 40, "D": 16}.items():
            ws.column_dimensions[column].width = width
        for (cell,) in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=4, max_col=4):
            cell.number_format = "DD.MM.YYYY"
        ws.row_dimensions[1].height = 24
    return buffer.getvalue()


@st.cache_data(show_spinner=False)
def _sample_frame(rows: int = 1000) -> pd.DataFrame:
    return sg.generate_sample_dataframe(rows=rows)


def _report_excel(frame: pd.DataFrame) -> bytes:
    'Итоговый отчёт по артикулам с подсветкой статусов.'
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name='Отчёт')
        ws = writer.book['Отчёт']
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        header_fill = PatternFill("solid", fgColor="312E81")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        colors = {
            wb.STATUS_UPDATED: ("DCFCE7", "166534"),
            wb.STATUS_UNCHANGED: ("DBEAFE", "1E40AF"),
            wb.STATUS_NEEDS_REVIEW: ("FEF3C7", "92400E"),
            wb.STATUS_MISMATCH: ("FFEDD5", "9A3412"),
            wb.STATUS_NOT_FOUND: ("E0E7FF", "3730A3"),
            wb.STATUS_NO_DATA: ("F1F5F9", "475569"),
            wb.STATUS_INVALID: ("FEE2E2", "991B1B"),
            wb.STATUS_DUPLICATE: ("EDE9FE", "5B21B6"),
            wb.STATUS_TOKEN: ("FCE7F3", "9D174D"),
            wb.STATUS_STOPPED: ("E5E7EB", "374151"),
            wb.STATUS_ERROR: ("FECACA", "7F1D1D"),
        }
        status_column = list(frame.columns).index('статус') + 1
        for row_index in range(2, ws.max_row + 1):
            cell = ws.cell(row=row_index, column=status_column)
            fill, font_color = colors.get(str(cell.value), ("FFFFFF", "000000"))
            for column in range(1, ws.max_column + 1):
                ws.cell(row=row_index, column=column).fill = PatternFill("solid", fgColor=fill)
            cell.font = Font(color=font_color, bold=True)
        widths = {
            'строка': 8,
            "vendorCode": 20,
            "nmID": 12,
            'статус': 22,
            'декларация': 36,
            'сертификат': 36,
            'срок действия': 14,
            'проблемы': 40,
            'сообщение': 52,
            'время проверки': 20,
            'время пост-проверки': 20,
        }
        for index, column in enumerate(frame.columns, start=1):
            ws.column_dimensions[get_column_letter(index)].width = widths.get(str(column), 18)
        ws.row_dimensions[1].height = 30
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Живой лог и прогресс
# ---------------------------------------------------------------------------


class LiveView:
    'Пишет строки лога в st.empty-контейнер и двигает st.progress.'

    def __init__(self, log_placeholder: Any, progress_bar: Any, status_placeholder: Any) -> None:
        self.log_placeholder = log_placeholder
        self.progress_bar = progress_bar
        self.status_placeholder = status_placeholder
        self.entries: list[tuple[str, str, str]] = []
        self.last_push = 0.0
        self.value = 0.0

    # --- лог ---
    def log(self, message: str, level: str = "info") -> None:
        self.entries.append((datetime.now().strftime("%H:%M:%S"), level, str(message)))
        now = time.monotonic()
        before_push = self.last_push
        self.last_push = now
        if now - before_push >= 0.12:
            self._render_log()

    def _render_log(self) -> None:
        rows = []
        for stamp, level, message in self.entries[-260:]:
            icon = LOG_ICONS.get(level, '•')
            color = LOG_COLORS.get(level, "#CBD5E1")
            rows.append(
                f'<div><span class="lt">{stamp}</span>'
                f'<span style="color:{color}">{icon} {html.escape(message)}</span></div>'
            )
        self.log_placeholder.markdown(f'<div class="log-box">{"".join(rows)}</div>', unsafe_allow_html=True)

    # --- прогресс ---
    def progress(self, value: float, text: str = "") -> None:
        try:
            value = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        if abs(value - self.value) >= 0.005 or value >= 1.0:
            self.value = value
            self.progress_bar.progress(value)
        if text:
            self.status_placeholder.caption(text)

    def finish(self) -> None:
        self._render_log()
        if self.value < 1.0:
            self.progress_bar.progress(1.0)


def _log_callback(live: Any):
    'Потокобезопасный логгер для Updater: пишет в очередь, рисует в main-потоке.'

    # Streamlit UI нельзя трогать из фонового потока, поэтому в боевом потоке
    # лог копится в списке и перерисовывается вызывающим кодом.
    class _Collector:
        def __init__(self) -> None:
            self.queue: list[tuple[str, str]] = []

        def __call__(self, message: str, level: str = "info") -> None:
            self.queue.append((message, level))

        def progress(self, value: float, text: str = "") -> None:
            self.queue.append((f"\0progress:{value:g}:{text}", "progress"))

    return _Collector()


def _drain(live: LiveView, collector: Any) -> None:
    while collector.queue:
        message, level = collector.queue.pop(0)
        if level == "progress":
            _, value, text = message.split(":", 2)
            live.progress(float(value), text)
        else:
            live.log(message, level)


# ---------------------------------------------------------------------------
# Запуск обновления
# ---------------------------------------------------------------------------


def _run_update(
    mode: str,
    token: str,
    rows: list[wb.DocumentRow],
    batch_size: int,
    delay: float,
    dry_run: bool,
    live_placeholder: Any,
) -> dict[str, Any]:
    'Выполняет полный цикл и возвращает результаты для session_state.'
    log_box, progress_bar, status_line = live_placeholder
    live = LiveView(log_box, progress_bar, status_line)
    log_history: list[tuple[str, str]] = []
    original_log = live.log

    def record(message: str, level: str = "info") -> None:
        log_history.append((str(message), level))
        original_log(message, level)

    live.log = record  # type: ignore[method-assign]

    mode_label = MODE_LABELS[mode]
    live.log(f"Старт · режим: {mode_label} · артикулов в файле: {len(rows)}", "info")
    live.log(
        f"Батч: {batch_size} карточек, пауза между запросами: {delay:g} с"
        + (' · DRY-RUN (без записи)' if dry_run else ""),
        "muted",
    )
    live.progress(0.01, 'Подключаемся к WB Content API…')

    if mode == MODE_DEMO:
        # Демо-клиент живой: его изменения стираются при каждой перерисовке
        # Streamlit. Держим его в session_state, чтобы повторный прогон был
        # честным (второй раз = «Без изменений», как на реальном WB).
        client = st.session_state.get("demo_client")
        if client is None:
            client = st.session_state["demo_client"] = wb.create_client(MODE_DEMO)
        live.log(f"Демо-клиент готов: {len(client.get_all_cards())} карточек в мок-базе WB", "success")
    else:
        client = wb.create_client(MODE_REAL, token)
        try:
            client.check_token()
            live.log('Токен принят: POST /content/v2/get/cards/list доступен', "success")
        except wb.WBTokenError as exc:
            live.log(f"Ошибка доступа: {exc}", "error")
            live.finish()
            results = [
                wb.ArticleResult(row.row_number, row.vendor_code, wb.STATUS_TOKEN, str(exc), source=row)
                for row in rows
            ]
            return {
                "results": results,
                "mode": mode,
                "error": str(exc),
                "started_at": datetime.now(),
                "log": log_history,
            }
        except wb.WBAPIError as exc:
            live.log(f"Ошибка API: {exc}", "error")
            live.finish()
            results = [
                wb.ArticleResult(row.row_number, row.vendor_code, wb.STATUS_ERROR, str(exc), source=row)
                for row in rows
            ]
            return {
                "results": results,
                "mode": mode,
                "error": str(exc),
                "started_at": datetime.now(),
                "log": log_history,
            }

    collector = _log_callback(live)
    effective_delay = delay if mode == MODE_REAL else 0.0
    if mode == MODE_DEMO and delay > 0:
        live.log('Демо-режим: пауза между батчами отключена (локальная эмуляция без лимитов)', "muted")
    updater = wb.Updater(
        client,
        batch_size=batch_size,
        delay=effective_delay,
        log=collector,
        progress=collector.progress,
        dry_run=dry_run,
    )

    # Updater вызывает callbacks из того же потока, но Streamlit-объекты
    # обновляем только здесь — между фазами.
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["results"] = updater.run(rows)
        except Exception as exc:  # noqa: BLE001 - показываем любую ошибку пользователю
            box["error"] = str(exc)
            box["results"] = [
                wb.ArticleResult(row.row_number, row.vendor_code, wb.STATUS_ERROR, str(exc), source=row)
                for row in rows
            ]

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    started = time.monotonic()
    while thread.is_alive():
        _drain(live, collector)
        time.sleep(0.15)
    thread.join()
    _drain(live, collector)

    results = box.get("results") or []
    elapsed = time.monotonic() - started
    stats = wb.summarize(results)
    live.log(
        f"Итог: обновлено {stats['updated']} из {stats['total']} · ошибок/проблем "
        f"{stats['errors']} · заняло {elapsed:.1f} с",
        "success" if not stats["errors"] else "warning",
    )
    live.finish()

    return {
        "results": results,
        "mode": mode,
        "error": box.get("error", ""),
        "started_at": datetime.now(),
        "elapsed": elapsed,
        "stats": stats,
        "dry_run": dry_run,
        "batch_size": batch_size,
        "delay": delay,
        "token_requests": dict(client.stats),
        "log": log_history,
    }


# ---------------------------------------------------------------------------
# Сайдбар
# ---------------------------------------------------------------------------


def render_sidebar() -> dict[str, Any]:
    with st.sidebar:
        st.markdown('### ⚙️ Подключение')
        mode_label = st.radio(
            'Режим работы',
            options=[MODE_REAL, MODE_DEMO],
            format_func=lambda value: MODE_LABELS[value],
            index=1,
            help='Демо-режим работает без ключа на локальной копии карточек WB.',
        )
        token = ""
        if mode_label == MODE_REAL:
            token = st.text_input(
                'API-ключ WB (категория «Контент»)',
                type="password",
                placeholder="eyJhbGciOi...",
                help='Токен создаётся в личном кабинете Wildberries: Настройки → Доступ к API.',
            )
            if token:
                st.caption('🔐 Ключ хранится только в памяти этой сессии.')
            else:
                st.warning('Без ключа боевой режим не запустится.', icon='🔑')
        else:
            st.info(
                'Песочница: используются мок-карточки в формате ответа '
                '`/content/v2/get/cards/list` и локальная эмуляция записи.',
                icon='🧪',
            )

        st.divider()
        st.markdown('### 🚦 Лимиты и скорость')
        batch_size = st.slider(
            'Размер батча (карточек за запрос)',
            min_value=1,
            max_value=wb.UPDATE_BATCH_LIMIT,
            value=min(100, wb.UPDATE_BATCH_LIMIT),
            step=1,
            help=f"WB принимает до {wb.UPDATE_BATCH_LIMIT} карточек в одном POST /cards/update.",
        )
        delay = st.slider(
            'Пауза между запросами, сек',
            min_value=0.0,
            max_value=15.0,
            value=float(wb.UPDATE_RATE_INTERVAL),
            step=0.5,
            help=(
                f"Лимит WB: {wb.UPDATE_RATE_PER_MINUTE} запросов/мин, "
                f"интервал {wb.UPDATE_RATE_INTERVAL:g} с. Меньше 6 с — риск 429."
            ),
        )
        dry_run = st.toggle(
            'Dry-run (показать план, ничего не писать)',
            value=False,
            help='Слияние выполняется полностью, но POST /cards/update не вызывается.',
        )

        st.divider()
        st.markdown('### 🧪 Тестовые данные')
        st.caption('Прайс на 1000 артикулов + 6 «грязных» строк и мок-база из 1000 карточек WB.')
        custom_rows = st.number_input(
            'Сколько артикулов сгенерировать', min_value=10, max_value=5000, value=1000, step=10
        )
        cols = st.columns(2)
        with cols[0]:
            if st.button('Сгенерировать Excel', width="stretch", help='Сгенерирует прайс на 1000 артикулов'):
                st.session_state["sample_rows"] = int(custom_rows)
                st.session_state["sample_bytes"] = _sample_excel_bytes(int(custom_rows))
                st.session_state["sample_name"] = f"wb_test_{int(custom_rows)}.xlsx"
                st.toast('Тестовый Excel готов', icon='🧾')
        with cols[1]:
            if st.button('Загрузить в память', width="stretch", help='Сразу подставит тестовый прайс в анализ'):
                st.session_state["sample_rows"] = int(custom_rows)
                st.session_state["sample_bytes"] = _sample_excel_bytes(int(custom_rows))
                st.session_state["sample_name"] = f"wb_test_{int(custom_rows)}.xlsx"
                st.session_state["upload_source"] = "sample"
                st.session_state.pop("run_report", None)
                st.session_state.pop("last_upload_signature", None)
                st.toast(f"Загружено {int(custom_rows)} артикулов в память", icon='📥')
                st.rerun()

        if st.session_state.get("sample_bytes"):
            st.download_button(
                '⬇️ Скачать тестовый Excel',
                data=st.session_state["sample_bytes"],
                file_name=st.session_state.get("sample_name", "wb_test.xlsx"),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )

        st.divider()
        st.caption(
            f"**Content API:** `{wb.WB_BASE_URL}`\n\n"
            f"Чтение: {wb.GET_CARDS_PATH} · {wb.READ_RATE_PER_MINUTE}/мин\n\n"
            f"Запись: {wb.UPDATE_CARDS_PATH} · {wb.UPDATE_RATE_PER_MINUTE}/мин"
        )

    return {
        "mode": mode_label,
        "token": token,
        "batch_size": int(batch_size),
        "delay": float(delay),
        "dry_run": bool(dry_run),
    }


# ---------------------------------------------------------------------------
# Загрузка прайса
# ---------------------------------------------------------------------------


def _read_price_bytes(data: bytes) -> pd.DataFrame:
    frame = pd.read_excel(io.BytesIO(data), sheet_name=0)
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    'Приводит заголовки к стандартным именам столбцов.'
    aliases = {
        "vendorcode": "vendorCode",
        "vendor": "vendorCode",
        'артикул': "vendorCode",
        'артикул продавца': "vendorCode",
        "declaration_number": "declaration_number",
        "declaration": "declaration_number",
        'номер декларации': "declaration_number",
        'декларация': "declaration_number",
        "certificate_number": "certificate_number",
        "certificate": "certificate_number",
        'номер сертификата': "certificate_number",
        'сертификат': "certificate_number",
        "expiry_date": "expiry_date",
        "expiry": "expiry_date",
        'срок действия': "expiry_date",
        'дата окончания': "expiry_date",
    }
    renamed = {}
    for column in frame.columns:
        key = str(column).strip()
        renamed[column] = aliases.get(key.lower(), key)
    frame = frame.rename(columns=renamed)
    return frame


def load_price() -> tuple[pd.DataFrame | None, list[wb.DocumentRow], str]:
    'Читает прайс из загруженного файла или тестового генератора.'
    frame: pd.DataFrame | None = None
    source_name = ""
    uploaded = st.session_state.get("price_upload")
    if st.session_state.get("upload_source") == "upload" and uploaded is not None:
        data = uploaded.getvalue()
        frame = _read_price_bytes(data)
        source_name = uploaded.name
    elif st.session_state.get("upload_source") == "sample":
        rows = int(st.session_state.get("sample_rows", 1000))
        frame = _sample_frame(rows).copy()
        source_name = f"wb_test_{rows}.xlsx (тестовый)"

    if frame is None:
        return None, [], ""

    frame = _normalize_columns(frame)
    required = {"vendorCode"}
    missing = required - set(frame.columns)
    if missing:
        st.error(
            'В файле нет обязательного столбца **vendorCode**. Ожидаются столбцы: '
            "`vendorCode`, `declaration_number`, `certificate_number`, `expiry_date`."
        )
        return None, [], source_name

    records = frame.to_dict("records")
    rows = [wb.normalize_row(record, index + 2) for index, record in enumerate(records)]
    return frame, rows, source_name


# ---------------------------------------------------------------------------
# Блоки интерфейса
# ---------------------------------------------------------------------------


def render_hero(mode: str) -> None:
    badge = 'Боевой режим' if mode == MODE_REAL else 'Демо / Песочница'
    st.markdown(
        f"""
        <div class="hero">
          <h1>📦 {APP_TITLE}</h1>
          <p>{APP_SUBTITLE}</p>
          <div class="pills">
            <span class="pill">🛡️ Safe-Merge: характеристики не теряются</span>
            <span class="pill">🔁 Content API v2</span>
            <span class="pill">📊 JSON Diff до / после</span>
            <span class="pill">⚙️ {badge}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_tile(label: str, value: Any, color: str, icon: str, subtitle: str = "") -> str:
    return (
        f'<div class="metric-tile">'
        f'<div class="mt-top"><div class="mt-label">{html.escape(label)}</div>'
        f'<div class="mt-icon">{icon}</div></div>'
        f'<div class="mt-value" style="color:{color}">{value}</div>'
        f'<div class="mt-sub">{html.escape(subtitle)}</div>'
        f"</div>"
    )


def render_metrics(
    stats: dict[str, int],
    mode: str,
    run_info: dict[str, Any] | None,
    rows: list[wb.DocumentRow] | None = None,
) -> None:
    # fill_metrics гарантирует «обновлено + ошибки/пропущено == строк в файле»
    # (строки без документов и «уже актуально» больше не выпадают из суммы).
    stats = fill_metrics(rows or [], stats)
    total = stats.get("total", 0)
    columns = st.columns(4, gap="medium")
    subtitles = {
        "total": 'строк в прайсе',
        # «Найдено» — сколько артикулов реально есть в WB, «Обновлено» —
        # сколько записано и прошло пост-проверку. Раньше в «обновлено»
        # подставлялось значение unchanged, и плитка врала после перезагрузки.
        "found": 'ищутся по артикулу продавца',
        "updated": f"{stats.get('unchanged', 0)} уже актуальны",
        "errors": (
            f"без документов {stats.get('no_data', 0)} · "
            f"не найдено {stats.get('not_found', 0)} · "
            f"прочие {max(0, stats.get('errors', 0) - stats.get('no_data', 0) - stats.get('not_found', 0))}"
        ),
    }
    if total and stats.get("found"):
        subtitles["found"] = f"{stats['found'] * 100 / total:.0f}% от файла"
    if run_info is None:
        subtitles["updated"] = 'запустите обновление'
    else:
        subtitles["updated"] = (
            f"dry-run · {run_info.get('elapsed', 0):.1f} с"
            if run_info.get("dry_run")
            else f"пост-проверка WB: ок · {run_info.get('elapsed', 0):.1f} с"
        )
    # Подсказка под плитками: сумма сходится — заказчику это главное.
    subtitles["errors"] += f" · всего {total}"
    for column, (key, label, color, icon) in zip(columns, METRIC_TILES):
        with column:
            st.markdown(metric_tile(label, stats.get(key, 0), color, icon, subtitles[key]), unsafe_allow_html=True)
    if total:
        st.caption(
            f"{stats.get('updated', 0)} обновлено + {stats.get('errors', 0)} ошибок/пропущено = "
            f"{stats.get('updated', 0) + stats.get('errors', 0)} из {total} строк файла"
            + (
                f" · {stats.get('unchanged', 0)} уже актуальны"
                if stats.get("unchanged")
                else ""
            )
            + "."
        )


def render_uploader() -> None:
    st.markdown('<div class="section-title">📄 Прайс с документами</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-sub">Excel (.xlsx) со столбцами '
        "<code>vendorCode</code>, <code>declaration_number</code>, <code>certificate_number</code>, "
        '<code>expiry_date</code>. Допускаются русские заголовки.</div>',
        unsafe_allow_html=True,
    )
    uploaded = st.file_uploader(
        'Перетащите файл .xlsx или выберите на диске',
        type=["xlsx"],
        accept_multiple_files=False,
        label_visibility="collapsed",
    )
    if uploaded is not None:
        signature = (uploaded.name, uploaded.size)
        if st.session_state.get("last_upload_signature") != signature:
            st.session_state["last_upload_signature"] = signature
            st.session_state["price_upload"] = uploaded
            st.session_state["upload_source"] = "upload"
            st.session_state.pop("run_report", None)


def render_preview(frame: pd.DataFrame, rows: list[wb.DocumentRow], source_name: str) -> None:
    empty_articles = sum(1 for row in rows if not row.vendor_code)
    no_docs = sum(1 for row in rows if row.vendor_code and not row.has_payload)
    bad_dates = sum(1 for row in rows if any('не является датой' in issue for issue in row.issues))
    info_cols = st.columns(4)
    info_cols[0].metric('Источник', source_name[:28] or '—')
    info_cols[1].metric('Строк', len(rows))
    info_cols[2].metric('Без документов', no_docs)
    info_cols[3].metric('Пустой артикул / плохая дата', f"{empty_articles} / {bad_dates}")

    view = frame.copy()
    head = display_frame(view.head(50))
    st.dataframe(head, width="stretch", height=285)
    st.caption(f"Показаны первые {len(head)} строк из {len(view)}.")

    issues = [(row.row_number, row.vendor_code or '—', "; ".join(row.issues)) for row in rows if row.issues]
    if issues:
        with st.expander(f"⚠️ Замечания к данным файла ({len(issues)})", expanded=False):
            st.dataframe(
                display_frame(pd.DataFrame(issues, columns=['строка', 'артикул продавца', 'замечание'])),
                width="stretch",
                height=200,
            )


def _short(value: Any, limit: int = 120) -> str:
    text = "" if value is None else json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + '…'


# --- Человеческий язык для технического diff -------------------------------


def _split_charc_field(field: str) -> tuple[str, str] | None:
    """`characteristic «Цвет»` -> ("Цвет", "characteristic"); иначе None.

    Второй элемент — исходный префикс пути (``characteristic`` / ``charcID``),
    чтобы вызывающий код сам решил, как назвать поле по-русски.
    """    
    for prefix in ("characteristic ", "charcID "):
        if field.startswith(prefix):
            return field[len(prefix):].strip().strip('«»'), prefix.strip()
    return None


def _strip_quotes(text: str) -> str:
    return text.strip().strip('«»').strip()


def _friendly_value(value: Any, limit: int = 110) -> str:
    """Значение поля без технического шума.

    `['Синий']` -> `Синий`, словарь габаритов -> `31 × 23 × 4 см`,
    размеры -> `M (2000000000001), L (…), …`.
    """    
    if value is None or value == [] or value == "":
        return '—'
    if isinstance(value, list):
        if not value:
            return '—'
        if all(not isinstance(item, (dict, list)) for item in value):
            text = ", ".join("" if item is None else str(item) for item in value).strip() or '—'
        elif all(isinstance(item, dict) and "chrtID" in item for item in value):
            text = ", ".join(
                f"{item.get('techSize') or '—'} ({', '.join(map(str, item.get('skus') or [])) or 'нет штрихкода'})"
                for item in value
            )
        else:
            text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, dict):
        if {"length", "width", "height"} <= set(value):
            text = f"{value.get('length')} × {value.get('width')} × {value.get('height')} см"
            if value.get("weightBrutto"):
                text += f", {value['weightBrutto']} кг"
        elif "number" in value:
            number = _strip_quotes(str(value.get("number") or ""))
            end = str(value.get("endDate") or "")[:10]
            text = f"№ {number}" + (f" · действует до {end}" if end else "")
        else:
            text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, bool):
        text = 'да' if value else 'нет'
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _charc_number(name: str) -> int | None:
    '`15001135` / `charcID 15001135` -> 15001135; иначе None.'
    text = name.strip()
    if text.lower().startswith("charcid "):
        text = text[len("charcid ") :].strip().strip('«»')
    return int(text) if text.isdigit() else None


def _charc_label(name: str) -> str:
    '`charcID 15001135` -> «Номер декларации соответствия».'
    number = _charc_number(name)
    if number is not None and number in DOCUMENT_CHARC_LABELS:
        return DOCUMENT_CHARC_LABELS[number]
    return name


def _is_document_charc(name: str) -> bool:
    'Характеристика — это номер/срок самого документа (дубль строки «Документ»).'
    number = _charc_number(name)
    if number is not None:
        return number in DOCUMENT_CHARC_LABELS
    return name.strip().lower() in DOCUMENT_CHARC_NAMES


def _is_expiry_charc(name: str) -> bool:
    'Характеристика — это именно срок действия документа.'
    number = _charc_number(name)
    if number is not None:
        return DOCUMENT_CHARC_LABELS.get(number) == wb.DOC_CHARACTERISTIC_NAMES["expiry"]
    return name.strip().lower() == wb.DOC_CHARACTERISTIC_NAMES["expiry"].lower()


def _looks_like_document_number(value: Any) -> bool:
    """Значение характеристики — номер декларации/сертификата (а не срок)?.

    В карточке, где WB не отдал имена характеристик, единственный ориентир —
    формат самого значения: `ЕАЭС N RU Д-RU.РА01.В.00001/26`.
    """    
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("number")
    if not isinstance(value, str):
        return False
    return wb.infer_document_kind(_strip_quotes(value)) is not None


def _expand_charc_paths(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`charcID «15001135»` -> `characteristic «charcID 15001135»`.

    `diff_cards` подписывает смену id характеристики путём `charcID «…»` — это
    та же характеристика, что и строка `characteristic «…»`. Без схлопывания
    id остаётся отдельной строкой с техническим названием и не проходит
    перевод на русский язык в `friendly_diff`.
    """    
    id_paths = {str(entry.get("field") or "") for entry in entries if str(entry.get("field") or "").startswith('charcID «')}
    if not id_paths:
        return entries
    expanded: list[dict[str, Any]] = []
    for entry in entries:
        field = str(entry.get("field") or "")
        if not field.startswith('characteristic «'):
            expanded.append(entry)
            continue
        name = _strip_quotes(field[len('characteristic «') :])
        if f"charcID «{name}»" not in id_paths:
            expanded.append(entry)
            continue
        # Осмысленный id: пустое имя есть ровно у той стороны, где WB его потерял.
        real_id = entry.get("before") or entry.get("after")
        display = name
        if isinstance(real_id, int) and not str(real_id).startswith("charcID"):
            display = f"charcID {real_id}"
        expanded.append({**entry, "field": f"characteristic «{display}»"})
    return expanded


def _merge_charc_row(seen: dict[str, tuple[dict[str, Any], dict[str, str]]], entry: dict[str, Any]) -> None:
    'Складывает строки одной характеристики в одну (без дублей по подписи).'
    texts = {"before_text": _friendly_value(entry.get("before")), "after_text": _friendly_value(entry.get("after"))}
    previous = seen.get(entry["label"])
    if previous is None:
        seen[entry["label"]] = (entry, texts)
        return
    row, old_texts = previous
    # Пустая подпись (`characteristic «Цвет»` без слова «Характеристика») уступает
    # полной; значения берём самые содержательные.
    for key in ("before_text", "after_text"):
        if old_texts[key] in ("", '—') and texts[key] not in ("", '—'):
            old_texts[key] = texts[key]
    if entry["label"].startswith('Характеристика '):
        seen[entry["label"]] = (entry, old_texts)
    else:
        seen[entry["label"]] = (row, old_texts)


def _guess_charc_label(charc_id: int, value: Any) -> str | None:
    """Название по формату значения, когда имя характеристики неизвестно.

    Справочник WB покрывает не все предметы, а имя в `get/cards/list` часто
    пустое — тогда единственный ориентир это само значение.
    """    
    if _looks_like_document_number(value):
        # `infer_document_kind` знает только декларации и сертификаты.
        return (
            wb.DOC_CHARACTERISTIC_NAMES["declaration"]
            if wb.infer_document_kind(_strip_quotes(str(_first_value(value)))) == "declaration"
            else wb.DOC_CHARACTERISTIC_NAMES["certificate"]
        )
    # Срок отличается от номера документа по формату; при неоднозначности
    # id решает: наименьший известный документный id у WB — срок действия.
    if _looks_like_date(value) and charc_id in DOCUMENT_CHARC_LABELS:
        return wb.DOC_CHARACTERISTIC_NAMES["expiry"]
    return None


def _first_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _looks_like_date(value: Any) -> bool:
    text = _strip_quotes(str(_first_value(value) or ""))
    return wb.parse_expiry(text) is not None if text else False


def _resolve_by_category(entries: list[dict[str, Any]], doc_charc_names: Mapping[int, str] | None = None) -> None:
    """Дописывает `_label` служебным строкам без имени (in place).

    Неизвестный предмет: часть id всё равно принадлежит документам. Если среди
    таких id ровно один номер документа и ровно один срок — отличаем их по
    значению (номер длиннее и содержит буквы, срок — дата), чтобы «Номер
    декларации соответствия» не оказался подменён датой. Остальным оставляем
    техническое имя: выдумывать название характеристики нельзя.
    """    
    names = doc_charc_names or {}
    for entry in entries:
        label = names.get(entry["_charc_id"])
        entry["_label"] = label or f"charcID {entry['_charc_id']}"
    known = [entry for entry in entries if names.get(entry["_charc_id"])]
    if known:
        return  # справочник предмета известен — названия уже русские
    numbered = [entry for entry in entries if _looks_like_document_number(entry.get("_value"))]
    dated = [entry for entry in entries if _looks_like_date(entry.get("_value"))]
    if len(numbered) == 1 and len(dated) == 1:
        kind = wb.infer_document_kind(_strip_quotes(str(_first_value(numbered[0]["_value"]))))
        numbered[0]["_label"] = wb.DOC_CHARACTERISTIC_NAMES["declaration" if kind == "declaration" else "certificate"]
        dated[0]["_label"] = wb.DOC_CHARACTERISTIC_NAMES["expiry"]
        return
    # Только одно из двух: тип документа различает формат номера, срок — нет.
    if len(numbered) == 1:
        kind = wb.infer_document_kind(_strip_quotes(str(_first_value(numbered[0]["_value"]))))
        numbered[0]["_label"] = wb.DOC_CHARACTERISTIC_NAMES["declaration" if kind == "declaration" else "certificate"]
    elif len(dated) == 1 and any(DOCUMENT_CHARC_LABELS.get(entry["_charc_id"]) for entry in entries):
        dated[0]["_label"] = wb.DOC_CHARACTERISTIC_NAMES["expiry"]


def _friendly_field(field: str) -> str:
    """Технический путь поля -> подпись на русском.

    Для документов номер живёт в значении, а не в подписи: в нём есть
    точки (`ЕАЭС N RU Д-RU.РА01.В.99999/26`), поэтому разрезать путь по
    последней `.` было бы неверно.
    """    
    text = field
    characteristic = _split_charc_field(text)
    if characteristic is not None:
        name, prefix = characteristic
        if prefix == "characteristic":
            return f"Характеристика «{_charc_label(name)}»"
        return f"ID характеристики «{name}»"
    if '«' in text:
        base, _, rest = text.partition('«')
        base = base.strip()
        inner = _strip_quotes(rest)
        if base.startswith("document "):
            return 'Документ'
        if base == "chrtID":
            return f"Размер · chrtID {inner}"
        if base in DIFF_FIELD_LABELS:
            return f"{DIFF_FIELD_LABELS[base]} «{inner}»"
        return base
    return DIFF_FIELD_LABELS.get(text, text)


def _document_text(entry: dict[str, Any]) -> str:
    'Строка про документ: номер и срок — всё, что нужно селлеру.'
    value = entry.get("after") if entry.get("level") != "removed" else entry.get("before")
    if isinstance(value, dict) and value.get("number"):
        text = f"№ {_strip_quotes(str(value['number']))}"
        end = str(value.get("endDate") or "")[:10]
        if end:
            text += f" · действует до {end}"
        elif value.get("isEndless"):
            text += ' · бессрочно'
        return text
    if isinstance(value, str):
        return f"№ {_strip_quotes(value)}"
    return 'документ'


def _doc_charc_names(article: wb.ArticleResult) -> dict[int, str]:
    """charcID -> русское название по обоим состояниям карточки.

    В карточке, которую WB отдаёт в `get/cards/list`, имя характеристики иногда
    пустое — тогда `diff_cards` подписывает путь как `charcID 15001135`.
    Название берём из её строки (часто оно есть) или из справочника известных
    документных характеристик.
    """    
    names: dict[int, str] = dict(DOCUMENT_CHARC_LABELS)
    for card in (article.before, article.after):
        for item in (card or {}).get("characteristics") or []:
            if not isinstance(item, dict):
                continue
            charc_id, name = _charc_number(str(item.get("id") or "")), _strip_quotes(str(item.get("name") or ""))
            if charc_id is not None and name:
                names.setdefault(charc_id, name)
    return names


def friendly_diff(entries: list[dict[str, Any]], doc_charc_names: Mapping[int, str] | None = None) -> list[dict[str, Any]]:
    """Переводит `wb.diff_cards` на понятный селлеру язык.

    Оставляет служебные ключи (`field`, `level`) нетронутыми и добавляет
    ``label`` — подпись поля, ``before_text`` / ``after_text`` — значения
    строкой.

    - технические пути (`charcID «…»`, `updatedAt`, `document …​.type`)
      отбрасываются: они есть в JSON, но селлеру не нужны;
    - имя характеристики, которой нет в карточке (только id), берётся из
      справочника известных документных характеристик (`15001135` ->
      «Номер декларации соответствия»); если id нет и в справочнике, имя
      восстанавливается по формату значения — номер документа и срок действия
      различимы даже без имени (дали ли карточке id случайный `charcID` WB,
      выдавать номер документа за срок нельзя);
    - строка характеристики одна: смена `id` у характеристики не дублирует
      её в списке отдельной строкой `charcID «…»`;
    - для добавленного документа одна строка вместо четырёх;
    - одинаковые строки «Не тронуто» (brand и характеристика «Бренд»)
      сворачиваются в одну.
    """    
    entries = _expand_charc_paths(list(entries))
    result: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    # label -> (строка, тексты); переведённое имя всегда главнее служебного.
    seen_charcs: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    deferred: list[dict[str, Any]] = []
    for entry in entries:
        field = str(entry.get("field") or "")
        base = field.split(".", 1)[0]
        if field in DIFF_HIDDEN_FIELDS or base in DIFF_HIDDEN_FIELDS:
            continue
        if field.startswith("charcID "):
            continue
        if field.startswith("document ") and field.endswith((".type", ".isEndless")):
            continue
        characteristic = _split_charc_field(field)
        if characteristic is not None:
            number = _charc_number(characteristic[0])
            if number is not None and doc_charc_names and number in doc_charc_names:
                # `characteristic «charcID 15001135»` -> `characteristic «Номер декларации…»`,
                # чтобы `_friendly_field` и проверка дублей работали по-русски.
                field = f"{characteristic[1]} «{doc_charc_names[number]}»"
            if entry.get("level") == "added" and _is_document_charc(characteristic[0]):
                # Новый документ выводится строкой «Документ» с номером и сроком —
                # его характеристики в «Добавлено» только повторяют эту строку.
                continue
            if number is not None:
                # Имя WB не отдал. Решаем после прохода: у настоящей строки
                # этой характеристики имя обычно есть — её подпись и берём.
                deferred.append({**entry, "_charc_id": number, "_value": entry.get("after") or entry.get("before")})
                continue
            _merge_charc_row(seen_charcs, {**entry, "label": _friendly_field(field)})
            continue
        if field.startswith("document "):
            text = _document_text(entry)
            if entry.get("level") == "kept":
                if field.endswith(".endDate") or text in seen_docs:
                    continue  # срок уже показан в строке документа
                seen_docs.add(text)
                result.append(
                    {**entry, "label": 'Документ', "before_text": f"{text} — уже в карточке", "after_text": f"{text} — уже в карточке"}
                )
            elif entry.get("level") == "added":
                result.append({**entry, "label": 'Документ', "before_text": '—', "after_text": text})
            else:
                result.append({**entry, "label": 'Документ', "before_text": text, "after_text": text})
            continue
        result.append(
            {
                **entry,
                "label": _friendly_field(field),
                "before_text": _friendly_value(entry.get("before")),
                "after_text": _friendly_value(entry.get("after")),
            }
        )

    unlabeled: list[dict[str, Any]] = []
    for entry in deferred:
        charc_id = entry["_charc_id"]
        label = DOCUMENT_CHARC_LABELS.get(charc_id) or _guess_charc_label(charc_id, entry.get("_value"))
        if label is None:
            unlabeled.append(entry)
            continue
        _merge_charc_row(seen_charcs, {**entry, "label": f"Характеристика «{label}»"})
    if unlabeled:
        _resolve_by_category(unlabeled, doc_charc_names)
    for entry in unlabeled:
        _merge_charc_row(seen_charcs, {**entry, "label": f"Характеристика «{entry['_label']}»"})

    # Строка характеристики — одна, знала ли её WB по имени или только по id.
    result.extend({**row, **texts} for row, texts in seen_charcs.values())

    merged: list[dict[str, Any]] = []
    seen_kept: set[tuple[str, str]] = set()
    for entry in result:
        if entry["level"] == "kept":
            key = (entry["label"], entry["before_text"])
            if key in seen_kept:
                continue
            seen_kept.add(key)
        merged.append(entry)
    return merged


def fill_metrics(rows: list[wb.DocumentRow], stats: dict[str, int] | None = None) -> dict[str, int]:
    """Счётчики для плиток метрик.

    Плитки «Успешно обновлено» и «Ошибки / пропущено» **всегда в сумме дают
    число строк в файле**: всё, что не записано и не «уже актуально»
    (ошибки, не найдено, без документов, дубликаты, остановка…), попадает
    в «Ошибки / пропущено». Так заказчик сразу видит, что ни одна строка
    не потерялась.
    """    
    total = len(rows) if rows else int((stats or {}).get("total", 0))
    updated = int((stats or {}).get("updated", 0))
    unchanged = int((stats or {}).get("unchanged", 0))
    return {
        "total": total,
        "found": int((stats or {}).get("found", 0)),
        "updated": updated,
        "errors": max(0, total - updated - unchanged),
        "unchanged": unchanged,
        "no_data": sum(1 for row in rows if not row.has_payload),
        "not_found": int((stats or {}).get("not_found", 0)),
        "invalid": int((stats or {}).get("invalid", 0)),
        "problems": int((stats or {}).get("problems", 0)),
    }


def display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Копия таблицы для показа: пустые ячейки (`None`) -> прочерк «—».

    Скачиваемые Excel/CSV не трогаем — там пустая ячейка значит «нет данных».
    """    

    def show(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return value
        if value is None or value == "":
            return '—'
        try:
            if pd.isna(value) is True:  # NaN / NaT / pd.NA
                return '—'
        except (TypeError, ValueError):
            return value
        return value

    view = frame.copy()
    for column in view.columns:
        view[column] = view[column].map(show)
    if "nmID" in view.columns:
        # nmID у ненайденных артикулов пустой — приводим к тексту, иначе
        # Arrow падает на смешанных типах int/str.
        view = view.assign(
            nmID=view["nmID"].map(lambda value: '—' if value is None or value == "" else str(value))
        )
    return view


def _document_items(documents: Any) -> list[dict[str, Any]]:
    """Список документов карточки из `documents` (WB отдаёт `{"items": [...]}`).

    Итерироваться по самому словарю нельзя: тогда «документами» станут его
    ключи (`items`, `isAutofilled`…), а не приложенные файлы.
    """    
    if isinstance(documents, dict):
        documents = documents.get("items")
    if not isinstance(documents, list):
        return []
    return [doc for doc in documents if isinstance(doc, dict)]


def _row_line(entry: dict[str, Any], title: str, css: str) -> str:
    'Одна строка построчного списка изменений (бейдж + поле + значение).'
    badge = {'Добавлено': "+", 'Изменено': '✎', 'Удалено': '✖', 'Не тронуто': '✓'}.get(title, '•')
    label = html.escape(entry["label"])
    if title == 'Не тронуто':
        return (
            f'<div class="diff-row"><span class="diff-chip chip-ok">{badge} ОК</span>'
            f'<span class="dr-field">{label}:</span> '
            f'<span class="dr-val2">{html.escape(entry["before_text"])}</span></div>'
        )
    before, after = entry["before_text"], entry["after_text"]
    if title == 'Добавлено':
        values = f'<span class="dr-val2">+ {html.escape(after)}</span>'
    elif title == 'Удалено':
        values = f'<span class="dr-val">− {html.escape(before)}</span>'
    else:
        values = f'<span class="dr-val">{html.escape(before)}</span> → <span class="dr-val2">{html.escape(after)}</span>'
    return (
        f'<div class="diff-row"><span class="diff-chip {css}">{badge}</span>'
        f'<span class="dr-field">{label}</span><br>{values}</div>'
    )


def _header(label: str, grouped: list[dict[str, Any]], title: str, css: str) -> str:
    rows = "".join(_row_line(entry, title, css) for entry in grouped)
    return f'<div class="diff-chip {css}" style="margin:10px 0 6px">{html.escape(label)}</div><div class="diff-group">{rows}</div>'


def render_diff(result: wb.ArticleResult) -> None:
    st.markdown('<div class="section-title">🛡️ Что изменилось в карточке</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-sub">Слева — что прибавится в карточке, справа — что останется как было. '
        'Старые характеристики (состав, цвет, комплектация), название, описание и габариты не теряются.</div>',
        unsafe_allow_html=True,
    )

    candidates = [
        item
        for item in st.session_state.get("catalog_rows", [])
        if item.get("result") and item.get("status") in (wb.FOUND_STATUSES | {wb.STATUS_NOT_FOUND})
    ]
    if not candidates:
        st.markdown(
            '<div class="empty-hint">Загрузите прайс и запустите обновление — здесь появится построчный дифф.</div>',
            unsafe_allow_html=True,
        )
        return

    labels = {item["label"]: item for item in candidates}
    options = list(labels)
    default_index = 0
    if result.vendor_code:
        for index, option in enumerate(options):
            if option.startswith(result.vendor_code):
                default_index = index
                break
    selected_label = st.selectbox(
        'Артикул для проверки',
        options=options,
        index=default_index,
        help='Доступны артикулы, найденные в WB (план слияния или результат прогона).',
    )
    item = labels[selected_label]
    article: wb.ArticleResult = item["result"]

    diffs = wb.diff_cards(article.before or {}, article.after or {}) if article.before and article.after else []
    friendly = friendly_diff(diffs, _doc_charc_names(article))
    added = [entry for entry in friendly if entry["level"] == "added"]
    changed = [entry for entry in friendly if entry["level"] == "changed"]
    removed = [entry for entry in friendly if entry["level"] == "removed"]
    kept = [entry for entry in friendly if entry["level"] == "kept"]

    status_icon = STATUS_ICON.get(article.status, '•')
    st.caption(f"{status_icon} {article.status} · {article.message}")

    # Наверху — результат на русском: что гарантированно сохранено.
    render_kept(article)

    with st.expander('📋 Построчный список изменений', expanded=bool(added or changed or removed)):
        if not friendly:
            st.info('Карточка без изменений: значения из файла уже привязаны.')
        if added:
            st.markdown(_header(f"Добавлено · {len(added)}", added, 'Добавлено', "chip-added"), unsafe_allow_html=True)
        if changed:
            st.markdown(
                _header(f"Изменено · {len(changed)}", changed, 'Изменено', "chip-changed"), unsafe_allow_html=True
            )
        if removed:
            st.markdown(
                _header(f"Удалено — проверьте! · {len(removed)}", removed, 'Удалено', "chip-removed"),
                unsafe_allow_html=True,
            )
        if kept:
            st.markdown(_header(f"Не тронуто · {len(kept)}", kept, 'Не тронуто', "chip-ok"), unsafe_allow_html=True)

    existing_docs = _document_items((article.before or {}).get("documents"))
    if existing_docs:
        with st.expander(f"📎 Документы, которые уже были в карточке ({len(existing_docs)})", expanded=False):
            st.dataframe(
                display_frame(
                    pd.DataFrame(
                        [
                            {
                                'вид': DOCUMENT_KIND_LABELS.get(int(doc.get("type") or 0), 'документ'),
                                'номер': doc.get("number") or '—',
                                'действует до': str(doc.get("endDate") or "")[:10] or 'бессрочно',
                            }
                            for doc in existing_docs
                        ]
                    )
                ),
                width="stretch",
                hide_index=True,
            )

    with st.expander('Показать технический JSON карточки (ДО / ПОСЛЕ)', expanded=False):
        st.caption('Для разработчиков: сырые ответы WB как есть, без переводов и без сокращений.')
        left, right = st.columns(2, gap="medium")
        with left:
            st.markdown('**ДО обновления**')
            st.json(article.before or {}, expanded=1)
        with right:
            st.markdown('**ПОСЛЕ обновления (Safe-Merge)**')
            st.json(article.after or {}, expanded=1)


def kept_summary(article: wb.ArticleResult) -> list[dict[str, str]]:
    'Строки таблицы «Что гарантированно сохранено» на русском языке.'
    before = article.before or {}
    kept_chars = [
        char
        for char in before.get("characteristics") or []
        if isinstance(char, dict)
        and str(char.get("name") or "").lower() not in wb.DOC_CHARACTERISTIC_NAMES.values()
        and char.get("value") not in (None, [], "")
    ]
    table = [
        {'что сохраняется': 'Название карточки', 'значение': _friendly_value(before.get("title"), 80)},
        {'что сохраняется': 'Описание', 'значение': _friendly_value(before.get("description"), 80)},
        {'что сохраняется': 'Габариты, см', 'значение': _friendly_value(before.get("dimensions"), 80)},
    ]
    for char in kept_chars:
        table.append(
            {
                'что сохраняется': f"Характеристика «{char.get('name') or '—'}»",
                'значение': _friendly_value(char.get("value"), 80),
            }
        )
    for size in before.get("sizes") or []:
        if not isinstance(size, dict):
            continue
        label = size.get("techSize") or size.get("tech_size") or '—'
        skus = ", ".join(map(str, size.get("skus") or [])) or 'нет штрихкода'
        table.append({'что сохраняется': f"Размер {label} (chrtID {size.get('chrtID') or '—'})", 'значение': f"штрихкод {skus}"})
    return table


def render_kept(article: wb.ArticleResult) -> None:
    'Таблица «Что гарантированно сохранено» — всегда на экране, без клика.'
    table = kept_summary(article)
    kept_chars = table[3:]
    sizes = (article.before or {}).get("sizes") or []
    st.markdown(
        '<div class="section-title" style="font-size:1rem">✅ Что гарантированно сохранено</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f"Название, описание и габариты карточки, **{len(kept_chars)}** характеристик(и) "
        f"и **{len(sizes)}** размер(ов) с chrtID перенесены в обновлённую карточку без изменений. "
        'Меняются только поля, которые пришли в вашем файле.'
    )
    st.dataframe(
        display_frame(pd.DataFrame(table)),
        width="stretch",
        height=min(340, 38 * (len(table) + 1)),
        hide_index=True,
    )


def render_report(run_report: dict[str, Any]) -> None:
    results: list[wb.ArticleResult] = run_report.get("results") or []
    if not results:
        return
    frame = pd.DataFrame([result.as_dict() for result in results])
    stats = run_report.get("stats") or wb.summarize(results)

    st.markdown('<div class="section-title">📊 Итоговый отчёт</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="section-sub">Сформирован {run_report.get("started_at", datetime.now()).strftime("%d.%m.%Y %H:%M:%S")}'
        f" · режим: {MODE_LABELS.get(run_report.get('mode'), run_report.get('mode'))}"
        + (' · DRY-RUN' if run_report.get("dry_run") else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    counter = frame['статус'].value_counts()
    chips = [
        f'<span class="diff-chip chip-kept">{STATUS_ICON.get(status, "")} {status}: {count}</span>'
        for status, count in counter.items()
    ]
    st.markdown("".join(chips), unsafe_allow_html=True)

    columns = st.columns([1, 1, 1])
    columns[0].download_button(
        '⬇️ Скачать отчёт Excel',
        data=_report_excel(frame),
        file_name=f"wb_docs_report_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        type="primary",
    )
    columns[1].download_button(
        '⬇️ Скачать CSV',
        data=frame.to_csv(sep=";", index=False).encode("utf-8-sig"),
        file_name=f"wb_docs_report_{datetime.now():%Y%m%d_%H%M%S}.csv",
        mime="text/csv",
        width="stretch",
    )
    columns[2].metric('Итого строк в отчёте', len(frame))

    filter_col, search_col = st.columns([2, 1])
    with filter_col:
        selected = st.multiselect(
            'Фильтр по статусу',
            options=list(counter.index),
            default=[status for status in counter.index if status != wb.STATUS_UPDATED],
            placeholder='Все статусы',
        )
    with search_col:
        query = st.text_input('Поиск по артикулу', placeholder="WB-TEST-0123")

    view = frame
    if selected:
        view = view[view['статус'].isin(selected)]
    if query and query.strip():
        view = view[view["vendorCode"].astype(str).str.contains(query.strip(), case=False, na=False)]
    # Пустые ячейки -> «—» (в скачиваемых Excel/CSV остаются пустыми).
    st.dataframe(display_frame(view), width="stretch", height=380)
    st.caption(
        f"Показано {len(view)} из {len(frame)} строк. Прочерк «—» значит «этого поля у карточки нет». "
        'Время проверки фиксируется по каждому артикулу; полный отчёт с подсветкой статусов скачивается кнопкой выше.'
    )


# ---------------------------------------------------------------------------
# Главный сценарий
# ---------------------------------------------------------------------------


def _catalog_rows(results: Iterable[wb.ArticleResult]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        if not result.before:
            continue
        kept = len(
            [
                char
                for char in result.before.get("characteristics") or []
                if isinstance(char, dict)
                and str(char.get("name") or "").lower() not in wb.DOC_CHARACTERISTIC_NAMES.values()
            ]
        )
        rows.append(
            {
                "label": f"{result.vendor_code or '—'} · {result.status}",
                "result": result,
                "kept": kept,
                "status": result.status,
            }
        )
    return rows


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon='📦', layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)

    settings = render_sidebar()
    render_hero(settings["mode"])

    st.markdown('<div class="section-title">📥 Шаг 1 · Загрузите прайс</div>', unsafe_allow_html=True)
    render_uploader()

    frame, rows, source_name = load_price()
    run_report: dict[str, Any] | None = st.session_state.get("run_report")
    st.divider()

    if frame is None or not rows:
        empty_stats = wb.summarize([])
        render_metrics(empty_stats, settings["mode"], None, [])
        st.session_state["catalog_rows"] = []
        st.markdown('### 2 · Проверка и запись')
        st.markdown(
            '<div class="empty-hint">Загрузите Excel-файл или нажмите '
            '«Загрузить в память» в сайдбаре, чтобы подставить тестовый прайс на 1000 артикулов.</div>',
            unsafe_allow_html=True,
        )
        return

    if run_report and run_report.get("stats"):
        stats = run_report["stats"]
    else:
        stats = {"total": len(rows), "found": 0, "updated": 0, "unchanged": 0, "no_data": sum(1 for row in rows if not row.has_payload)}
        st.caption(
            f"Файл загружен: {len(rows)} строк, из них без номера документа — {stats['no_data']}. "
            'Нажмите «Запустить обновление», чтобы сверить артикулы с карточками WB.'
        )
    render_metrics(stats, settings["mode"], run_report, rows)
    if run_report:
        st.caption(
            f"Последний прогон: {run_report.get('started_at', datetime.now()):%d.%m.%Y %H:%M:%S} · "
            + ("DRY-RUN" if run_report.get("dry_run") else 'запись в WB')
            + f" · {run_report.get('elapsed', 0):.1f} с"
        )
    st.divider()

    st.markdown('<div class="section-title">👀 Шаг 2 · Проверьте данные</div>', unsafe_allow_html=True)
    render_preview(frame, rows, source_name)
    st.divider()

    st.markdown('<div class="section-title">🚀 Шаг 3 · Массовое обновление</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-sub">Чтение карточек → Safe-Merge → запись батчами → повторное чтение '
        'и проверка целостности каждого артикула.</div>',
        unsafe_allow_html=True,
    )

    action_col, hint_col = st.columns([1, 2])
    with action_col:
        start = st.button(
            '🚀 Запустить обновление',
            type="primary",
            width="stretch",
            disabled=settings["mode"] == MODE_REAL and not settings["token"],
        )
    with hint_col:
        if settings["mode"] == MODE_REAL and not settings["token"]:
            st.warning('Введите API-ключ WB в сайдбаре или переключитесь на Демо-режим.')
        elif settings["dry_run"]:
            st.info('Включён Dry-run: карточки не будут записаны в WB.')
        else:
            st.caption(
                f"Лимит записи WB: {wb.UPDATE_RATE_PER_MINUTE} запросов/мин · батч {settings['batch_size']} · "
                f"пауза {settings['delay']:g} с."
            )

    placeholders = st.container()
    with placeholders:
        log_box = st.empty()
        progress_bar = st.progress(0.0)
        status_line = st.empty()
    status_line.caption('Ожидание запуска…')

    if start:
        live_ph = (log_box, progress_bar, status_line)
        report = _run_update(
            mode=settings["mode"],
            token=settings["token"],
            rows=rows,
            batch_size=settings["batch_size"],
            delay=settings["delay"],
            dry_run=settings["dry_run"],
            live_placeholder=live_ph,
        )
        st.session_state["run_report"] = report
        st.session_state["catalog_rows"] = _catalog_rows(report.get("results") or [])
        st.session_state["selected_result"] = next(
            (item["result"] for item in st.session_state["catalog_rows"] if item["result"].before), None
        )
        run_report = report
        stats = report.get("stats") or wb.summarize(report["results"])
        # Перерисовываем метрики в этом же проходе, чтобы цифры не отставали
        # на один rerun; следующий проход нарисует их из session_state.
        render_metrics(stats, settings["mode"], report, rows)
        if report.get("error"):
            st.error(f"Обновление завершилось с ошибкой: {report['error']}")

    elif run_report:
        live = LiveView(log_box, progress_bar, status_line)
        for message, level in run_report.get("log", []) if isinstance(run_report, dict) else []:
            live.log(message, level)
        live.finish()

    current_result = st.session_state.get("selected_result")
    if current_result is None:
        current_result = next(
            (item["result"] for item in st.session_state.get("catalog_rows", []) if item["result"].before), None
        )
    if current_result is None:
        current_result = next(
            (
                result
                for result in (run_report or {}).get("results", [])
                if result.before is not None
            ),
            None,
        )
    if current_result is None:
        current_result = wb.ArticleResult(0, "", wb.STATUS_NO_DATA, "")

    st.divider()
    diff_anchor = st.container()
    with diff_anchor:
        render_diff(current_result)

    if run_report:
        st.divider()
        render_report(run_report)


if __name__ == "__main__":
    main()
