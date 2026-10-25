"""Тесты ядра Safe-Merge: слияние карточки WB с документом из прайса.

Главный инвариант: после слияния и после записи через ``/content/v2/cards/update``
в карточке остаются **все** прежние поля (характеристики, размеры с ``chrtID``,
габариты, название, описание, ранее прикреплённые документы), а поля документов
из прайса добавляются или обновляются.
"""

from __future__ import annotations

import copy

import pytest

import wb_client as wb
from conftest import charc_ids, make_card, make_row, prices


# ---------------------------------------------------------------------------
# Базовая безопасность: старое не теряется
# ---------------------------------------------------------------------------


def test_merge_keeps_every_original_characteristic(card, row):
    """Каждая характеристика карточки остаётся на месте с тем же значением."""
    merged = wb.safe_merge(card, row)

    old = charc_ids(card)
    new = charc_ids(merged)
    for char_id, value in old.items():
        assert char_id in new, f"характеристика {char_id} пропала из карточки"
        assert new[char_id] == value, f"значение характеристики {char_id} изменилось"

    # Цвет / состав / комплектация — те самые поля, которые теряются при наивной записи.
    assert prices(merged)["Цвет"] == ["Чёрный"]
    assert prices(merged)["Состав"] == ["100% хлопок"]
    assert prices(merged)["Комплектация"] == ["Футболка — 1 шт."]


def test_merge_keeps_empty_characteristic_values(card, row):
    """Пустые значения характеристик не вычищаются (WB ждёт полный массив)."""
    merged = wb.safe_merge(card, row)
    assert prices(merged)["Декоративные элементы"] == [""]
    assert len(merged["characteristics"]) == len(card["characteristics"]) + 2  # декларация + срок


def test_merge_keeps_sizes_dimensions_title_description(card, row):
    merged = wb.safe_merge(card, row)

    assert merged["sizes"] == card["sizes"]
    assert [size["chrtID"] for size in merged["sizes"]] == [500_002, 500_003]
    assert merged["sizes"][0]["skus"] == ["20000000000001"]
    assert merged["dimensions"] == card["dimensions"]
    assert merged["title"] == card["title"]
    assert merged["description"] == card["description"]
    assert merged["brand"] == card["brand"]
    assert merged["imtID"] == card["imtID"]


def test_merge_does_not_mutate_input_card(card, row):
    """Слияние — чистая функция: исходная карточка остаётся нетронутой."""
    snapshot = copy.deepcopy(card)
    merged = wb.safe_merge(card, row)
    assert card == snapshot, "safe_merge изменил переданную карточку"
    assert merged is not card


def test_merge_keeps_foreign_documents(card, row):
    """СГР (type=3) остаётся, а документ типа из прайса дописывается."""
    merged = wb.safe_merge(card, row)
    numbers = [item.get("number") for item in merged["documents"]["items"]]

    assert "RU.77.99.11.001.R.000123.01.24" in numbers
    assert row.declaration in numbers


# ---------------------------------------------------------------------------
# Новое значение реально добавляется
# ---------------------------------------------------------------------------


def test_merge_adds_declaration_characteristic(card, row):
    merged = wb.safe_merge(card, row)
    by_id = {item["id"]: item for item in merged["characteristics"]}

    assert by_id[15001135]["value"] == [row.declaration]
    assert by_id[15001135]["name"] == "Номер декларации соответствия"
    assert by_id[15001138]["value"] == ["31.12.2030"]


def test_merge_updates_existing_doc_characteristic_in_place(card, row):
    """Если характеристика уже есть — значение обновляется, дубликат не создаётся."""
    card["characteristics"].append(
        {"id": 15001135, "name": "Номер декларации соответствия", "value": ["ЕАЭС N RU Д-RU.РА01.В.00001/24"]}
    )
    merged = wb.safe_merge(card, row)

    declarations = [item for item in merged["characteristics"] if item["id"] == 15001135]
    assert len(declarations) == 1
    assert declarations[0]["value"] == [row.declaration]


def test_merge_resolves_slot_by_charc_id_when_name_is_unusual(card, row):
    """Название характеристики в карточке может отличаться — спасает charcID."""
    card["characteristics"].append({"id": 15001136, "name": "Сертификат", "value": ["ЕАЭС RU С-RU.АБ12.В.00001/24"]})
    row.certificate = "ЕАЭС RU С-RU.АБ12.В.99999/25"
    merged = wb.safe_merge(card, row)

    certs = [item for item in merged["characteristics"] if item["id"] == 15001136]
    assert len(certs) == 1
    assert certs[0]["value"] == ["ЕАЭС RU С-RU.АБ12.В.99999/25"]


