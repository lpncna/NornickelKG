"""
Рекурсивный обход папок-источников на Яндекс.Диске и скачивание файлов
в локальный staging-каталог с сохранением структуры папок.

Используется библиотека `yadisk` (pip install yadisk).
Документация: https://yadisk.readthedocs.io/

Файлы, уже скачанные (совпадает путь + размер + дата модификации),
повторно не загружаются — это даёт идемпотентный запуск пайплайна.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import yadisk

import config

logger = logging.getLogger("yadisk_sync")

MANIFEST_PATH = config.STAGING_DIR / "_manifest.json"


@dataclass
class RemoteFile:
    remote_path: str       # путь на Я.Диске
    local_path: str        # путь в staging после скачивания
    size: int
    modified: str
    md5: str | None


def get_client() -> yadisk.YaDisk:
    if not config.YADISK_TOKEN:
        raise RuntimeError(
            "Не задан YADISK_TOKEN. Получите OAuth-токен приложения с "
            "доступом на чтение Диска и положите в переменную окружения "
            "YADISK_TOKEN."
        )
    client = yadisk.YaDisk(token=config.YADISK_TOKEN)
    if not client.check_token():
        raise RuntimeError("YADISK_TOKEN недействителен или истёк.")
    return client


def _walk_remote(client: yadisk.YaDisk, path: str) -> Iterator["yadisk.objects.ResourceObject"]:
    """Рекурсивно обходит папку на Я.Диске, отдавая только файлы (не директории)."""
    for item in client.listdir(path):
        if item.type == "dir":
            yield from _walk_remote(client, item.path)
        else:
            yield item


def _load_manifest() -> dict[str, dict]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sync_all() -> list[RemoteFile]:
    """
    Скачивает все файлы из config.SOURCE_FOLDERS в config.STAGING_DIR,
    пропуская уже скачанные и не изменившиеся файлы.
    Возвращает список RemoteFile для всех файлов (и вновь скачанных, и ранее
    скачанных), чтобы дальнейшие шаги пайплайна видели полный набор.
    """
    client = get_client()
    manifest = _load_manifest()
    results: list[RemoteFile] = []

    for source_folder in config.SOURCE_FOLDERS:
        logger.info("Обход папки на Я.Диске: %s", source_folder)
        try:
            remote_items = list(_walk_remote(client, source_folder))
        except yadisk.exceptions.PathNotFoundError:
            logger.error("Папка не найдена на Я.Диске: %s", source_folder)
            continue

        for item in remote_items:
            remote_path = item.path  # напр. "disk:/Задача 2 / .../file.pdf"
            rel_path = remote_path.split(":", 1)[-1].lstrip("/")
            local_path = config.STAGING_DIR / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            key = remote_path
            cached = manifest.get(key)
            same = (
                cached
                and cached.get("size") == item.size
                and cached.get("modified") == str(item.modified)
                and local_path.exists()
            )

            if not same:
                logger.info("Скачиваю: %s", remote_path)
                client.download(remote_path, str(local_path))
                manifest[key] = {
                    "size": item.size,
                    "modified": str(item.modified),
                    "md5": item.md5,
                    "local_path": str(local_path),
                }
            else:
                logger.debug("Пропускаю (не изменился): %s", remote_path)

            results.append(
                RemoteFile(
                    remote_path=remote_path,
                    local_path=str(local_path),
                    size=item.size,
                    modified=str(item.modified),
                    md5=item.md5,
                )
            )

    _save_manifest(manifest)
    logger.info("Синхронизация завершена: %d файлов", len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    files = sync_all()
    print(f"Скачано/проверено файлов: {len(files)}")
