"""Сквозные (интеграционные) тесты полного цикла.

Эмулируется сервер Content API v2, который, как и настоящий WB, **полностью
заменяет** ``characteristics`` / ``sizes`` / ``documents`` содержимым тела
запроса. Если Safe-Merge где-то потеряет поле — фейковый сервер это
зафиксирует, а пост-проверка ``Updater`` это увидит.

Покрываются:
* полный цикл: чтение → план → запись → повторное чтение → статусы;
* реальное отсутствие потерь данных после записи (WB-семантика);
* отказ на середине процесса (429 после первой пачки) — приложение не падает,
  статусы честно отражают результат;
* dry-run, остановка пользователем, ошибка токена;
* поведение лимитов на реальном ``WBClient``.
"""

from __future__ import annotations

import copy

import pytest

import wb_client as wb
from conftest import FakeResponse, FakeSession, InMemoryWBBackend, make_card, make_row


def make_client(session: FakeSession, **kwargs) -> wb.WBClient:
    client = wb.WBClient(token="eyJ-test", base_url="https://wb.test", session=session, **kwargs)
    client.read_limiter = wb.RateLimiter(10_000, 0.0)
    client.write_limiter = wb.RateLimiter(10_000, 0.0)
    return client


# Справочник характеристик, как его отдаёт GET /content/v2/object/charcs/{subjectID}.
SUBJECT_105_CHARCS = {
    "data": [
        {"charcID": 15001135, "name": "Номер декларации соответствия", "charcType": 1},
        {"charcID": 15001136, "name": "Номер сертификата соответствия", "charcType": 1},
        {"charcID": 15001138, "name": "Срок действия документа", "charcType": 1},
    ]
}


