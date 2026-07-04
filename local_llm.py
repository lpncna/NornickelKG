"""
Локальный бесплатный бэкенд извлечения свойств документа — через Ollama,
работает полностью офлайн на вашей машине, без API-ключей и затрат.

Установка (один раз):
    brew install ollama
    ollama serve &                        # если не поднят как фоновый сервис
    ollama pull qwen2.5:14b-instruct      # или другая модель, см. config.py

Модель по умолчанию — Qwen2.5 14B Instruct: хорошо работает с русским языком
и структурированным извлечением, разумный баланс качества/скорости для
M-серии Mac с достаточным объёмом unified memory (14B в 4-bit кванте — это
~9 ГБ). Если модель слишком медленная или не помещается в память — можно
переключиться на модель поменьше, например qwen2.5:7b-instruct
(export LOCAL_LLM_MODEL=qwen2.5:7b-instruct).

Использует нативный Ollama API (/api/chat) с параметром `format` — туда
можно передать JSON Schema напрямую, и Ollama гарантирует, что ответ будет
ей соответствовать (аналог response_format: json_schema у облачных API).
"""
from __future__ import annotations

import json
import logging

import requests

import config

logger = logging.getLogger("local_llm")

OLLAMA_API_URL = "http://localhost:11434/api/chat"


def check_ollama_available() -> bool:
    try:
        requests.get("http://localhost:11434/api/version", timeout=3)
        return True
    except Exception:
        return False


def extract_properties_local(doc_type: str, prompt: str, system_prompt: str, schema: dict) -> dict:
    """
    Вызывает локальную модель через Ollama со структурированным выводом по
    JSON Schema. Поднимает исключение при недоступности Ollama или ошибке
    генерации — вызывающий код (properties_llm.py) сам решает, что делать
    дальше (в отличие от облачного бюджета, здесь нет смысла в "мягкой"
    остановке — либо Ollama работает, либо нет).
    """
    if not check_ollama_available():
        raise RuntimeError(
            "Ollama не отвечает на localhost:11434. Убедитесь, что она запущена: "
            "`ollama serve` (или проверьте, что приложение Ollama открыто), и что "
            f"модель скачана: `ollama pull {config.LOCAL_LLM_MODEL}`."
        )

    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    # Реальный лимит на документ задаёт MAX_SECONDS_PER_DOCUMENT в main.py
    # (через сигнал прерывания) — здесь просто большой запас, чтобы этот
    # HTTP-таймаут не срабатывал раньше него на медленных генерациях.
    resp = requests.post(OLLAMA_API_URL, json=payload, timeout=1200)
    resp.raise_for_status()
    data = resp.json()

    raw = data.get("message", {}).get("content", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"doc_type": doc_type, "_parse_error": True, "_raw_response": raw}

    # doc_type мог быть определён самой моделью (единый проход
    # классификация+извлечение) — не перезаписываем его, если он уже есть
    # в ответе. Подставляем переданный doc_type только как запасной вариант.
    parsed.setdefault("doc_type", doc_type)
    return parsed