def test_merge_skips_characteristic_without_charc_id(card):
    """Без charcID характеристику не добавить — WB вернёт 400. Слияние не падает."""
    row = wb.normalize_row({"vendorCode": "WB-TEST-0001", "declaration_number": "ЕАЭС N RU Д-RU.1/25"})
    assert row.charc_ids == {}

    merged = wb.safe_merge(card, row)
    assert all(item["id"] != 15001135 for item in merged["characteristics"])
    # Документ при этом всё равно попадает в documents.items.
    assert row.declaration in [item["number"] for item in merged["documents"]["items"]]


def test_merge_replaces_declaration_of_same_kind(card, row):
    """Старая декларация (type=2) заменяется новой, сертификат и СГР не трогаются."""
    card["documents"]["items"].append(
        {
            "id": "doc-1-decl",
            "type": 2,
            "number": "ЕАЭС N RU Д-RU.РА01.В.00001/24",
            "endDate": "2025-01-01T00:00:00Z",
            "isEndless": False,
        }
    )
    merged = wb.safe_merge(card, row)
    items = merged["documents"]["items"]
    declarations = [item for item in items if item.get("type") == 2]

    assert len(declarations) == 1
    assert declarations[0]["number"] == row.declaration
    assert declarations[0]["endDate"] == "2030-12-31T00:00:00Z"
    # Заменённый документ теряет серверный id: это новая запись, старый id недействителен.
    assert "id" not in declarations[0]
    assert any(item.get("type") == 3 for item in items)


def test_merge_without_expiry_does_not_touch_dates(card):
    """Если срока в прайсе нет — даты существующего документа остаются."""
    card["documents"]["items"][0]["endDate"] = "2029-01-01T00:00:00Z"
    row = make_row(expiry_date=None)
    merged = wb.safe_merge(card, row)

    assert merged["documents"]["items"][0]["endDate"] == "2029-01-01T00:00:00Z"


def test_second_apply_does_not_reuse_old_expiry(card):
    """Регресс: при повторном слиянии старый endDate не «протекает» в новый номер.

    Сценарий: первый прогон привязал декларацию со сроком 31.12.2030, второй
    прогон меняет только номер (без срока) — срок от предыдущей строки не должен
    автоматически переехать на новый документ.
    """
    first = wb.safe_merge(card, make_row())
    second = wb.safe_merge(first, make_row(expiry_date=None, declaration_number="ЕАЭС N RU Д-RU.РА01.В.54321/25"))

    declarations = [item for item in second["documents"]["items"] if item.get("type") == 2]
    assert declarations[0]["number"] == "ЕАЭС N RU Д-RU.РА01.В.54321/25"
    assert declarations[0]["endDate"] == ""  # TODO(BUG-2): сейчас протекает "2030-12-31T00:00:00Z"


def test_merge_without_documents_block(card, row):
    """Кабинет может вернуть карточку без documents — слияние создаёт объект с нуля."""
    card.pop("documents")
    merged = wb.safe_merge(card, row)

    items = merged["documents"]["items"]
    assert [item["type"] for item in items] == [wb.DOCUMENT_TYPE_DECLARATION]
    assert all(key != "overallVerdict" for key in merged["documents"])


# ---------------------------------------------------------------------------
# Формирование тела запроса POST /content/v2/cards/update
# ---------------------------------------------------------------------------


def test_payload_contains_everything_wb_would_overwrite(card, row):
    payload = wb.build_update_payload(wb.safe_merge(card, row))

    assert payload["nmID"] == card["nmID"]
    assert payload["vendorCode"] == card["vendorCode"]
    # sizes идут с chrtID: без него WB не свяжет размер с существующим.
    assert [size["chrtID"] for size in payload["sizes"]] == [500_002, 500_003]
    assert payload["dimensions"] == card["dimensions"]
    # Все характеристики, а не только документные.
    assert {item["id"] for item in payload["characteristics"]} >= {1, 12, 13, 17, 21, 99}
    assert payload["documents"]["items"]


