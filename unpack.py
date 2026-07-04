"""
Рекурсивная распаковка архивов (.rar/.zip/.7z) из staging в unpacked,
с сохранением смысловой связки "основной документ + приложения" как
единого bundle (см. саммари: архив = docx + PDF-приложения = одна единица
знания, а не независимые документы).

Идемпотентно: bundle_id — детерминированный хеш от пути архива (не
случайный uuid4), и если папка с результатом распаковки уже существует и не
пуста — архив повторно НЕ распаковывается, файлы берутся из уже готового
output/unpacked. Это критично на корпусе с сотнями архивов: без этого
каждый перезапуск пайплайна (после сбоя, Ctrl+C, новой правки кода)
разворачивал бы всё заново.

Требует системный `unrar` или `unar` для .rar (rarfile — обёртка, сама
распаковку не делает) и py7zr для .7z.
    pip install rarfile py7zr --break-system-packages
    apt-get install unrar   # или unar
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger("unpack")

# Многотомные RAR: "name.part1.rar", "name.part2.rar", ... — rarfile умеет
# автоматически подхватывать остальные тома из той же папки, но ТОЛЬКО если
# открыть именно первый том. Остальные части нужно отфильтровать заранее,
# иначе каждая пытается стать самостоятельным архивом и падает с
# NeedFirstVolume.
_RAR_PART_RE = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)\.rar$", re.IGNORECASE)
# Старый стиль многотомности: "name.rar" (первый том) + "name.r00", "name.r01", ...
_RAR_OLD_STYLE_RE = re.compile(r"^(?P<base>.+)\.r\d{2}$", re.IGNORECASE)


@dataclass
class DocBundle:
    bundle_id: str
    files: list[str] = field(default_factory=list)   # локальные пути всех файлов bundle
    main_file: str | None = None                      # эвристически определённый "главный" документ
    source_archive: str | None = None                  # если bundle из архива — путь к нему


def _stable_id(path: Path) -> str:
    """Детерминированный id по пути файла — для идемпотентности между запусками."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]


def _guess_main_file(files: list[Path]) -> Path | None:
    """
    Эвристика: главный документ — это .docx/.doc, если он один, иначе —
    самый крупный по размеру файл из docx/pdf (обычно основной текст крупнее
    отдельных PDF-приложений со сканами таблиц/чертежей).
    """
    docx_files = [f for f in files if f.suffix.lower() in (".docx", ".doc")]
    if len(docx_files) == 1:
        return docx_files[0]
    candidates = [f for f in files if f.suffix.lower() in (".docx", ".doc", ".pdf")]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_size)


def _extract_archive_cached(archive_path: Path, dest_dir: Path) -> list[Path]:
    """
    Распаковывает архив в dest_dir, но если dest_dir уже существует и
    непуста (результат прошлого запуска) — просто возвращает уже лежащие
    там файлы, не трогая архив заново.
    """
    if dest_dir.exists():
        existing = sorted(p for p in dest_dir.rglob("*") if p.is_file())
        if existing:
            logger.debug("Уже распаковано ранее, использую кэш: %s", dest_dir)
            return existing

    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()

    if suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest_dir)
    elif suffix == ".rar":
        import rarfile
        with rarfile.RarFile(archive_path) as rf:
            rf.extractall(dest_dir)
    elif suffix == ".7z":
        import py7zr
        with py7zr.SevenZipFile(archive_path, mode="r") as zf:
            zf.extractall(dest_dir)
    else:
        raise ValueError(f"Неподдерживаемый тип архива: {suffix}")

    return sorted(p for p in dest_dir.rglob("*") if p.is_file())


def _filter_multivolume_rar_parts(staged_files: list[str]) -> list[str]:
    """
    Из списка файлов убирает все части многотомного RAR, кроме первой:
    - "name.part2.rar", "name.part3.rar", ... — убираются, остаётся только
      "name.part1.rar" (rarfile сам найдёт остальные части рядом);
    - "name.r00", "name.r01", ... (старый стиль) — убираются целиком, точкой
      входа остаётся "name.rar".
    Файлы физически никуда не деваются — просто не рассматриваются как
    отдельные bundles, extract_archive возьмёт их автоматически по мере
    надобности через открытие первого тома.
    """
    result = []
    for f in staged_files:
        name = Path(f).name
        m = _RAR_PART_RE.match(name)
        if m and int(m.group("num")) != 1:
            logger.debug("Пропускаю не-первый том многотомного RAR: %s", f)
            continue
        if _RAR_OLD_STYLE_RE.match(name):
            logger.debug("Пропускаю продолжение многотомного RAR (старый стиль): %s", f)
            continue
        result.append(f)
    return result


def build_bundles(staged_files: list[str]) -> list[DocBundle]:
    """
    Из плоского списка скачанных файлов строит список DocBundle:
    - каждый архив -> один bundle из распакованных файлов;
    - каждый "одиночный" файл (не входящий в архив) -> bundle из одного файла;
    - вложенные архивы разворачиваются рекурсивно;
    - многотомные RAR обрабатываются как один архив через первый том;
    - уже распакованные ранее архивы повторно не трогаются (идемпотентно).
    """
    bundles: list[DocBundle] = []
    staged_files = _filter_multivolume_rar_parts(staged_files)
    skipped_cached = 0

    for f in staged_files:
        path = Path(f)
        if path.suffix.lower() in config.ARCHIVE_EXTENSIONS:
            bundle_id = _stable_id(path)
            dest_dir = config.UNPACKED_DIR / bundle_id
            already_done = dest_dir.exists() and any(dest_dir.rglob("*"))
            if already_done:
                skipped_cached += 1
            else:
                logger.info("Распаковываю архив %s -> %s", path, dest_dir)

            try:
                extracted = _extract_archive_cached(path, dest_dir)
                # рекурсивно разворачиваем вложенные архивы внутри распакованного
                queue = list(extracted)
                all_files: list[Path] = []
                while queue:
                    item = queue.pop()
                    if item.suffix.lower() in config.ARCHIVE_EXTENSIONS:
                        nested_dir = item.parent / (item.stem + "__unpacked")
                        queue.extend(_extract_archive_cached(item, nested_dir))
                    else:
                        all_files.append(item)
            except Exception as e:
                logger.error("Не удалось распаковать архив %s: %s. Пропускаю.", path, e)
                continue

            bundles.append(
                DocBundle(
                    bundle_id=bundle_id,
                    files=[str(p) for p in all_files],
                    main_file=str(_guess_main_file(all_files)) if all_files else None,
                    source_archive=str(path),
                )
            )
        else:
            bundles.append(
                DocBundle(
                    bundle_id=_stable_id(path),
                    files=[str(path)],
                    main_file=str(path),
                    source_archive=None,
                )
            )

    if skipped_cached:
        logger.info("Пропущено повторное распаковка (уже было ранее): %d архивов", skipped_cached)
    logger.info("Собрано bundles: %d", len(bundles))
    return bundles
