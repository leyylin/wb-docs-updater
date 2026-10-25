"""Тесты UI-хелперов ``app.py``.

Streamlit-виджеты здесь не вызываются — проверяем чистые функции, которые
превращают технический diff и агрегаты в понятный селлеру текст:

* ``friendly_diff`` — русские подписи, схлопывание служебных полей;
* ``fill_metrics`` — плитки «обновлено + ошибки/пропущено == строк в файле»;
* ``display_frame`` — прочерк «—» вместо ``None`` только в отображении;
* ``kept_summary`` — таблица «Что гарантированно сохранено»;
* ``_row_line`` — бейдж «✓ ОК» вместо обрезанного «не».
"""

from __future__ import annotations

import pytest

app = pytest.importorskip("app")

import pandas as pd  # noqa: E402

import wb_client as wb  # noqa: E402


# ---------------------------------------------------------------------------
# friendly_diff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, expected",
    [
        ("title", "Название карточки"),
        ("description", "Описание"),
        ("vendorCode", "Артикул продавца"),
        ("dimensions", "Габариты, см"),
        ("sizes", "Размеры и штрихкоды"),
        ("subjectName", "Предмет"),
        ("characteristic «Цвет»", "Характеристика «Цвет»"),
        ("characteristic «Номер декларации соответствия»", "Характеристика «Номер декларации соответствия»"),
        ("chrtID «M»", "Размер · chrtID M"),
        ("document 2:«ЕАЭС N RU Д-RU.РА01.В.99999/26»", "Документ"),
        # Имя характеристики, если WB вернул его пустым (в карточке пришёл только id).
        ("characteristic «charcID 15001135»", "Характеристика «Номер декларации соответствия»"),
        ("characteristic «charcID 15001138»", "Характеристика «Срок действия документа»"),
        ("characteristic «charcID 15001136»", "Характеристика «Номер сертификата соответствия»"),
        ("characteristic «charcID 99999999»", "Характеристика «charcID 99999999»"),
    ],
)
def test_friendly_field_is_russian(field, expected):
    assert app._friendly_field(field) == expected


def test_friendly_field_has_no_english_terms():
    labels = [app._friendly_field(field) for field in ("characteristic «Цвет»", "charcID «Цвет»", "title")]
    for label in labels:
        assert "characteristic" not in label
        assert "charcID" not in label


def test_friendly_value_cleans_technical_noise():
    assert app._friendly_value(["Синий"]) == "Синий"
    assert app._friendly_value([]) == "—"
    assert app._friendly_value(None) == "—"
    assert app._friendly_value(True) == "да"
    assert app._friendly_value({"length": 31, "width": 23, "height": 4, "weightBrutto": 0.21}) == "31 × 23 × 4 см, 0.21 кг"
    sizes = app._friendly_value([{"chrtID": 1, "techSize": "M", "skus": ["2000000000001"]}])
    assert sizes == "M (2000000000001)"
    assert app._friendly_value("x" * 300).endswith("…")


def test_friendly_diff_uses_russian_document_names(card, row):
    merged = wb.safe_merge(card, row)
    entries = app.friendly_diff(wb.diff_cards(card, merged))
    labels = [entry["label"] for entry in entries]

    # Новый документ показан одной строкой и не дублируется характеристикой.
    docs = [entry for entry in entries if entry["level"] == "added" and entry["label"] == "Документ"]
    assert len(docs) == 1, labels
    assert not any("charcID" in label for label in labels)
    assert not any("characteristic " in label for label in labels)
    assert not any("Номер декларации" in label for label in labels), labels
    # Служебные поля документа не попадают в построчный список.
    assert not any("».type" in label or "isEndless" in label for label in labels)
    assert "Дата изменения карточки" not in labels  # updatedAt скрыт


def test_friendly_diff_added_document_is_one_line(card, row):
    entries = app.friendly_diff(wb.diff_cards(card, wb.safe_merge(card, row)))
    docs = [entry for entry in entries if entry["level"] == "added" and entry["label"] == "Документ"]
    assert docs, entries
    # Новый номер — ровно одна строка «Документ», а не `document 2:«…».type`.
    assert len(docs) == 1, docs
    assert docs[0]["after_text"].startswith("№ ЕАЭС N RU"), docs[0]
    assert "действует до 2030-12-31" in docs[0]["after_text"]
    assert not any(entry["label"].endswith((".type", ".isEndless", ".endDate")) for entry in entries)