def test_payload_uses_value_arrays_for_single_char_ids(card, row):
    """WB принимает только массивы [charcID, ...]: одиночные значения тоже оборачиваются."""
    payload = wb.build_update_payload(wb.safe_merge(card, row))
    for item in payload["characteristics"]:
        assert isinstance(item["value"], list)


def test_payload_drops_unpatchable_characteristics(card, row):
    """Характеристики без id или с value=None в запрос не попадают."""
    card["characteristics"].append({"name": "Без ID", "value": ["x"]})
    card["characteristics"].append({"id": 77, "name": "Пустышка", "value": None})
    payload = wb.build_update_payload(wb.safe_merge(card, row))

    ids = [item["id"] for item in payload["characteristics"]]
    assert 77 not in ids
    assert len(ids) == len({*ids})


def test_payload_requires_nm_id(card):
    card.pop("nmID")
    with pytest.raises(wb.WBAPIError, match="nmID"):
        wb.build_update_payload(card)


# ---------------------------------------------------------------------------
# Пост-проверка целостности
# ---------------------------------------------------------------------------


def test_verify_integrity_passes_for_clean_merge(card, row):
    merged = wb.safe_merge(card, row)
    assert wb.verify_integrity(card, merged, row) == []


def test_verify_integrity_detects_lost_characteristic(card, row):
    damaged = copy.deepcopy(card)
    damaged["characteristics"] = [
        item for item in damaged["characteristics"] if item.get("name") != "Состав"
    ]
    merged = wb.safe_merge(damaged, row)
    problems = wb.verify_integrity(card, merged, row)

    assert any("Состав" in problem for problem in problems)


def test_verify_integrity_detects_lost_document(card, row):
    wiped = copy.deepcopy(card)
    wiped["documents"]["items"] = []
    merged = wb.safe_merge(wiped, row)
    problems = wb.verify_integrity(card, merged, row)

    assert any("Потеряны документы" in problem for problem in problems)


def test_verify_integrity_detects_missing_new_value(card, row):
    """WB «проглотил» запрос, но декларации в ответе нет — это должно быть видно."""
    merged = wb.safe_merge(card, row)
    merged["characteristics"] = [item for item in merged["characteristics"] if item["id"] != 15001135]
    problems = wb.verify_integrity(card, merged, row)

    # Декларация после merge лежит в одном месте: либо в characteristics
    # (если у предмета есть charcID), либо в documents.items — проверяем оба.
    assert problems, "потеря декларации не замечена"
    assert any("деклараци" in problem.lower() for problem in problems)
    assert all(item.get("id") != 15001135 for item in merged["characteristics"])


def test_verify_integrity_detects_dropped_document_number(card, row):
    """Если число документов не изменилось, но новый номер не записался — проблема."""
    merged = wb.safe_merge(card, row)
    for item in merged["documents"]["items"]:
        if item.get("type") == 2:
            item["number"] = ""
    for char in merged["characteristics"]:
        if char.get("id") == 15001135:
            char["value"] = [""]

    assert wb.verify_integrity(card, merged, row)


def test_verify_integrity_ignores_declaration_replacement(card, row):
    """Штатная замена номера декларации не считается потерей данных."""
    before = copy.deepcopy(card)
    before["documents"]["items"].append(
        {"id": "doc-old", "type": 2, "number": "ЕАЭС N RU Д-RU.РА01.В.00001/24"}
    )
    merged = wb.safe_merge(before, row)
    assert wb.verify_integrity(before, merged, row) == []


def test_diff_marks_added_changed_and_kept(card, row):
    merged = wb.safe_merge(card, row)
    changes = wb.diff_cards(card, merged)
    levels = {change["field"]: change["level"] for change in changes}

    assert levels["characteristic «Номер декларации соответствия»"] == "added"
    assert levels["characteristic «Цвет»"] == "kept"
    assert not [change for change in changes if change["level"] == "removed"]


# ---------------------------------------------------------------------------
# Повторный прогон: идемпотентность
# ---------------------------------------------------------------------------


def test_already_applied_after_merge(card, row):
    assert wb.already_applied(card, row) is False
    merged = wb.safe_merge(card, row)
    assert wb.already_applied(merged, row) is True


def test_already_applied_requires_all_values(card, row):
    row.declaration = "ЕАЭС N RU Д-RU.РА01.В.12345/25"
    row.certificate = "ЕАЭС RU С-RU.АБ12.В.12345/25"
    partially = wb.safe_merge(card, make_row(certificate_number=None))
    assert wb.already_applied(partially, row) is False
