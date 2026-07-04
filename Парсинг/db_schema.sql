-- Реляционная БД под фильтрацию/поиск по структурным признакам.
-- Полные тексты живут в JSON-файлах (output/json/*.json), сюда идёт
-- только то, что нужно для фильтрации, джойнов и графа связей.

CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    bundle_id       TEXT,
    source_path     TEXT,
    source_ext      TEXT,
    doc_type        TEXT,
    is_duplicate_of TEXT,           -- document_id канонического экземпляра, если это дубль
    json_path       TEXT,           -- путь к полному JSON-документу
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document_properties (
    document_id TEXT,
    prop_key    TEXT,               -- напр. "asset_name", "country", "commissioned_year"
    prop_value  TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS authors (
    document_id TEXT,
    name        TEXT,
    org         TEXT,
    orcid       TEXT,
    email       TEXT,
    degree      TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS images (
    image_id      TEXT PRIMARY KEY,
    document_id   TEXT,
    page_number   INTEGER,
    local_path    TEXT,
    caption       TEXT,
    is_decorative INTEGER,          -- 0/1, консервативная эвристика — не автоудаление
    width         INTEGER,
    height        INTEGER,
    linked_entity TEXT,             -- напр. "research_team", "equipment", "conclusion" — куда привязано
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS tables_extracted (
    table_id     TEXT PRIMARY KEY,
    document_id  TEXT,
    page_number  INTEGER,
    columns_json TEXT,
    rows_json    TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS document_links (
    from_document_id TEXT,
    ref_raw          TEXT,          -- сырая ссылка/номер источника, как в тексте
    to_document_id   TEXT,          -- заполняется, если удалось сопоставить с другим документом корпуса
    FOREIGN KEY (from_document_id) REFERENCES documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_props_key_value ON document_properties(prop_key, prop_value);
CREATE INDEX IF NOT EXISTS idx_images_document ON images(document_id);
CREATE INDEX IF NOT EXISTS idx_tables_document ON tables_extracted(document_id);
