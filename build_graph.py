"""
Строит граф знаний из output/json/*.json (результат graph_extract.py) и
либо выгружает в output/graph.cypher (вставить в Neo4j Browser или
`cat output/graph.cypher | cypher-shell -u neo4j -p ПАРОЛЬ`), либо грузит
напрямую в работающий Neo4j через официальный bolt-драйвер.

Модель графа:
    (:Document {id, title, doc_type, source_path})
    (:Entity:<Metal|Material|Organization|Asset|Country|Person|Method|
        Equipment|Experiment|Property|Publication|Other|Term> {key, name})
    (:Keyword {key, name})
    (:Document)-[:MENTIONS]->(:Entity)
    (:Document)-[:HAS_KEYWORD]->(:Keyword)
    (:Entity)-[:USES_MATERIAL|OPERATES_AT_CONDITION|PRODUCES_OUTPUT|
        DESCRIBED_IN|VALIDATED_BY|CONTRADICTS|LOCATED_IN|AFFILIATED_WITH|
        HAS_PROPERTY|PART_OF|OTHER_RELATION
        {predicate, unit, source, doc_id}]->(:Entity)

Тип связи (relation_type) — контролируемый словарь, определяется моделью
на этапе извлечения (graph_extract.py), а не свободный текст — это
реальный тип Neo4j-связи, а не просто свойство.

Сущности дедуплицируются по нормализованному имени+типу через ВЕСЬ корпус
(не по документам отдельно) — одна и та же сущность из разных документов
схлопывается в один узел графа. Дополнительно нормализация распознаёт
известные синонимы/аббревиатуры (ALIAS_MAP) и убирает организационно-
правовые формы (ООО/ПАО/АО/LLC/JSC).

Запуск:
    python3 build_graph.py --export-cypher
    python3 build_graph.py --load-neo4j --uri bolt://localhost:7687 \
        --user neo4j --password ВАШ_ПАРОЛЬ

Для --load-neo4j нужен официальный драйвер:
    pip install neo4j
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import config

LABEL_MAP = {
    "person": "Person",
    "organization": "Organization",
    "asset": "Asset",
    "country": "Country",
    "metal": "Metal",
    "material": "Material",
    "method": "Method",
    "equipment": "Equipment",
    "experiment": "Experiment",
    "property": "Property",
    "publication": "Publication",
    "other": "Other",
}
FALLBACK_LABEL = "Term"  # для subject/object из relations, не найденных среди entities

# Контролируемый словарь связей (см. graph_extract.RELATION_TYPES) — тип
# связи в Neo4j, а не текст в свойстве. Онтология из ТЗ хакатона
# (uses_material/operates_at_condition/produces_output/described_in/
# validated_by/contradicts) + расширение под остальной корпус.
RELATION_TYPE_MAP = {
    "uses_material": "USES_MATERIAL",
    "operates_at_condition": "OPERATES_AT_CONDITION",
    "produces_output": "PRODUCES_OUTPUT",
    "described_in": "DESCRIBED_IN",
    "validated_by": "VALIDATED_BY",
    "contradicts": "CONTRADICTS",
    "located_in": "LOCATED_IN",
    "affiliated_with": "AFFILIATED_WITH",
    "has_property": "HAS_PROPERTY",
    "part_of": "PART_OF",
    "other": "OTHER_RELATION",
}
DEFAULT_RELATION_TYPE = "OTHER_RELATION"

# Организационно-правовые формы — без их удаления "ООО «Институт
# Гипроникель»" и "Институт Гипроникель" считаются РАЗНЫМИ сущностями и
# граф расщепляется на дубли одного и того же реального объекта.
_LEGAL_FORM_RE = re.compile(
    r"\b(ооо|оао|пао|зао|ао|гк|llc|jsc|ltd|inc|corp|co)\b\.?",
    re.IGNORECASE,
)
_QUOTES_RE = re.compile(r"[«»\"'“”]")
_PAREN_RE = re.compile(r"\([^)]*\)")

# Известные синонимы/сокращения/переводы одного и того же понятия — НЕ
# решает произвольный кросс-языковой перевод (это отдельная, более сложная
# задача), но закрывает конкретные частые случаи из горно-металлургической
# тематики. Ключ и значение — уже в нормализованном виде (нижний регистр).
# Дополняйте по мере обнаружения новых дублей в графе.
ALIAS_MAP = {
    "electrowinning": "электроэкстракция",
    "пвп": "печь взвешенной плавки",
    "fluidized bed furnace": "печь взвешенной плавки",
    "fsf": "печь взвешенной плавки",
    "eaf": "электродуговая печь",
    "electric arc furnace": "электродуговая печь",
    "smelting": "плавка",
    "leaching": "выщелачивание",
    "flotation": "флотация",
    "roasting": "обжиг",
    "gipronickel institute": "институт гипроникель",
    "llc gipronickel institute": "институт гипроникель",
}


import sqlite3

# Путь к индексу терминов (строится один раз через build_term_index.py из
# большого JSONL-словаря вида metallurgy_dict.jsonl). Если файла нет —
# просто не используется, ничего не ломается (только ALIAS_MAP работает).
TERM_INDEX_PATH = config.BASE_DIR / "output" / "term_index.sqlite3"
_term_index_conn: sqlite3.Connection | None = None
_term_index_checked = False


def _term_index_ready() -> bool:
    global _term_index_conn, _term_index_checked
    if _term_index_checked:
        return _term_index_conn is not None
    _term_index_checked = True
    if TERM_INDEX_PATH.exists():
        try:
            _term_index_conn = sqlite3.connect(str(TERM_INDEX_PATH), check_same_thread=False)
        except Exception:
            _term_index_conn = None
    return _term_index_conn is not None


def _lookup_term_index(key: str) -> str | None:
    """
    Ищет нормализованный ключ в словаре терминов. Возвращает 'wd:<id>' —
    канонический ключ по внешнему идентификатору (устойчив к любым
    вариантам написания/языка), либо None, если словарь недоступен или
    ключ не найден (в т.ч. если алиас был неоднозначным и исключён при
    построении индекса — см. build_term_index.py).
    """
    if not _term_index_ready():
        return None
    try:
        row = _term_index_conn.execute(
            "SELECT canonical_id FROM term_index WHERE alias = ? LIMIT 1", (key,)
        ).fetchone()
    except Exception:
        return None
    return f"wd:{row[0]}" if row else None


def base_normalize(name: str) -> str:
    """
    Нормализация БЕЗ обращения к ALIAS_MAP/словарю терминов — используется
    и здесь, и при построении самого индекса (build_term_index.py), чтобы
    не создавать циклическую зависимость от файла индекса при его сборке.
    """
    key = name.strip().lower()
    key = _PAREN_RE.sub(" ", key)      # "AO Kanex (Joint Stock Company)" -> "ao kanex"
    key = _QUOTES_RE.sub("", key)      # убираем «» " ' “”
    key = _LEGAL_FORM_RE.sub(" ", key)  # убираем ООО/ПАО/АО/LLC/JSC и т.п.
    key = re.sub(r"\s+", " ", key).strip()
    key = key.strip(".,;:()[]")
    return key


def normalize_key(name: str) -> str:
    key = base_normalize(name)
    key = ALIAS_MAP.get(key, key)      # известные синонимы/переводы -> канон

    from_dict = _lookup_term_index(key)
    if from_dict:
        return from_dict

    return key


def get_canonical_document_ids() -> set[str] | None:
    """
    Исключаем найденные дубликаты (is_duplicate_of IS NOT NULL в БД) —
    иначе в граф попадут лишние Document-узлы с тем же содержимым. Если БД
    ещё не собрана — просто берём все JSON без фильтрации.
    """
    if not config.DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(config.DB_PATH))
        rows = conn.execute("SELECT document_id FROM documents WHERE is_duplicate_of IS NULL").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return None


def load_documents() -> list[dict]:
    canonical_ids = get_canonical_document_ids()
    docs = []
    for p in config.JSON_OUT_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if canonical_ids is not None and data.get("document_id") not in canonical_ids:
            continue
        docs.append(data)
    return docs


def build_graph_data(docs: list[dict]):
    documents: dict[str, dict] = {}
    entities: dict[str, dict] = {}
    mentions: list[tuple[str, str]] = []
    keyword_edges: list[tuple[str, str]] = []
    relations: list[dict] = []

    for doc in docs:
        doc_id = doc["document_id"]
        props = doc.get("properties") or {}
        if props.get("_error") or props.get("_parse_error"):
            continue

        title = props.get("title") or Path(doc.get("source_path", "")).stem
        documents[doc_id] = {
            "id": doc_id,
            "title": title[:500],
            "doc_type": doc.get("doc_type", "unknown"),
            "source_path": doc.get("source_path", ""),
        }

        # индекс сущностей ЭТОГО документа по нормализованному имени — для
        # сопоставления subject/object из relations с уже описанными entities
        by_name: dict[str, str] = {}

        for ent in props.get("entities", []) or []:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            etype = ent.get("type", "other")
            label = LABEL_MAP.get(etype, "Other")
            key = f"{normalize_key(name)}|{etype}"
            if key not in entities:
                entities[key] = {"key": key, "name": name, "label": label}
            mentions.append((doc_id, key))
            by_name[normalize_key(name)] = key

        for kw in props.get("keywords", []) or []:
            kw = kw.strip()
            if kw:
                keyword_edges.append((doc_id, kw))

        for rel in props.get("relations", []) or []:
            subj = (rel.get("subject") or "").strip()
            obj = (rel.get("object") or "").strip()
            if not subj or not obj:
                continue

            subj_key = by_name.get(normalize_key(subj)) or f"{normalize_key(subj)}|term"
            obj_key = by_name.get(normalize_key(obj)) or f"{normalize_key(obj)}|term"

            if subj_key not in entities:
                entities[subj_key] = {"key": subj_key, "name": subj, "label": FALLBACK_LABEL}
            if obj_key not in entities:
                entities[obj_key] = {"key": obj_key, "name": obj, "label": FALLBACK_LABEL}

            # relation_type — контролируемый словарь (см. graph_extract.py).
            # Для JSON, извлечённых ДО введения этого поля, его нет —
            # аккуратный фолбэк на OTHER_RELATION, не падаем.
            rel_type_raw = (rel.get("relation_type") or "other").strip().lower()
            neo4j_rel_type = RELATION_TYPE_MAP.get(rel_type_raw, DEFAULT_RELATION_TYPE)

            relations.append({
                "subject": subj_key,
                "object": obj_key,
                "relation_type": neo4j_rel_type,
                "predicate": (rel.get("predicate") or "")[:200],
                "unit": rel.get("unit") or "",
                "source": rel.get("source") or "",
                "doc_id": doc_id,
            })

    return documents, entities, mentions, keyword_edges, relations


# ---------------------------------------------------------------------------
# Экспорт в .cypher (текстовый файл, без зависимостей)
# ---------------------------------------------------------------------------

def _cypher_str(value: str) -> str:
    """Экранирование строкового литерала для встраивания в текст Cypher-запроса."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def export_cypher(documents, entities, mentions, keyword_edges, relations, out_path: Path) -> None:
    lines = [
        "// Автоматически сгенерировано build_graph.py",
        "// Все операции — MERGE, безопасно запускать повторно (идемпотентно).",
        "",
        "// --- Документы ---",
    ]
    for d in documents.values():
        lines.append(
            f"MERGE (doc:Document {{id: '{_cypher_str(d['id'])}'}}) "
            f"SET doc.title = '{_cypher_str(d['title'])}', "
            f"doc.doc_type = '{_cypher_str(d['doc_type'])}', "
            f"doc.source_path = '{_cypher_str(d['source_path'])}';"
        )

    lines.append("\n// --- Сущности ---")
    for e in entities.values():
        lines.append(
            f"MERGE (e:Entity:`{e['label']}` {{key: '{_cypher_str(e['key'])}'}}) "
            f"SET e.name = '{_cypher_str(e['name'])}';"
        )

    lines.append("\n// --- Документ упоминает сущность ---")
    for doc_id, ent_key in mentions:
        lines.append(
            f"MATCH (doc:Document {{id: '{_cypher_str(doc_id)}'}}), "
            f"(e:Entity {{key: '{_cypher_str(ent_key)}'}}) "
            f"MERGE (doc)-[:MENTIONS]->(e);"
        )

    lines.append("\n// --- Ключевые слова ---")
    seen_kw = set()
    for doc_id, kw in keyword_edges:
        kw_key = normalize_key(kw)
        if kw_key not in seen_kw:
            lines.append(f"MERGE (k:Keyword {{key: '{_cypher_str(kw_key)}'}}) SET k.name = '{_cypher_str(kw)}';")
            seen_kw.add(kw_key)
        lines.append(
            f"MATCH (doc:Document {{id: '{_cypher_str(doc_id)}'}}), "
            f"(k:Keyword {{key: '{_cypher_str(kw_key)}'}}) "
            f"MERGE (doc)-[:HAS_KEYWORD]->(k);"
        )

    lines.append("\n// --- Связи между сущностями (типизированные) ---")
    for r in relations:
        lines.append(
            f"MATCH (s:Entity {{key: '{_cypher_str(r['subject'])}'}}), "
            f"(o:Entity {{key: '{_cypher_str(r['object'])}'}}) "
            f"MERGE (s)-[rel:`{r['relation_type']}` {{predicate: '{_cypher_str(r['predicate'])}', "
            f"doc_id: '{_cypher_str(r['doc_id'])}'}}]->(o) "
            f"SET rel.unit = '{_cypher_str(r['unit'])}', rel.source = '{_cypher_str(r['source'])}';"
        )

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Прямая загрузка через официальный neo4j-driver (параметризованные запросы)
# ---------------------------------------------------------------------------