def test_added_has_no_duplicate_of_document_line(card, row):
    """Номер и срок в «Добавлено» — одной строкой «Документ», без повторов."""
    added = [entry for entry in app.friendly_diff(wb.diff_cards(card, wb.safe_merge(card, row))) if entry["level"] == "added"]
    labels = [entry["label"] for entry in added]
    assert labels.count("Документ") == 1, labels
    assert "Характеристика «Номер декларации соответствия»" not in labels, labels
    assert "Характеристика «Срок действия документа»" not in labels, labels
    numbers = [entry["after_text"] for entry in added if "ЕАЭС" in entry["after_text"]]
    assert len(numbers) == 1, numbers


def _doc_card(charc_ids, documents):
    """Карточка, где имя doc-характеристики пустое, а есть только charcID."""
    characteristics = [{"id": charc_id, "name": "", "value": [f"номер {charc_id}/26"]} for charc_id in charc_ids]
    return {
        "nmID": 1,
        "vendorCode": "A",
        "subjectID": 105,
        "characteristics": characteristics,
        "documents": documents,
    }


def test_empty_name_characteristic_shows_russian_name():
    # WB не отдал имя характеристики — известен только charcID.
    card = {"nmID": 1, "vendorCode": "A", "characteristics": [{"id": 15001135, "name": "", "value": ["старый"]}]}
    merged = {**card, "characteristics": [{"id": 15001135, "name": "", "value": ["новый"]}]}
    result = wb.ArticleResult(2, "A", wb.STATUS_UPDATED, "", before=card, after=merged)

    labels = [entry["label"] for entry in app.friendly_diff(wb.diff_cards(card, merged), app._doc_charc_names(result))]

    assert "Характеристика «Номер декларации соответствия»" in labels, labels
    assert not any("charcID" in label for label in labels), labels


def test_unknown_charcid_name_stays_technical():
    card = {"nmID": 1, "characteristics": [{"id": 999, "name": "", "value": ["старый"]}]}
    merged = {**card, "characteristics": [{"id": 999, "name": "", "value": ["новый"]}]}
    result = wb.ArticleResult(2, "A", wb.STATUS_UPDATED, "", before=card, after=merged)

    labels = [entry["label"] for entry in app.friendly_diff(wb.diff_cards(card, merged), app._doc_charc_names(result))]

    # Неизвестный id — название выдумывать нельзя, показываем как есть.
    assert "Характеристика «charcID 999»" in labels, labels


def test_name_from_either_side_of_the_card():
    # `after` хранит имя (например, demo-клиент), `before` — нет.
    card = {"nmID": 1, "characteristics": [{"id": 15001138, "name": "", "value": ["31.12.2030"]}]}
    merged = {"nmID": 1, "characteristics": [{"id": 15001138, "name": "Срок действия документа", "value": ["31.12.2031"]}]}
    result = wb.ArticleResult(2, "A", wb.STATUS_UPDATED, "", before=card, after=merged)

    labels = [entry["label"] for entry in app.friendly_diff(wb.diff_cards(card, merged), app._doc_charc_names(result))]

    assert "Характеристика «Срок действия документа»" in labels, labels


# ---------------------------------------------------------------------------
# _document_items (спойлер «Документы, которые уже были в карточке»)
# ---------------------------------------------------------------------------


def test_document_items_reads_items_list(card):
    docs = app._document_items(card["documents"])
    # Один документ, а не три ключа словаря (`items`, `isAutofilled`, …).
    assert len(docs) == 1, docs
    assert docs[0]["number"].startswith("RU.77")


def test_document_items_handles_plain_list_and_garbage():
    assert app._document_items([{"type": 2, "number": "X"}]) == [{"type": 2, "number": "X"}]
    assert app._document_items({"items": [{"type": 2}, "мусор"]}) == [{"type": 2}]
    assert app._document_items(None) == []
    assert app._document_items({}) == []
    assert app._document_items({"items": None}) == []
    # Раньше итерация по словарю давала пустые строки: ключи вместо документов.
    assert all(doc.get("number") for doc in app._document_items({"items": [{"number": "1"}]}))


def test_friendly_diff_kept_rows_have_single_source(card, row):
    entries = app.friendly_diff(wb.diff_cards(card, wb.safe_merge(card, row)))
    kept = [(entry["label"], entry["before_text"]) for entry in entries if entry["level"] == "kept"]
    assert len(kept) == len(set(kept)), "дубли в «Не тронуто»"


def test_friendly_diff_detects_removed_fields():
    before = {"nmID": 1, "title": "Платье", "characteristics": [{"id": 5, "name": "Цвет", "value": ["Белый"]}]}
    after = {"nmID": 1, "title": "Платье", "characteristics": []}
    entries = app.friendly_diff(wb.diff_cards(before, after))
    removed = [entry for entry in entries if entry["level"] == "removed"]
    assert removed and removed[0]["label"] == "Характеристика «Цвет»", entries


# ---------------------------------------------------------------------------
# _row_line
# ---------------------------------------------------------------------------


