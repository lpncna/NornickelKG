"""
Извлечение таблиц отдельно от параграфов (составы концентрата, KPI,
эффективность реагентов и т.п. живут только в таблицах — см. саммари).

Для PDF используем pdfplumber.extract_tables() как более универсальный
вариант (camelot требует "чистых" таблиц с явными линиями и системных
зависимостей Ghostscript — можно подключить как второй проход для PDF,
где pdfplumber даёт пустой/некачественный результат).

pip install pdfplumber python-docx --break-system-packages
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExtractedTable:
    table_id: str
    page_number: int | None
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)


def _is_probably_real_table(table: ExtractedTable, min_fill_ratio: float = 0.35) -> bool:
    """
    Отсекает ложные срабатывания детектора таблиц на графиках/диаграммах —
    типичный паттерн: одна "колонка" с длинным перемешанным текстом (оси
    двух графиков рядом на странице слились в один блок) либо таблица из
    сплошь пустых ячеек. Настоящая табличная структура почти всегда имеет
    ≥2 колонки и заметную долю заполненных ячеек.
    """
    if len(table.columns) < 2:
        return False
    if not table.rows:
        return False
    total_cells = len(table.columns) * len(table.rows)
    if total_cells == 0:
        return False
    non_empty = sum(1 for c in table.columns if c.strip())
    non_empty += sum(1 for row in table.rows for cell in row if cell.strip())
    fill_ratio = non_empty / (total_cells + len(table.columns))
    return fill_ratio >= min_fill_ratio


def extract_from_pdf(path: Path) -> list[ExtractedTable]:
    import pdfplumber

    results: list[ExtractedTable] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            for raw_table in page.extract_tables():
                if not raw_table or len(raw_table) < 2:
                    continue
                header = [str(c or "").strip() for c in raw_table[0]]
                rows = [[str(c or "").strip() for c in r] for r in raw_table[1:]]
                table = ExtractedTable(
                    table_id=str(uuid.uuid4()),
                    page_number=i,
                    columns=header,
                    rows=rows,
                )
                if _is_probably_real_table(table):
                    results.append(table)
    return results


def extract_from_docx(path: Path) -> list[ExtractedTable]:
    import docx

    results: list[ExtractedTable] = []
    d = docx.Document(str(path))
    for table in d.tables:
        if not table.rows:
            continue
        header = [cell.text.strip() for cell in table.rows[0].cells]
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows[1:]]
        parsed = ExtractedTable(
            table_id=str(uuid.uuid4()),
            page_number=None,
            columns=header,
            rows=rows,
        )
        # Для docx таблиц фильтр по заполненности не применяем — они почти
        # всегда настоящие (созданы автором вручную, не детектором на глаз).
        results.append(parsed)
    return results


def extract_tables(path: Path) -> list[ExtractedTable]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_from_pdf(path)
    if suffix in (".docx", ".doc", ".docm"):
        return extract_from_docx(path)
    return []