def load_via_driver(documents, entities, mentions, keyword_edges, relations,
                     uri: str, user: str, password: str) -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            session.run(
                "UNWIND $rows AS row "
                "MERGE (doc:Document {id: row.id}) "
                "SET doc.title = row.title, doc.doc_type = row.doc_type, doc.source_path = row.source_path",
                rows=list(documents.values()),
            )

            by_label: dict[str, list[dict]] = defaultdict(list)
            for e in entities.values():
                by_label[e["label"]].append(e)
            for label, rows in by_label.items():
                # Label нельзя параметризовать в Cypher — используем f-string,
                # но значения label берутся только из фиксированного LABEL_MAP
                # / FALLBACK_LABEL, не из произвольного пользовательского ввода.
                session.run(
                    f"UNWIND $rows AS row MERGE (e:Entity:`{label}` {{key: row.key}}) SET e.name = row.name",
                    rows=rows,
                )

            session.run(
                "UNWIND $rows AS row "
                "MATCH (doc:Document {id: row.doc_id}), (e:Entity {key: row.ent_key}) "
                "MERGE (doc)-[:MENTIONS]->(e)",
                rows=[{"doc_id": d, "ent_key": k} for d, k in mentions],
            )

            kw_nodes = {normalize_key(kw): kw for _, kw in keyword_edges}
            session.run(
                "UNWIND $rows AS row MERGE (k:Keyword {key: row.key}) SET k.name = row.name",
                rows=[{"key": k, "name": v} for k, v in kw_nodes.items()],
            )
            session.run(
                "UNWIND $rows AS row "
                "MATCH (doc:Document {id: row.doc_id}), (k:Keyword {key: row.key}) "
                "MERGE (doc)-[:HAS_KEYWORD]->(k)",
                rows=[{"doc_id": d, "key": normalize_key(kw)} for d, kw in keyword_edges],
            )

            by_rel_type: dict[str, list[dict]] = defaultdict(list)
            for r in relations:
                by_rel_type[r["relation_type"]].append(r)
            for rel_type, rows in by_rel_type.items():
                # Тип связи, как и label выше, нельзя параметризовать в
                # Cypher — но значения приходят только из фиксированного
                # RELATION_TYPE_MAP, не из произвольного текста.
                session.run(
                    f"UNWIND $rows AS row "
                    f"MATCH (s:Entity {{key: row.subject}}), (o:Entity {{key: row.object}}) "
                    f"MERGE (s)-[r:`{rel_type}` {{predicate: row.predicate, doc_id: row.doc_id}}]->(o) "
                    f"SET r.unit = row.unit, r.source = row.source",
                    rows=rows,
                )
    finally:
        driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-cypher", action="store_true")
    parser.add_argument("--load-neo4j", action="store_true")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    docs = load_documents()
    print(f"Загружено документов из output/json: {len(docs)}")
    documents, entities, mentions, keyword_edges, relations = build_graph_data(docs)
    rel_type_counts = defaultdict(int)
    for r in relations:
        rel_type_counts[r["relation_type"]] += 1
    rel_summary = ", ".join(f"{t}={c}" for t, c in sorted(rel_type_counts.items(), key=lambda x: -x[1]))
    print(f"Узлов Document: {len(documents)}, Entity: {len(entities)}, "
          f"связей MENTIONS: {len(mentions)}, всего связей между сущностями: {len(relations)} ({rel_summary}), "
          f"ключевых слов: {len(set(normalize_key(k) for _, k in keyword_edges))}")

    if args.export_cypher or not args.load_neo4j:
        out_path = config.BASE_DIR / "output" / "graph.cypher"
        export_cypher(documents, entities, mentions, keyword_edges, relations, out_path)
        print(f"Cypher-скрипт сохранён: {out_path}")

    if args.load_neo4j:
        if not args.password:
            raise SystemExit("Укажите --password для подключения к Neo4j")
        load_via_driver(documents, entities, mentions, keyword_edges, relations,
                         args.uri, args.user, args.password)
        print("Загружено напрямую в Neo4j.")