def test_kept_row_uses_ok_badge(card, row):
    entries = app.friendly_diff(wb.diff_cards(card, wb.safe_merge(card, row)))
    kept = next(entry for entry in entries if entry["level"] == "kept")
    html_row = app._row_line(kept, "Не тронуто", "chip-ok")

    assert "ОК" in html_row
    assert ">не<" not in html_row
    assert kept["label"] in html_row or kept["label"].replace("«", "«") in html_row


def test_added_row_has_no_empty_arrow():
    entry = {"label": "Документ", "before_text": "—", "after_text": "№ 123", "level": "added"}
    html_row = app._row_line(entry, "Добавлено", "chip-added")
    assert "→" not in html_row
    assert "+ № 123" in html_row


# ---------------------------------------------------------------------------
# fill_metrics
# ---------------------------------------------------------------------------


def test_metrics_sum_equals_file_rows(rows_1000_results):
    rows, results = rows_1000_results
    filled = app.fill_metrics(rows, wb.summarize(results))

    assert filled["total"] == len(rows) == 1006
    assert filled["updated"] == 1000
    # 1 без документов + 2 не найдено + 3 невалидных = 6
    assert filled["no_data"] == 1
    assert filled["not_found"] == 2
    assert filled["invalid"] == 3
    assert filled["errors"] == 6
    assert filled["updated"] + filled["errors"] == filled["total"]


def test_metrics_account_no_data_without_run(rows):
    filled = app.fill_metrics(rows, {"total": len(rows)})
    assert filled["errors"] == len(rows)
    assert filled["updated"] + filled["errors"] == filled["total"]


def test_metrics_keep_unchanged_out_of_errors(rows):
    filled = app.fill_metrics(rows, {"updated": 2, "unchanged": 1})
    assert filled["updated"] + filled["unchanged"] + filled["errors"] == filled["total"]


# ---------------------------------------------------------------------------
# display_frame
# ---------------------------------------------------------------------------


def test_display_frame_replaces_none_with_dash():
    frame = pd.DataFrame(
        {
            "vendorCode": ["A", "B"],
            "nmID": [None, 42],
            "декларация": [None, "ЕАЭС N RU"],
            "сообщение": [None, "ok"],
        }
    )
    shown = app.display_frame(frame)

    assert shown.loc[0, "nmID"] == "—"
    assert shown.loc[0, "декларация"] == "—"
    assert shown.loc[0, "сообщение"] == "—"
    assert shown.loc[1, "декларация"] == "ЕАЭС N RU"
    # Исходный DataFrame не испорчен — Excel/CSV скачиваются как были.
    # (pandas превращает None в NaN — поэтому проверяем «пусто», а не `is None`.)
    assert pd.isna(frame.loc[0, "декларация"])
    assert frame.loc[0, "декларация"] != "—"


def test_display_frame_keeps_numbers():
    frame = pd.DataFrame({"nmID": [1, 2], "статус": ["Обновлено", "Ошибка"]})
    shown = app.display_frame(frame)
    # nmID показывается строкой: у ненайденных артикулов там «—».
    assert list(shown["nmID"]) == ["1", "2"]
    assert list(shown["статус"]) == ["Обновлено", "Ошибка"]


# ---------------------------------------------------------------------------
# kept_summary
# ---------------------------------------------------------------------------


def test_kept_summary_lists_title_description_and_sizes(card, row):
    result = wb.ArticleResult(2, card["vendorCode"], wb.STATUS_UPDATED, "", before=card, after=wb.safe_merge(card, row))
    table = app.kept_summary(result)
    by_name = {item["что сохраняется"]: item["значение"] for item in table}

    assert by_name["Название карточки"] == card["title"]
    assert by_name["Описание"] == card["description"]
    assert "×" in by_name["Габариты, см"]
    color = next(value for name, value in by_name.items() if name == "Характеристика «Цвет»")
    assert color, "цвет должен попасть в таблицу сохранённых"
    # Документные характеристики в «сохранённое» не попадают — они и меняются.
    assert "Характеристика «Номер декларации соответствия»" not in by_name
    assert any(name.startswith("Размер M") for name in by_name)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rows():
    """Реальные строки тестового прайса (1006 строк) — без сети."""
    import sample_generator as sg

    frame = sg.load_sample_dataframe()
    return [wb.normalize_row(record, index + 2) for index, record in enumerate(frame.to_dict("records"))]


@pytest.fixture(scope="module")
def rows_1000_results(rows):
    """Прогон демо-клиента: 1000 обновлено, 6 «проблемных» строк."""
    client = wb.create_client("demo")
    results = wb.Updater(client, delay=0.0).run(rows)
    return rows, results
