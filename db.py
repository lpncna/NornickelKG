from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    schema_path = Path(__file__).parent / "db_schema.sql"
    with get_connection() as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))


def _flatten_properties(properties: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Плоский список (key, value) для простых типов; списки/словари -> JSON-строкой."""
    flat: list[tuple[str, str]] = []
    for k, v in properties.items():
        key = f"{prefix}{k}"
        if isinstance(v, (dict, list)):
            flat.append((key, json.dumps(v, ensure_ascii=False)))
        elif v is not None:
            flat.append((key, str(v)))
    return flat


def insert_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    bundle_id: str,
    source_path: str,
    source_ext: str,
    doc_type: str,
    is_duplicate_of: str | None,
    json_path: str,
    properties: dict,
    images: list[dict],
    tables: list[dict],
    document_links: list[str],
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO documents
           (document_id, bundle_id, source_path, source_ext, doc_type,
            is_duplicate_of, json_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (document_id, bundle_id, source_path, source_ext, doc_type, is_duplicate_of, json_path),
    )

    for key, value in _flatten_properties(properties):
        conn.execute(
            "INSERT INTO document_properties (document_id, prop_key, prop_value) VALUES (?, ?, ?)",
            (document_id, key, value),
        )

    for author in properties.get("authors", []) or []:
        conn.execute(
            """INSERT INTO authors (document_id, name, org, orcid, email, degree)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                document_id,
                author.get("name"),
                author.get("org"),
                author.get("orcid"),
                author.get("email"),
                author.get("degree"),
            ),
        )

    for img in images:
        conn.execute(
            """INSERT OR REPLACE INTO images
               (image_id, document_id, page_number, local_path, caption,
                is_decorative, width, height, linked_entity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                img["image_id"], document_id, img.get("page_number"),
                img["local_path"], img.get("caption"),
                int(bool(img.get("is_decorative"))), img.get("width"), img.get("height"),
                img.get("linked_entity"),
            ),
        )

    for tbl in tables:
        conn.execute(
            """INSERT OR REPLACE INTO tables_extracted
               (table_id, document_id, page_number, columns_json, rows_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                tbl["table_id"], document_id, tbl.get("page_number"),
                json.dumps(tbl.get("columns", []), ensure_ascii=False),
                json.dumps(tbl.get("rows", []), ensure_ascii=False),
            ),
        )

    for ref in document_links:
        conn.execute(
            "INSERT INTO document_links (from_document_id, ref_raw, to_document_id) VALUES (?, ?, NULL)",
            (document_id, ref),
        )
