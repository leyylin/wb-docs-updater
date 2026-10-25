"""Тесты сетевого слоя: ретраи, лимиты, пагинация и понятные ошибки.

``requests.Session`` не мокается через ``unittest.mock``: используется
управляемый двойник :class:`conftest.FakeSession`, который отдаёт заранее
заданные ответы и записывает все тела запросов. Так проверяется реальный путь
``WBClient._request → fetch_cards_page → send_update → Updater.run``.
"""

from __future__ import annotations

import time

import pytest

import wb_client as wb
from conftest import FakeResponse, FakeSession, InMemoryWBBackend, make_card, make_row


def make_client(session: FakeSession, **kwargs) -> wb.WBClient:
    client = wb.WBClient(token="eyJ-test", base_url="https://wb.test", session=session, **kwargs)
    # Убираем реальные паузы лимитера: интервал и окно = 0.
    client.read_limiter = wb.RateLimiter(10_000, 0.0)
    client.write_limiter = wb.RateLimiter(10_000, 0.0)
    return client


# ---------------------------------------------------------------------------
# Пагинация по курсору
# ---------------------------------------------------------------------------


def test_pagination_returns_all_cards_once():
    cards = [make_card(nmID=10_000_000 + i, vendorCode=f"V-{i}") for i in range(1, 8)]
    backend = InMemoryWBBackend(cards, page_size=2)
    session = backend.session()
    client = make_client(session)

    result = client.get_all_cards(limit=2)

    assert [card["nmID"] for card in result] == sorted(card["nmID"] for card in cards)
    assert len({card["nmID"] for card in result}) == len(cards)
    assert session.history[wb.GET_CARDS_PATH] == 4  # 3 полные страницы + пустая


def test_pagination_stops_on_same_cursor():
    """Сервер, повторяющий курсор, не должен зацикливать выгрузку."""
    cursor = {"updatedAt": "2026-09-01T09:01:00Z", "nmID": 10_000_001}
    session = FakeSession(
        {("POST", wb.GET_CARDS_PATH): FakeResponse(200, {"cards": [make_card()], "cursor": dict(cursor)})}
    )
    client = make_client(session)

    result = client.get_all_cards(limit=1)

    assert len(result) == 1
    assert session.history[wb.GET_CARDS_PATH] <= 3


def test_pagination_stops_on_empty_cursor():
    session = FakeSession(
        {("POST", wb.GET_CARDS_PATH): FakeResponse(200, {"cards": [make_card()], "cursor": {}})}
    )
    client = make_client(session)
    assert len(client.get_all_cards(limit=1)) == 1


def test_pagination_respects_limit_and_filter():
    backend = InMemoryWBBackend([make_card(nmID=10_000_000 + i) for i in range(1, 4)], page_size=10)
    session = backend.session()
    client = make_client(session)

    client.get_all_cards(limit=1000)

    body = session.requests[0].json
    assert body["cursor"]["limit"] == wb.GET_CARDS_PAGE_LIMIT  # ограничено официальным максимумом
    assert body["filter"] == {"withPhoto": -1}


def test_pagination_warns_when_page_limit_reached():
    """Упёрлись в max_pages — функция возвращает то, что успела прочитать."""
    cards = [make_card(nmID=10_000_000 + i) for i in range(1, 10)]
    session = InMemoryWBBackend(cards, page_size=1).session()
    client = make_client(session)

    result = client.get_all_cards(limit=1, max_pages=3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Ошибки и повторы
# ---------------------------------------------------------------------------


def test_429_is_retried_and_eventually_succeeds(no_sleep):
    ok = FakeResponse(200, {"cards": [], "cursor": {}})
    session = FakeSession(
        {("POST", wb.GET_CARDS_PATH): [FakeResponse(429, {"errorText": "too many"}, {"Retry-After": "1"}), ok]}
    )
    client = make_client(session)

    assert client.get_all_cards(limit=5) == []
    assert session.history[wb.GET_CARDS_PATH] == 2
    assert client.stats["retries"] == 1
    assert no_sleep == [1.0]  # Retry-After из заголовка уважается


def test_5xx_is_retried_with_backoff(no_sleep):
    ok = FakeResponse(200, {"cards": [], "cursor": {}})
    session = FakeSession({("POST", wb.GET_CARDS_PATH): [FakeResponse(503), FakeResponse(502), ok]})
    client = make_client(session)

    client.get_all_cards(limit=5)

    assert session.history[wb.GET_CARDS_PATH] == 3
    assert no_sleep == [10.0, 20.0]  # экспоненциальный backoff 10 с, 20 с


def test_429_gives_up_as_retryable_error(no_sleep):
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(429, {"errorText": "rate limit"})})
    client = make_client(session, max_retries=2)

    with pytest.raises(wb.WBAPIError) as excinfo:
        client.get_all_cards(limit=5)

    assert excinfo.value.retryable is True
    assert excinfo.value.status_code == 429
    assert session.history[wb.GET_CARDS_PATH] == 3


