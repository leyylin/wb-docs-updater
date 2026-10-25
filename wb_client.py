"""Клиент Wildberries Content API v2 с безопасным слиянием (Safe-Merge).

Главная проблема: ``POST /content/v2/cards/update`` перезаписывает карточку
целиком — и массив ``characteristics``, и объект ``documents``. Если отправить
только документ, цвет, состав, комплектация, габариты и ранее прикреплённые
документы будут стёрты.

Решение:

1. Читаем карточки продавца через ``POST /content/v2/get/cards/list``
   с пагинацией по курсору (``settings.cursor``).
2. Находим карточку по ``vendorCode``.
3. Копируем её целиком: ``nmID``, ``imtID``, ``vendorCode``, ``sizes``
   (с ``chrtID``), ``dimensions``, все ``characteristics`` и все ``documents``.
4. Точечно дописываем номера декларации/сертификата и срок действия —
   в существующие характеристики или в ``documents.items``.
5. Отправляем полный объект обратно в ``POST /content/v2/cards/update``
   батчами с учётом лимитов (10 запросов/мин, интервал 6 с) и повторяем
   запросы при 429/5xx.
6. Перечитываем карточки и убеждаемся, что документы применились, а старые
   данные остались на месте.

В режиме «Демо / Песочница» :class:`DemoWBClient` подменяет сетевой слой:
хранит карточки в памяти, отдаёт их страницами по курсору и реально
применяет обновления, поэтому весь сценарий проходит без API-ключа.

Самопроверка::

    python wb_client.py
"""

from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

BASE_DIR = Path(__file__).resolve().parent
MOCK_CARDS_JSON = BASE_DIR / "mock_wb_cards.json"

WB_BASE_URL = "https://content-api.wildberries.ru"
GET_CARDS_PATH = "/content/v2/get/cards/list"
UPDATE_CARDS_PATH = "/content/v2/cards/update"
CHARCS_PATH = "/content/v2/object/charcs/{subject_id}"

# Официальные лимиты на профиль продавца.
UPDATE_BATCH_LIMIT = 100  # сколько карточек кладём в один батч (API примет до 3000)
GET_CARDS_PAGE_LIMIT = 100
UPDATE_RATE_INTERVAL = 6.0  # сек между запросами cards/update
UPDATE_RATE_PER_MINUTE = 10
READ_RATE_INTERVAL = 0.6  # сек между остальными запросами категории «Контент»
READ_RATE_PER_MINUTE = 100

DOCUMENT_TYPE_CERTIFICATE = 1
DOCUMENT_TYPE_DECLARATION = 2

# Диапазон Excel-серийников, которые считаем датой: 1900-01-01 .. 2100-12-31.
# Нужен, чтобы числа вроде артикула «44205» не превращались в случайную дату.
EXCEL_SERIAL_MIN = 1
EXCEL_SERIAL_MAX = 73_450

# Слоты полей документов в прайсе.
DOC_SPECS: tuple[str, ...] = ("declaration", "certificate", "expiry")

DOC_CHARACTERISTIC_NAMES: dict[str, str] = {
    "declaration": "Номер декларации соответствия",
    "certificate": "Номер сертификата соответствия",
    "expiry": "Срок действия документа",
}

# Как поле может называться в характеристиках разных предметов.
_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "declaration": (
        "номер декларации соответствия",
        "номер декларации о соответствии",
        "номер декларации",
        "декларация о соответствии",
    ),
    "certificate": (
        "номер сертификата соответствия",
        "номер сертификата",
        "сертификат соответствия",
    ),
    "expiry": (
        "срок действия документа",
        "дата окончания срока действия",
        "срок действия",
        "действует до",
    ),
}

_NAME_TO_SLOT: dict[str, str] = {alias: slot for slot, aliases in _NAME_ALIASES.items() for alias in aliases}
_DOC_SLOTS = set(DOC_SPECS)

# Технические поля карточки: их нельзя отправить в characteristics.
SYSTEM_FIELDS = {
    "nmID",
    "imtID",
    "nmUUID",
    "subjectID",
    "subjectName",
    "vendorCode",
    "brand",
    "title",
    "description",
    "needKiz",
    "photos",
    "video",
    "sizes",
    "dimensions",
    "characteristics",
    "documents",
    "tags",
    "createdAt",
    "updatedAt",
}

_DECLARATION_RE = re.compile(r"^\s*(ЕАЭС|EАЭС|EAEU)?\s*N?\s*RU\s*Д", re.IGNORECASE)
_CERTIFICATE_RE = re.compile(r"^\s*(ЕАЭС|EAEU)?\s*RU\s*С", re.IGNORECASE)
_BLANK_MARKERS = {"", "nan", "nat", "none", "null", "<na>"}


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class WBAPIError(RuntimeError):
    """Ошибка ответа Wildberries API."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.retryable = retryable


class WBTokenError(WBAPIError):
    """Токен не указан или отклонён (401/403) — повторять запрос бессмысленно."""


# Единый текст остановки: по нему update_cards() отличает «Стоп» от сбоя API.
_STOP_ERROR = "Остановлено пользователем"


# ---------------------------------------------------------------------------
# Нормализация значений
# ---------------------------------------------------------------------------


def is_blank(value: Any) -> bool:
    """Пустое значение: None, NaN, NaT, '' или 'nan'."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        if value != value:  # NaN/NaT не равны сами себе
            return True
    except Exception:  # экзотические типы, которые не сравниваются
        pass
    return str(value).strip().lower() in _BLANK_MARKERS


def clean_text(value: Any) -> str:
    """Строка без лишних пробелов; NaN и пустые -> ''."""
    if is_blank(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _excel_serial_to_date(serial: float) -> date | None:
    """Excel-серийник (дни от 30.12.1899) -> дата, если серийник вообще похож на дату."""
    if not EXCEL_SERIAL_MIN <= serial <= EXCEL_SERIAL_MAX:
        return None
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(serial))).date()
    except (OverflowError, ValueError):
        return None


_MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def parse_expiry(value: Any) -> tuple[str, str] | None:
    """Разбирает срок действия в ``(DD.MM.YYYY, ISO)``; ``None`` если не дата."""
    candidate: date | None = None

    if isinstance(value, datetime):
        candidate = value.date()
    elif isinstance(value, date):
        candidate = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel-серийный номер даты (дни с 30.12.1899); диапазон — 1900..2100 гг.
        candidate = _excel_serial_to_date(float(value))
    elif hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        try:
            candidate = value.to_pydatetime().date()
        except (AttributeError, TypeError, ValueError):
            candidate = None

    if candidate is None:
        text = clean_text(value)
        if not text:
            return None

        iso_candidate: date | None = None
        try:
            iso_candidate = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            iso_candidate = None
        if iso_candidate is not None:
            candidate = iso_candidate

        lowered = text.lower()
        word_month = re.search(r"(\d{1,2})?\s*(?:" + "|".join(_MONTHS_RU) + r")", lowered)
        year_part = re.search(r"(\d{4})", lowered)
        if word_month and year_part:
            try:
                candidate = date(
                    int(year_part.group(1)),
                    _MONTHS_RU[word_month.group(0).split()[-1]],
                    int(word_month.group(1) or 1),
                )
            except (ValueError, KeyError, AttributeError):
                candidate = None

        if candidate is None:
            for fmt in (
                "%d.%m.%Y",
                "%d.%m.%y",
                "%Y-%m-%d",
                "%d/%m/%Y",
                "%Y/%m/%d",
                "%d-%m-%Y",
                "%Y-%m-%d %H:%M:%S",
                "%d.%m.%Y %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%d %m %Y",
                "%B %d, %Y",
                "%d %B %Y",
                "%d %b %Y",
            ):
                try:
                    candidate = datetime.strptime(text.rstrip(" г."), fmt).date()
                    break
                except ValueError:
                    continue

        if candidate is None and iso_candidate is None:
            match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
            if match:
                day, month, year = (int(part) for part in match.groups())
                if year < 100:
                    year += 2000
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    return None
        if candidate is None:
            return None

    if candidate is None or not (1900 <= candidate.year <= 9999):
        return None
    return candidate.strftime("%d.%m.%Y"), candidate.strftime("%Y-%m-%dT00:00:00Z")


def looks_like(number: str, kind: str) -> bool:
    """Похож ли номер на декларацию о соответствии или на сертификат."""
    if not number:
        return False
    if kind == "declaration":
        return bool(_DECLARATION_RE.match(number))
    if kind == "certificate":
        return bool(_CERTIFICATE_RE.match(number))
    return False


