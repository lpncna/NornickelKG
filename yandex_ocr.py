"""
Клиент Yandex Vision OCR — основной OCR-движок для страниц PDF с битым
текстовым слоем. Модель 'markdown' (по умолчанию, см. config.py) сохраняет
структуру таблиц прямо в распознанном тексте — не нужен отдельный проход
детектора таблиц для отсканированных страниц, в отличие от Tesseract,
который отдаёт только плоский текст.

Документация: https://aistudio.yandex.ru/docs/ru/vision/concepts/ocr/
"""
from __future__ import annotations

import base64

import requests

import config


def recognize_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Отправляет изображение на распознавание в Yandex Vision OCR и
    возвращает распознанный текст (в Markdown, если модель это
    поддерживает и в ответе есть поле 'markdown' — иначе собирается из
    обычной построчной разметки).

    Поднимает исключение при ошибке сети/API/авторизации — решение,
    откатываться ли на Tesseract, принимает вызывающий код
    (extract_text.ocr_page_fallback).
    """
    if not config.YANDEX_API_KEY:
        raise RuntimeError("Не задан YANDEX_API_KEY.")

    payload = {
        "content": base64.b64encode(image_bytes).decode("ascii"),
        "mimeType": mime_type,
        "languageCodes": ["ru", "en"],
        "model": config.YANDEX_OCR_MODEL,
    }
    headers = {
        "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(config.YANDEX_OCR_API_URL, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    return _extract_text(resp.json())


def _extract_text(data: dict) -> str:
    """
    Формат ответа Vision OCR может немного отличаться между моделями —
    разбираем защищённо, не полагаясь на единственную жёсткую схему.
    """
    result = data.get("result", data)
    annotation = result.get("textAnnotation", result) if isinstance(result, dict) else {}
    if not isinstance(annotation, dict):
        return ""

    markdown = annotation.get("markdown")
    if markdown:
        return markdown

    lines_out: list[str] = []
    for block in annotation.get("blocks", []) or []:
        for line in block.get("lines", []) or []:
            text = line.get("text", "")
            if text:
                lines_out.append(text)
    return "\n".join(lines_out)
