"""
Дедупликация: находим документы с совпадающим нормализованным текстом
(напр. docx-версия статьи и отдельный PDF того же текста) и помечаем
дубликаты, оставляя один "канонический" экземпляр.
"""
from __future__ import annotations

import hashlib
import re


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)          # схлопнуть пробелы/переносы строк
    text = re.sub(r"[^\w\s]", "", text)       # убрать пунктуацию
    return text.strip()


def text_hash(text: str) -> str:
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_duplicates(documents: list[dict]) -> dict[str, list[str]]:
    """
    documents: [{"document_id": ..., "full_text": ...}, ...]
    Возвращает {hash: [document_id, document_id, ...]} только для групп
    из 2+ документов (реальные дубликаты).
    """
    by_hash: dict[str, list[str]] = {}
    for doc in documents:
        h = text_hash(doc["full_text"])
        by_hash.setdefault(h, []).append(doc["document_id"])
    return {h: ids for h, ids in by_hash.items() if len(ids) > 1}


def pick_canonical(document_ids: list[str], documents_by_id: dict[str, dict]) -> str:
    """
    Из группы дубликатов выбираем канонический экземпляр: предпочитаем
    .docx как источник (структурированный, легче парсить таблицы), иначе —
    файл с наибольшим количеством извлечённых изображений/таблиц.
    """
    def score(doc_id: str) -> tuple:
        doc = documents_by_id[doc_id]
        is_docx = doc.get("source_ext", "").lower() in (".docx", ".doc")
        richness = len(doc.get("images", [])) + len(doc.get("tables", []))
        return (is_docx, richness)

    return max(document_ids, key=score)
