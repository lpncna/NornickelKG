"""
Строит SQLite-индекс синонимов из большого JSONL-словаря терминов
(metallurgy_dict.jsonl, ~3.5 млн записей вида:
{"id": "Q...", "names": {"ru": "...", "en": "..."},
 "aliases": {"ru": [...], "en": [...]},
 "descriptions": {...}, "matched_classes": [...]}).

Судя по id (Wikidata Q-коды) — это фактически общий словарь Wikidata, не
только металлургический, но это не мешает: используется только как
источник синонимов для сущностей, которые мы САМИ извлекли из корпуса, так
что нерелевантные записи просто никогда не совпадут ни с чем.

Правила безопасности (согласованы с пользователем):
  - алиасы короче 3 символов после нормализации ИГНОРИРУЮТСЯ (риск ложных
    слияний вроде химического символа "Co" с кодом страны "CO");
  - если один и тот же нормализованный алиас указывает на РАЗНЫЕ id —
    такой алиас считается неоднозначным и ИСКЛЮЧАЕТСЯ из индекса целиком
    (не используется вообще, ни для одного из конфликтующих id).

Результат: output/term_index.sqlite3, таблица
term_index(alias UNIQUE, canonical_id, name_ru, name_en) — используется из
build_graph.normalize_key() автоматически, если файл существует.

Запуск (разово, файл большой — может занять несколько минут):
    python3 build_term_index.py metallurgy_dict.jsonl
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import config
from build_graph import base_normalize as normalize_key

MIN_ALIAS_LENGTH = 3
BATCH_SIZE = 50_000


def iter_alias_candidates(record: dict):
    """Отдаёт (сырой_алиас, id, name_ru, name_en) для каждого варианта имени/алиаса записи."""
    canonical_id = record.get("id", "")
    names = record.get("names") or {}
    aliases = record.get("aliases") or {}
    name_ru = names.get("ru")
    name_en = names.get("en")

    candidates = []
    if name_ru:
        candidates.append(name_ru)
    if name_en:
        candidates.append(name_en)
    for a in (aliases.get("ru") or []):
        candidates.append(a)
    for a in (aliases.get("en") or []):
        candidates.append(a)

    for raw in candidates:
        if raw:
            yield raw, canonical_id, name_ru, name_en


def build_index(jsonl_path: Path, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = OFF")   # разовая массовая загрузка, надёжность не критична
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("DROP TABLE IF EXISTS raw_aliases")
    conn.execute("DROP TABLE IF EXISTS term_index")
    conn.execute("CREATE TABLE raw_aliases (alias TEXT, canonical_id TEXT, name_ru TEXT, name_en TEXT)")

    batch = []
    total_lines = 0
    total_candidates = 0
    skipped_short = 0

    print(f"Читаю {jsonl_path} ...")
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            for raw_alias, canonical_id, name_ru, name_en in iter_alias_candidates(record):
                key = normalize_key(str(raw_alias))
                if len(key) < MIN_ALIAS_LENGTH:
                    skipped_short += 1
                    continue
                batch.append((key, canonical_id, name_ru, name_en))
                total_candidates += 1

            if len(batch) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT INTO raw_aliases (alias, canonical_id, name_ru, name_en) VALUES (?, ?, ?, ?)",
                    batch,
                )
                batch.clear()

            if total_lines % 200_000 == 0:
                print(f"  ...строк: {total_lines}, кандидатов алиасов: {total_candidates}")

    if batch:
        conn.executemany(
            "INSERT INTO raw_aliases (alias, canonical_id, name_ru, name_en) VALUES (?, ?, ?, ?)",
            batch,
        )
    conn.commit()
    print(f"Всего строк в словаре: {total_lines}")
    print(f"Кандидатов алиасов (после фильтра длины >= {MIN_ALIAS_LENGTH}): {total_candidates}")
    print(f"Отброшено как слишком короткие: {skipped_short}")

    ambiguous_count = conn.execute(
        "SELECT COUNT(*) FROM (SELECT alias FROM raw_aliases GROUP BY alias HAVING COUNT(DISTINCT canonical_id) > 1)"
    ).fetchone()[0]
    print(f"Неоднозначных алиасов (исключаются из индекса): {ambiguous_count}")

    print("Строю итоговый индекс...")
    conn.execute("""
        CREATE TABLE term_index AS
        SELECT alias, canonical_id, name_ru, name_en
        FROM raw_aliases
        WHERE alias IN (
            SELECT alias FROM raw_aliases GROUP BY alias HAVING COUNT(DISTINCT canonical_id) = 1
        )
        GROUP BY alias
    """)
    conn.execute("CREATE UNIQUE INDEX idx_term_index_alias ON term_index(alias)")
    conn.execute("DROP TABLE raw_aliases")
    conn.commit()

    final_count = conn.execute("SELECT COUNT(*) FROM term_index").fetchone()[0]
    print(f"Готово. Однозначных алиасов в индексе: {final_count}")
    print(f"Индекс сохранён: {db_path}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl_path", help="Путь к metallurgy_dict.jsonl")
    parser.add_argument("--output", default=None,
                         help="Путь к итоговому .sqlite3 (по умолчанию output/term_index.sqlite3)")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else (config.BASE_DIR / "output" / "term_index.sqlite3")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    build_index(Path(args.jsonl_path), out_path)