def test_401_raises_token_error_without_retries():
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(401, {"errorText": "unauthorized"})})
    client = make_client(session)

    with pytest.raises(wb.WBTokenError):
        client.check_token()
    assert session.history[wb.GET_CARDS_PATH] == 1  # повторы бессмысленны


def test_403_explains_missing_scope():
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(403, {"errorText": "no access"})})
    with pytest.raises(wb.WBTokenError, match="Контент"):
        make_client(session).check_token()


def test_400_surfaces_api_message():
    session = FakeSession(
        {("POST", wb.UPDATE_CARDS_PATH): FakeResponse(400, {"errorText": "MissingRequiredCharacteristics"})}
    )
    client = make_client(session)
    with pytest.raises(wb.WBAPIError, match="MissingRequiredCharacteristics") as excinfo:
        client.send_update([{"nmID": 1}])
    assert excinfo.value.retryable is False


def test_non_json_response_raises_clear_error():
    session = FakeSession({("POST", wb.GET_CARDS_PATH): FakeResponse(200, None, text="<html>proxy error</html>")})
    client = make_client(session)
    with pytest.raises(wb.WBAPIError, match="JSON"):
        client.get_all_cards()


def test_network_error_is_wrapped_after_retries(no_sleep, monkeypatch):
    class Exploding(FakeSession):
        def request(self, method, url, json=None, timeout=None):
            raise wb.requests.ConnectionError("boom")

    client = make_client(Exploding({}), max_retries=1)
    with pytest.raises(wb.WBAPIError, match="Сетевая ошибка"):
        client.get_all_cards()
    assert client.stats["retries"] == 2


def test_empty_token_rejected():
    with pytest.raises(ValueError, match="токен"):
        wb.WBClient(token="   ")


def test_bearer_prefix_stripped():
    session = FakeSession({})
    client = wb.WBClient(token="Bearer eyJ-abc", session=session)
    assert client.token == "eyJ-abc"
    assert session.headers["Authorization"] == "eyJ-abc"


# ---------------------------------------------------------------------------
# Лимитер
# ---------------------------------------------------------------------------


def test_rate_limiter_spaces_requests_by_interval():
    limiter = wb.RateLimiter(per_minute=1000, interval=0.05)
    started = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - started

    assert elapsed >= 0.15, f"интервал не соблюдён: {elapsed:.3f} с < 0.15 с"


