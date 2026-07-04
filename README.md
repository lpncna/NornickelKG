# Пайплайн (Задача 2 «Фабрика гипотез»)

Скачивает документы с Яндекс.Диска, распаковывает архивы (сохраняя связку
«основной документ + приложения»), извлекает полный текст (PDF/DOCX/DOCM/PPTX,
с OCR-фолбэком для отсканированных страниц), изображения и таблицы, и за
**один вызов локальной LLM (Ollama)** одновременно определяет тип документа
и извлекает сущности/связи для графа знаний (`graph_extract.py`). Результат:

- `output/json/<document_id>.json` — по одному файлу на документ: полный
  текст, постраничная разбивка, изображения, таблицы, `properties`
  (`doc_type`, `title`, `entities`, `relations`, `keywords`), ссылки на
  другие документы;
- `output/catalog.sqlite3` — реляционная БД для фильтрации/джойнов;
- `output/images/<document_id>/` — извлечённые изображения (для `.pptx`
  пока не реализовано, только текст слайдов + заметки докладчика).

Граф в Neo4j строится **отдельным шагом**, не во время `main.py`:
```bash
python3 build_graph.py --export-cypher                 # -> output/graph.cypher
python3 build_graph.py --load-neo4j --uri bolt://localhost:7687 --user neo4j --password ПАРОЛЬ
```

## Установка

```bash
pip install -r requirements.txt --break-system-packages

# для .rar нужен системный распаковщик:
sudo apt-get install -y unrar   # или unar (conda: conda install -c conda-forge unrar)

# Tesseract — бесплатный OCR-фолбэк для страниц с битым текстовым слоем:
sudo apt-get install -y tesseract-ocr tesseract-ocr-rus
# (conda-альтернатива без sudo: conda install -c conda-forge tesseract,
#  плюс отдельно скачать rus.traineddata, см. раздел ниже)

# LibreOffice — фолбэк-конвертер для старых .doc/битых .docx (опционально):
sudo apt-get install -y libreoffice

# локальная LLM:
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
```

### Tesseract без прав администратора (conda-only окружение)

```bash
conda install -c conda-forge tesseract -y
mkdir -p $CONDA_PREFIX/share/tessdata
curl -L -o $CONDA_PREFIX/share/tessdata/rus.traineddata \
  https://github.com/tesseract-ocr/tessdata/raw/main/rus.traineddata
export TESSDATA_PREFIX=$CONDA_PREFIX/share/tessdata
```

## Переменные окружения

```bash
export YADISK_TOKEN=...             # OAuth-токен приложения с доступом к чтению Диска

export LLM_BACKEND=local            # у нас нет доступа к Yandex API — обязательно local
export LOCAL_LLM_MODEL=qwen2.5:7b   # подбирайте под доступную RAM (см. ниже)

export MAX_SECONDS_PER_DOCUMENT=900 # таймаут на документ, опционально
```

Бэкенд `LLM_BACKEND=yandex` (платный YandexGPT) существует как альтернатива, но временно недоступна.

### Подбор модели под доступную память

`qwen2.5:14b-instruct` (~9 ГБ весов) требует заметно больше свободной RAM,
чем часто есть на CPU-инференсе — при нехватке Ollama падает с ошибкой

```bash
export LOCAL_LLM_MODEL=qwen2.5:7b   # ~4.7 ГБ, безопаснее при <8 ГБ свободной RAM
```
## Запуск

```bash
python3 main.py                          # полный прогон, 1 процесс
python3 main.py --workers 4              # то же самое, 4 параллельных процесса
python3 main.py --skip-yadisk            # переиспользовать уже скачанный output/staging
python3 main.py --skip-llm                # проверить extract-слой без вызовов LLM
python3 main.py --limit 20               # только первые 20 bundles (тест)
python3 main.py --yadisk-limit 50        # скачать не более 50 новых файлов
python3 main.py --rebuild-db-only        # пересобрать catalog.sqlite3 из output/json
```

Обработка идемпотентна и возобновляема: `document_id`/`bundle_id` — это
детерминированные хеши путей (не `uuid4`), а перед LLM-вызовом main.py
проверяет, нет ли уже готового JSON с непустыми `properties` с прошлого
прогона — повторный запуск после сбоя/Ctrl+C не платит за уже успешно
обработанные документы заново (документы с `_error`/`_parse_error` в
`properties` пересчитываются автоматически).

### Многопроцессность и Ollama

При `--workers > 1` каждый bundle обрабатывается в своём процессе, но все
процессы стучатся в один и тот же локальный инстанс Ollama — число
одновременных LLM-запросов ограничено отдельно через
`config.OLLAMA_MAX_CONCURRENT_REQUESTS` (если задано), независимо от
`--workers`. Начинайте с малого числа воркеров (2-4), если свободной RAM
немного — извлечение текста/таблиц/картинок тоже требует памяти помимо
самой модели.

### Шардинг между несколькими машинами

```bash
python3 main.py --shard-index 0 --shard-count 2   # машина A
python3 main.py --shard-index 1 --shard-count 2   # машина B
# затем, собрав output/json с обеих машин в одно место:
python3 main.py --rebuild-db-only
```

## Структура модулей

| Файл | Роль |
|---|---|
| `config.py` | пути, бэкенды LLM/OCR, пороги эвристик, бюджеты |
| `yadisk_sync.py` | рекурсивное скачивание папок с Я.Диска, идемпотентно (манифест) |
| `unpack.py` | распаковка .rar/.zip/.7z (в т.ч. многотомных), идемпотентно (кэш) |
| `extract_text.py` | текст по страницам/слайдам (PDF/DOCX/DOCM/PPTX) + OCR-фолбэк |
| `extract_images.py` | изображения из PDF/DOCX + is_decorative |
| `extract_tables.py` | таблицы отдельно от параграфов + фильтр ложных срабатываний |
| `classify.py` | regex-классификатор жанров документа (эвристика, без LLM) |
| `graph_extract.py` | единый LLM-вызов: doc_type + entities + relations + keywords |
| `local_llm.py` | бэкенд Ollama (бесплатно, локально) для graph_extract.py |
| `properties_llm.py` | legacy: старая пер-типовая JSON-схема через Yandex (не используется main.py) |
| `classify_llm.py` | legacy: LLM-классификация вторым проходом (не используется main.py) |
| `budget.py` | трекер бюджета в ₽ для платного Yandex-бэкенда |
| `yandex_ocr.py` | клиент Yandex Vision OCR (платный, основной OCR-движок при наличии ключа) |
| `dedup.py` | дедупликация по хешу нормализованного текста |
| `db.py` / `db_schema.sql` | запись в SQLite |
| `build_graph.py` | отдельный шаг: JSON-корпус -> граф в Neo4j (Cypher-файл или прямая загрузка) |
| `main.py` | оркестратор пайплайна (`--workers`, `--shard-*`, `--rebuild-db-only`, `--limit`) |

## Известные точки расширения

1. **Картинки из `.pptx`** — пока не извлекаются, только текст слайдов + заметки.
2. **Сопоставление `document_links` с реальными `document_id`** — сейчас
   это сырые номера ссылок `[1]`, `[2,5]`, резолвинг не реализован.
3. **camelot** как второй проход для PDF-таблиц со сложной сеткой линий.
