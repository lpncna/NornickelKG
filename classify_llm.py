"""
Классификация типа документа через локальную LLM (Ollama) — используется
как второй проход, только для документов, которые regex-классификатор
(classify.py) не смог уверенно определить. Это дешевле и быстрее, чем
гонять LLM на всём корпусе: чёткие случаи (УДК+Аннотация, СПРАВКА по
компании и т.п.) уже отловлены бесплатно, LLM решает только неоднозначные.

Классификация — маленький, быстрый вызов (модель возвращает всего одно
поле doc_type), в отличие от полного извлечения свойств — не путать с
properties_llm.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

import config
import classify

logger = logging.getLogger("classify_llm")

OLLAMA_API_URL = "http://172.19.48.1:11434/api/chat"

# Проверяем доступность Ollama один раз за прогон, а не таймаутом на каждом
# документе — если она не запущена, сразу откатываемся на unknown для всех
# оставшихся документов без задержек.
_ollama_available: bool | None = None


def _ollama_ready() -> bool:
    global _ollama_available
    if _ollama_available is not None:
        return _ollama_available
    try:
        requests.get("http://172.19.48.1:11434/api/version", timeout=3)
        _ollama_available = True
    except Exception:
        _ollama_available = False
        logger.warning(
            "Ollama недоступна на localhost:11434 — LLM-классификация "
            "отключена на весь прогон, документы без regex-совпадения "
            "останутся 'unknown'."
        )
    return _ollama_available

# Окно текста для классификации — меньше, чем для извлечения свойств
# (config.MAX_LLM_INPUT_CHARS), т.к. для определения жанра документа
# начала обычно достаточно, а запрос должен быть быстрым.
CLASSIFY_TEXT_WINDOW = 4000

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

_CATEGORY_DESCRIPTIONS = """
- scientific_article: научная статья — УДК, аннотация, ключевые слова, разделы (введение/методология/заключение), список литературы, один автор или авторский коллектив, один предмет исследования.
- asset_profile: справка по активу/предприятию — описание конкретного завода/рудника/актива, оборудование, состав руды/концентрата в %, история модернизаций по годам.
- review: формальный обзор — гриф "УТВЕРЖДАЮ", номер вида ОИП-XX-ГГГГ, список исполнителей, оглавление, тематические разделы.
- analytical_note: аналитическая записка без формального грифа — сравнительные таблицы по нескольким рудникам/странам, много цитирования сторонних источников.
- presentation: слайды презентации или доклада — набор тезисов/иллюстраций для устного выступления, не связный текст статьи.
- market_report: рыночный/квартальный отчёт по товарному металлу — цены, объёмы производства, прогнозы спроса/предложения, обычно на английском (CRU, Brook Hunt, ALTA и т.п.).
- journal_issue: целый номер научного журнала — оглавление с несколькими статьями разных авторов внутри одного файла, не единая статья.
- unknown: не подходит уверенно ни под одну из категорий выше.
""".strip()

SYSTEM_PROMPT = (
    "Ты классифицируешь технический/научный документ горно-металлургической "
    "тематики по жанру. Определи ОДНУ наиболее подходящую категорию из "
    "списка на основе текста и (если дана) папки-источника.\n\n"
    f"Категории:\n{_CATEGORY_DESCRIPTIONS}\n\n"
    "Если сомневаешься между двумя категориями — выбери ту, что упомянута "
    "выше первой. Если не подходит ничего уверенно — unknown."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"doc_type": {"type": "string", "enum": ALL_TYPES}},
    "required": ["doc_type"],
}


def _folder_hint(source_path: str) -> str:
    """
    Название папки сразу под "Источники информации" — дешёвый и надёжный
    сигнал (Доклады/Журналы/Материалы конференций/Обзоры/Статьи), который
    помогает LLM не гадать вслепую.
    """
    parts = Path(source_path).parts
    if "Источники информации" in parts:
        idx = parts.index("Источники информации")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def classify_document_llm(full_text: str, source_path: str) -> str:
    """
    Определяет doc_type через локальную модель. При недоступности Ollama
    или любой другой ошибке — тихо откатывается на DOC_TYPE_UNKNOWN
    (вызывающий код и так подставлял unknown до этого вызова, хуже не
    станет), не прерывая обработку остального корпуса.
    """
    if not _ollama_ready():
        return classify.DOC_TYPE_UNKNOWN

    folder = _folder_hint(source_path)
    snippet = full_text[:CLASSIFY_TEXT_WINDOW]

    prompt = (
        (f"Папка-источник: {folder}\n\n" if folder else "")
        + f"Текст документа (начало):\n{snippet}"
    )

    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        resp = requests.post(OLLAMA_API_URL, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("message", {}).get("content", "")
        import json
        parsed = json.loads(raw)
        doc_type = parsed.get("doc_type", classify.DOC_TYPE_UNKNOWN)
        return doc_type if doc_type in ALL_TYPES else classify.DOC_TYPE_UNKNOWN
    except Exception as e:
        logger.warning("LLM-классификация не удалась (%s), оставляю unknown", e)
        return classify.DOC_TYPE_UNKNOWN
