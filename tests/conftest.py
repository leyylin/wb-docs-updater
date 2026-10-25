"""Общие фикстуры и тестовые двойники для набора pytest.

Набор проверяет проект двумя уровнями:

* **юнит-уровень** — чистые функции ``wb_client`` (Safe-Merge, парсеры,
  проверка целостности) без сети;
* **интеграционный уровень** — :class:`WBClient` с подменённой
  ``requests.Session`` (см. :class:`FakeSession`), чтобы проверить реальный
  цикл «чтение → слияние → запись → пост-проверка», ретраи и лимиты.

``app.py`` тянет ``streamlit``, поэтому его импорт защищён флагом: тесты
валидации Excel используют ``pytest.importorskip``.
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import threading  # noqa: E402

import wb_client as wb  # noqa: E402  (после правки sys.path)


# ---------------------------------------------------------------------------
# Построение карточек
# ---------------------------------------------------------------------------


def make_card(**overrides: Any) -> dict[str, Any]:
    """Реалистичная карточка WB в формате ``/content/v2/get/cards/list``."""
    card: dict[str, Any] = {
        "nmID": 10_000_001,
        "imtID": 20_000_001,
        "nmUUID": "00000000-0000-4000-8000-000000000001",
        "subjectID": 105,
        "subjectName": "Футболки",
        "vendorCode": "WB-TEST-0001",
        "brand": "Base Line",
        "title": "Футболка Base Line #0001",
        "description": "Практичная модель. Состав: 100% хлопок.",
        "needKiz": False,
        "photos": [{"big": f"https://basket.example/{10_000_001}/1.webp"}],
        "video": "",
        "sizes": [
            {"chrtID": 500_002, "techSize": "S", "wbSize": "S", "skus": ["20000000000001"]},
            {"chrtID": 500_003, "techSize": "M", "wbSize": "M", "skus": ["20000000000002"]},
        ],
        "dimensions": {"length": 31, "width": 23, "height": 4, "weightBrutto": 0.21},
        "characteristics": [
            {"id": 1, "name": "Наименование", "value": ["Футболка Base Line #0001"]},
            {"id": 12, "name": "Цвет", "value": ["Чёрный"]},
            {"id": 13, "name": "Состав", "value": ["100% хлопок"]},
            {"id": 17, "name": "Комплектация", "value": ["Футболка — 1 шт."]},
            {"id": 21, "name": "Модель", "value": ["BL-0001"]},
            # Пустое значение тоже обязано пережить слияние.
            {"id": 99, "name": "Декоративные элементы", "value": [""]},
        ],
        "tags": [{"id": 101, "name": "Новинки"}],
        "createdAt": "2026-06-03T09:01:00Z",
        "updatedAt": "2026-09-01T09:01:00Z",
        "documents": {
            "items": [
                {
                    "id": "doc-1-cgr",
                    "type": 3,
                    "number": "RU.77.99.11.001.R.000123.01.24",
                    "productNumber": "",
                    "tradeName": "Футболка Base Line #0001",
                    "applicant": "ООО «Бейс Лайн»",
                    "startDate": "2024-01-01T00:00:00Z",
                    "endDate": "2029-01-01T00:00:00Z",
                    "isEndless": False,
                }
            ],
            "excludeDocuments": False,
            "overallVerdict": {"status": "valid", "reasons": [], "date": "2026-09-01T10:00:00Z"},
        },
    }
    card.update(overrides)
    return card


def make_row(**overrides: Any) -> wb.DocumentRow:
    """Строка прайса с предзаполненными charcID (как после плана)."""
    payload = {
        "vendorCode": "WB-TEST-0001",
        "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.12345/25",
        "certificate_number": None,
        "expiry_date": "31.12.2030",
    }
    payload.update(overrides)
    row = wb.normalize_row(payload)
    row.charc_ids = {
        "declaration": 15001135,
        "certificate": 15001136,
        "expiry": 15001138,
    }
    return row


def prices(card: dict[str, Any]) -> dict[str, list[Any]]:
    """Характеристики карточки в виде ``{название: значения}``."""
    return {
        str(item.get("name")): list(item.get("value") or [])
        for item in card.get("characteristics") or []
        if isinstance(item, dict)
    }


def charc_ids(card: dict[str, Any]) -> dict[int, Any]:
    return {int(item["id"]): item.get("value") for item in card.get("characteristics") or [] if "id" in item}


# ---------------------------------------------------------------------------
# Поддельный ``requests.Session``
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    url: str
    json: Any


class FakeResponse:
    """Минимальный двойник :class:`requests.Response`."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("No JSON object could be decoded")
        return copy.deepcopy(self._payload)


Handler = Callable[[str, str, Any], FakeResponse]


