"""Генератор демо-данных для WB Docs Updater.

Создаёт:
  * sample_documents.xlsx — прайс на 1000 артикулов с документами;
  * mock_wb_cards.json  — ответ WB Content API v2 ``/content/v2/get/cards/list``
    с реалистичными карточками (характеристики, габариты, chrtID размеров,
    баркоды, фото и уже прикреплённые документы).

Запуск напрямую::

    python sample_generator.py
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_XLSX = BASE_DIR / "sample_documents.xlsx"
MOCK_CARDS_JSON = BASE_DIR / "mock_wb_cards.json"

# Имя характеристики WB, в которую мы пишем значение. Чарка ID запрашивается
# в справочнике предмета (GET /content/v2/object/charcs/{subjectID}) — здесь
# только «демо-ID», нужные, чтобы мок-карточки выглядели как настоящие.
DOC_CHARACTERISTIC_IDS: dict[str, int] = {
    "declaration": 15001135,
    "certificate": 15001136,
    "expiry": 15001138,
}

DOC_CHARACTERISTIC_NAMES: dict[str, str] = {
    "declaration": "Номер декларации соответствия",
    "certificate": "Номер сертификата соответствия",
    "expiry": "Срок действия документа",
}

# Системные/технические поля карточки: их нельзя отправлять в characteristics
# или изменять через /content/v2/cards/update, поэтому они всегда
# сравниваются как «остаются по умолчанию».
SYSTEM_FIELDS = {
    "nmID",
    "imtID",
    "nmUUID",
    "subjectID",
    "subjectName",
    "vendorCode",
    "sizes",
    "dimensions",
    "photos",
    "video",
    "tags",
    "needKiz",
    "createdAt",
    "updatedAt",
    "characteristics",
    "documents",
}

# Типы документов в поле documents.items[].type (официальный справочник WB).
DOCUMENT_TYPE_CERTIFICATE = 1
DOCUMENT_TYPE_DECLARATION = 2

SAMPLE_COLUMNS = [
    "vendorCode",
    "declaration_number",
    "certificate_number",
    "expiry_date",
]

_COLORS = ["Чёрный", "Белый", "Синий", "Бежевый", "Красный", "Зелёный", "Серый", "Розовый"]
_COMPOSITIONS = [
    "100% хлопок",
    "100% полиэстер",
    "Хлопок 95%, эластан 5%",
    "Вискоза 70%, нейлон 30%",
    "100% лён",
    "Шерсть 50%, акрил 50%",
]
_GENDERS = ["Женский", "Мужской", "Унисекс"]
_SEASONS = ["Всесезон", "Лето", "Зима", "Демисезон"]
_COUNTRIES = ["Россия", "Китай", "Турция", "Индия", "Узбекистан"]
_KITS = [
    "Футболка — 1 шт.",
    "Платье — 1 шт.",
    "Куртка — 1 шт., чехол — 1 шт.",
    "Брюки — 1 шт.",
    "Комплект: топ и шорты",
    "Свитер — 1 шт.",
]
_PACKAGES = ["Пакет", "Коробка", "Пакет с подвесом"]
_SUBJECTS = [
    (105, "Футболки"),
    (129, "Платья"),
    (154, "Куртки"),
    (187, "Брюки"),
    (201, "Свитеры"),
]
_SIZES = [("S", "S"), ("M", "M"), ("L", "L"), ("XL", "XL"), ("42", "42"), ("44", "44")]


def _declaration_for(index: int) -> str:
    """Номер декларации о соответствии для тестовой строки ``index``."""
    return _declaration(index)


def _certificate_for(index: int) -> str:
    """Номер сертификата соответствия для тестовой строки ``index``."""
    return _certificate(index)


def vendor_code(index: int) -> str:
    """Детерминированный артикул продавца: WB-TEST-0001 ... WB-TEST-1000.

    :04d — только минимальная ширина: при index > 9999 номер станет
    ``WB-TEST-10000`` (не обрежется и не задублируется), система нумерации
    остаётся лексикографически упорядоченной до 9999.
    """
    return f"WB-TEST-{index:04d}"


def _declaration(index: int) -> str:
    year = date.today().year % 100
    return f"ЕАЭС N RU Д-RU.РА01.В.{index:05d}/{year:02d}"


def _certificate(index: int) -> str:
    year = date.today().year % 100
    return f"ЕАЭС RU С-RU.АБ12.В.{index:05d}/{year:02d}"


def generate_sample_dataframe(
    rows: int = 1000,
    seed: int = 42,
    clean: bool = False,
) -> pd.DataFrame:
    """Возвращает DataFrame с тестовыми документами.

    По умолчанию в конец добавляются 7 «грязных» строк, чтобы проверить
    валидацию: артикулы без документов, пустой артикул, несуществующий
    артикул, просроченный документ и дубликат артикула.
    ``clean=True`` даёт идеальный прайс ровно на ``rows`` строк.
    """
    rnd = random.Random(seed)
    today = date.today()
    data: list[dict[str, Any]] = []

    for i in range(1, rows + 1):
        mode = i % 10
        declaration: str | None = None
        certificate: str | None = None

        if mode < 7:
            declaration = _declaration(i)
        elif mode < 9:
            certificate = _certificate(i)
        else:
            declaration = _declaration(i)
            certificate = _certificate(i)

        if i % 37 == 0 or i in (14, 52, 90, 108):
            expiry = today - timedelta(days=rnd.randint(1, 120))
        else:
            expiry = today + timedelta(days=rnd.randint(60, 900))

        data.append(
            {
                "vendorCode": vendor_code(i),
                "declaration_number": declaration,
                "certificate_number": certificate,
                "expiry_date": expiry,
            }
        )

    if not clean:
        data.extend(
            [
                # Дубль 0001 — обновить можно, в файле берётся первая строка.
                {"vendorCode": "WB-TEST-0001", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.77888/25", "certificate_number": None, "expiry_date": today + timedelta(days=500)},
                # Дубль 0002 — «завтра» не дата, попадает в проверку входных данных.
                {"vendorCode": "WB-TEST-0002", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.77", "certificate_number": None, "expiry_date": "завтра"},
                # Нет ни номера документа, ни срока — писать нечего.
                {"vendorCode": "WB-TEST-1002", "declaration_number": None, "certificate_number": None, "expiry_date": None},
                # Документ просрочен (2019 год).
                {"vendorCode": "WB-TEST-1003", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.10003/19", "certificate_number": None, "expiry_date": "31.12.2019"},
                # Пустой артикул.
                {"vendorCode": "", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.99999/25", "certificate_number": None, "expiry_date": today + timedelta(days=365)},
                # Такого артикула в кабинете нет.
                {"vendorCode": "WB-UNKNOWN-9999", "declaration_number": "ЕАЭС N RU Д-RU.РА01.В.88888/25", "certificate_number": None, "expiry_date": today + timedelta(days=365)},
            ]
        )

    return pd.DataFrame(data, columns=SAMPLE_COLUMNS)


def generate_sample_excel(
    path: str | Path | None = None,
    rows: int = 1000,
    force: bool = True,
    clean: bool = False,
) -> pd.DataFrame:
    """Создаёт Excel-файл с тестовым прайсом (лист «Документы»)."""
    target = Path(path) if path else SAMPLE_XLSX
    df = generate_sample_dataframe(rows=rows, clean=clean)

    if target.exists() and not force:
        return df

    target.parent.mkdir(parents=True, exist_ok=True)
    display_df = df.copy()
    display_df["expiry_date"] = [
        None if pd.isna(value) or str(value).strip() == "" else value for value in display_df["expiry_date"]
    ]

    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        display_df.to_excel(writer, index=False, sheet_name="Документы")
        ws = writer.book["Документы"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        header_fill = _solid_fill("4F46E5")
        header_font = _font(color="FFFFFF", bold=True, size=11)
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = _alignment(horizontal="center", vertical="center")

        widths = {"A": 22, "B": 40, "C": 40, "D": 16}
        for column, width in widths.items():
            ws.column_dimensions[column].width = width
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=4, max_col=4):
            for cell in row:
                cell.number_format = "DD.MM.YYYY"
        ws.row_dimensions[1].height = 24

    return df


def _mock_card(index: int, seed: int = 42) -> dict[str, Any]:
    """Одна карточка в формате ответа WB ``/content/v2/get/cards/list``."""
    nm_id = 10_000_000 + index
    imt_id = 20_000_000 + index
    subject_id, subject_name = _SUBJECTS[index % len(_SUBJECTS)]
    title = f"{subject_name} Base Line #{index:04d}"
    color = _COLORS[index % len(_COLORS)]
    composition = _COMPOSITIONS[index % len(_COMPOSITIONS)]
    gender = _GENDERS[index % len(_GENDERS)]
    season = _SEASONS[index % len(_SEASONS)]
    country = _COUNTRIES[index % len(_COUNTRIES)]
    kit = _KITS[index % len(_KITS)]
    package = _PACKAGES[index % len(_PACKAGES)]
    size_a, wb_size_a = _SIZES[index % len(_SIZES)]
    size_b, wb_size_b = _SIZES[(index + 2) % len(_SIZES)]
    updated = datetime(2026, 9, 1, 9, 0, 0) + timedelta(minutes=index)
    created = updated - timedelta(days=90)

    characteristics: list[dict[str, Any]] = [
        {"id": 1, "name": "Наименование", "value": [title]},
        {"id": 12, "name": "Цвет", "value": [color]},
        {"id": 13, "name": "Состав", "value": [composition]},
        {"id": 14, "name": "Пол", "value": [gender]},
        {"id": 15, "name": "Сезон", "value": [season]},
        {"id": 16, "name": "Страна производства", "value": [country]},
        {"id": 17, "name": "Комплектация", "value": [kit]},
        {"id": 18, "name": "Вид упаковки", "value": [package]},
        {"id": 19, "name": "Вес товара без упаковки (г)", "value": [str(180 + index % 500)]},
        {"id": 20, "name": "Бренд", "value": ["Base Line"]},
        {"id": 21, "name": "Модель", "value": [f"BL-{index:04d}"]},
    ]

    documents: list[dict[str, Any]] = []
    doc_status: dict[str, Any] = {
        "status": "valid",
        "verifiedExemplar": False,
        "reasons": [],
        "date": "2026-09-01T10:00:00Z",
    }
    if index % 43 != 0:  # у части карточек в предмете нет характеристики декларации
        if index % 17 == 0:
            characteristics.append(
                {
                    "id": DOC_CHARACTERISTIC_IDS["declaration"],
                    "name": DOC_CHARACTERISTIC_NAMES["declaration"],
                    "value": [f"ЕАЭС N RU Д-RU.РА01.В.{index:05d}/24"],
                }
            )
        if index % 23 == 0:
            characteristics.append(
                {
                    "id": DOC_CHARACTERISTIC_IDS["certificate"],
                    "name": DOC_CHARACTERISTIC_NAMES["certificate"],
                    "value": [f"ЕАЭС RU С-RU.АБ12.В.{index:05d}/24"],
                }
            )
        if index % 29 == 0:
            characteristics.append(
                {
                    "id": DOC_CHARACTERISTIC_IDS["expiry"],
                    "name": DOC_CHARACTERISTIC_NAMES["expiry"],
                    "value": ["31.12.2025"],
                }
            )
        documents.append(
            {
                "id": f"doc-{index}-cert",
                "type": DOCUMENT_TYPE_CERTIFICATE,
                "number": f"ЕАЭС RU С-RU.АБ12.В.{index:05d}/24",
                "productNumber": "",
                "tradeName": title,
                "applicant": "ООО «Бейс Лайн»",
                "startDate": "2024-01-01T00:00:00Z",
                "endDate": "2027-01-01T00:00:00Z",
                "isEndless": False,
                "status": dict(doc_status),
            }
        )
    # У каждой седьмой карточки уже есть СГР — проверим, что Safe-Merge её не сотрёт.
    if index % 7 == 0:
        documents.append(
            {
                "id": f"doc-{index}-cgr",
                "type": 3,
                "number": f"RU.77.99.11.001.R.{index:06d}.01.24",
                "productNumber": "",
                "tradeName": title,
                "applicant": "ООО «Бейс Лайн»",
                "startDate": "2024-01-01T00:00:00Z",
                "endDate": "2029-01-01T00:00:00Z",
                "isEndless": False,
                "status": dict(doc_status),
            }
        )

    barcode_a = f"20{index:011d}"
    barcode_b = f"21{index:011d}"
    image_volume = (nm_id // 100_000) * 100_000

    return {
        "nmID": nm_id,
        "imtID": imt_id,
        "nmUUID": f"00000000-0000-4000-8000-{index:012d}",
        "subjectID": subject_id,
        "subjectName": subject_name,
        "vendorCode": vendor_code(index),
        "brand": "Base Line",
        "title": title,
        # У каждой 43-й карточки кабинет WB вернул карточку без блока documents.
        **(
            {}
            if index % 43 == 0
            else {
                "documents": {
                    "items": documents,
                    "excludeDocuments": False,
                    "overallVerdict": {
                        "status": "valid" if documents else "pending",
                        "verifiedExemplar": False,
                        "reasons": [],
                        "date": "2026-09-01T10:00:00Z",
                    },
                }
            }
        ),
        "description": (
            f"{title}. Практичная модель из качественного материала. "
            f"Состав: {composition}. Страна производства: {country}."
        ),
        "needKiz": False,
        "photos": [
            {
                "big": f"https://basket-12.wbbasket.ru/vol{image_volume}/part{nm_id // 1000}/{nm_id}/images/big/1.webp",
                "c220x220": f"https://basket-12.wbbasket.ru/vol{image_volume}/part{nm_id // 1000}/{nm_id}/images/c220x220/1.webp",
                "hq": f"https://basket-12.wbbasket.ru/vol{image_volume}/part{nm_id // 1000}/{nm_id}/images/hq/1.webp",
                "tm": f"https://basket-12.wbbasket.ru/vol{image_volume}/part{nm_id // 1000}/{nm_id}/images/tm/1.webp",
            }
        ],
        "video": "",
        "sizes": [
            {
                "chrtID": 500_000 + index * 2,
                "techSize": size_a,
                "wbSize": wb_size_a,
                "skus": [barcode_a],
            },
            {
                "chrtID": 500_001 + index * 2,
                "techSize": size_b,
                "wbSize": wb_size_b,
                "skus": [barcode_b],
            },
        ],
        "dimensions": {
            "length": 30 + index % 5,
            "width": 22 + index % 4,
            "height": 4,
            "weightBrutto": round(0.2 + (index % 30) / 100, 2),
        },
        "characteristics": characteristics,
        "tags": [{"id": 101, "name": "Новинки"}] if index % 4 == 0 else [],
        "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def generate_mock_cards(
    path: str | Path | None = None,
    rows: int = 1000,
    force: bool = True,
) -> list[dict[str, Any]]:
    """Создаёт JSON-ответ WB API v2 с ``rows`` карточками."""
    target = Path(path) if path else MOCK_CARDS_JSON

    if target.exists() and not force:
        with target.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("cards", payload)

    cards = [_mock_card(i) for i in range(1, rows + 1)]
    payload = {
        "cards": cards,
        "cursor": {
            "updatedAt": cards[-1]["updatedAt"],
            "nmID": cards[-1]["nmID"],
            "total": len(cards),
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, separators=(",", ": "))

    return cards


def ensure_sample_files(rows: int = 1000) -> None:
    """Создаёт демо-файлы, если их ещё нет."""
    if not SAMPLE_XLSX.exists():
        generate_sample_excel(rows=rows, force=True)
    if not MOCK_CARDS_JSON.exists():
        generate_mock_cards(rows=rows, force=True)


def load_sample_dataframe() -> pd.DataFrame:
    """Читает демо-прайс с диска (генерирует при отсутствии)."""
    ensure_sample_files()
    df = pd.read_excel(SAMPLE_XLSX, sheet_name="Документы")
    df.columns = [str(column).strip() for column in df.columns]
    return df


def _solid_fill(color: str):
    from openpyxl.styles import PatternFill

    return PatternFill("solid", fgColor=color)


def _font(**kwargs):
    from openpyxl.styles import Font

    return Font(**kwargs)


def _alignment(**kwargs):
    from openpyxl.styles import Alignment

    return Alignment(**kwargs)


if __name__ == "__main__":
    frame = generate_sample_excel(force=True)
    generate_mock_cards(force=True)
    print(f"Создан {SAMPLE_XLSX.name}: {len(frame)} строк")
    print(f"Создан {MOCK_CARDS_JSON.name}: 1000 карточек")
