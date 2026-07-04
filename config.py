"""
Конфигурация пайплайна.

Токен Яндекс.Диска НЕ хардкодить в файл — брать из переменной окружения.
Получить токен: https://yandex.ru/dev/disk/poligon/ (OAuth-токен приложения
с правами на чтение диска).
"""
import os
from pathlib import Path

# ---- Яндекс.Диск ----
YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")

# Папки-источники на Я.Диске, которые нужно обойти рекурсивно.
# Пути указываются как они видны в корне диска — сверяйте точное написание
# командой: yadisk.YaDisk(token=...).listdir('/'), см. README.
SOURCE_FOLDERS = [
    "/Задача 2. Научный клубок",
]

# ---- Локальные пути ----
BASE_DIR = Path(__file__).parent
STAGING_DIR = BASE_DIR / "output" / "staging"      # сюда скачиваются сырые файлы
UNPACKED_DIR = BASE_DIR / "output" / "unpacked"     # сюда распаковываются архивы
JSON_OUT_DIR = BASE_DIR / "output" / "json"         # итоговые JSON-документы
IMAGES_OUT_DIR = BASE_DIR / "output" / "images"     # извлечённые изображения
DB_PATH = BASE_DIR / "output" / "catalog.sqlite3"   # реляционная БД
LOG_DIR = BASE_DIR / "logs"

for d in (STAGING_DIR, UNPACKED_DIR, JSON_OUT_DIR, IMAGES_OUT_DIR, LOG_DIR, DB_PATH.parent):
    d.mkdir(parents=True, exist_ok=True)

# ---- Пороговые значения ----
# Доля "битых" (cid-код / не-ASCII мусор) символов на странице, выше которой
# страница считается требующей OCR/vision-fallback вместо обычного pdftotext.
BROKEN_TEXT_LAYER_THRESHOLD = 0.25

# Изображения меньше этого размера (в пикселях по большей стороне) или с
# экстремальным соотношением сторон считаются потенциально декоративными
# (иконки, разделители) и уходят в отдельную очередь на ручную проверку,
# а не сразу выбрасываются.
MIN_MEANINGFUL_IMAGE_SIDE = 120
MAX_ASPECT_RATIO_DECORATIVE = 8.0  # ширина/высота или наоборот

# Расширения архивов, которые нужно разворачивать рекурсивно
ARCHIVE_EXTENSIONS = {".rar", ".zip", ".7z"}

# Максимальное время на обработку ОДНОГО документа (текст+таблицы+картинки),
# секунд. pdfplumber иногда очень надолго зависает на поиске таблиц в
# больших/сложных PDF — без этого лимита один такой файл может застопорить
# весь прогон. При превышении документ пропускается, обработка идёт дальше.
MAX_SECONDS_PER_DOCUMENT = int(os.environ.get("MAX_SECONDS_PER_DOCUMENT", "180"))

# Число параллельных потоков обработки документов (текст/таблицы/картинки +
# LLM-вызов). 1 = последовательно, как раньше. На CPU-сервере без GPU имеет
# смысл ставить в районе числа ядер / 4-8 (Ollama сама распределяет потоки
# на одно ядро на инференс) — экспериментируйте под конкретное железо.
# ВАЖНО: при NUM_WORKERS > 1 обязательно поднимите OLLAMA_NUM_PARALLEL
# (переменная окружения ДЛЯ ПРОЦЕССА `ollama serve`, не для этого скрипта)
# хотя бы до того же значения — иначе Ollama сама всё равно обработает
# запросы по одному, и параллелизм в этом коде не даст эффекта.
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "1"))

# ---- Бэкенд извлечения свойств ----
# "yandex" — платно, облако (YandexGPT, см. properties_llm.py);
# "local"  — бесплатно, локальная модель через Ollama (см. local_llm.py),
#            требует установленный Ollama и скачанную модель.
#   export LLM_BACKEND=local
LLM_BACKEND = os.environ.get("LLM_BACKEND", "yandex")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct")

# Yandex AI Studio (для извлечения свойств по JSON-схемам).
# API-ключ и folder_id — НЕ хардкодить, только через переменные окружения:
#   export YANDEX_API_KEY=...
#   export YANDEX_FOLDER_ID=...
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")

# yandexgpt/rc — самая свежая (release candidate) версия YandexGPT Pro,
# лучше подходит для извлечения структурированных данных, чем Lite.
# Альтернатива подешевле: "yandexgpt-lite/rc".
YANDEX_MODEL = os.environ.get("YANDEX_MODEL", "yandexgpt/rc")

YANDEX_API_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"

# Сколько символов текста документа реально уходит в промпт (не влияет на
# то, что сохраняется в JSON — там всегда полный текст; ограничивает только
# то, что видит LLM при извлечении свойств).
MAX_LLM_INPUT_CHARS = 12000
MAX_LLM_OUTPUT_TOKENS = 4000

# Ориентировочная цена YandexGPT Pro, ₽ за 1000 токенов (входные+выходные
# считаются по одной ставке для простоты оценки — точные раздельные тарифы
# и актуальность проверяйте в консоли Yandex Cloud, раздел Billing:
# https://yandex.cloud/ru/docs/ai-studio/pricing — тарифы меняются).
LLM_PRICE_PER_1K_TOKENS_RUB = 0.80

# Максимальный бюджет на LLM-извлечение свойств за один запуск main.py, в
# рублях. При достижении лимита пайплайн останавливается ПЕРЕД следующим
# вызовом (см. budget.py), уже обработанное — сохраняется. Задаётся через
# переменную окружения, чтобы не редактировать код перед каждым прогоном:
#   export MAX_LLM_BUDGET_RUB=300
MAX_LLM_BUDGET_RUB = float(os.environ.get("MAX_LLM_BUDGET_RUB", "300"))

# Yandex Vision OCR — основной OCR-движок для страниц с битым текстовым
# слоем; Tesseract (см. extract_text.py) — бесплатный фолбэк, если Vision
# OCR недоступен (нет сети/ошибка) или бюджет на OCR исчерпан.
YANDEX_OCR_API_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
# 'markdown' — распознаёт текст с сохранением структуры таблиц прямо в
# результате (важно для сканов таблиц в журналах); 'page' — только текст.
YANDEX_OCR_MODEL = os.environ.get("YANDEX_OCR_MODEL", "markdown")

# Ориентировочная цена, ₽ за страницу (проверяйте актуальность в консоли:
# https://aistudio.yandex.ru/docs/ru/vision/pricing.html)
YANDEX_OCR_PRICE_PER_PAGE_RUB = 0.15

# Максимальный бюджет на OCR за один запуск main.py, в рублях. В отличие от
# LLM-бюджета, при исчерпании пайплайн НЕ останавливается — просто
# переключается на бесплатный Tesseract для оставшихся страниц.
#   export MAX_OCR_BUDGET_RUB=100
MAX_OCR_BUDGET_RUB = float(os.environ.get("MAX_OCR_BUDGET_RUB", "100"))
