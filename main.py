"""
Оркестратор всего пайплайна:

  Я.Диск -> staging -> unpack (bundles) -> extract text/images/tables
  -> dedup -> classify -> LLM properties -> JSON-файл + запись в SQLite

Запуск:
    export YADISK_TOKEN=...
    export YANDEX_API_KEY=...
    export YANDEX_FOLDER_ID=...
    python3 main.py                  # полный прогон
    python3 main.py --skip-yadisk    # работать с уже скачанным staging/unpacked
    python3 main.py --skip-llm       # без шага извлечения свойств (быстрая проверка extract-слоя)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

import config
import db
import unpack
import dedup
import classify
import graph_extract
import extract_text
import extract_images
import extract_tables
import yadisk_sync
from budget import BudgetTracker, BudgetExceededError

logger = logging.getLogger("main")

SUPPORTED_TEXT_EXTS = {".pdf", ".docx", ".doc", ".docm", ".pptx"}


def stable_document_id(main_path: Path) -> str:
    """
    Детерминированный id по пути главного файла (а не uuid4) — нужен, чтобы
    между запусками можно было узнать "этот документ уже обработан" и не
    платить за него повторно после остановки по бюджету.
    """
    return hashlib.sha256(str(main_path).encode("utf-8")).hexdigest()[:32]


def load_existing_llm_result(document_id: str) -> tuple[str, dict, list[str]] | None:
    """
    Если для этого document_id уже есть JSON с успешно извлечёнными
    данными (не _error/_parse_error) с прошлого запуска — переиспользуем
    его вместо повторного вызова LLM. Возвращает (doc_type, properties,
    document_links) — doc_type теперь тоже определяется этим же единым
    вызовом, а не отдельным шагом классификации.
    """
    json_path = config.JSON_OUT_DIR / f"{document_id}.json"
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        props = data.get("properties") or {}
        if props and not props.get("_error") and not props.get("_parse_error"):
            return data.get("doc_type", classify.DOC_TYPE_UNKNOWN), props, data.get("document_links", [])
    except Exception:
        pass
    return None


def process_bundle(
    bundle: unpack.DocBundle,
    skip_llm: bool,
    budget: BudgetTracker | None,
) -> list[dict]:
    """
    Обрабатывает один bundle. Как правило, для JSON/БД создаётся один
    "логический документ" на main_file bundle'а, а приложения (доп. PDF)
    сохраняются как attachments внутри того же документа — так связка
    "основной документ + приложения" не разваливается на независимые записи.

    Поднимает BudgetExceededError, если LLM-шаг упёрся в лимит бюджета —
    это должно остановить дальнейшую обработку в run().
    """
    if not bundle.main_file:
        logger.warning("Bundle %s без определённого главного файла, пропуск", bundle.bundle_id)
        return []

    main_path = Path(bundle.main_file)
    if main_path.suffix.lower() not in SUPPORTED_TEXT_EXTS:
        logger.warning("Неподдерживаемый главный файл %s, пропуск", main_path)
        return []

    document_id = stable_document_id(main_path)

    # Для .doc/.docm (и потенциально битых .docx) резолвим ОДИН раз путь,
    # который реально открывается python-docx (см. extract_text.py) — и
    # текст, и таблицы, и картинки берём уже с этого пути, чтобы не гонять
    # LibreOffice трижды на один и тот же файл.
    suffix = main_path.suffix.lower()
    if suffix in (".docx", ".doc", ".docm"):
        readable_path = extract_text.resolve_docx_path(main_path)
    else:
        readable_path = main_path

    extracted = extract_text.extract_text(readable_path)
    page_texts = {p.page_number: p.text for p in extracted.pages}

    tables = extract_tables.extract_tables(readable_path)

    images_out_dir = config.IMAGES_OUT_DIR / document_id
    if suffix == ".pdf":
        images = extract_images.extract_from_pdf(readable_path, page_texts, images_out_dir)
    elif suffix in (".docx", ".doc", ".docm"):
        images = extract_images.extract_from_docx(readable_path, images_out_dir)
    else:
        # .pptx — извлечение картинок из презентаций пока не реализовано
        # (extract_images.py покрывает только PDF/DOCX), текст и заметки
        # докладчика уже извлекаются через extract_text.extract_from_pptx.
        images = []

    # Приложения bundle'а (кроме главного файла) — их текст/таблицы/картинки
    # тоже извлекаем и прикладываем как attachments, не как отдельные документы.
    attachments = []
    for f in bundle.files:
        if f == bundle.main_file:
            continue
        f_path = Path(f)
        if f_path.suffix.lower() not in SUPPORTED_TEXT_EXTS:
            continue
        try:
            att_readable = (
                extract_text.resolve_docx_path(f_path)
                if f_path.suffix.lower() in (".docx", ".doc", ".docm")
                else f_path
            )
            att_extracted = extract_text.extract_text(att_readable)
            att_tables = extract_tables.extract_tables(att_readable)
            attachments.append({
                "source_path": str(f_path),
                "full_text": att_extracted.full_text,
                "tables": [t.__dict__ for t in att_tables],
            })
        except Exception as e:
            logger.exception("Ошибка извлечения приложения %s: %s", f_path, e)

    properties = {}
    document_links: list[str] = []

    if skip_llm:
        # Без LLM вообще — берём быструю бесплатную regex-классификацию,
        # свойств/сущностей/связей не будет.
        doc_type = classify.classify_document(extracted.full_text)
    else:
        cached = load_existing_llm_result(document_id)
        if cached is not None:
            logger.info("Результат для %s уже есть с прошлого запуска, переиспользую", main_path)
            doc_type, properties, document_links = cached
        else:
            try:
                result = graph_extract.extract(
                    extracted.full_text, [t.__dict__ for t in tables], str(main_path), budget=budget
                )
                doc_type = result.get("doc_type", classify.DOC_TYPE_UNKNOWN)
                properties = result
                document_links = graph_extract.extract_document_links(extracted.full_text)
            except BudgetExceededError:
                # Пробрасываем наверх без изменений — run() должен остановить
                # дальнейшую обработку, а не считать это ошибкой конкретного
                # документа.
                raise
            except Exception as e:
                logger.exception("Ошибка единого извлечения для %s: %s", main_path, e)
                # Регекс — честный запасной вариант, чтобы документ не
                # остался вообще без типа из-за сбоя LLM.
                doc_type = classify.classify_document(extracted.full_text)
                properties = {"_error": str(e)}

    document = {
        "document_id": document_id,
        "bundle_id": bundle.bundle_id,
        "source_path": str(main_path),
        "source_ext": main_path.suffix.lower(),
        "source_archive": bundle.source_archive,
        "doc_type": doc_type,
        "full_text": extracted.full_text,
        "pages": [p.__dict__ for p in extracted.pages],
        "images": [i.__dict__ for i in images],
        "tables": [t.__dict__ for t in tables],
        "attachments": attachments,
        "properties": properties,
        "document_links": document_links,
    }
    return [document]


def write_json(document: dict) -> Path:
    out_path = config.JSON_OUT_DIR / f"{document['document_id']}.json"
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def rebuild_db_from_json() -> None:
    """
    Пересобирает output/catalog.sqlite3 с нуля из всех *.json в output/json/,
    заново пересчитывая дедупликацию по ПОЛНОМУ объединённому набору.

    Нужно после распределённой обработки на нескольких машинах (см.
    --shard-index/--shard-count): каждая машина пишет свои JSON независимо
    (коллизий по document_id не будет — они строго разделены между
    машинами), но у каждой своя локальная БД, которая не видит документы
    другой машины. Скопируйте output/json с обеих машин в одну папку
    (простое копирование файлов, дубликатов имён не будет) и запустите:
        python3 main.py --rebuild-db-only
    """
    logger.info("Пересборка БД из %s ...", config.JSON_OUT_DIR)
    all_documents = []
    for json_path in config.JSON_OUT_DIR.glob("*.json"):
        try:
            all_documents.append(json.loads(json_path.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("Не удалось прочитать %s: %s", json_path, e)

    dup_groups = dedup.find_duplicates(all_documents)
    docs_by_id = {d["document_id"]: d for d in all_documents}
    duplicate_map: dict[str, str] = {}
    for _hash, ids in dup_groups.items():
        canonical = dedup.pick_canonical(ids, docs_by_id)
        for doc_id in ids:
            if doc_id != canonical:
                duplicate_map[doc_id] = canonical

    db.init_db()
    with db.get_connection() as conn:
        # Сначала дочерние таблицы (у них внешний ключ на documents),
        # documents — последней, иначе SQLite ругается на FOREIGN KEY.
        conn.execute("DELETE FROM document_properties")
        conn.execute("DELETE FROM authors")
        conn.execute("DELETE FROM images")
        conn.execute("DELETE FROM tables_extracted")
        conn.execute("DELETE FROM document_links")
        conn.execute("DELETE FROM documents")
        for doc in all_documents:
            db.insert_document(
                conn,
                document_id=doc["document_id"],
                bundle_id=doc["bundle_id"],
                source_path=doc["source_path"],
                source_ext=doc["source_ext"],
                doc_type=doc["doc_type"],
                is_duplicate_of=duplicate_map.get(doc["document_id"]),
                json_path=str(config.JSON_OUT_DIR / f"{doc['document_id']}.json"),
                properties=doc.get("properties", {}),
                images=doc.get("images", []),
                tables=doc.get("tables", []),
                document_links=doc.get("document_links", []),
            )
        conn.commit()

    logger.info(
        "Готово. Документов в объединённой БД: %d, найдено дублей: %d",
        len(all_documents), len(duplicate_map),
    )


def run(skip_yadisk: bool, skip_llm: bool, budget_rub: float, limit: int | None,
        yadisk_limit: int | None, shard_index: int | None, shard_count: int | None) -> None:
    db.init_db()

    if skip_yadisk:
        staged_files = [
            str(p) for p in config.STAGING_DIR.rglob("*")
            if p.is_file() and p.name != ".DS_Store" and p.name != "_manifest.json"
        ]
    else:
        remote_files = yadisk_sync.sync_all()
        staged_files = [rf.local_path for rf in remote_files]

    bundles = unpack.build_bundles(staged_files)

    if shard_count:
        if shard_index is None or not (0 <= shard_index < shard_count):
            raise ValueError("--shard-index должен быть в диапазоне [0, --shard-count)")
        # bundle_id — это уже hex-хеш (см. unpack._stable_id), поэтому просто
        # берём его как число и делим по модулю. Детерминированно и не
        # требует координации между машинами — каждая независимо посчитает
        # тот же самый разбитый на части список.
        bundles = [b for b in bundles if int(b.bundle_id, 16) % shard_count == shard_index]
        logger.info(
            "Шардинг: обрабатываю часть %d из %d (%d bundles из общего числа)",
            shard_index, shard_count, len(bundles),
        )

    if limit:
        bundles = bundles[:limit]
        logger.info("Ограничение --limit: обрабатываю только первые %d bundles", limit)

    if skip_llm:
        budget = None
    elif config.LLM_BACKEND == "local":
        budget = None  # локальная модель бесплатна, бюджет не нужен
        logger.info("LLM-извлечение: локальная модель %s (Ollama, бесплатно)", config.LOCAL_LLM_MODEL)
    else:
        budget = BudgetTracker(budget_rub)
        logger.info("Бюджет на LLM-извлечение: %.2f₽", budget.max_rub)

    processed_count = 0
    stopped_by_budget = False
    total = len(bundles)

    with ThreadPoolExecutor(max_workers=config.NUM_WORKERS) as executor:
        # Список (не dict + as_completed!): as_completed отдаёt future только
        # когда он УЖЕ завершён, поэтому timeout= в result() на нём ничего
        # не проверяет (тест на синтетике подтвердил — таймаут просто не
        # сработал бы никогда). Идём по futures в порядке подачи — тогда
        # result(timeout=...) действительно ждёт не дольше лимита, а другие
        # воркеры пула продолжают работать над остальными bundles параллельно.
        futures = [(bundle, executor.submit(process_bundle, bundle, skip_llm, budget)) for bundle in bundles]
        try:
            for completed, (bundle, future) in enumerate(futures, start=1):
                try:
                    # future.result(timeout=...) НЕ прерывает выполнение
                    # потока, если тот завис (Python не умеет принудительно
                    # убивать потоки) — просто перестаёт его ждать и идёт
                    # дальше. В отличие от старого signal.alarm это не
                    # освобождает ресурсы зависшего потока немедленно, но
                    # это неизбежный компромисс при NUM_WORKERS > 1 (сигналы
                    # работают только в главном потоке процесса).
                    docs = future.result(timeout=config.MAX_SECONDS_PER_DOCUMENT)
                    for doc in docs:
                        write_json(doc)
                        processed_count += 1
                    logger.info(
                        "[%d/%d] Готово: %s",
                        completed, total, bundle.main_file or bundle.bundle_id,
                    )
                except FutureTimeoutError:
                    logger.error(
                        "[%d/%d] Таймаут (>%ds) на документе %s. Идём дальше "
                        "(поток мог остаться работать в фоне до естественного завершения).",
                        completed, total, config.MAX_SECONDS_PER_DOCUMENT, bundle.main_file,
                    )
                except BudgetExceededError as e:
                    logger.warning("%s Останавливаю приём новых документов.", e)
                    stopped_by_budget = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as e:
                    logger.exception(
                        "[%d/%d] Ошибка обработки bundle %s: %s",
                        completed, total, bundle.bundle_id, e,
                    )
        except KeyboardInterrupt:
            logger.warning("Прервано пользователем — останавливаю пул потоков.")
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    # Дедупликация и запись в БД — из уже записанных на диск JSON (та же
    # функция, что используется для объединения шардов, см. --rebuild-db-only).
    # Работает даже если прогон был прерван на середине: подхватит всё, что
    # успело записаться в output/json на этот момент.
    rebuild_db_from_json()

    logger.info(
        "Готово. Обработано документов за этот запуск: %d%s",
        processed_count,
        f" | ОСТАНОВЛЕНО ПО БЮДЖЕТУ (потрачено {budget.spent_rub:.2f}₽)" if stopped_by_budget else "",
    )
    if stopped_by_budget:
        logger.info(
            "Необработанные bundles остались нетронуты — запустите main.py ещё раз "
            "(с тем же или увеличенным --budget), уже готовые документы будут "
            "переиспользованы бесплатно, LLM позовётся только для новых."
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-yadisk", action="store_true",
                         help="Не скачивать с Я.Диска, работать с уже скачанным staging/")
    parser.add_argument("--skip-llm", action="store_true",
                         help="Не вызывать Anthropic API для извлечения свойств")
    parser.add_argument("--budget", type=float, default=config.MAX_LLM_BUDGET_RUB,
                         help="Максимальный бюджет на LLM-извлечение за этот запуск, ₽ "
                              "(по умолчанию из MAX_LLM_BUDGET_RUB / config.py)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Обработать только первые N bundles (для теста на малой выборке)")
    parser.add_argument("--yadisk-limit", type=int, default=None,
                         help="Скачать с Я.Диска не более N новых файлов (для теста без "
                              "скачивания всего корпуса). Уже скачанные ранее файлы в "
                              "лимит не считаются.")
    parser.add_argument("--shard-index", type=int, default=None,
                         help="Номер части корпуса для этой машины (0-based), см. --shard-count")
    parser.add_argument("--shard-count", type=int, default=None,
                         help="На сколько машин делим корпус (напр. 2 — запустить с "
                              "--shard-index 0 на одной и --shard-index 1 на другой)")
    parser.add_argument("--rebuild-db-only", action="store_true",
                         help="Не обрабатывать ничего — пересобрать catalog.sqlite3 из уже "
                              "готовых output/json/*.json (для объединения после шардинга)")
    parser.add_argument("--workers", type=int, default=None,
                         help="Число параллельных потоков обработки документов "
                              "(по умолчанию из NUM_WORKERS / config.py, обычно 1). "
                              "При >1 обязательно поднимите OLLAMA_NUM_PARALLEL для ollama serve.")
    args = parser.parse_args()

    if args.workers is not None:
        config.NUM_WORKERS = args.workers

    if args.rebuild_db_only:
        rebuild_db_from_json()
    else:
        run(skip_yadisk=args.skip_yadisk, skip_llm=args.skip_llm,
            budget_rub=args.budget, limit=args.limit, yadisk_limit=args.yadisk_limit,
            shard_index=args.shard_index, shard_count=args.shard_count)