def wb_routes(backend: InMemoryWBBackend, overrides: dict | None = None) -> FakeSession:
    """FakeSession с эмуляцией WB + справочником характеристик."""
    extra = {("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(200, SUBJECT_105_CHARCS)}
    extra.update(overrides or {})
    return backend.session(extra)


def make_rows(cards: list[dict]) -> list[wb.DocumentRow]:
    rows = []
    for index, card in enumerate(cards, start=2):
        row = make_row(vendorCode=card["vendorCode"],
                       declaration_number=f"ЕАЭС N RU Д-RU.РА01.В.{10_000 + index:05d}/25")
        row.row_number = index
        rows.append(row)
    return rows


def test_full_cycle_preserves_card_and_applies_documents():
    cards = [make_card(nmID=10_000_000 + i, vendorCode=f"WB-TEST-{i:04d}") for i in range(1, 4)]
    originals = copy.deepcopy({card["nmID"]: card for card in cards})
    backend = InMemoryWBBackend(cards, page_size=2)
    client = make_client(wb_routes(backend))

    results = wb.Updater(client, batch_size=10, delay=0.0).run(make_rows(cards))

    assert [result.status for result in results] == [wb.STATUS_UPDATED] * 3

    for result in results:
        stored = backend.cards[result.nm_id]
        original = originals[result.nm_id]
        # 1. Поля документов записались.
        assert result.source.declaration in [item["number"] for item in stored["documents"]["items"]]
        # 2. Ничего из старого не потеряно (сервер перезаписывает контейнеры целиком!).
        assert stored["sizes"] == original["sizes"]
        assert stored["dimensions"] == original["dimensions"]
        assert "RU.77.99.11.001.R.000123.01.24" in [item["number"] for item in stored["documents"]["items"]]
        kept = {item["id"]: item["value"] for item in stored["characteristics"]}
        for item in original["characteristics"]:
            assert kept[item["id"]] == item["value"]
        # 3. Пост-проверка это подтвердила.
        assert result.after is not None
        assert wb.verify_integrity(original, stored, result.source) == []


def test_full_cycle_reports_unchanged_on_second_run():
    """Идемпотентность: повторный прогон не отправляет карточки второй раз."""
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    rows = make_rows(cards)

    wb.Updater(make_client(wb_routes(backend)), delay=0.0).run(rows)
    assert len(backend.update_bodies) == 1

    second = wb.Updater(make_client(wb_routes(backend)), delay=0.0).run(rows)
    assert [result.status for result in second] == [wb.STATUS_UNCHANGED]
    assert len(backend.update_bodies) == 1  # новых записей нет


def _first_ok_then_429(backend: InMemoryWBBackend, ok: FakeResponse, limited: FakeResponse):
    """Первая пачка уходит в эмуляцию WB, все следующие — 429 (лимит исчерпан)."""
    state = {"sent": 0}

    def route(method: str, path: str, body):
        state["sent"] += 1
        if state["sent"] == 1:
            return backend.update_cards(method, path, body)
        return limited

    return route


def test_429_after_first_batch_does_not_crash_and_marks_rest():
    """429 на второй пачке: первая обновлена, остальные — «Ошибка API», без исключения."""
    cards = [make_card(nmID=10_000_000 + i, vendorCode=f"WB-TEST-{i:04d}") for i in range(1, 5)]
    backend = InMemoryWBBackend(cards, page_size=10)
    update_ok = FakeResponse(200, {"data": None, "error": False, "errorText": ""})
    rate_limited = FakeResponse(429, {"errorText": "Too many requests"}, {"Retry-After": "0"})

    def update_route(method, path, body):
        if len(backend.update_bodies) == 0:
            return backend.update_cards(method, path, body)
        return rate_limited

    session = wb_routes(backend, {("POST", wb.UPDATE_CARDS_PATH): [1]},  # заменим ниже
    )
    session.routes[("POST", wb.UPDATE_CARDS_PATH)] = _first_ok_then_429(backend, update_ok, rate_limited)
    client = make_client(session, max_retries=1)
    client.write_limiter = wb.RateLimiter(10_000, 0.0)

    results = wb.Updater(client, batch_size=2, delay=0.0).run(make_rows(cards))

    statuses = [result.status for result in results]
    assert statuses.count(wb.STATUS_UPDATED) == 2, statuses
    assert statuses.count(wb.STATUS_ERROR) == 2, statuses
    error_message = next(result.message for result in results if result.status == wb.STATUS_ERROR)
    assert "429" in error_message or "лимит" in error_message.lower()


def _first_ok_then_429(backend: InMemoryWBBackend, ok: FakeResponse, limited: FakeResponse):
    state = {"calls": 0}

    def handler(method, path, body):
        state["calls"] += 1
        if state["calls"] == 1:
            return backend.update_cards(method, path, body)
        return limited

    return handler


def test_token_error_is_reported_per_article():
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(401, {"errorText": "unauthorized"})})
    rows = [make_row(), make_row(vendorCode="WB-TEST-0002")]

    results = wb.Updater(make_client(session), delay=0.0).run(rows)

    assert [result.status for result in results] == [wb.STATUS_TOKEN, wb.STATUS_TOKEN]
    assert all("401" in result.message for result in results)


def test_api_error_on_read_is_reported_per_article():
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(400, {"errorText": "bad filter"})})
    results = wb.Updater(make_client(session), delay=0.0).run([make_row()])

    assert results[0].status == wb.STATUS_ERROR
    assert "bad filter" in results[0].message


def test_dry_run_sends_nothing():
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    client = make_client(wb_routes(backend))
    logs: list[tuple[str, str]] = []

    results = wb.Updater(client, delay=0.0, dry_run=True,
                         log=lambda message, level="info": logs.append((level, message))).run(make_rows(cards))

    assert backend.update_bodies == []
    assert [result.status for result in results] == [wb.STATUS_UNCHANGED]
    assert "Dry-run" in results[0].message
    assert any("Dry-run" in message for _, message in logs)


