"""
Единый проход извлечения: за ОДИН вызов модели на документ одновременно
определяем тип документа и извлекаем сущности + связи между ними для
графа знаний (вместо прежней схемы "жёсткие поля под каждый doc_type").

Формат вывода одинаков для всех типов документов:
    doc_type  — жанр документа (см. classify.py, ALL_TYPES)
    title     — заголовок/тема документа
    entities  — узлы будущего графа: люди, организации, активы/предприятия,
                страны, металлы, методы, оборудование
    relations — рёбра графа: тройки субъект-предикат-объект, с единицей
                измерения и источником (таблица/текст), где уместно
    keywords  — ключевые слова/темы

Работает через оба бэкенда (config.LLM_BACKEND): "yandex" (облако, платно,
с бюджетом) или "local" (Ollama, бесплатно) — как и раньше в
properties_llm.py, интерфейс не меняется для вызывающего кода (main.py).
"""
from __future__ import annotations

import json
import re

import requests

import config
import classify
import local_llm
from budget import BudgetTracker, BudgetExceededError

API_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"

ALL_TYPES = [
    classify.DOC_TYPE_SCIENTIFIC_ARTICLE,
    classify.DOC_TYPE_ASSET_PROFILE,
    classify.DOC_TYPE_REVIEW,
    classify.DOC_TYPE_ANALYTICAL_NOTE,
    classify.DOC_TYPE_PRESENTATION,
    classify.DOC_TYPE_MARKET_REPORT,
    classify.DOC_TYPE_JOURNAL_ISSUE,
    classify.DOC_TYPE_UNKNOWN,
]

ENTITY_TYPES = [
    "person", "organization", "asset", "country", "metal", "material",
    "method", "equipment", "experiment", "property", "publication", "other",
]

# Контролируемый словарь типов связей — вместо произвольного текста в
# predicate. Основа — онтология из ТЗ хакатона (uses_material,
# operates_at_condition, produces_output, described_in, validated_by,
# contradicts), расширенная типами, которые реально нужны для остального
# корпуса (аффилиация людей, локация активов, общие атрибуты).
RELATION_TYPES = [
    "uses_material", "operates_at_condition", "produces_output",
    "described_in", "validated_by", "contradicts",
    "located_in", "affiliated_with", "has_property", "part_of", "other",
]

UNIFIED_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ALL_TYPES},
        "title": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ENTITY_TYPES},
                    "note": {"type": "string"},
                },
                "required": ["name", "type"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "relation_type": {"type": "string", "enum": RELATION_TYPES},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "unit": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["subject", "relation_type", "object"],
            },
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_type", "entities", "relations"],
}

_CATEGORY_HINTS = """
- scientific_article: научная статья (УДК, аннотация, список литературы)
- asset_profile: справка по активу/предприятию (завод, рудник, оборудование)
- review: формальный обзор (гриф УТВЕРЖДАЮ, номер ОИП)
- analytical_note: сравнительная записка без формального грифа
- presentation: слайды доклада/презентации
- market_report: рыночный/квартальный отчёт по металлу (цены, объёмы, обычно EN)
- journal_issue: целый номер журнала с оглавлением, несколько статей внутри
- unknown: не подходит уверенно ни под одну категорию
""".strip()