class FakeSession:
    """Очередь ответов по ``(method, path)`` + запись всех запросов.

    ``routes`` — словарь ``(method, path) -> handler | [responses]``.
    Если список ответов исчерпан, отдаётся последний (удобно для 429-сценариев).
    """

    def __init__(
        self,
        routes: dict[tuple[str, str], Any],
        default: Any = None,
    ) -> None:
        self.routes = routes
        self.default = default
        self.headers: dict[str, str] = {}
        self.requests: list[RecordedRequest] = []
        self.history: dict[str, int] = {}
        self._state: dict[tuple[str, str], int] = {}

    @staticmethod
    def path_of(url: str) -> str:
        return "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]

    def request(self, method: str, url: str, json: Any = None, timeout: float | None = None) -> FakeResponse:
        path = self.path_of(url)
        self.requests.append(RecordedRequest(method, url, copy.deepcopy(json)))
        key = (method.upper(), path)
        self.history[path] = self.history.get(path, 0) + 1

        route = self.routes.get(key, self.default)
        if route is None:
            return FakeResponse(404, {"errorText": f"unmapped route {method} {path}"})
        if callable(route):
            result = route(method, path, json)
        elif isinstance(route, list):
            index = self._state.get(key, 0)
            self._state[key] = index + 1
            result = route[min(index, len(route) - 1)]
        else:
            result = route
        return copy.deepcopy(result)

    # --- помощники ---
    def updates(self) -> list[list[dict[str, Any]]]:
        """Тела всех запросов на запись (каждый — батч)."""
        return [req.json for req in self.requests if req.url.rstrip("/").endswith(wb.UPDATE_CARDS_PATH)]

    def update_payloads(self) -> list[dict[str, Any]]:
        return [card for batch in self.updates() for card in batch]


class InMemoryWBBackend:
    """Эмуляция серверной части Content API v2 на FakeSession.

    Отдаёт карточки по курсору и «применяет» запись — но, как настоящий WB,
    **полностью заменяет** ``characteristics`` и ``documents`` тем, что пришло
    в теле запроса. Любое отличие в теле от исходной карточки видно снаружи.
    """

    def __init__(self, cards: Iterable[dict[str, Any]], page_size: int = 2) -> None:
        self.cards = {card["nmID"]: copy.deepcopy(card) for card in cards}
        self.page_size = page_size
        self.update_bodies: list[list[dict[str, Any]]] = []

    def _ordered(self) -> list[dict[str, Any]]:
        return sorted(
            self.cards.values(),
            key=lambda card: (str(card.get("updatedAt") or ""), int(card.get("nmID") or 0)),
        )

    def list_cards(self, method: str, path: str, body: Any) -> FakeResponse:
        cursor = (body or {}).get("cursor") or {}
        limit = int(cursor.get("limit") or self.page_size)
        ordered = self._ordered()
        start = 0
        marker = cursor.get("nmID")
        if marker:
            index = next((i for i, card in enumerate(ordered) if card.get("nmID") == marker), None)
            start = len(ordered) if index is None else index + 1
        page = ordered[start : start + limit]
        next_cursor: dict[str, Any] = {}
        if page and start + limit < len(ordered):
            next_cursor = {"updatedAt": page[-1]["updatedAt"], "nmID": page[-1]["nmID"]}
        return FakeResponse(200, {"cards": copy.deepcopy(page), "cursor": next_cursor})

    def update_cards(self, method: str, path: str, body: Any) -> FakeResponse:
        self.update_bodies.append(copy.deepcopy(body))
        for item in body:
            stored = self.cards.get(item.get("nmID"))
            if stored is None:
                return FakeResponse(200, {"data": None, "error": True, "errorText": "не найдено"})
            # WB перезаписывает именно те контейнеры, которые есть в теле.
            stored["characteristics"] = copy.deepcopy(item.get("characteristics") or [])
            stored["sizes"] = copy.deepcopy(item.get("sizes") or [])
            if "dimensions" in item:
                stored["dimensions"] = copy.deepcopy(item["dimensions"])
            if "documents" in item:
                stored["documents"] = copy.deepcopy(item["documents"])
            stored["updatedAt"] = "2026-10-01T00:00:00Z"
        return FakeResponse(200, {"data": None, "error": False, "errorText": ""})

    def session(self, extra: dict[tuple[str, str], Any] | None = None) -> FakeSession:
        routes: dict[tuple[str, str], Any] = {
            ("POST", wb.GET_CARDS_PATH): self.list_cards,
            ("POST", wb.UPDATE_CARDS_PATH): self.update_cards,
        }
        routes.update(extra or {})
        return FakeSession(routes)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture()
def card() -> dict[str, Any]:
    return make_card()


@pytest.fixture()
def row() -> wb.DocumentRow:
    return make_row()


@pytest.fixture()
def cards_file(tmp_path: Path) -> Callable[[list[dict[str, Any]]], Path]:
    """Фабрика JSON-файлов с карточками для :class:`wb.DemoWBClient`."""
    counter = {"n": 0}

    def _factory(cards: list[dict[str, Any]]) -> Path:
        counter["n"] += 1
        target = tmp_path / f"cards_{counter['n']}.json"
        target.write_text(json.dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8")
        return target

    return _factory


@pytest.fixture()
def demo_client(cards_file: Callable[[list[dict[str, Any]]], Path]) -> wb.DemoWBClient:
    """Демо-клиент на маленькой мок-базе (файлы на диске не трогаются)."""
    cards = [
        make_card(nmID=10_000_000 + i, vendorCode=f"WB-TEST-{i:04d}", imtID=20_000_000 + i)
        for i in range(1, 6)
    ]
    return wb.DemoWBClient(cards_path=cards_file(cards))


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Блокирует реальные паузы ``stop_event.wait`` и запоминает их длительность.

    Клиент не спит по-настоящему: иначе тесты ретраев шли бы минутами.
    """
    sleeps: list[float] = []

    def fake_wait(self: threading.Event, timeout: float | None = None) -> bool:
        if timeout:
            sleeps.append(float(timeout))
        return self.is_set()

    monkeypatch.setattr(threading.Event, "wait", fake_wait)
    yield sleeps