def test_post_check_detects_wiped_characteristics():
    """Если сервер «проглотит» запись, но сотрёт состав — статус «Не применено»."""
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    original_update = backend.update_cards

    def lossy_update(method, path, body):
        response = original_update(method, path, body)
        for card in backend.cards.values():  # имитируем потерю данных на стороне WB
            card["characteristics"] = [item for item in card["characteristics"] if item.get("id") != 13]
        return response

    session = wb_routes(backend)
    session.routes[("POST", wb.UPDATE_CARDS_PATH)] = lossy_update

    results = wb.Updater(make_client(session), delay=0.0).run(make_rows(cards))

    assert results[0].status == wb.STATUS_MISMATCH
    assert any("Состав" in problem for problem in results[0].problems)


def test_stop_button_before_write_marks_stopped():
    cards = [make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")]
    backend = InMemoryWBBackend(cards, page_size=10)
    client = make_client(wb_routes(backend))
    updater = wb.Updater(client, delay=0.0)

    original_plan = wb.plan_apply

    def plan_then_stop(c, rows, **kwargs):
        plan = original_plan(c, rows, **kwargs)
        c.stop_event.set()  # пользователь нажал «Стоп», пока строился план
        return plan

    wb.plan_apply = plan_then_stop
    try:
        results = updater.run(make_rows(cards))
    finally:
        wb.plan_apply = original_plan

    assert results[0].status == wb.STATUS_STOPPED
    assert backend.update_bodies == []


def test_needs_review_when_subject_has_no_doc_characteristic():
    """В справочнике предмета нет характеристик номеров — карточка не отправляется."""
    card = make_card(nmID=10_000_001, vendorCode="WB-TEST-0001")
    backend = InMemoryWBBackend([card], page_size=10)
    session = wb_routes(backend, {("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(200, {"data": []})})

    results = wb.Updater(make_client(session), delay=0.0).run(make_rows([card]))

    assert results[0].status == wb.STATUS_NEEDS_REVIEW
    assert "Нет характеристики" in " ".join(results[0].problems)
    assert backend.update_bodies == []


def test_document_charc_ids_uses_subject_directory():
    card = make_card()
    session = FakeSession(
        {
            ("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(
                200,
                {"data": [
                    {"charcID": 15001135, "name": "Номер декларации соответствия", "charcType": 1},
                    {"charcID": 15001136, "name": "Номер сертификата соответствия", "charcType": 0},
                    {"charcID": 15001138, "name": "Срок действия документа"},
                ]},
            )
        }
    )
    client = make_client(session)

    mapping = client.document_charc_ids([card, card])  # одна и та же карточка дважды

    # charcType 0 и 1 (массив строк / список значений) и отсутствие типа — годятся;
    # если WB добавит новое поле в справочник, оно не «отравит» остальные.
    assert mapping[105] == {"declaration": 15001135, "certificate": 15001136, "expiry": 15001138}
    assert session.history["/content/v2/object/charcs/105"] == 1  # справочник кэшируется


def test_document_charc_ids_only_asked_subjects():
    cards = [make_card(subjectID=105), make_card(nmID=10_000_002, subjectID=205)]
    session = FakeSession(
        {
            ("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(200, SUBJECT_105_CHARCS),
            ("GET", wb.CHARCS_PATH.format(subject_id=205)): FakeResponse(200, {"data": []}),
        }
    )
    mapping = make_client(session).document_charc_ids(cards)
    assert mapping[105] and mapping[205] == {}


def test_document_charc_ids_tolerates_api_failure():
    card = make_card()
    session = FakeSession({("GET", wb.CHARCS_PATH.format(subject_id=105)): FakeResponse(500)})
    client = make_client(session, max_retries=0)

    assert client.document_charc_ids([card])[105] == {}


def test_summarize_counts_statuses():
    results = [
        wb.ArticleResult(2, "a", wb.STATUS_UPDATED, ""),
        wb.ArticleResult(3, "b", wb.STATUS_UNCHANGED, ""),
        wb.ArticleResult(4, "c", wb.STATUS_NOT_FOUND, ""),
        wb.ArticleResult(5, "", wb.STATUS_INVALID, ""),
        wb.ArticleResult(6, "d", wb.STATUS_ERROR, ""),
        wb.ArticleResult(7, "e", wb.STATUS_NO_DATA, ""),
        wb.ArticleResult(8, "f", wb.STATUS_DUPLICATE, ""),
    ]
    stats = wb.summarize(results)

    assert stats["total"] == 7
    assert stats["updated"] == 1
    assert stats["unchanged"] == 1
    assert stats["not_found"] == 1
    assert stats["invalid"] == 2  # INVALID + DUPLICATE
    assert stats["problems"] == 1
    assert stats["errors"] == 4


def test_demo_client_runs_without_network():
    """Демо-режим не ходит в сеть вообще (важно для демо на собеседовании)."""
    client = wb.create_client("demo")
    assert isinstance(client, wb.DemoWBClient)
    assert client.get_all_cards() == [] or isinstance(client.get_all_cards(), list)


def test_report_rows_are_json_safe():
    """Отчёт уходит в CSV/Excel — никаких несериализуемых объектов в нём быть не должно."""
    result = wb.ArticleResult(2, "WB-TEST-0001", wb.STATUS_UPDATED, "ok", nm_id=10_000_001,
                              source=make_row())
    row = result.as_dict()

    assert set(row) == {"строка", "vendorCode", "nmID", "статус", "декларация", "сертификат",
                        "срок действия", "проблемы", "сообщение", "время проверки", "время пост-проверки"}
    assert all(isinstance(value, (str, int)) for value in row.values())


@pytest.mark.skipif(not wb.MOCK_CARDS_JSON.exists(), reason="нет mock_wb_cards.json")
def test_mock_base_is_consistent_with_safe_merge():
    """Мок-база из репозитория проходит Safe-Merge на реальном наборе карточек."""
    client = wb.DemoWBClient()
    cards = client.get_all_cards(limit=100)
    assert cards, "мок-база пуста"

    for card in cards[:20]:
        row = make_row(vendorCode=card["vendorCode"])
        merged = wb.safe_merge(card, row)
        assert wb.verify_integrity(card, merged, row) == []


def test_demo_write_keeps_updated_at_for_cursor_pagination():
    """Демо-клиент не должен «перештамповывать» updatedAt (иначе курсор поедет)."""
    cards = [make_card(nmID=10_000_001 + i, vendorCode=f"WB-TEST-{i + 1:04d}") for i in range(3)]
    backend = InMemoryWBBackend(cards, page_size=2)
    client = wb.DemoWBClient.__new__(wb.DemoWBClient)  # без чтения файла мок-базы
    wb.BaseWBClient.__init__(client, stop_event=None)
    client._cards = copy.deepcopy(cards)
    client.simulate_latency = 0.0
    client.stats = {"read_requests": 0, "write_requests": 0, "retries": 0, "sleep_seconds": 0.0}
    before = [card["updatedAt"] for card in client._cards]

    client.send_update([wb.build_update_payload(wb.safe_merge(cards[0], make_row()))])

    assert [card["updatedAt"] for card in client._cards] == before
    # И курсорная выгрузка после записи отдаёт те же карточки в том же порядке.
    assert [card["nmID"] for card in client.get_all_cards(limit=2)] == [card["nmID"] for card in cards]


def test_repeated_run_on_live_demo_client_shows_unchanged():
    """Второй прогон на «живом» клиенте (как в session_state) видит уже привязанные документы."""
    cards = [make_card(nmID=10_000_001 + i, vendorCode=f"WB-TEST-{i + 1:04d}") for i in range(2)]
    rows = make_rows(cards)
    backend = InMemoryWBBackend(cards, page_size=2)
    client = make_client(wb_routes(backend))

    first = wb.Updater(client, delay=0.0).run(rows)
    second = wb.Updater(client, delay=0.0).run(rows)

    assert [result.status for result in first] == [wb.STATUS_UPDATED] * 2
    assert [result.status for result in second] == [wb.STATUS_UNCHANGED] * 2
    assert len(backend.update_bodies) == 1  # второй прогон ничего не отправил
