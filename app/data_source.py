import random
import time


def get_answer(query: str) -> dict:
    """
    Обработка запроса на естественном языке.

    Ожидаемый реальный вызов (потом): text-to-Cypher / поиск по графу знаний
    + синтез ответа моделью с указанием источников.

    Возвращает:
    {
        "answer": str,                 # синтезированный текстовый ответ
        "sources": [                   # список источников, на которые опирается ответ
            {"title": str, "authors": str, "year": int, "doi": str, "q_rating": str}
        ],
        "entities": [                  # сущности, задействованные в ответе (для графа)
            {"id": str, "label": str, "type": str}   # type: material/process/equipment/team/article/property
        ],
        "edges": [                     # связи между сущностями
            {"from": str, "to": str, "relation": str}
        ]
    }
    """
    time.sleep(0.4)  # имитация задержки сети/модели

    return {
        "answer": (
            "По найденным данным, повышение температуры флотации в диапазоне "
            "180–220°C связано с ростом выхода целевого компонента при использовании "
            "реагента X. Эффект зафиксирован в трёх независимых экспериментах."
        ),
        "sources": [
            {"title": "Влияние температурного режима на флотацию сульфидных руд",
             "authors": "Иванов А.В., Петрова С.М.", "year": 2022,
             "doi": "10.1234/example.2022.001", "q_rating": "Q1"},
            {"title": "Оптимизация реагентного режима при обогащении медно-никелевых руд",
             "authors": "Сидоров К.Н.", "year": 2021,
             "doi": "10.1234/example.2021.045", "q_rating": "Q2"},
        ],
        "entities": [
            {"id": "1", "label": "Реагент X", "type": "material"},
            {"id": "2", "label": "Флотация", "type": "process"},
            {"id": "3", "label": "Температурный режим 180-220°C", "type": "property"},
            {"id": "4", "label": "Выход компонента", "type": "property"},
            {"id": "5", "label": "Иванов А.В.", "type": "team"},
        ],
        "edges": [
            {"from": "1", "to": "2", "relation": "используется в"},
            {"from": "3", "to": "2", "relation": "параметр процесса"},
            {"from": "2", "to": "4", "relation": "влияет на"},
            {"from": "5", "to": "1", "relation": "исследовал"},
        ],
    }


def get_documents(filters: dict | None = None) -> list[dict]:
    """
    Список документов с метаданными для таблицы/карточек.

    filters (потом): {"year_from": int, "year_to": int, "doc_type": str, "q_rating": str}

    Возвращает список словарей:
    {
        "title": str, "authors": str, "year": int, "journal": str,
        "q_rating": str,           # Q1-Q4
        "citations": int,
        "citations_per_year": float,
        "doc_type": str,           # experimental / review / meta-analysis
        "is_foreign": bool,
        "open_access": bool,
        "relevance": float         # 0..1
    }
    """
    mock_docs = [
        {"title": "Влияние температурного режима на флотацию сульфидных руд",
         "authors": "Иванов А.В., Петрова С.М.", "year": 2022, "journal": "Минеральное сырьё",
         "q_rating": "Q1", "citations": 34, "citations_per_year": 8.5,
         "doc_type": "experimental", "is_foreign": False, "open_access": True, "relevance": 0.94},
        {"title": "Оптимизация реагентного режима при обогащении медно-никелевых руд",
         "authors": "Сидоров К.Н.", "year": 2021, "journal": "Minerals Engineering",
         "q_rating": "Q2", "citations": 12, "citations_per_year": 2.4,
         "doc_type": "experimental", "is_foreign": True, "open_access": False, "relevance": 0.81},
        {"title": "Обзор методов интенсификации флотационных процессов",
         "authors": "Кузнецова О.П. и др.", "year": 2020, "journal": "Обогащение руд",
         "q_rating": "Q3", "citations": 51, "citations_per_year": 8.5,
         "doc_type": "review", "is_foreign": False, "open_access": True, "relevance": 0.76},
    ]
    return mock_docs


def get_stats() -> dict:
    """
    Сводные цифры для верхней панели.
    """
    return {
        "documents": 128,
        "entities": 947,
        "relations": 2310,
        "queries_processed": 56,
    }
