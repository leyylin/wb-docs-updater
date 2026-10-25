"""Тесты валидации входящего Excel-прайса.

Проверяется путь «файл .xlsx → pandas → нормализация заголовков →
``wb_client.normalize_row`` → статусы в отчёте»: битые строки, пустые
артикулы, дубликаты, неверные даты и номера документов.

Замечание по архитектуре: ``load_price`` и ``_normalize_columns`` живут в
``app.py`` и требуют запущенного Streamlit. Тесты ниже используют
``pytest.importorskip("streamlit")`` и вызывают чистые функции напрямую;
чтение Excel проверяется без UI через ``pd.read_excel`` + те же хелперы.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import sample_generator as sg
import wb_client as wb


# ---------------------------------------------------------------------------
# Легенда для тестовых файлов
# ---------------------------------------------------------------------------


@pytest.fixture()
def price_frame() -> pd.DataFrame:
    """«Грязный» прайс: дубликат, пустой артикул, битая дата, лишние пробелы."""
    soon = (date.today() + timedelta(days=200)).strftime("%d.%m.%Y")
    return pd.DataFrame(
        [
            {"vendorCode": "WB-TEST-0001", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.00001/25",
             "certificate_number": None, "expiry_date": soon},
            {"vendorCode": "WB-TEST-0001", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.77777/25",
             "certificate_number": None, "expiry_date": soon},
            {"vendorCode": None, "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.99999/25",
             "certificate_number": None, "expiry_date": soon},
            {"vendorCode": "  WB-TEST-0002  ", "declaration_number": None,
             "certificate_number": "ЕАЭС RU С-RU.АБ12.В.00002/25", "expiry_date": "завтра"},
            {"vendorCode": "WB-TEST-0003", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.00003/25",
             "certificate_number": None, "expiry_date": datetime(2030, 12, 31)},
        ]
    )


def _excel_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Чтение файла
# ---------------------------------------------------------------------------


def test_excel_roundtrip_keeps_rows_and_types(price_frame):
    data = _excel_bytes(price_frame)
    loaded = pd.read_excel(io.BytesIO(data), sheet_name=0)

    assert len(loaded) == len(price_frame)
    assert list(loaded.columns) == ["vendorCode", "declaration_number", "certificate_number", "expiry_date"]
    rows = [wb.normalize_row(record, index + 2) for index, record in enumerate(loaded.to_dict("records"))]

    # Пустой артикул: в Excel превращается в NaN и должен отсеяться как пустой.
    assert rows[2].vendor_code == ""
    assert rows[3].vendor_code == "WB-TEST-0002"  # пробелы обрезаны
    assert rows[4].expiry == "31.12.2030"  # дата из ячейки Excel
    assert rows[3].expiry_iso == "" and "не является датой" in " ".join(rows[3].issues)


def test_read_price_rejects_broken_bytes():
    with pytest.raises(Exception):
        pd.read_excel(io.BytesIO(b"not an excel file"), sheet_name=0)


def test_missing_vendor_code_column_is_reported():
    """Без колонки vendorCode анализ должен явно об этом сказать, а не падать."""
    app = pytest.importorskip("app", reason="streamlit не установлен")
    frame = pd.DataFrame([{"артикул продавца": "X", "декларация": "ЕАЭС N RU Д-RU.1/25"}])
    normalized = app._normalize_columns(frame)

    assert "vendorCode" in normalized.columns
    assert "declaration_number" in normalized.columns

    broken = pd.DataFrame([{"Штрихкод": "2000000000001"}])
    assert "vendorCode" not in app._normalize_columns(broken).columns


# ---------------------------------------------------------------------------
# Валидация отдельных строк
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   ", float("nan"), "nan", "None", pd.NaT])
def test_blank_vendor_code_is_empty(blank):
    assert wb.normalize_row({"vendorCode": blank}).vendor_code == ""


def test_vendor_code_strips_whitespace_and_collapses_spaces():
    row = wb.normalize_row({"vendorCode": "  WB \t TEST  0001  "})
    assert row.vendor_code == "WB TEST 0001"


def test_row_without_payload_has_no_payload_flag():
    row = wb.normalize_row({"vendorCode": "WB-TEST-0001", "declaration_number": None, "expiry_date": None})
    assert row.has_payload is False


def test_bad_date_is_reported_but_row_survives():
    row = wb.normalize_row({"vendorCode": "WB-TEST-0001", "declaration_number": "ЕАЭС N RU Д-RU.1/25",
                            "expiry_date": "послезавтра"})
    assert any("не является датой" in issue for issue in row.issues)
    assert row.declaration  # номер сохранился — запись возможна и без срока


def test_declaration_in_certificate_column_is_moved():
    row = wb.normalize_row({"vendorCode": "X", "declaration_number": None,
                            "certificate_number": "ЕАЭС N RU Д-RU.РА01.В.00001/25"})
    assert row.declaration == "ЕАЭС N RU Д-RU.РА01.В.00001/25"
    assert row.certificate == ""
    assert any("перенесён" in issue for issue in row.issues)


def test_number_without_eaes_format_is_flagged():
    row = wb.normalize_row({"vendorCode": "X", "declaration_number": "12345"})
    assert any("не похож на формат ЕАЭС" in issue for issue in row.issues)
    assert row.declaration == "12345"


# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("31.12.2030", "31.12.2030"),
        ("31/12/2030", "31.12.2030"),
        ("2030-12-31", "31.12.2030"),
        ("1 января 2030", "01.01.2030"),
        ("31.12.30", "31.12.2030"),
        (date(2030, 12, 31), "31.12.2030"),
        (datetime(2030, 12, 31, 10, 30), "31.12.2030"),
        (pd.Timestamp("2030-12-31"), "31.12.2030"),
        (44205.0, "09.01.2021"),  # серийный номер Excel
    ],
)
def test_parse_expiry_formats(value, expected):
    parsed = wb.parse_expiry(value)
    assert parsed is not None and parsed[0] == expected


@pytest.mark.parametrize("value", ["", None, "завтра", "TBD", "32.13.2030", float("nan")])
def test_parse_expiry_rejects_garbage(value):
    assert wb.parse_expiry(value) is None


def test_parse_expiry_is_not_fooled_by_two_digit_year_ambiguity():
    """DD.MM.YYYY трактуется как день-месяц, а не месяц-день."""
    assert wb.parse_expiry("05.01.2028")[0] == "05.01.2028"
    assert wb.parse_expiry("25.12.2028")[0] == "25.12.2028"


@pytest.mark.parametrize(
    "serial, iso",
    [
        (44205, "2021-01-09"),  # 09.01.2021 — серийник вне прежнего окна 20000..80000
        (16255, "1944-07-02"),
        (73415, "2100-12-31"),
        (2999, "1908-03-17"),  # 17.03.1908 — «фильтр по истечении» не должен врать
    ],
)
def test_parse_expiry_accepts_excel_serial(serial, iso):
    """Живые люди пишут даты числами: 40000/45000 Excel — это не «дата отсутствует»."""
    parsed = wb.parse_expiry(serial)
    assert parsed is not None, f"серийник {serial} не распознан как дата"
    assert parsed[1].startswith(iso)


@pytest.mark.parametrize("value", [0, -5, 1e12, 999_999])
def test_parse_expiry_rejects_implausible_numbers(value):
    """А вот артикул/длина/цена, случайно попавшие в колонку срока, — это не дата."""
    assert wb.parse_expiry(value) is None


# ---------------------------------------------------------------------------
# Дубликаты и отчёт по строкам (plan_apply)
# ---------------------------------------------------------------------------


def test_plan_apply_marks_duplicates_and_empty_articles(price_frame, cards_file):
    cards = [
        {"nmID": 10_000_001, "vendorCode": "WB-TEST-0001", "subjectID": 105, "subjectName": "Футболки",
         "characteristics": [], "sizes": [{"chrtID": 1, "techSize": "S"}],
         "documents": {"items": []}},
        {"nmID": 10_000_002, "vendorCode": "WB-TEST-0002", "subjectID": 105, "subjectName": "Футболки",
         "characteristics": [], "sizes": [{"chrtID": 2, "techSize": "M"}],
         "documents": {"items": []}},
    ]
    client = wb.DemoWBClient(cards_path=cards_file(cards))
    records = price_frame.to_dict("records")
    rows = [wb.normalize_row(record, index + 2) for index, record in enumerate(records)]

    plan = wb.plan_apply(client, rows)
    by_row = {result.row_number: result for result in plan.results}

    assert by_row[3].status == wb.STATUS_DUPLICATE
    assert by_row[3].vendor_code == "WB-TEST-0001"
    assert by_row[4].status == wb.STATUS_INVALID
    assert by_row[4].message == "Пустой vendorCode"
    # Пробелы в артикуле не мешают найти карточку (кроме дубля, который отсекается раньше).
    assert by_row[5].status in {wb.STATUS_UPDATED, wb.STATUS_NEEDS_REVIEW}


def test_plan_apply_marks_unknown_article(cards_file):
    client = wb.DemoWBClient(cards_path=cards_file([{"nmID": 1, "vendorCode": "known", "characteristics": [],
                                                     "sizes": [], "documents": {"items": []}}]))
    plan = wb.plan_apply(client, [wb.normalize_row({"vendorCode": "unknown",
                                                    "declaration_number": "ЕАЭС N RU Д-RU.1/25"})])
    assert plan.results[0].status == wb.STATUS_NOT_FOUND


def test_plan_apply_marks_row_without_documents(cards_file):
    client = wb.DemoWBClient(cards_path=cards_file([{"nmID": 1, "vendorCode": "known", "characteristics": [],
                                                     "sizes": [], "documents": {"items": []}}]))
    plan = wb.plan_apply(client, [wb.normalize_row({"vendorCode": "known"})])
    assert plan.results[0].status == wb.STATUS_NO_DATA


def test_plan_apply_keeps_first_row_of_duplicate_pair(cards_file):
    """Из двух строк с одним артикулом применяется первая, вторая — только отчёт."""
    cards = [{"nmID": 1, "vendorCode": "dup", "subjectID": 105, "subjectName": "Футболки",
              "characteristics": [], "sizes": [{"chrtID": 1}], "documents": {"items": []}}]
    client = wb.DemoWBClient(cards_path=cards_file(cards))
    rows = [
        wb.normalize_row({"vendorCode": "dup", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.1/25"}, 2),
        wb.normalize_row({"vendorCode": "dup", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.2/25"}, 3),
    ]

    plan = wb.plan_apply(client, rows)

    assert plan.results[0].status != wb.STATUS_DUPLICATE
    assert plan.results[1].status == wb.STATUS_DUPLICATE
    assert len(plan.cards_to_update) == 1


def test_sample_generator_dirty_rows_are_detected(price_frame):
    """Генератор обязан создавать ровно те «грязные» кейсы, на которые опирается демо."""
    frame = sg.generate_sample_dataframe(rows=100, clean=False)
    rows = [wb.normalize_row(record, index + 2) for index, record in enumerate(frame.to_dict("records"))]

    duplicated = [row.vendor_code for row in rows if row.vendor_code]
    assert len(duplicated) > len(set(duplicated)), "в сэмпле нет дубликата артикула"
    assert any(not row.vendor_code for row in rows), "в сэмпле нет строки с пустым артикулом"
    assert any(not row.has_payload for row in rows), "в сэмпле нет строки без документов"
    assert any("не является датой" in " ".join(row.issues) for row in rows), "в сэмпле нет битой даты"
    assert any(wb.expired(row.expiry) for row in rows if row.expiry), "в сэмпле нет просроченного документа"


def test_clean_sample_has_exact_row_count():
    frame = sg.generate_sample_dataframe(rows=50, clean=True)
    assert len(frame) == 50
    assert frame["vendorCode"].is_unique
    assert frame["vendorCode"].notna().all()