def infer_document_kind(number: str) -> str | None:
    """Определяет тип документа по номеру: 'declaration' | 'certificate' | None."""
    if looks_like(number, "declaration"):
        return "declaration"
    if looks_like(number, "certificate"):
        return "certificate"
    return None


# ---------------------------------------------------------------------------
# Строка прайса
# ---------------------------------------------------------------------------


@dataclass
class DocumentRow:
    """Одна строка прайса, приведённая к внутреннему виду."""

    row_number: int
    vendor_code: str
    declaration: str = ""
    certificate: str = ""
    expiry: str = ""
    expiry_iso: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    charc_ids: dict[str, int] = field(default_factory=dict)

    @property
    def has_payload(self) -> bool:
        return bool(self.declaration or self.certificate or self.expiry_iso)

    @property
    def values(self) -> dict[str, str]:
        return {"declaration": self.declaration, "certificate": self.certificate, "expiry": self.expiry}


def normalize_row(row: Mapping[str, Any], row_number: int = 2) -> DocumentRow:
    """Приводит «сырой» ряд Excel к :class:`DocumentRow` и собирает замечания."""
    declaration = clean_text(row.get("declaration_number"))
    certificate = clean_text(row.get("certificate_number"))
    issues: list[str] = []

    if declaration and not certificate and infer_document_kind(declaration) == "certificate":
        declaration, certificate = "", declaration
        issues.append("Номер декларации похож на сертификат — перенесён в сертификат")
    elif declaration and infer_document_kind(declaration) is None:
        issues.append("Номер декларации не похож на формат ЕАЭС")

    if certificate and not declaration and infer_document_kind(certificate) == "declaration":
        declaration, certificate = certificate, ""
        issues.append("Номер сертификата похож на декларацию — перенесён в декларацию")
    elif certificate and infer_document_kind(certificate) is None:
        issues.append("Номер сертификата не похож на формат ЕАЭС")

    expiry_parsed = parse_expiry(row.get("expiry_date"))
    if expiry_parsed is None and not is_blank(row.get("expiry_date")):
        issues.append(f"Срок действия «{clean_text(row.get('expiry_date'))}» не является датой")

    return DocumentRow(
        row_number=row_number,
        vendor_code=clean_text(row.get("vendorCode")),
        declaration=declaration,
        certificate=certificate,
        expiry=expiry_parsed[0] if expiry_parsed else "",
        expiry_iso=expiry_parsed[1] if expiry_parsed else "",
        raw=dict(row),
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Safe-Merge
# ---------------------------------------------------------------------------


def _classify_doc_characteristic(item: Mapping[str, Any]) -> str | None:
    """Определяет, к какому doc-полю относится характеристика карточки."""
    name = clean_text(item.get("name")).lower()
    if name in _NAME_TO_SLOT:
        return _NAME_TO_SLOT[name]
    values = item.get("value")
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, str):
            return infer_document_kind(clean_text(first))
    return None


def _document_kind(number: str) -> int:
    """Тип документа по номеру: 2 — декларация, 1 — сертификат, 0 — неизвестно."""
    kind = infer_document_kind(number)
    if kind == "declaration":
        return DOCUMENT_TYPE_DECLARATION
    if kind == "certificate":
        return DOCUMENT_TYPE_CERTIFICATE
    return 0


# Слот -> тип документа WB (documents.items[].type), если номер живёт там.
DOCUMENT_TYPE_BY_SLOT = {
    "declaration": DOCUMENT_TYPE_DECLARATION,
    "certificate": DOCUMENT_TYPE_CERTIFICATE,
}


