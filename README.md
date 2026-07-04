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

Если Neo4j недоступна (не поднята/сбой у облачного провайдера) — веб-интерфейс
(`app.py`) всё равно работает: `data_source.py` считает граф **прямо в
Python-памяти** через `build_graph.load_documents()` + `build_graph_data()`,
без Neo4j вообще. См. раздел "Веб-интерфейс (Streamlit)" ниже.

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
export YADISK_TOKEN=TOKEN           # OAuth-токен приложения с доступом к чтению Диска

export LLM_BACKEND=local            # у нас нет доступа к Yandex API — обязательно local
export LOCAL_LLM_MODEL=qwen2.5:7b   # подбирайте под доступную RAM (см. ниже)

export MAX_SECONDS_PER_DOCUMENT=900 # таймаут на документ, опционально
```

Бэкенд `LLM_BACKEND=yandex` (платный YandexGPT) существует как альтернатива, но временно недоступна.

### Подбор модели под доступную память

`qwen2.5:14b-instruct` (~9 ГБ весов) требует заметно больше свободной RAM,
чем часто есть на CPU-инференсе — при нехватке Ollama падает с ошибкой
вида `failed to allocate CPU_REPACK buffer`, а не просто медленно работает.

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

**Пример запуска** (ограниченная память, загрузка файлов не нужна,
2 параллельных процесса):
```bash
export LLM_BACKEND=local
export MAX_SECONDS_PER_DOCUMENT=900
export LOCAL_LLM_MODEL=qwen2.5:7b
export YADISK_TOKEN=TOKEN

python3 main.py --skip-yadisk --yadisk-limit 10 --workers 2
```

Обработка идемпотентна и возобновляема: `document_id`/`bundle_id` — это
детерминированные хеши путей (не `uuid4`), а перед LLM-вызовом main.py
проверяет, нет ли уже готового JSON с непустыми `properties` с прошлого
прогона — повторный запуск после сбоя/Ctrl+C не платит за уже успешно
обработанные документы заново (документы с `_error`/`_parse_error` в
`properties` пересчитываются автоматически).

### Многопроцессность, Ollama и память

При `--workers > 1` каждый bundle обрабатывается в своём процессе, но все
процессы стучатся в один и тот же локальный инстанс Ollama — число
одновременных LLM-запросов ограничено отдельно через
`config.OLLAMA_MAX_CONCURRENT_REQUESTS` (если задано), независимо от
`--workers`. Начинайте с малого числа воркеров (2-4), если свободной RAM
немного — извлечение текста/таблиц/картинок тоже требует памяти помимо
самой модели.

## Веб-интерфейс (Streamlit)

`app.py` + `data_source.py` — чат с ответами по графу знаний, вкладка
"Граф знаний" (визуализация) и "Документы" (фильтруемый список).
`app.py` не содержит логики работы с данными — вся она в `data_source.py`.

### Установка

```bash
pip install streamlit plotly networkx
```

`app.py` и `data_source.py` должны лежать **в той же папке**, что и
`config.py`, `build_graph.py`, `local_llm.py` (импортируются как модули
пайплайна, без них `data_source.py` откатится на демонстрационные
mock-данные с пометкой в ответе).

### Запуск

```bash
export LLM_BACKEND=local
export LOCAL_LLM_MODEL=qwen2.5:7b

streamlit run app.py
```

Откроется `http://localhost:8501` в браузере.

### Как считается граф для сайта

`data_source.py` строит граф **в памяти** при первом запросе через
`build_graph.load_documents()` + `build_graph_data()` — те же функции, что
использует `build_graph.py --export-cypher`, просто без выгрузки в Neo4j.
Результат кэшируется на время жизни процесса Streamlit — если `main.py` в
фоне дообработал новые документы, **граф на сайте сам не обновится**:
перезапустите Streamlit (`Ctrl+C`, снова `streamlit run app.py`), чтобы
кэш пересчитался с учётом новых `output/json/*.json`.

Если Neo4j поднята и заполнена (`build_graph.py --load-neo4j`) —
`data_source.py` можно переключить на реальные Cypher-запросы через
`query_graph.py` (text-to-Cypher) вместо in-memory поиска — это отдельная
версия файла, дающая более точный семантический поиск за счёт LLM-
генерации запросов, ценой зависимости от живой Neo4j.

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
| `build_graph.py` | JSON-корпус -> граф (Cypher-файл, прямая загрузка в Neo4j, или in-memory для сайта) |
| `query_graph.py` | text-to-Cypher поиск по живой Neo4j (опционально, требует запущенную Neo4j) |
| `main.py` | оркестратор пайплайна (`--workers`, `--shard-*`, `--rebuild-db-only`, `--limit`) |
| `app.py` | Streamlit UI: чат, граф знаний, документы |
| `data_source.py` | вся логика данных для `app.py` — единственный файл, который меняется при смене бэкенда |

## Известные точки расширения

1. **Картинки из `.pptx`** — пока не извлекаются, только текст слайдов + заметки.
2. **Сопоставление `document_links` с реальными `document_id`** — сейчас
   это сырые номера ссылок `[1]`, `[2,5]`, резолвинг не реализован.
3. **camelot** как второй проход для PDF-таблиц со сложной сеткой линий.
4. **Neo4j** — опциональна для веб-интерфейса; при наличии рабочего
   инстанса даёт более точный семантический поиск через `query_graph.py`.