def test_rate_limiter_enforces_per_minute_window(monkeypatch):
    """Третий запрос при лимите 2/мин упирается в окно скользящей минуты."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock["now"])
    limiter = wb.RateLimiter(per_minute=2, interval=0.0)
    limiter.acquire()
    limiter.acquire()

    class StopOnFirstCheck(wb.threading.Event):
        """Как только лимитер начал ждать — «пользователь нажал стоп»."""

        def is_set(self) -> bool:
            return True

    with pytest.raises(InterruptedError):
        limiter.acquire(StopOnFirstCheck())
    assert len(limiter._stamps) == 2  # третий запрос не был отправлен


def test_rate_limiter_releases_slot_after_a_minute(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock["now"])
    limiter = wb.RateLimiter(per_minute=2, interval=0.0)
    limiter.acquire()
    limiter.acquire()
    clock["now"] += 61.0
    limiter.acquire()  # окно прошло — снова можно
    assert len(limiter._stamps) == 1



def test_rate_limiter_stop_event_interrupts_wait():
    limiter = wb.RateLimiter(per_minute=1, interval=60.0)
    limiter.acquire()
    stop = wb.threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        limiter.acquire(stop)


# ---------------------------------------------------------------------------
# update_cards: паузы, батчи, ретраи на уровне пачки
# ---------------------------------------------------------------------------


class RecordingClient(wb.BaseWBClient):
    mode = "real"

    def __init__(self, responses=None):
        super().__init__()
        self.calls: list[list[dict]] = []
        self.responses = list(responses or [])

    def send_update(self, payload):
        self.calls.append(list(payload))
        if self.responses:
            outcome = self.responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"error": False}


def test_update_cards_batches_and_counts():
    client = RecordingClient()
    cards = [{"nmID": i, "vendorCode": f"V{i}", "characteristics": [], "sizes": []} for i in range(5)]

    outcome = client.update_cards(cards, batch_size=2, delay=0.0)

    assert outcome["ok"] is True
    assert outcome["batches"] == 3
    assert outcome["updated"] == 5
    assert [len(call) for call in client.calls] == [2, 2, 1]


def test_update_cards_retries_retryable_error(no_sleep):
    client = RecordingClient(
        [
            wb.WBAPIError("HTTP 429", 429, retryable=True),
            wb.WBAPIError("HTTP 500", 500, retryable=True),
            {"error": False},
        ]
    )
    outcome = client.update_cards([{"nmID": 1, "vendorCode": "V", "characteristics": [], "sizes": []}])

    assert outcome["ok"] is True
    assert len(client.calls) == 3
    assert no_sleep == [6.0, 12.0]


def test_update_cards_stops_after_retry_budget(no_sleep):
    client = RecordingClient([wb.WBAPIError("HTTP 500", 500, retryable=True)] * 10)
    outcome = client.update_cards([{"nmID": 1, "vendorCode": "V", "characteristics": [], "sizes": []}])

    assert outcome["ok"] is False
    assert "500" in outcome["error"]
    assert len(client.calls) == 4  # 1 попытка + 3 повтора


def test_update_cards_does_not_retry_400(no_sleep):
    client = RecordingClient([wb.WBAPIError("HTTP 400", 400)])
    outcome = client.update_cards([{"nmID": 1, "vendorCode": "V", "characteristics": [], "sizes": []}])

    assert outcome["ok"] is False
    assert len(client.calls) == 1


def test_update_cards_propagates_token_error():
    client = RecordingClient([wb.WBTokenError("401", 401)])
    with pytest.raises(wb.WBTokenError):
        client.update_cards([{"nmID": 1, "vendorCode": "V", "characteristics": [], "sizes": []}])


def test_update_cards_pauses_between_batches(no_sleep, monkeypatch):
    """В боевом режиме между пачками выдерживается пауза из настроек."""
    client = RecordingClient()
    cards = [{"nmID": i, "vendorCode": f"V{i}", "characteristics": [], "sizes": []} for i in range(3)]
    base = [100.0]

    def fake_monotonic():
        base[0] += 1.0  # каждый вызов — +1 с «прошло»
        return base[0]

    monkeypatch.setattr(wb.time, "monotonic", fake_monotonic)
    client.update_cards(cards, batch_size=1, delay=6.0)

    assert any(value > 0 for value in no_sleep), "пауза между батчами не выдержана"
    assert client.stats["sleep_seconds"] > 0


def test_demo_client_does_not_pause(no_sleep, cards_file):
    cards = [make_card(nmID=10_000_000 + i, vendorCode=f"V{i}") for i in range(1, 4)]
    client = wb.DemoWBClient(cards_path=cards_file(cards))
    payload = [
        {"nmID": card["nmID"], "vendorCode": card["vendorCode"], "characteristics": card["characteristics"],
         "sizes": card["sizes"]}
        for card in cards
    ]

    client.update_cards(payload, batch_size=1, delay=6.0)
    assert no_sleep == []  # демо-режим не ждёт: серверных лимитов нет


def test_stop_event_aborts_batches():
    client = RecordingClient()
    cards = [{"nmID": i, "vendorCode": f"V{i}", "characteristics": [], "sizes": []} for i in range(4)]
    client.stop_event.set()

    # «Стоп» — это не сбой API: update_cards поднимает InterruptedError,
    # чтобы Updater пометил карточки «Остановлено», а не «Ошибка API».
    with pytest.raises(InterruptedError):
        client.update_cards(cards, batch_size=1)

    assert client.calls == []


def test_api_level_error_stops_remaining_batches():
    client = RecordingClient([{"error": True, "errorText": "MissingRequiredCharacteristics"}])
    cards = [{"nmID": i, "vendorCode": f"V{i}", "characteristics": [], "sizes": []} for i in range(4)]

    outcome = client.update_cards(cards, batch_size=1)

    assert outcome["ok"] is False
    assert "MissingRequiredCharacteristics" in outcome["error"]
    assert len(client.calls) == 1