def _iso_date(value: Any) -> str:
    """'31.12.2030' | '2030-12-31T00:00:00Z' -> '2030-12-31' (для сравнения сроков)."""
    text = clean_text(value)
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    try:
        return datetime.strptime(text, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return text


def _doc_slots_of(card: Mapping[str, Any]) -> set[str]:
    """Какие doc-поля уже присутствуют в массиве characteristics."""
    return {
        slot
        for item in card.get("characteristics") or []
        if isinstance(item, dict) and (slot := _classify_doc_characteristic(item))
    }


def _existing_doc_items(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    docs = card.get("documents")
    items = docs.get("items") if isinstance(docs, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def safe_merge(card: Mapping[str, Any], document: Mapping[str, Any] | DocumentRow) -> dict[str, Any]:
    """Возвращает полную копию карточки с аккуратно добавленными документами.

    Ничего существующего не удаляется: все характеристики (включая пустые
    значения и дубли), название, описание, размеры с ``chrtID``, габариты,
    ярлыки и прикреплённые документы сохраняются.
    """
    row = document if isinstance(document, DocumentRow) else normalize_row(document or {})
    merged: dict[str, Any] = copy.deepcopy(dict(card))

    characteristics = merged.get("characteristics")
    characteristics = copy.deepcopy(characteristics) if isinstance(characteristics, list) else []

    # Находим уже существующие doc-характеристики (по имени или по charcID).
    id_to_slot = {int(char_id): slot for slot, char_id in row.charc_ids.items() if char_id is not None}
    existing: dict[str, dict[str, Any]] = {}
    for item in characteristics:
        if not isinstance(item, dict):
            continue
        slot = _classify_doc_characteristic(item)
        if slot is None:
            try:
                slot = id_to_slot.get(int(item.get("id")))
            except (TypeError, ValueError):
                slot = None
        if slot and slot not in existing:
            existing[slot] = item

    for slot, value in row.values.items():
        if not value:
            continue
        target = existing.get(slot)
        if target is not None:
            target["value"] = [value]
            if not clean_text(target.get("name")):
                target["name"] = DOC_CHARACTERISTIC_NAMES[slot]
            continue
        charc_id = row.charc_ids.get(slot)
        if charc_id is None:
            # Без charcID характеристику не добавить: WB вернёт 400.
            continue
        entry = {"id": int(charc_id), "name": DOC_CHARACTERISTIC_NAMES[slot], "value": [value]}
        characteristics.append(entry)
        existing[slot] = entry

    merged["characteristics"] = characteristics

    # Объект documents обновляется целиком, поэтому передаём и старые документы.
    if row.declaration or row.certificate:
        documents = copy.deepcopy(merged.get("documents")) if isinstance(merged.get("documents"), dict) else {}
        items = _existing_doc_items(merged)
        wanted = []
        if row.declaration:
            wanted.append((DOCUMENT_TYPE_DECLARATION, row.declaration))
        if row.certificate:
            wanted.append((DOCUMENT_TYPE_CERTIFICATE, row.certificate))

        for doc_type, number in wanted:
            same_number = [item for item in items if clean_text(item.get("number")).lower() == number.lower()]
            if same_number:
                item = same_number[0]
                if int(item.get("type") or 0) not in (0, doc_type):
                    item["type"] = doc_type
            else:
                # Заменяем только документ того же вида (декларацию на декларацию),
                # остальные прикреплённые документы остаются как были.
                same_type = [
                    item
                    for item in items
                    if int(item.get("type") or 0) == doc_type
                    and _document_kind(clean_text(item.get("number"))) == doc_type
                ]
                if same_type:
                    item = same_type[0]
                    item["number"] = number
                    item.pop("id", None)  # старый id относится к прежнему номеру
                    # Срок прежнего документа относится к прежнему номеру: не переносим
                    # его на новый. Берём даты только из строки прайса.
                    item["endDate"] = row.expiry_iso or ""
                    item["isEndless"] = not row.expiry_iso
                else:
                    item = {
                        "type": doc_type,
                        "number": number,
                        "productNumber": "",
                        "tradeName": clean_text(merged.get("title")),
                        "applicant": "",
                        "startDate": "",
                        "endDate": row.expiry_iso or "",
                        "isEndless": not row.expiry_iso,
                    }
                    items.append(item)
            if row.expiry_iso:
                item["endDate"] = row.expiry_iso
                item["isEndless"] = False

        documents["items"] = items
        documents.pop("overallVerdict", None)  # расчётное поле ответа, не для записи
        merged["documents"] = documents

    return merged


def build_update_payload(card: Mapping[str, Any]) -> dict[str, Any]:
    """Объект для ``POST /content/v2/cards/update`` из полной карточки.

    Передаём всё изменяемое: ``nmID``, ``vendorCode``, ``sizes`` (с
    ``chrtID``), ``dimensions``, полный ``characteristics`` и ``documents``.
    """
    if card.get("nmID") is None:
        raise WBAPIError("У карточки нет nmID — отправлять нечего")

    sizes: list[dict[str, Any]] = []
    for size in card.get("sizes") or []:
        if not isinstance(size, dict):
            continue
        clean_size: dict[str, Any] = {}
        for key in ("chrtID", "techSize", "wbSize", "skus", "price"):
            if size.get(key) not in (None, ""):
                clean_size[key] = copy.deepcopy(size[key])
        sizes.append(clean_size)

    characteristics: list[dict[str, Any]] = []
    for item in card.get("characteristics") or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        values = item.get("value")
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        characteristics.append({"id": item["id"], "value": copy.deepcopy(values)})

    payload: dict[str, Any] = {
        "nmID": card.get("nmID"),
        "vendorCode": card.get("vendorCode"),
        "sizes": sizes,
        "characteristics": characteristics,
    }
    if isinstance(card.get("dimensions"), dict):
        payload["dimensions"] = copy.deepcopy(card["dimensions"])
    if isinstance(card.get("documents"), dict):
        payload["documents"] = {
            "items": copy.deepcopy(_existing_doc_items(card)),
            "excludeDocuments": bool(card["documents"].get("excludeDocuments", False)),
        }
    return payload


def nm_key(nm_id: Any) -> str:
    """Нормализованный ключ карточки: WB отдаёт nmID числом, но бывает и строкой."""
    return clean_text(nm_id)


def index_cards_by_vendor(cards: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Индекс vendorCode -> карточки (у одного артикула их может быть несколько)."""
    index: dict[str, list[Mapping[str, Any]]] = {}
    for card in cards:
        vendor = clean_text(card.get("vendorCode"))
        if vendor:
            index.setdefault(vendor, []).append(card)
    return index


def diff_cards(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Плоский diff двух карточек: поле / было / стало / уровень.

    Уровни: ``added`` (новое), ``changed``, ``removed`` (потеря данных —
    красный), ``kept`` (было и осталось без изменений). Уровень ``kept``
    нужен инспектору безопасности: он наглядно показывает, что старые
    характеристики (состав, цвет, комплектация) сохранены.
    """
    before = before or {}
    after = after or {}
    changes: list[dict[str, Any]] = []

    def add(field_path: str, old: Any, new: Any, level: str | None = None, keep_same: bool = False) -> None:
        if level is None:
            if old == new:
                if keep_same:
                    level = "kept"
                else:
                    return
            elif old in (None, "", [], {}) and new not in (None, "", [], {}):
                level = "added"
            elif new in (None, "", [], {}) and old not in (None, "", [], {}):
                level = "removed"
            else:
                level = "changed"
        changes.append({"field": field_path, "before": old, "after": new, "level": level})

    for key in sorted(set(before) | set(after)):
        if key in {"characteristics", "documents", "updatedAt"}:
            continue
        add(key, before.get(key), after.get(key), keep_same=True)

    def char_map(card: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for item in card.get("characteristics") or []:
            if isinstance(item, dict):
                name = clean_text(item.get("name")) or f"charcID {item.get('id')}"
                # Несколько характеристик с одинаковым именем не должны затирать друг друга.
                unique = name
                counter = 2
                while unique in result:
                    unique = f"{name} #{counter}"
                    counter += 1
                result[unique] = item
        return result

    old_chars = char_map(before)
    new_chars = char_map(after)
    for name in list(dict.fromkeys([*old_chars, *new_chars])):
        old = old_chars.get(name)
        new = new_chars.get(name)
        if old and new and old.get("id") != new.get("id"):
            add(f"charcID «{name}»", old.get("id"), new.get("id"))
        add(
            f"characteristic «{name}»",
            old.get("value") if old else None,
            new.get("value") if new else None,
            keep_same=True,
        )

    def doc_map(card: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for item in _existing_doc_items(card):
            key = f"{item.get('type')}:«{clean_text(item.get('number'))}»"
            result[key] = item
        return result

    old_docs = doc_map(before)
    new_docs = doc_map(after)
    removed: list[tuple[str, Mapping[str, Any]]] = []
    for key in list(dict.fromkeys([*old_docs, *new_docs])):
        old = old_docs.get(key)
        new = new_docs.get(key)
        if old and not new:
            if _document_kind(clean_text(old.get("number"))) in (DOCUMENT_TYPE_DECLARATION, DOCUMENT_TYPE_CERTIFICATE):
                removed.append((key, old))  # возможно, номер просто заменён — проверим ниже
            else:
                add(f"document {key}", old.get("number"), None, "removed")
        elif new and not old:
            add(
                f"document {key}",
                None,
                {"type": new.get("type"), "number": new.get("number"), "endDate": new.get("endDate")},
                "added",
            )
        elif old and new:
            for field_name in ("type", "number", "endDate", "isEndless"):
                add(f"document {key}.{field_name}", old.get(field_name), new.get(field_name), keep_same=True)

    for key, old in removed:
        doc_type = int(old.get("type") or 0) or _document_kind(clean_text(old.get("number")))
        same_kind = [
            item
            for item in new_docs.values()
            if int(item.get("type") or 0) == doc_type
            and _document_kind(clean_text(item.get("number"))) == doc_type
        ]
        if same_kind:
            # Номер декларации/сертификата заменён на новый — это не потеря данных.
            add(
                f"document {key}",
                old.get("number"),
                same_kind[0].get("number"),
                "changed",
            )
        else:
            add(f"document {key}", old.get("number"), None, "removed")

    return changes


def verify_integrity(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    document: DocumentRow | None = None,
) -> list[str]:
    """Проверяет, что ничего не потеряно и документы применились.

    Возвращает список проблем (пустой список = всё в порядке).
    """
    problems: list[str] = []
    doc_slots = _DOC_SLOTS

    old_chars = [item for item in before.get("characteristics") or [] if isinstance(item, dict)]
    new_chars = [item for item in after.get("characteristics") or [] if isinstance(item, dict)]
    new_values: dict[Any, list[Any]] = {}
    for item in new_chars:
        new_values.setdefault(item.get("id"), []).append(item.get("value"))

    # 1. Все недокументные характеристики остаются с теми же значениями.
    for item in old_chars:
        slot = _classify_doc_characteristic(item)
        if slot in doc_slots:
            continue
        # ``in`` по списку — проверка по значению, а не по ссылке: потеря
        # характеристики должна ловиться, даже если её нет в новом списке.
        if not any(item.get("value") == value for value in new_values.get(item.get("id"), [])):
            name = clean_text(item.get("name")) or item.get("id")
            problems.append(f"Изменена/потеряна характеристика «{name}»")

    # 2. Количество характеристик не уменьшилось.
    if len(new_chars) < len(old_chars):
        problems.append(f"Характеристик стало меньше: {len(old_chars)} → {len(new_chars)}")

    # 3. Размеры, штрихкоды, габариты, название и описание нетронуты.
    old_sizes = [(str(s.get("chrtID")), s.get("techSize")) for s in before.get("sizes") or [] if isinstance(s, dict)]
    new_sizes = [(str(s.get("chrtID")), s.get("techSize")) for s in after.get("sizes") or [] if isinstance(s, dict)]
    if sorted(old_sizes) != sorted(new_sizes):
        problems.append("Изменились размеры/штрихкоды карточки (sizes)")
    if before.get("dimensions") != after.get("dimensions"):
        problems.append("Изменились габариты (dimensions)")
    for field_name in ("title", "description", "vendorCode", "nmID", "imtID"):
        if before.get(field_name) != after.get(field_name):
            problems.append(f"Изменилось поле «{field_name}»")

    # 4. Ранее прикреплённые документы остались (кроме заменяемых номеров
    #    сертификата/декларации и документов, которые сняло само WB).
    replacements = document.values if document is not None else {}
    replace_by_type = {
        DOCUMENT_TYPE_DECLARATION: clean_text(replacements.get("declaration")),
        DOCUMENT_TYPE_CERTIFICATE: clean_text(replacements.get("certificate")),
    }

    def lost_documents(card: Mapping[str, Any]) -> set[str]:
        lost: set[str] = set()
        for item in _existing_doc_items(card):
            number = clean_text(item.get("number"))
            if not number:
                continue
            doc_type = int(item.get("type") or 0)
            new_number = replace_by_type.get(doc_type) or ""
            if new_number and number.lower() != new_number.lower() and _document_kind(number) == doc_type:
                continue
            lost.add(number.lower())
        return lost

    lost = lost_documents(before) - {
        clean_text(item.get("number")).lower() for item in _existing_doc_items(after)
    }
    if lost:
        problems.append("Потеряны документы: " + ", ".join(sorted(lost)[:4]))

    # 5. Новые значения реально записались. Сверяем по СЛОТАМ (declaration/
    #    certificate/expiry), а не простым поиском строки: иначе удалённая
    #    характеристика декларации не замечается, если номер совпал с
    #    «другим» значением карточки — а раньше подстрока в json.dumps()
    #    ловила ложные совпадения на именах характеристик и «T00:00:00Z».
    if document is not None:
        slots_after = _doc_slots_of(after)

        # Значения doc-характеристик в ответе WB: slot -> список вариантов.
        slot_values_after: dict[str, list[list[str]]] = {}
        for item in new_chars:
            slot_after = _classify_doc_characteristic(item)
            if not slot_after:
                continue
            raw = item.get("value") if isinstance(item.get("value"), list) else [item.get("value")]
            slot_values_after.setdefault(slot_after, []).append([clean_text(v) for v in raw])

        def slot_value(slot_name: str) -> str:
            values = sorted(v for values in slot_values_after.get(slot_name, []) for v in values if v)
            if values:
                return values[0]
            # Для сертификата/декларации WB может хранить номер в documents.items.
            doc_type = DOCUMENT_TYPE_BY_SLOT.get(slot_name)
            numbers = sorted(
                clean_text(item.get("number")).lower()
                for item in _existing_doc_items(after)
                if doc_type is not None
                and int(item.get("type") or 0) == doc_type
                and clean_text(item.get("number"))
            )
            return numbers[0] if numbers else ""

        expiry_after = slot_value("expiry")

        for slot, value in document.values.items():
            if not value:
                continue
            current = slot_value(slot)
            # Срок жизни документа (endDate в documents.items) — валидное место
            # для «Срока действия», даже если характеристики в предмете нет.
            if slot == "expiry":
                end_dates = {
                    _iso_date(item.get("endDate"))
                    for item in _existing_doc_items(after)
                    if clean_text(item.get("endDate"))
                }
                if _iso_date(value) not in end_dates and not (slots_after & {"expiry"}):
                    problems.append(f"Поле «{DOC_CHARACTERISTIC_NAMES[slot]}» не записалось")
                elif end_dates and _iso_date(value) not in end_dates and not (slots_after & {"expiry"}):
                    problems.append(
                        f"Поле «{DOC_CHARACTERISTIC_NAMES[slot]}» хранит другой срок: "
                        f"{', '.join(sorted(end_dates))}"
                    )
                elif _iso_date(value) != _iso_date(current) and (slots_after & {"expiry"}):
                    problems.append(
                        f"Поле «{DOC_CHARACTERISTIC_NAMES[slot]}» хранит другой срок: {current or '—'}"
                    )
                continue
            if slot not in slots_after:
                problems.append(f"Поле «{DOC_CHARACTERISTIC_NAMES[slot]}» не записалось: нет характеристики")
                continue
            if value.lower() not in current.lower():
                problems.append(
                    f"Поле «{DOC_CHARACTERISTIC_NAMES[slot]}» хранит другое значение: {current or '—'}"
                )

    return problems


# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------


class RateLimiter:
    """Скользящее окно + минимальный интервал между запросами."""

    def __init__(self, per_minute: int, interval: float) -> None:
        self.per_minute = max(1, int(per_minute))
        self.interval = max(0.0, float(interval))
        self._lock = threading.Lock()
        self._stamps: list[float] = []

    def acquire(self, stop_event: threading.Event | None = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._stamps = [stamp for stamp in self._stamps if now - stamp < 60.0]
                wait = 0.0
                if self._stamps:
                    wait = max(wait, self._stamps[-1] + self.interval - now)
                if len(self._stamps) >= self.per_minute:
                    wait = max(wait, self._stamps[0] + 60.0 - now)
                if wait <= 0:
                    self._stamps.append(now)
                    return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError(_STOP_ERROR)
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# Клиенты
# ---------------------------------------------------------------------------


class BaseWBClient:
    """Общая логика Safe-Merge и пакетной работы."""

    mode = "base"
    rate_note = ""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stats: dict[str, float] = {"read_requests": 0, "write_requests": 0, "retries": 0, "sleep_seconds": 0.0}
        self.stop_event = stop_event or threading.Event()

    # --- сетевой слой (переопределяется) ---------------------------------
    def fetch_cards_page(self, cursor: Mapping[str, Any], limit: int = GET_CARDS_PAGE_LIMIT) -> dict[str, Any]:
        raise NotImplementedError

    def send_update(self, payload: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError

    def check_token(self) -> None:
        """Проверка доступа (в демо ничего не делает)."""

    def document_charc_ids(self, cards: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, int]]:
        """subjectID -> {'declaration'|'certificate'|'expiry': charcID}."""
        return {}

    # --- общий сценарий ---------------------------------------------------
    def get_all_cards(
        self,
        limit: int = GET_CARDS_PAGE_LIMIT,
        on_page: Callable[[int, int, int], None] | None = None,
        max_pages: int = 2000,
    ) -> list[dict[str, Any]]:
        """Все карточки продавца с пагинацией по курсору."""
        page_limit = max(1, min(int(limit), GET_CARDS_PAGE_LIMIT))
        cursor: dict[str, Any] = {"limit": page_limit}
        cards: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        seen_nm: set[Any] = set()

        for page in range(1, max_pages + 1):
            data = self.fetch_cards_page(cursor, page_limit)
            chunk = data.get("cards") if isinstance(data, dict) else None
            if isinstance(chunk, list):
                for card in chunk:
                    if not isinstance(card, dict):
                        continue
                    # WB при кривом курсоре может повторить карточку: дубль
                    # ломает и план, и пост-проверку — отбрасываем по nmID.
                    nm_id = card.get("nmID")
                    if nm_id is not None and nm_id in seen_nm:
                        continue
                    if nm_id is not None:
                        seen_nm.add(nm_id)
                    cards.append(card)
            if on_page:
                on_page(page, len(chunk) if isinstance(chunk, list) else 0, len(cards))

            next_cursor = data.get("cursor") if isinstance(data, dict) else None
            if not isinstance(chunk, list) or len(chunk) < page_limit or not isinstance(next_cursor, dict):
                break
            key = f"{next_cursor.get('updatedAt')}|{next_cursor.get('nmID')}"
            if not next_cursor.get("updatedAt") or not next_cursor.get("nmID") or key in seen_cursors:
                break
            seen_cursors.add(key)
            cursor = dict(next_cursor, limit=page_limit)
        return cards

    def update_cards(
        self,
        cards: Sequence[dict[str, Any]],
        batch_size: int = UPDATE_BATCH_LIMIT,
        delay: float = 0.0,
        on_batch: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Отправляет карточки батчами: лимит, пауза, повторы 429/5xx."""
        batch_size = max(1, min(int(batch_size), UPDATE_BATCH_LIMIT))
        total = len(cards)
        outcome: dict[str, Any] = {"ok": True, "error": "", "batches": 0, "updated": 0}
        total_batches = math.ceil(total / batch_size) if total else 0
        last_sent = 0.0

        for start in range(0, total, batch_size):
            if self.stop_event.is_set():
                outcome.update(ok=False, error=_STOP_ERROR)
                break
            batch = list(cards[start : start + batch_size])
            # Паузу держим только в боевом режиме: демо-эмуляция API
            # не имеет серверных лимитов и прогон не должен ждать.
            if delay > 0 and getattr(self, "mode", "") == "real":
                pause = last_sent + float(delay) - time.monotonic()
                if pause > 0:
                    if self.stop_event.wait(pause):
                        outcome.update(ok=False, error=_STOP_ERROR)
                        break
                    self.stats["sleep_seconds"] += pause

            attempt = 0
            while True:
                try:
                    response = self.send_update([build_update_payload(card) for card in batch])
                    break
                except WBTokenError:
                    raise
                except WBAPIError as exc:
                    if not exc.retryable or attempt >= 3:
                        outcome.update(ok=False, error=str(exc))
                        break
                    attempt += 1
                    self.stats["retries"] += 1
                    if self.stop_event.wait(min(60.0, 6.0 * (2 ** (attempt - 1)))):
                        outcome.update(ok=False, error=_STOP_ERROR)
                        break
                except requests.RequestException as exc:  # сеть
                    attempt += 1
                    self.stats["retries"] += 1
                    if attempt >= 4:
                        outcome.update(ok=False, error=f"Сетевая ошибка: {exc}")
                        break
                    if self.stop_event.wait(min(30.0, 3.0 * (2 ** (attempt - 1)))):
                        outcome.update(ok=False, error=_STOP_ERROR)
                        break
            if not outcome["ok"]:
                break

            last_sent = time.monotonic()
            outcome["batches"] += 1
            if isinstance(response, dict) and response.get("error"):
                outcome.update(
                    ok=False,
                    error=str(response.get("errorText") or "WB API вернул ошибку"),
                )
                break
            outcome["updated"] += len(batch)
            if on_batch:
                on_batch(outcome["batches"], total_batches, len(batch))

        if not outcome["ok"] and outcome["error"] == _STOP_ERROR:
            # Отдельный тип: Updater отличает «пользователь нажал Стоп»
            # от «WB вернул ошибку» и не маскирует остановку под STATUS_ERROR.
            raise InterruptedError(_STOP_ERROR)
        return outcome


class WBClient(BaseWBClient):
    """Боевой клиент WB Content API v2."""

    mode = "real"
    rate_note = f"{UPDATE_RATE_PER_MINUTE} запросов/мин, интервал {UPDATE_RATE_INTERVAL:.0f} с"

    def __init__(
        self,
        token: str,
        base_url: str = WB_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__(stop_event=stop_event)
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ValueError("Не указан API-токен Wildberries (категория «Контент»).")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "wb-docs-updater/2.0",
            }
        )
        self.read_limiter = RateLimiter(READ_RATE_PER_MINUTE, READ_RATE_INTERVAL)
        self.write_limiter = RateLimiter(UPDATE_RATE_PER_MINUTE, UPDATE_RATE_INTERVAL)
        self._charc_cache: dict[int, dict[str, int]] = {}

    # --- низкий уровень ---------------------------------------------------
    def _request(self, method: str, path: str, payload: Any = None, limiter: RateLimiter | None = None) -> Any:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if limiter is not None:
                limiter.acquire(self.stop_event)
            try:
                response = self.session.request(method, url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                attempt += 1
                self.stats["retries"] += 1
                if attempt > self.max_retries:
                    raise WBAPIError(f"Сетевая ошибка WB API: {exc}") from exc
                self.stop_event.wait(min(30.0, 2.0 * (2 ** (attempt - 1))))
                continue

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise WBAPIError("WB API вернул ответ не в формате JSON.", status) from exc
            if status == 401:
                raise WBTokenError("WB API отклонил токен (401). Проверьте ключ и категорию «Контент».", 401)
            if status == 403:
                raise WBTokenError("Доступ запрещён (403): у токена нет категории «Контент».", 403)
            if status == 402:
                raise WBAPIError("Требуется оплата (402): проверьте тариф WB.", 402)
            if status == 413:
                raise WBAPIError("Превышен лимит объёма запроса (413). Уменьшите размер батча.", 413)
            if status == 429 or status >= 500:
                attempt += 1
                self.stats["retries"] += 1
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 0.0
                except ValueError:
                    wait = 0.0
                wait = wait or min(120.0, 10.0 * (2 ** (attempt - 1)))
                if attempt > self.max_retries:
                    raise WBAPIError(
                        f"Лимит запросов превышен или сбой сервера WB (HTTP {status}). "
                        f"Попробуйте позже, пауза не помогла.",
                        status,
                        retryable=True,
                    )
                self.stats["sleep_seconds"] += wait
                self.stop_event.wait(wait)
                continue

            try:
                body = response.json()
            except ValueError:
                body = response.text[:800]
            raise WBAPIError(f"WB API: {_extract_api_error(body) or f'HTTP {status}'}", status, body)

    # --- сетевой слой -----------------------------------------------------
    def fetch_cards_page(self, cursor: Mapping[str, Any], limit: int = GET_CARDS_PAGE_LIMIT) -> dict[str, Any]:
        settings = {"cursor": dict(cursor, limit=int(limit)), "filter": {"withPhoto": -1}}
        data = self._request("POST", f"{GET_CARDS_PATH}?locale=ru", settings, limiter=self.read_limiter)
        self.stats["read_requests"] += 1
        return data if isinstance(data, dict) else {"cards": [], "cursor": {}}

    def send_update(self, payload: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        data = self._request("POST", UPDATE_CARDS_PATH, list(payload), limiter=self.write_limiter)
        self.stats["write_requests"] += 1
        return data if isinstance(data, dict) else {"error": True, "errorText": "Пустой ответ WB API"}

    def check_token(self) -> None:
        self.fetch_cards_page({"limit": 1}, limit=1)

    def document_charc_ids(self, cards: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, int]]:
        """Читает charcID полей документов из справочника предметов."""
        subjects = {int(card["subjectID"]) for card in cards if card.get("subjectID")}
        for subject_id in sorted(subjects - set(self._charc_cache)):
            try:
                data = self._request(
                    "GET", CHARCS_PATH.format(subject_id=subject_id), None, limiter=self.read_limiter
                )
            except WBAPIError:
                self._charc_cache[subject_id] = {}
                continue
            mapping: dict[str, int] = {}
            for item in (data.get("data") or []) if isinstance(data, dict) else []:
                if not isinstance(item, dict):
                    continue
                slot = _NAME_TO_SLOT.get(clean_text(item.get("name")).lower())
                # 0 = «список значений», 1 = «массив строк»: оба типа пишутся
                # массивом value, поэтому годится всё, что не число/дата.
                if slot and int(item.get("charcType") or 0) in (0, 1):
                    mapping.setdefault(slot, int(item.get("charcID")))
            self._charc_cache[subject_id] = mapping
        return {subject: dict(self._charc_cache.get(subject) or {}) for subject in subjects}


class DemoWBClient(BaseWBClient):
    """Песочница без API-ключа: эмулирует Content API v2 на мок-базе карточек."""

    mode = "demo"
    rate_note = "без ограничений (локальная эмуляция)"

    def __init__(
        self,
        cards_path: str | Path | None = None,
        stop_event: threading.Event | None = None,
        simulate_latency: float = 0.0,
    ) -> None:
        super().__init__(stop_event=stop_event)
        path = Path(cards_path) if cards_path else MOCK_CARDS_JSON
        if not path.exists():
            from sample_generator import generate_mock_cards

            generate_mock_cards(path, force=True)
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        cards = payload.get("cards", payload) if isinstance(payload, dict) else payload
        if not isinstance(cards, list) or not cards:
            raise ValueError(f"Файл {path.name} не содержит карточек WB API v2.")
        self.cards_path = path
        self._initial = copy.deepcopy(cards)
        self._cards: list[dict[str, Any]] = copy.deepcopy(cards)
        self.simulate_latency = max(0.0, float(simulate_latency))
        self._charc_cache = self._build_charc_cache(self._cards)

    @staticmethod
    def _build_charc_cache(cards: Iterable[Mapping[str, Any]] = ()) -> dict[int, dict[str, int]]:
        from sample_generator import DOC_CHARACTERISTIC_IDS

        subjects = {subject for subject in (card.get("subjectID") for card in cards) if subject}
        return {int(subject): dict(DOC_CHARACTERISTIC_IDS) for subject in subjects}

    # --- имитация API -----------------------------------------------------
    def fetch_cards_page(self, cursor: Mapping[str, Any], limit: int = GET_CARDS_PAGE_LIMIT) -> dict[str, Any]:
        if self.stop_event.is_set():
            raise InterruptedError(_STOP_ERROR)
        if self.simulate_latency:
            self.stop_event.wait(self.simulate_latency)
        self.stats["read_requests"] += 1
        ordered = sorted(
            self._cards, key=lambda card: (str(card.get("updatedAt") or ""), int(card.get("nmID") or 0))
        )
        start = 0
        marker = cursor.get("nmID")
        updated_at = cursor.get("updatedAt")
        if marker:
            # Позиционный курсор: WB отдаёт блок карточек ПОСЛЕ пары (updatedAt, nmID).
            marker_index = next(
                (index for index, card in enumerate(ordered) if card.get("nmID") == marker),
                None,
            )
            if marker_index is not None and (
                updated_at is None or str(ordered[marker_index].get("updatedAt") or "") == str(updated_at)
            ):
                start = marker_index + 1
            elif marker_index is not None:
                # Документ по курсору «съехал» (карточку пересохранили): продолжаем
                # со следующей карточки, чтобы не зациклиться на первой странице.
                start = marker_index + 1
            else:
                boundary = (str(updated_at or ""), int(marker or 0))
                start = next(
                    (
                        index
                        for index, card in enumerate(ordered)
                        if (str(card.get("updatedAt") or ""), int(card.get("nmID") or 0)) >= boundary
                    ),
                    len(ordered),
                )
        page = ordered[start : start + int(limit)]
        next_cursor: dict[str, Any] = {}
        more = start + int(limit) < len(ordered)
        if page and more:
            next_cursor = {
                "updatedAt": page[-1].get("updatedAt"),
                "nmID": page[-1].get("nmID"),
                "total": max(0, len(ordered) - start - len(page)),
            }
        return {"cards": copy.deepcopy(page), "cursor": next_cursor}

    def send_update(self, payload: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if self.stop_event.is_set():
            raise InterruptedError(_STOP_ERROR)
        if self.simulate_latency:
            self.stop_event.wait(self.simulate_latency * 2)
        self.stats["write_requests"] += 1
        by_nm = {card.get("nmID"): card for card in self._cards}

        unknown = []
        for item in payload:
            stored = by_nm.get(item.get("nmID"))
            if stored is None or clean_text(stored.get("vendorCode")) != clean_text(item.get("vendorCode")):
                unknown.append(item.get("vendorCode") or item.get("nmID"))
        if unknown:
            return {
                "data": None,
                "error": True,
                "errorText": "Карточки не найдены: " + ", ".join(map(str, unknown[:10])),
                "additionalErrors": unknown[:10],
            }

        # Валидация как у WB: обязательные поля и уровни по характеристикам.
        for item in payload:
            if not item.get("sizes") or not item.get("characteristics"):
                return {
                    "data": None,
                    "error": True,
                    "errorText": "MissingRequiredCharacteristics: sizes и characteristics обязательны",
                }
            for size in item["sizes"]:
                if not size.get("chrtID"):
                    return {"data": None, "error": True, "errorText": "sizes.chrtID обязателен для существующего размера"}

        for item in payload:
            stored = by_nm[item["nmID"]]
            old_names = {
                item.get("id"): clean_text(item.get("name"))
                for item in stored.get("characteristics") or []
                if isinstance(item, dict)
            }
            characteristics = []
            for char in item.get("characteristics") or []:
                name = old_names.get(char.get("id")) or ""
                characteristics.append(
                    {"id": char.get("id"), "name": name, "value": copy.deepcopy(char.get("value"))}
                )
            stored["characteristics"] = characteristics
            stored["sizes"] = copy.deepcopy(item.get("sizes") or [])
            stored["dimensions"] = copy.deepcopy(item.get("dimensions") or stored.get("dimensions") or {})
            if item.get("documents") is not None:
                documents = copy.deepcopy(item["documents"])
                items = documents.get("items") or []
                for doc in items:
                    if isinstance(doc, dict) and not doc.get("id"):
                        doc["id"] = f"doc-{stored['nmID']}-{doc.get('type')}"
                documents["overallVerdict"] = {"status": 1 if items else 0, "reasons": []}
                stored["documents"] = documents
            # Как и боевой WB, НЕ трогаем updatedAt: курсор пагинации опирается
            # на пару (updatedAt, nmID), и «перештамповка» времени на текущее
            # сдвигает карточки в порядок выгрузки — страницы поедут.
        return {"data": None, "error": False, "errorText": "", "additionalErrors": {}}

    def document_charc_ids(self, cards: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, int]]:
        from sample_generator import DOC_CHARACTERISTIC_IDS

        subjects = {int(card.get("subjectID") or 0) for card in cards}
        return {subject: dict(DOC_CHARACTERISTIC_IDS) for subject in subjects or {0}}

    # --- служебное для UI ------------------------------------------------
    def snapshot(self, nm_id: Any) -> dict[str, Any] | None:
        """Текущее состояние карточки в «базе WB» (после всех записей)."""
        for card in self._cards:
            if card.get("nmID") == nm_id:
                return copy.deepcopy(card)
        return None

    def initial_snapshot(self, nm_id: Any) -> dict[str, Any] | None:
        """Состояние карточки до запуска демо (как будто только что из WB)."""
        for card in self._initial:
            if card.get("nmID") == nm_id:
                return copy.deepcopy(card)
        return None


# ---------------------------------------------------------------------------
# Статусы и результаты по артикулам
# ---------------------------------------------------------------------------

STATUS_UPDATED = "Обновлено"
STATUS_NOT_FOUND = "Не найдено в WB"
STATUS_NEEDS_REVIEW = "Требует проверки"
STATUS_UNCHANGED = "Без изменений"
STATUS_NO_DATA = "Нет данных для записи"
STATUS_INVALID = "Ошибка валидации"
STATUS_DUPLICATE = "Дубликат артикула"
STATUS_TOKEN = "Ошибка токена"
STATUS_STOPPED = "Остановлено"
STATUS_MISMATCH = "Не применено на WB"
STATUS_ERROR = "Ошибка API"

FOUND_STATUSES = {
    STATUS_UPDATED,
    STATUS_UNCHANGED,
    STATUS_NEEDS_REVIEW,
    STATUS_MISMATCH,
    STATUS_ERROR,
    STATUS_STOPPED,
}


@dataclass
class ArticleResult:
    """Итог по одному артикулу."""

    row_number: int
    vendor_code: str
    status: str
    message: str
    checked_at: datetime = field(default_factory=datetime.now)
    nm_id: Any = None
    post_check_at: datetime | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    source: DocumentRow | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_UPDATED

    @property
    def found(self) -> bool:
        return self.nm_id is not None or self.status in FOUND_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "строка": self.row_number,
            "vendorCode": self.vendor_code,
            "nmID": self.nm_id if self.nm_id is not None else "",
            "статус": self.status,
            "декларация": self.source.declaration if self.source else "",
            "сертификат": self.source.certificate if self.source else "",
            "срок действия": self.source.expiry if self.source else "",
            "проблемы": "; ".join(self.problems),
            "сообщение": self.message,
            "время проверки": self.checked_at.strftime("%d.%m.%Y %H:%M:%S"),
            "время пост-проверки": self.post_check_at.strftime("%d.%m.%Y %H:%M:%S") if self.post_check_at else "",
        }


@dataclass
class ApplyPlan:
    """Результат сверки прайса с карточками: что писать, что пропустить."""

    results: list[ArticleResult]
    cards_to_update: list[dict[str, Any]]
    catalog_cards: list[dict[str, Any]]
    subjects_without_charc: list[str] = field(default_factory=list)


def expired(expiry: str) -> bool:
    parsed = parse_expiry(expiry)
    return bool(parsed) and datetime.strptime(parsed[0], "%d.%m.%Y").date() < date.today()


def already_applied(card: Mapping[str, Any], row: DocumentRow) -> bool:
    """Проверяет, что все значения из строки уже есть в карточке WB."""
    if not row.has_payload:
        return False
    if row.declaration or row.certificate:
        numbers = {clean_text(item.get("number")).lower() for item in _existing_doc_items(card)}
        numbers |= {clean_text(item.get("value")) for item in card.get("characteristics") or [] if isinstance(item, dict)}
        wanted = {number.lower() for number in (row.declaration, row.certificate) if number}
        if not wanted <= numbers:
            return False
    if row.expiry:
        values = [
            str(item.get("value"))
            for item in card.get("characteristics") or []
            if isinstance(item, dict) and _classify_doc_characteristic(item) == "expiry"
        ]
        doc_ends = {_iso_date(item.get("endDate")) for item in _existing_doc_items(card)}
        # Срок в карточке может быть отформатирован иначе (список значений
        # приходит как "['31.12.2030']") — сравниваем нормализованно.
        targets = {piece.strip().strip("'\"") for raw in values for piece in str(raw).strip("[]").split(",")}
        if not any(row.expiry in value or _iso_date(value) == row.expiry_iso[:10] for value in targets) and (
            row.expiry_iso[:10] not in doc_ends
        ):
            return False
    return True


def plan_apply(
    client: BaseWBClient,
    rows: Sequence[DocumentRow],
    cards: Sequence[Mapping[str, Any]] | None = None,
    charc_map: Mapping[int, Mapping[str, int]] | None = None,
) -> ApplyPlan:
    """Сверяет прайс с карточками продавца и формирует план записи.

    ``cards`` и ``charc_map`` можно передать заранее (тогда дополнительных
    запросов в WB не будет) — так же работает :meth:`Updater.run`.
    """
    catalog = list(cards) if cards is not None else client.get_all_cards()
    index = index_cards_by_vendor(catalog)
    charc_map = charc_map if charc_map is not None else client.document_charc_ids(catalog)

    counter: dict[str, int] = {}
    for row in rows:
        if row.vendor_code:
            counter[row.vendor_code] = counter.get(row.vendor_code, 0) + 1

    results: list[ArticleResult] = []
    to_update: list[dict[str, Any]] = []
    missing_subjects: list[str] = []
    handled: set[str] = set()

    for row in rows:
        if not row.vendor_code:
            results.append(ArticleResult(row.row_number, "", STATUS_INVALID, "Пустой vendorCode", source=row))
            continue
        if counter.get(row.vendor_code, 0) > 1:
            if row.vendor_code in handled:
                results.append(
                    ArticleResult(
                        row.row_number,
                        row.vendor_code,
                        STATUS_DUPLICATE,
                        "Артикул повторяется в файле — применена первая строка",
                        source=row,
                    )
                )
                continue
            handled.add(row.vendor_code)
        if not row.has_payload:
            results.append(
                ArticleResult(
                    row.row_number,
                    row.vendor_code,
                    STATUS_NO_DATA,
                    "В строке нет номера документа или срока действия",
                    source=row,
                )
            )
            continue

        matches = index.get(row.vendor_code) or []
        if not matches:
            results.append(
                ArticleResult(
                    row.row_number,
                    row.vendor_code,
                    STATUS_NOT_FOUND,
                    "Карточка с таким vendorCode не найдена в WB",
                    source=row,
                )
            )
            continue

        card = matches[0]
        if len(matches) > 1:
            row.issues.append(f"Найдено карточек с этим vendorCode: {len(matches)} — обновлена первая")
        nm_id = card.get("nmID")
        subject_id = int(card.get("subjectID") or 0)
        ids = charc_map.get(subject_id, {})
        row.charc_ids = dict(ids)

        if already_applied(card, row):
            results.append(
                ArticleResult(
                    row.row_number,
                    row.vendor_code,
                    STATUS_UNCHANGED,
                    "Документы уже привязаны — записывать нечего",
                    nm_id=nm_id,
                    before=copy.deepcopy(card),
                    after=copy.deepcopy(card),
                    source=row,
                )
            )
            continue

        merged = safe_merge(card, row)
        merged_slots = _doc_slots_of(merged)
        # Сертификат WB умеет хранить и в documents.items (type=1) — тогда
        # отдельная характеристика для него не нужна.
        cert_in_merged_docs = any(
            isinstance(item, dict) and int(item.get("type") or 0) == DOCUMENT_TYPE_CERTIFICATE
            for item in (merged.get("documents") or {}).get("items") or []
        )
        missing_slots = [
            slot
            for slot, value in row.values.items()
            # Срок не считается «отсутствующим»: он уходит вместе с документом.
            if value and slot != "expiry" and slot not in merged_slots
            and not (slot == "certificate" and cert_in_merged_docs)
        ]
        if row.expiry_iso and not merged_slots:  # срок без номера документа
            missing_slots.append("expiry")
        if missing_slots:
            names = [DOC_CHARACTERISTIC_NAMES[slot] for slot in missing_slots]
            subject = clean_text(card.get("subjectName")) or str(subject_id)
            if subject not in missing_subjects:
                missing_subjects.append(subject)
            results.append(
                ArticleResult(
                    row.row_number,
                    row.vendor_code,
                    STATUS_NEEDS_REVIEW,
                    "В предмете нет характеристики "
                    + ", ".join(f"«{name}»" for name in names)
                    + f" (subjectID {subject_id}) — WB отклонит запись, карточка не изменена",
                    nm_id=nm_id,
                    before=copy.deepcopy(card),
                    source=row,
                    problems=[f"Нет характеристики: {name}" for name in names],
                )
            )
            continue

        problems = verify_integrity(card, merged, row)
        if problems:
            results.append(
                ArticleResult(
                    row.row_number,
                    row.vendor_code,
                    STATUS_NEEDS_REVIEW,
                    "Слияние не прошло проверку: " + "; ".join(problems[:3]),
                    nm_id=nm_id,
                    before=copy.deepcopy(card),
                    after=copy.deepcopy(merged),
                    source=row,
                    problems=problems,
                )
            )
            continue

        note = "Срок действия истёк" if expired(row.expiry) else ""
        results.append(
            ArticleResult(
                row.row_number,
                row.vendor_code,
                STATUS_UPDATED,
                note or "Готово к записи",
                nm_id=nm_id,
                before=copy.deepcopy(card),
                after=copy.deepcopy(merged),
                source=row,
                problems=[note] if note else [],
            )
        )
        to_update.append(merged)

    return ApplyPlan(results, to_update, catalog, missing_subjects)


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------


class Updater:
    """Полный цикл: чтение -> Safe-Merge -> запись батчами -> проверка."""

    def __init__(
        self,
        client: BaseWBClient,
        batch_size: int = UPDATE_BATCH_LIMIT,
        delay: float = UPDATE_RATE_INTERVAL,
        log: Callable[[str, str], None] | None = None,
        progress: Callable[[float, str], None] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.batch_size = max(1, min(int(batch_size), UPDATE_BATCH_LIMIT))
        self.delay = max(0.0, float(delay))
        self.log = log or (lambda message, level="info": None)
        self.progress = progress or (lambda value, text: None)
        self.dry_run = dry_run
        # Состояние прогона объявлено заранее: без него UI не может обратиться
        # к результатам до старта/после исключения в run().
        self.planned: dict[int, ArticleResult] = {}
        self.results: list[ArticleResult] = []
        self.catalog: list[dict[str, Any]] = []

    def _mark_interrupted(self, written: Sequence[Any]) -> None:
        """«Стоп» посреди записи: отправленные не трогаем, остальные — «Остановлено»."""
        sent = set(written)
        for key, result in self.planned.items():
            if key in sent:
                result.status = STATUS_MISMATCH
                result.message = "Отправлено, но результат не подтверждён: нажали «Стоп»"
                continue
            result.status = STATUS_STOPPED
            result.message = "Остановлено до записи"

    def read_catalog(self) -> list[dict[str, Any]]:
        """Читает все карточки продавца с прогрессом по страницам курсора."""
        return self.client.get_all_cards(
            on_page=lambda page, size, total: self.log(
                f"     страница {page}: {size} карточек (всего {total})", "muted"
            )
        )

    def _document_charc_ids(self, cards: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, int]]:
        """charcID документных характеристик по предметам — без падения всего прогона."""
        try:
            return self.client.document_charc_ids(cards)
        except (WBAPIError, InterruptedError) as exc:
            self.log(f"     ⚠ не удалось получить справочник характеристик: {exc}", "warning")
            return {}

    def run(self, rows: Sequence[DocumentRow]) -> list[ArticleResult]:
        self.planned = {}
        self.results = []
        self.catalog = []
        self.progress(0.02, "Читаем карточки продавца…")
        self.log(f"1/4. POST {GET_CARDS_PATH} — читаем карточки (пагинация по курсору)…")
        try:
            cards = self.read_catalog()
            charc_map = self._document_charc_ids(cards)
            plan = plan_apply(self.client, rows, cards=cards, charc_map=charc_map)
        except WBTokenError as exc:
            self.log(f"Ошибка доступа: {exc}", "error")
            self.results = [
                ArticleResult(row.row_number, row.vendor_code, STATUS_TOKEN, str(exc), source=row) for row in rows
            ]
            return self.results
        except WBAPIError as exc:
            self.log(f"Ошибка API: {exc}", "error")
            self.results = [
                ArticleResult(row.row_number, row.vendor_code, STATUS_ERROR, str(exc), source=row) for row in rows
            ]
            return self.results
        except InterruptedError:
            # Каталог читается долго (пагинация + справочники предметов):
            # «Стоп» в этот момент — штатное завершение, а не ошибка API.
            self.log("Остановлено пользователем до записи.", "warning")
            self.results = [
                ArticleResult(row.row_number, row.vendor_code, STATUS_STOPPED, "Остановлено до записи", source=row)
                for row in rows
            ]
            return self.results
        self.catalog = plan.catalog_cards

        self.log(
            f"     карточек получено: {len(plan.catalog_cards)}; артикулов к записи: {len(plan.cards_to_update)}",
            "success",
        )
        for subject in plan.subjects_without_charc:
            self.log(f"     ⚠ предмет «{subject}»: нет характеристики номера документа", "warning")
        self.progress(0.15, "План готов, отправляем изменения…")

        # Соответствие «карточка в плане ↔ результат» строится 1:1 в момент
        # формирования плана (в порядке добавления), а не по vendorCode:
        # иначе при выгрузке > UPDATE_BATCH_LIMIT карточек повторная
        # дедупликация по артикулу отдавала в запись весь каталог.
        self.planned = {}
        planned_cards: dict[Any, dict[str, Any]] = {}
        for result, card in zip(
            (item for item in plan.results if item.status == STATUS_UPDATED and item.after),
            plan.cards_to_update,
        ):
            self.planned[nm_key(card.get("nmID"))] = result
            planned_cards[nm_key(card.get("nmID"))] = card
        self.results = plan.results

        if self.dry_run:
            for result in self.planned.values():
                result.status = STATUS_UNCHANGED
                result.message = "Dry-run: карточка НЕ отправлена в WB"
            self.log("Dry-run: запись в WB отключена — показан только план изменений.", "warning")
            self.progress(1.0, "Готово (dry-run)")
            return self.results

        # В запись идёт только то, что прошло проверку целостности.
        queued = [planned_cards[key] for key in self.planned]
        total_batches = max(1, math.ceil(len(queued) / self.batch_size))
        self.log(
            f"2/4. POST {UPDATE_CARDS_PATH}: {len(queued)} карточек, батч {self.batch_size}, "
            f"пауза {self.delay:g} с (лимит WB: {UPDATE_RATE_PER_MINUTE}/мин, интервал {UPDATE_RATE_INTERVAL:.0f} с)"
        )

        all_keys = [key for key in self.planned]
        written: list[Any] = []

        def on_batch(done: int, total: int, size: int) -> None:
            start = (done - 1) * self.batch_size
            written.extend(all_keys[start : start + size])
            self.progress(0.15 + 0.65 * done / max(1, total), f"Батч {done}/{total}")
            self.log(f"     батч {done}/{total}: отправлено {size} карточек — OK", "success")

        if queued:
            try:
                outcome = self.client.update_cards(queued, self.batch_size, self.delay, on_batch)
            except InterruptedError:
                self.log("Запись остановлена пользователем.", "warning")
                self._mark_interrupted(written)
                self.progress(1.0, "Остановлено пользователем")
                return self.results
            except WBTokenError as exc:
                self.log(f"Ошибка токена: {exc}", "error")
                for result in self.planned.values():
                    result.status = STATUS_TOKEN
                    result.message = str(exc)
                self.progress(1.0, "Запись невозможна: ошибка токена")
                return self.results
            if not outcome["ok"]:
                # Уже отправленные пачки проверяем отдельно, остальные — честно «не записаны».
                for key, result in self.planned.items():
                    if key in written:
                        continue
                    result.status = STATUS_ERROR
                    result.message = str(outcome["error"])
                self.log(f"Запись прервана: {outcome['error']}", "error")
                if not written:
                    self.progress(1.0, "Запись прервана")
                    return self.results
            self.log(f"     API принял карточек: {outcome['updated']}", "success")
        else:
            self.log("     нет карточек, требующих записи", "warning")

        to_check = {key: result for key, result in self.planned.items() if key in written or not queued}
        if not to_check:
            self.progress(1.0, "Запись прервана: не отправлено ни одной карточки")
            return self.results

        self.progress(0.82, "Перечитываем карточки и проверяем результат…")
        self.log("3/4. Повторное чтение карточек — проверяем, что документы применились и данные целы…")
        try:
            verified = self.read_catalog()
        except (WBAPIError, InterruptedError) as exc:
            self.log(f"Не удалось перечитать карточки: {exc}", "warning")
            verified = []
        verified_by_nm = {nm_key(card.get("nmID")): card for card in verified}
        verified_index = index_cards_by_vendor(verified)

        for key, result in to_check.items():
            result.post_check_at = datetime.now()
            if self.client.stop_event.is_set():
                result.status = STATUS_STOPPED
                result.message = "Проверка прервана пользователем"
                continue
            after = verified_by_nm.get(key)
            if after is None:  # nmID в ответе нет — ищем по артикулу
                after = (verified_index.get(result.vendor_code) or [None])[0]
            if after is None:
                result.status = STATUS_MISMATCH
                result.message = "Карточка не найдена при повторном чтении"
                continue
            result.after = copy.deepcopy(after)
            problems = verify_integrity(result.before or {}, after, result.source)
            kept = [note for note in result.problems if note.startswith("Срок действия истёк")]
            result.problems = list(dict.fromkeys([*kept, *problems]))
            if problems:
                result.status = STATUS_MISMATCH
                result.message = "Документы не применились: " + "; ".join(problems[:3])
            else:
                result.status = STATUS_UPDATED
                note = " • срок действия истёк" if result.source and expired(result.source.expiry) else ""
                result.message = "Документы привязаны, старые данные сохранены" + note

        self.progress(0.98, "Формируем отчёт…")
        self.log("4/4. Готово: отчёт по артикулам сформирован.", "success")
        self.progress(1.0, "Обновление завершено")
        return self.results


def summarize(results: Sequence[ArticleResult]) -> dict[str, int]:
    """Агрегированные счётчики для метрик дашборда."""
    summary = {"total": len(results), "found": 0, "updated": 0, "problems": 0, "unchanged": 0, "no_data": 0}
    for result in results:
        if result.found:
            summary["found"] += 1
        if result.status == STATUS_UPDATED:
            summary["updated"] += 1
        elif result.status == STATUS_UNCHANGED:
            summary["unchanged"] += 1
        elif result.status == STATUS_NO_DATA:
            summary["no_data"] += 1
        elif result.status in {STATUS_NEEDS_REVIEW, STATUS_MISMATCH, STATUS_ERROR, STATUS_TOKEN, STATUS_STOPPED}:
            summary["problems"] += 1
    summary["not_found"] = sum(1 for result in results if result.status == STATUS_NOT_FOUND)
    summary["invalid"] = sum(
        1 for result in results if result.status in {STATUS_INVALID, STATUS_DUPLICATE}
    )
    summary["errors"] = summary["problems"] + summary["not_found"] + summary["invalid"]
    return summary


def _extract_api_error(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("errorText", "message", "detail", "title"):
            if body.get(key):
                return str(body[key])
        if body.get("additionalErrors"):
            return json.dumps(body["additionalErrors"], ensure_ascii=False)[:500]
    if isinstance(body, str):
        return body[:500]
    return ""


def create_client(mode: str, token: str | None = None, stop_event: threading.Event | None = None) -> BaseWBClient:
    """Фабрика клиентов: ``mode`` = 'demo' | 'real'."""
    if mode in {"demo", "sandbox", "mock"}:
        return DemoWBClient(stop_event=stop_event)
    return WBClient(token=token or "", stop_event=stop_event)


# ---------------------------------------------------------------------------
# Самопроверка
# ---------------------------------------------------------------------------


def _selftest() -> None:
    from sample_generator import generate_sample_dataframe

    frame = generate_sample_dataframe()
    rows = [normalize_row(record, index + 2) for index, record in enumerate(frame.to_dict("records"))]
    client = DemoWBClient()
    updater = Updater(client, batch_size=100, delay=0.0)
    results = updater.run(rows)
    stats = summarize(results)
    print("Итог:", stats)
    assert stats["updated"] > 900, stats

    updated = [result for result in results if result.status == STATUS_UPDATED]
    for result in updated[:200]:
        assert result.before and result.after
        assert not verify_integrity(result.before, result.after, result.source)

    sample = next(result for result in updated if result.after and result.after["documents"]["items"])
    print(f"Пример diff для {sample.vendor_code}:")
    for change in diff_cards(sample.before, sample.after)[:8]:
        print(f"  [{change['level']:8}] {change['field']}: {change['before']} -> {change['after']}")

    client2 = DemoWBClient()
    card = client2.get_all_cards(limit=3)[0]
    row = normalize_row(
        {
            "vendorCode": card["vendorCode"],
            "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.12345/25",
            "expiry_date": "31.12.2030",
        }
    )
    row.charc_ids = client2.document_charc_ids([card])[card["subjectID"]]
    merged = safe_merge(card, row)
    assert not verify_integrity(card, merged, row)
    print("Safe-Merge: старые данные сохранены ✔")
    print("parse_expiry:", parse_expiry("31.12.2030"), parse_expiry("1 января 2030"), parse_expiry("44205"))


if __name__ == "__main__":
    _selftest()
