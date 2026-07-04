"""
Извлечение изображений из PDF (PyMuPDF/fitz) и DOCX (python-docx), с
эвристической разметкой "вероятно декоративное" (иконки, разделители) —
по итогам обсуждения такие изображения не удаляются безвозвратно, а
помечаются флагом is_decorative для последующей фильтрации, чтобы не
терять содержательные картинки по ошибке (пороги эвристики консервативны).

Изображения привязываются к номеру страницы и, если рядом в тексте есть
подпись вида "Рис. N" / "Figure N", то и к этой подписи — это даёт
привязку "диаграмма к выводам", "фото оборудования к разделу оборудования"
на следующем шаге (properties_llm.py сопоставляет caption с текстом рядом).

pip install PyMuPDF python-docx --break-system-packages
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import config

_CAPTION_RE = re.compile(
    r"(Рис(?:унок)?\.?\s*\d+[.:]?[^\n]{0,150}|Figure\s*\d+[.:]?[^\n]{0,150})",
    re.IGNORECASE,
)


@dataclass
class ExtractedImage:
    image_id: str
    page_number: int | None
    local_path: str
    caption: str | None
    is_decorative: bool
    width: int
    height: int


def _is_decorative(width: int, height: int) -> bool:
    if max(width, height) < config.MIN_MEANINGFUL_IMAGE_SIDE:
        return True
    aspect = max(width, height) / max(1, min(width, height))
    if aspect > config.MAX_ASPECT_RATIO_DECORATIVE:
        return True
    return False


def _find_caption_near(page_text: str) -> str | None:
    m = _CAPTION_RE.search(page_text or "")
    return m.group(0).strip() if m else None


def extract_from_pdf(path: Path, page_texts: dict[int, str], out_dir: Path) -> list[ExtractedImage]:
    import fitz  # PyMuPDF

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractedImage] = []

    doc = fitz.open(str(path))
    for page_index in range(len(doc)):
        page = doc[page_index]
        page_number = page_index + 1
        caption = _find_caption_near(page_texts.get(page_number, ""))

        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            ext = base_image["ext"]
            width, height = base_image.get("width", 0), base_image.get("height", 0)

            image_id = str(uuid.uuid4())
            out_path = out_dir / f"{image_id}.{ext}"
            out_path.write_bytes(img_bytes)

            results.append(
                ExtractedImage(
                    image_id=image_id,
                    page_number=page_number,
                    local_path=str(out_path),
                    caption=caption,
                    is_decorative=_is_decorative(width, height),
                    width=width,
                    height=height,
                )
            )
    doc.close()
    return results


def extract_from_docx(path: Path, out_dir: Path) -> list[ExtractedImage]:
    import docx
    from PIL import Image
    import io

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractedImage] = []

    d = docx.Document(str(path))
    full_text = "\n".join(p.text for p in d.paragraphs)
    caption = _find_caption_near(full_text)  # грубая привязка на уровне документа

    for rel in d.part.rels.values():
        if "image" not in rel.reltype:
            continue
        img_bytes = rel.target_part.blob
        try:
            im = Image.open(io.BytesIO(img_bytes))
            width, height = im.size
            ext = (im.format or "png").lower()
        except Exception:
            width = height = 0
            ext = "bin"

        image_id = str(uuid.uuid4())
        out_path = out_dir / f"{image_id}.{ext}"
        out_path.write_bytes(img_bytes)

        results.append(
            ExtractedImage(
                image_id=image_id,
                page_number=None,
                local_path=str(out_path),
                caption=caption,
                is_decorative=_is_decorative(width, height),
                width=width,
                height=height,
            )
        )
    return results
