"""
Классификатор типа документа по структурным маркерам (regex, без LLM),
согласно 4 типам из саммари анализа.
"""
from __future__ import annotations

import re

DOC_TYPE_SCIENTIFIC_ARTICLE = "scientific_article"
DOC_TYPE_ASSET_PROFILE = "asset_profile"
DOC_TYPE_REVIEW = "review"          # формальный обзор (ОИП)
DOC_TYPE_ANALYTICAL_NOTE = "analytical_note"  # тип 4
DOC_TYPE_PRESENTATION = "presentation"        # слайды доклада/презентации
DOC_TYPE_MARKET_REPORT = "market_report"      # рыночный отчёт CRU/Brook Hunt/ALTA и т.п.
DOC_TYPE_JOURNAL_ISSUE = "journal_issue"      # целый номер журнала, несколько статей в одном файле
DOC_TYPE_UNKNOWN = "unknown"

# Типы, для которых имеет смысл платное/затратное LLM-извлечение структурных
# свойств (см. properties_llm.py) — остальные получают properties={} без
# обращения к LLM, т.к. для них полнотекстового поиска достаточно (решение
# принято по итогам анализа корпуса: рыночные отчёты, презентации и целые
# номера журналов не дают выгоды от строгой JSON-схемы).
PROPERTY_EXTRACTION_TYPES = {
    DOC_TYPE_SCIENTIFIC_ARTICLE,
    DOC_TYPE_ASSET_PROFILE,
    DOC_TYPE_REVIEW,
    DOC_TYPE_ANALYTICAL_NOTE,
}

_UDK_RE = re.compile(r"\bУДК\b", re.IGNORECASE)
_ABSTRACT_RE = re.compile(r"\b(Аннотация|Abstract)\b", re.IGNORECASE)
_ASSET_RE = re.compile(r"СПРАВКА\s+по\s+компании", re.IGNORECASE)
_APPROVED_RE = re.compile(r"\bУТВЕРЖДАЮ\b", re.IGNORECASE)
_REVIEW_RE = re.compile(r"\bОБЗОР\b", re.IGNORECASE)
_REVIEW_CODE_RE = re.compile(r"ОИП-\d+-\d{4}")

# Первые N символов текста, где ищем маркеры (заголовочная часть документа)
HEADER_WINDOW = 3000


def classify_document(full_text: str) -> str:
    header = full_text[:HEADER_WINDOW]

    if _UDK_RE.search(header) and _ABSTRACT_RE.search(header):
        return DOC_TYPE_SCIENTIFIC_ARTICLE

    if _ASSET_RE.search(header):
        return DOC_TYPE_ASSET_PROFILE

    if (_APPROVED_RE.search(header) and _REVIEW_RE.search(header)) or _REVIEW_CODE_RE.search(header):
        return DOC_TYPE_REVIEW

    # Тип 4 определить положительным маркером сложно (по саммари — он
    # "начинается сразу с темы", т.е. определяется по остаточному принципу).
    # Условный сигнал: наличие плотных сравнительных таблиц по рудникам —
    # здесь используем эвристику по ключевым словам как маркер средней силы.
    if re.search(r"рудник\w*.{0,200}(страна|метод|состав)", header, re.IGNORECASE | re.DOTALL):
        return DOC_TYPE_ANALYTICAL_NOTE

    return DOC_TYPE_UNKNOWN
