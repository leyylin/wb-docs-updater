# Дополнительные сценарии записи: токен посреди работы, «Стоп»,
# недоступный справочник характеристик, конфликты в каталоге.
import copy

import pytest

import wb_client as wb
from conftest import FakeResponse, InMemoryWBBackend, make_card, make_row

from test_integration import (
    SUBJECT_105_CHARCS,
    make_client,
    make_rows,
    wb_routes,
)


def test_write_token_error_marks_all_planned():
    """Токен отозвали между чтением и записью: карточки помечаются «Ошибка токена»."""
    cards = [make_card(nmID=10_000_001 + i, vendorCode=f"WB-TEST-{i + 1:04d}") for i in range(2)]
    backend = InMemoryWBBackend(cards, page_size=10)
    session = wb_routes(backend, {("POST", wb.UPDATE_CARDS_PATH): FakeResponse(403, {"errorText": "no content"})})

    results = wb.Updater(make_client(session), delay=0.0).run(make_rows(cards))

    assert [result.status for result in results] == [wb.STATUS_TOKEN] * 2
    assert backend.update_bodies == []


def test_charc_directory_failure_does_not_block_write():
    """Справочник характеристик недоступен: ничего не пишем молча — «Требует проверки»."""
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    session = wb_routes(backend, {("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(500)})

    results = wb.Updater(make_client(session, max_retries=0), delay=0.0).run(make_rows(cards))

    # Без справочника нельзя создать характеристики — честный «Требует проверки»,
    # а не «Обновлено» с молча потерянным номером.
    assert results[0].status == wb.STATUS_NEEDS_REVIEW, results[0].message
    assert "Номер декларации соответствия" in results[0].message
    assert backend.update_bodies == []


def test_stop_while_reading_catalog_is_not_api_error():
    """«Стоп» на этапе чтения каталога — это «Остановлено», а не «Ошибка API»."""
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    client = make_client(wb_routes(backend), stop_event=wb.threading.Event())
    client.stop_event.set()

    results = wb.Updater(client, delay=0.0).run(make_rows(cards))

    assert [result.status for result in results] == [wb.STATUS_STOPPED]
    assert backend.update_bodies == []


def test_stop_inside_write_marks_sent_and_unsent_differently():
    """Стоп посреди записи: отправленные — «не подтверждено», остальные — «Остановлено»."""
    cards = [make_card(nmID=10_000_000 + i, vendorCode=f"WB-TEST-{i:04d}") for i in range(1, 5)]
    backend = InMemoryWBBackend(cards, page_size=10)
    session = wb_routes(backend)
    client = make_client(session)
    updater = wb.Updater(client, batch_size=2, delay=0.0)
    original = backend.update_cards

    def stop_after_first(method, path, body):
        response = original(method, path, body)
        client.stop_event.set()  # имитируем нажатие «Стоп» во время записи
        return response

    session.routes[("POST", wb.UPDATE_CARDS_PATH)] = stop_after_first
    results = updater.run(make_rows(cards))

    statuses = [result.status for result in results]
    assert statuses.count(wb.STATUS_MISMATCH) == 2, statuses  # отправлены, но не проверены
    assert statuses.count(wb.STATUS_STOPPED) == 2, statuses


def test_merged_card_not_reflected_is_needs_review():
    """Если Safe-Merge не смог сохранить номер — карточка блокируется, а не пишется."""
    card = make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")
    backend = InMemoryWBBackend([card], page_size=10)
    original_safe_merge = wb.safe_merge

    def leaky_merge(source, document):
        merged = original_safe_merge(source, document)
        merged["characteristics"] = [item for item in merged["characteristics"] if item.get("id") != 15001135]
        return merged

    session = wb_routes(backend)
    client = make_client(session)
    wb.safe_merge = leaky_merge
    try:
        plan = wb.plan_apply(
            client, make_rows([card]), cards=client.get_all_cards(), charc_map=client.document_charc_ids([card])
        )
    finally:
        wb.safe_merge = original_safe_merge

    assert [result.status for result in plan.results] == [wb.STATUS_NEEDS_REVIEW]
    assert plan.cards_to_update == []


def test_duplicate_vendor_code_in_catalog_updates_first_and_warns():
    """Две карточки с одним vendorCode: пишем первую и предупреждаем в замечаниях."""
    cards = [
        make_card(nmID=10_000_001, vendorCode="WB-TEST-0001"),
        make_card(nmID=10_000_002, vendorCode="WB-TEST-0001"),
    ]
    backend = InMemoryWBBackend(cards, page_size=10)
    client = make_client(wb_routes(backend))
    row = make_row(vendorCode="WB-TEST-0001")

    plan = wb.plan_apply(client, [row], cards=client.get_all_cards(), charc_map=client.document_charc_ids(cards))

    assert [result.nm_id for result in plan.results] == [10_000_001]
    assert any("Найдено карточек" in issue for issue in row.issues)


def test_demo_client_snapshot_helpers():
    """Демо-клиент отдаёт снимок «до» и «сейчас» для инспектора JSON Diff."""
    demo = wb.DemoWBClient()
    nm_id = demo._cards[0]["nmID"]

    assert demo.snapshot(nm_id)["nmID"] == nm_id
    initial = demo.initial_snapshot(nm_id)
    assert initial == demo.snapshot(nm_id)
    assert demo.snapshot(-1) is None and demo.initial_snapshot(-1) is None

    # Снимок — копия: правка не протекает внутрь демо-базы.
    initial["title"] = "hacked"
    assert demo.snapshot(nm_id)["title"] != "hacked"


@pytest.mark.parametrize("slot", ["declaration", "certificate", "expiry"])
def test_doc_slots_have_valid_document_types(slot):
    """Каждый слот документов, который можно записать в documents.items, описан."""
    if slot == "expiry":
        assert slot not in wb.DOCUMENT_TYPE_BY_SLOT  # срок живёт в endDate
    else:
        assert wb.DOCUMENT_TYPE_BY_SLOT[slot] in (wb.DOCUMENT_TYPE_CERTIFICATE, wb.DOCUMENT_TYPE_DECLARATION)


def test_iso_date_normalizes_both_formats():
    assert wb._iso_date("31.12.2030") == wb._iso_date("2030-12-31T00:00:00Z") == "2030-12-31"