SYSTEM_PROMPT = (
    "Ты анализируешь технический/научный документ горно-металлургической "
    "тематики для построения графа знаний. За один проход:\n\n"
    "1) определи doc_type документа по категориям ниже;\n\n"
    "2) извлеки entities — узлы графа. Тип каждой сущности выбирай СТРОГО "
    "по смыслу, не по ближайшему совпадению:\n"
    "   - person: конкретный человек (ФИО)\n"
    "   - organization: компания/институт/предприятие как юрлицо\n"
    "   - asset: конкретный завод/рудник/месторождение/производственный объект\n"
    "   - country: страна\n"
    "   - metal: чистый химический элемент-металл (Ni, Cu, Pt) как таковой\n"
    "   - material: вещество/соединение/состав/химическая формула/концентрат/"
    "сплав/руда (например 'LiNi0.86Co0.07Mn0.05Al0.00O2', 'концентрат', "
    "'известняк') — НЕ equipment, даже если встречается в тексте про "
    "оборудование\n"
    "   - method: метод/технология/способ переработки (например 'флотация', "
    "'метод конечных элементов', 'электроэкстракция')\n"
    "   - equipment: физическое оборудование/аппарат/установка (например "
    "'печь Ванюкова', 'CAE Fidesys' как программный комплекс тоже сюда)\n"
    "   - experiment: конкретный проведённый эксперимент/испытание/опыт "
    "(например 'эксперимент по выщелачиванию №3', 'лабораторные испытания "
    "флотации при pH 8') — НЕ сам метод, а конкретная его постановка/прогон\n"
    "   - property: измеримая характеристика/параметр без конкретного "
    "числового значения как отдельная сущность (например 'температура "
    "плавления', 'извлечение никеля', 'содержание серы') — числовое "
    "значение самого параметра указывай в relations через unit/object, а "
    "не создавай property на каждое число\n"
    "   - publication: КОНКРЕТНАЯ внешняя публикация/статья/источник, "
    "упомянутая или процитированная в тексте (например из списка "
    "литературы) — НЕ сам документ, который ты сейчас анализируешь\n"
    "   - other: не подходит ни под одну категорию выше\n\n"
    "ВАЖНО про именование: если сущность упоминается в тексте и на русском, "
    "и на английском (или сокращённо и полностью, например 'ПВП' и 'печь "
    "взвешенной плавки'), используй ОДНО каноничное имя для всех вхождений "
    "в рамках этого документа — предпочитай полное русское название, если "
    "оно встречается в тексте.\n\n"
    "3) извлеки relations — связи между сущностями. Для КАЖДОЙ связи "
    "выбери relation_type СТРОГО из списка (это не текст, а категория):\n"
    "   - uses_material: процесс/оборудование/эксперимент использует "
    "материал/сырьё (например 'флотация' -uses_material-> 'концентрат')\n"
    "   - operates_at_condition: процесс/оборудование работает при "
    "определённом условии/параметре (например 'печь Ванюкова' "
    "-operates_at_condition-> '1250°C', unit='°C')\n"
    "   - produces_output: процесс/оборудование/эксперимент производит "
    "результат/продукт (например 'выщелачивание' -produces_output-> "
    "'раствор сульфата никеля')\n"
    "   - described_in: сущность (метод/эксперимент/актив) описана в "
    "конкретной publication\n"
    "   - validated_by: утверждение/метод подтверждён экспериментом или "
    "публикацией\n"
    "   - contradicts: два факта/вывода из разных источников противоречат "
    "друг другу\n"
    "   - located_in: актив/организация расположены в стране/месте\n"
    "   - affiliated_with: человек работает в/аффилирован с организацией\n"
    "   - has_property: сущность обладает характеристикой/параметром "
    "(например 'концентрат' -has_property-> 'содержание_Ni_%', "
    "object='12.5', unit='%')\n"
    "   - part_of: сущность является частью другой (оборудование — часть "
    "актива, лаборатория — часть института)\n"
    "   - other: связь не подходит ни под одну категорию выше (используй "
    "predicate, чтобы описать её текстом)\n\n"
    "Дополнительно в predicate можешь дать более точную словесную "
    "формулировку связи (не обязательно, но желательно для нюансов), в "
    "unit — единицу измерения, в source — таблицу/раздел текста, откуда "
    "взято. ОБЯЗАТЕЛЬНО связывай method/experiment с тем, к чему они "
    "применяются — не оставляй их изолированными узлами без единой связи.\n\n"
    "4) выдели keywords — ключевые темы документа.\n\n"
    f"Категории doc_type:\n{_CATEGORY_HINTS}\n\n"
    "Извлекай только то, что явно подтверждено в тексте или таблицах, не "
    "выдумывай значения. Если сущностей/связей мало или нет — оставляй "
    "пустые списки, это нормально для презентаций и рыночных отчётов."
)


def _folder_hint(source_path: str) -> str:
    from pathlib import Path
    parts = Path(source_path).parts
    if "Источники информации" in parts:
        idx = parts.index("Источники информации")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def _build_prompt(full_text: str, tables: list[dict], source_path: str) -> str:
    text_snippet = full_text[:config.MAX_LLM_INPUT_CHARS]
    tables_json = json.dumps(tables[:10], ensure_ascii=False)
    folder = _folder_hint(source_path)
    regex_hint = classify.classify_document(full_text)

    parts = []
    if folder:
        parts.append(f"Папка-источник: {folder}")
    if regex_hint != classify.DOC_TYPE_UNKNOWN:
        parts.append(f"Предварительная эвристика по структурным маркерам предполагает тип: {regex_hint} (можешь не согласиться, если текст говорит другое).")
    parts.append(f"Таблицы из документа:\n{tables_json}")
    parts.append(f"Текст документа:\n{text_snippet}")
    return "\n\n".join(parts)


def extract(
    full_text: str,
    tables: list[dict],
    source_path: str,
    budget: BudgetTracker | None = None,
) -> dict:
    """
    Единый вызов: возвращает {"doc_type", "title", "entities", "relations",
    "keywords"}. При ошибке парсинга — {"doc_type": "unknown", "_parse_error": True, ...}.
    """
    prompt = _build_prompt(full_text, tables, source_path)

    if config.LLM_BACKEND == "local":
        return local_llm.extract_properties_local(
            doc_type="(определяется моделью)",
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            schema=UNIFIED_SCHEMA,
        )

    if not config.YANDEX_API_KEY or not config.YANDEX_FOLDER_ID:
        raise RuntimeError("Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID в переменных окружения.")

    if budget is not None:
        estimated_input_tokens = int((len(prompt) + len(SYSTEM_PROMPT)) / 2.5)
        budget.check_before_call(estimated_input_tokens, config.MAX_LLM_OUTPUT_TOKENS)

    model_uri = f"gpt://{config.YANDEX_FOLDER_ID}/{config.YANDEX_MODEL}"
    payload = {
        "model": model_uri,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": config.MAX_LLM_OUTPUT_TOKENS,
        "temperature": 0.2,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "graph_extraction", "schema": UNIFIED_SCHEMA},
        },
    }
    headers = {
        "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
        "OpenAI-Project": config.YANDEX_FOLDER_ID,
        "Content-Type": "application/json",
    }
    resp = requests.post(API_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    if budget is not None:
        usage = data.get("usage", {})
        budget.record_actual(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"doc_type": classify.DOC_TYPE_UNKNOWN, "_parse_error": True,
                "_raw_response": json.dumps(data, ensure_ascii=False)}

    raw = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"doc_type": classify.DOC_TYPE_UNKNOWN, "_parse_error": True, "_raw_response": raw}


def extract_document_links(full_text: str) -> list[str]:
    return sorted(set(re.findall(r"\[(\d+(?:,\s*\d+)*)\]", full_text)))
