"""
Извлечение свойств документа под конкретную JSON-схему через Yandex AI
Studio (YandexGPT), OpenAI-совместимый эндпоинт с response_format:
json_schema — модель гарантированно возвращает валидный JSON нужной формы
(не нужно парсить markdown-обёртки вручную, в отличие от обычного текстового
режима).

Документация: https://aistudio.yandex.ru/docs/en/ai-studio/operations/generation/completions-structured.html

pip install requests --break-system-packages
"""
from __future__ import annotations

import json
import re

import requests

import config
import local_llm
from budget import BudgetTracker, BudgetExceededError
from classify import (
    DOC_TYPE_SCIENTIFIC_ARTICLE,
    DOC_TYPE_ASSET_PROFILE,
    DOC_TYPE_REVIEW,
    DOC_TYPE_ANALYTICAL_NOTE,
    DOC_TYPE_UNKNOWN,
)

# JSON Schema (не просто пример формы, а валидная схема для response_format)
# под каждый doc_type. Null-объединения намеренно не используются — строгий
# режим structured output у многих провайдеров их не любит; вместо null
# модель просит присылать пустую строку/пустой список (см. SYSTEM_PROMPT).
JSON_SCHEMAS: dict[str, dict] = {
    DOC_TYPE_SCIENTIFIC_ARTICLE: {
        "type": "object",
        "properties": {
            "udk": {"type": "string"},
            "title_ru": {"type": "string"},
            "title_en": {"type": "string"},
            "authors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "org": {"type": "string"},
                        "orcid": {"type": "string"},
                        "email": {"type": "string"},
                        "degree": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            "keywords_ru": {"type": "array", "items": {"type": "string"}},
            "keywords_en": {"type": "array", "items": {"type": "string"}},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "unit": {"type": "string"},
                        "condition": {"type": "string"},
                        "source_table": {"type": "string"},
                    },
                    "required": ["subject", "predicate", "object"],
                },
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "raw": {"type": "string"},
                    },
                    "required": ["n", "raw"],
                },
            },
        },
        "required": ["title_ru", "authors"],
    },
    DOC_TYPE_ASSET_PROFILE: {
        "type": "object",
        "properties": {
            "asset_name": {"type": "string"},
            "country": {"type": "string"},
            "commodity": {"type": "array", "items": {"type": "string"}},
            "ownership": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "share_pct": {"type": "number"},
                    },
                    "required": ["owner"],
                },
            },
            "facility": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "technology": {"type": "string"},
                    "commissioned_year": {"type": "integer"},
                },
            },
            "feed_composition_pct": {
                "type": "object",
                "properties": {
                    "Ni": {"type": "number"},
                    "Cu": {"type": "number"},
                    "S": {"type": "number"},
                    "Fe": {"type": "number"},
                },
            },
            "capacity_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "year": {"type": "integer"},
                        "event": {"type": "string"},
                        "effect": {"type": "string"},
                    },
                    "required": ["year", "event"],
                },
            },
            "kpi": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "value_pct": {"type": "number"},
                        "as_of_year": {"type": "integer"},
                    },
                    "required": ["metric"],
                },
            },
        },
        "required": ["asset_name", "country"],
    },
    DOC_TYPE_REVIEW: {
        "type": "object",
        "properties": {
            "review_code": {"type": "string"},
            "approved_by": {"type": "string"},
            "executors": {"type": "array", "items": {"type": "string"}},
            "topic": {"type": "string"},
            "entities_mentioned": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mine": {"type": "string"},
                        "country": {"type": "string"},
                        "method": {"type": "string"},
                        "since_year": {"type": "integer"},
                        "note": {"type": "string"},
                    },
                    "required": ["mine"],
                },
            },
            "cited_sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["topic"],
    },
    DOC_TYPE_UNKNOWN: {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "topic": {"type": "string"},
            "mentioned_countries": {"type": "array", "items": {"type": "string"}},
            "mentioned_commodities": {"type": "array", "items": {"type": "string"}},
            "mentioned_years": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["topic"],
    },
}
JSON_SCHEMAS[DOC_TYPE_ANALYTICAL_NOTE] = JSON_SCHEMAS[DOC_TYPE_REVIEW]

SYSTEM_PROMPT = (
    "Ты извлекаешь структурированные данные из технического/научного "
    "документа горно-металлургической тематики. Заполняй только те поля, "
    "для которых есть явное подтверждение в тексте; если данных нет — "
    "используй пустую строку \"\" или пустой список [], но не выдумывай "
    "значения."
)


def _build_prompt(doc_type: str, full_text: str, tables: list[dict]) -> str:
    text_snippet = full_text[:config.MAX_LLM_INPUT_CHARS]
    tables_json = json.dumps(tables[:10], ensure_ascii=False)
    return (
        f"Тип документа: {doc_type}.\n\n"
        f"Таблицы, извлечённые из документа (используй их как источник для "
        f"количественных полей, не пересказывай текстом):\n{tables_json}\n\n"
        f"Текст документа:\n{text_snippet}"
    )


def extract_properties(
    doc_type: str,
    full_text: str,
    tables: list[dict],
    budget: BudgetTracker | None = None,
) -> dict:
    """
    budget: если передан, перед вызовом API проверяется, не превысит ли
    этот вызов лимит (см. budget.py). Если превысит — поднимается
    BudgetExceededError и запрос НЕ отправляется. Вызывающий код
    (main.py) должен поймать это исключение и корректно остановить
    дальнейшую обработку.
    """
    schema = JSON_SCHEMAS.get(doc_type, JSON_SCHEMAS[DOC_TYPE_UNKNOWN])
    prompt = _build_prompt(doc_type, full_text, tables)

    if config.LLM_BACKEND == "local":
        # Бесплатный локальный путь — без бюджета, без API-ключей.
        return local_llm.extract_properties_local(doc_type, prompt, SYSTEM_PROMPT, schema)

    if not config.YANDEX_API_KEY or not config.YANDEX_FOLDER_ID:
        raise RuntimeError(
            "Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID в переменных окружения."
        )

    if budget is not None:
        # Грубая оценка: для кириллицы ~1 токен на 2.5 символа. Output
        # оцениваем по максимуму (MAX_LLM_OUTPUT_TOKENS) — консервативно,
        # чтобы не проскочить лимит, если ответ окажется длиннее ожидаемого.
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
            "json_schema": {"name": doc_type, "schema": schema},
        },
    }
    headers = {
        "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
        "OpenAI-Project": config.YANDEX_FOLDER_ID,
        "Content-Type": "application/json",
    }
    resp = requests.post(config.YANDEX_API_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    if budget is not None:
        usage = data.get("usage", {})
        budget.record_actual(
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        )

    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"doc_type": doc_type, "_parse_error": True, "_raw_response": json.dumps(data, ensure_ascii=False)}

    raw = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
    try:
        parsed = json.loads(raw)
        parsed["doc_type"] = doc_type
        return parsed
    except json.JSONDecodeError:
        return {"doc_type": doc_type, "_parse_error": True, "_raw_response": raw}


def extract_document_links(full_text: str) -> list[str]:
    """
    Грубое извлечение упоминаний других документов/источников в тексте —
    по строкам вида "[1]", "[2, 5]" в теле статьи (ссылки на нумерованный
    список ЛИТЕРАТУРА/REFERENCES) — как основа для сборки графа связей
    между документами на этапе после LLM-извлечения references.
    """
    return sorted(set(re.findall(r"\[(\d+(?:,\s*\d+)*)\]", full_text)))
