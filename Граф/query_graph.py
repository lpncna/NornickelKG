"""
Семантический поиск по графу знаний на естественном языке (GraphRAG-слой
поверх того, что строит build_graph.py):

    вопрос на русском/английском -> LLM переводит в Cypher -> проверка на
    безопасность (только чтение) -> выполнение в Neo4j -> LLM формулирует
    понятный ответ на основе результата.

Работает через тот же LLM_BACKEND, что и graph_extract.py ("local" — Ollama,
бесплатно; "yandex" — облако, платно).

Запуск:
    python3 query_graph.py "Какие организации связаны с флотацией?" \
        --uri bolt://localhost:7687 --user neo4j --password ВАШ_ПАРОЛЬ

    python3 query_graph.py "Кто аффилирован с Институтом Гипроникель?" \
        --password ВАШ_ПАРОЛЬ --show-cypher
"""
from __future__ import annotations

import argparse
import json
import re

import requests

import config
import local_llm

GRAPH_SCHEMA_DESCRIPTION = """
Узлы:
  (:Document {id, title, doc_type, source_path})
  (:Entity:<Metal|Material|Organization|Asset|Country|Person|Method|Equipment|
            Experiment|Property|Publication|Other|Term> {key, name})
  (:Keyword {key, name})

Связи:
  (:Document)-[:MENTIONS]->(:Entity)
  (:Document)-[:HAS_KEYWORD]->(:Keyword)
  (:Entity)-[:USES_MATERIAL|OPERATES_AT_CONDITION|PRODUCES_OUTPUT|DESCRIBED_IN|
             VALIDATED_BY|CONTRADICTS|LOCATED_IN|AFFILIATED_WITH|HAS_PROPERTY|
             PART_OF|OTHER_RELATION
             {predicate, unit, source, doc_id}]->(:Entity)

Метка узла = тип сущности (получить через labels(e)). Свойства узла Entity:
key (нормализованный ключ), name (отображаемое имя).
""".strip()

# Запрещаем любые операции записи — модель иногда "хочет помочь" и
# генерирует CREATE/MERGE вместо чтения; выполняться должны только запросы
# на чтение графа.
_FORBIDDEN_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|LOAD\s+CSV|CALL|FOREACH)\b",
    re.IGNORECASE,
)

CYPHER_SCHEMA = {
    "type": "object",
    "properties": {"cypher": {"type": "string"}},
    "required": ["cypher"],
}

SYSTEM_PROMPT_CYPHER = (
    "Ты переводишь вопрос пользователя на естественном языке в Cypher-запрос "
    "для Neo4j. Используй ТОЛЬКО операции чтения: MATCH, OPTIONAL MATCH, "
    "WHERE, RETURN, ORDER BY, LIMIT, WITH, UNWIND. НИКОГДА не используй "
    "CREATE, MERGE, DELETE, SET, CALL, DROP — только чтение. Всегда "
    "добавляй LIMIT (не больше 50), если пользователь явно не просил иное. "
    "Верни ТОЛЬКО сам запрос в поле cypher, без пояснений.\n\n"
    f"Схема графа:\n{GRAPH_SCHEMA_DESCRIPTION}"
)

SYSTEM_PROMPT_ANSWER = (
    "Ты помогаешь интерпретировать результат запроса к графу знаний и "
    "отвечать на вопрос пользователя понятным языком. Опирайся ТОЛЬКО на "
    "переданные данные, не выдумывай факты сверх них. Если данных нет или "
    "результат пустой — так и скажи."
)


def is_safe_cypher(query: str) -> bool:
    return bool(query.strip()) and not _FORBIDDEN_RE.search(query)


def _call_llm(system_prompt: str, user_prompt: str, schema: dict | None) -> str:
    """
    Общий вызов LLM через тот же бэкенд, что и graph_extract.py. Если
    schema передана — просит structured output и возвращает содержимое
    JSON-поля как есть (raw JSON-строку); если schema=None — просит
    обычный текстовый ответ.
    """
    if config.LLM_BACKEND == "local":
        if schema is not None:
            result = local_llm.extract_properties_local(
                doc_type="query", prompt=user_prompt, system_prompt=system_prompt, schema=schema
            )
            return json.dumps(result, ensure_ascii=False)
        payload = {
            "model": config.LOCAL_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }
        resp = requests.post(local_llm.OLLAMA_API_URL, json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    if not config.YANDEX_API_KEY or not config.YANDEX_FOLDER_ID:
        raise RuntimeError("Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID в переменных окружения.")

    model_uri = f"gpt://{config.YANDEX_FOLDER_ID}/{config.YANDEX_MODEL}"
    payload = {
        "model": model_uri,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 800,
        "temperature": 0.3,
        "stream": False,
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "query", "schema": schema},
        }
    headers = {
        "Authorization": f"Api-Key {config.YANDEX_API_KEY}",
        "OpenAI-Project": config.YANDEX_FOLDER_ID,
        "Content-Type": "application/json",
    }
    resp = requests.post(config.YANDEX_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def generate_cypher(question: str) -> str:
    raw = _call_llm(SYSTEM_PROMPT_CYPHER, question, CYPHER_SCHEMA)
    try:
        parsed = json.loads(raw)
        return parsed.get("cypher", "")
    except json.JSONDecodeError:
        return ""


def run_cypher(cypher: str, uri: str, user: str, password: str) -> list[dict]:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(default_access_mode="READ") as session:
            result = session.run(cypher)
            return [dict(record) for record in result]
    finally:
        driver.close()


def answer_in_natural_language(question: str, cypher: str, rows: list[dict]) -> str:
    rows_json = json.dumps(rows[:30], ensure_ascii=False, default=str)
    user_prompt = (
        f"Вопрос пользователя: {question}\n\n"
        f"Выполненный Cypher-запрос: {cypher}\n\n"
        f"Результат из графа (может быть обрезан до 30 строк):\n{rows_json}\n\n"
        f"Сформулируй краткий, понятный ответ на русском на основе этих данных."
    )
    return _call_llm(SYSTEM_PROMPT_ANSWER, user_prompt, schema=None)


def ask(question: str, uri: str, user: str, password: str, show_cypher: bool = False) -> str:
    cypher = generate_cypher(question)
    if show_cypher:
        print(f"[Cypher]\n{cypher}\n")

    if not is_safe_cypher(cypher):
        return (
            "Не удалось безопасно выполнить запрос: сгенерированный Cypher "
            "содержит операции записи или пуст. Попробуйте переформулировать "
            "вопрос."
        )

    try:
        rows = run_cypher(cypher, uri, user, password)
    except Exception as e:
        return f"Ошибка выполнения запроса к Neo4j: {e}"

    if show_cypher:
        print(f"[Найдено строк: {len(rows)}]\n")

    return answer_in_natural_language(question, cypher, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--show-cypher", action="store_true",
                         help="Показать сгенерированный Cypher-запрос и число найденных строк")
    args = parser.parse_args()

    answer = ask(args.question, args.uri, args.user, args.password, show_cypher=args.show_cypher)
    print(answer)
