#!/usr/bin/env python3
"""Private online backup of DUO PILOT's database and .env, using only stdlib.

This does not save execution worktrees, images, or CLI credentials. A database
backup can describe an execution in progress; workspace recovery needs an idle
full backup. Source and destination directories must be managed by the operator.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
import tarfile
import tempfile
import time
import uuid


ARCHIVE_RE = re.compile(r"duopilot-state-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{12}\.tar\.gz\Z")
KEEP = 7


class BackupError(RuntimeError):
    """A public error which must never include file contents or raw exceptions."""


def checked_path(path: Path, *, allow_missing: bool = False) -> Path:
    """Reject symlinks in the path and its existing ancestors, without resolve()."""
    path = Path(os.path.abspath(os.fspath(path)))
    for entry in (*reversed(path.parents), path):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise BackupError("Fichier ou dossier de sauvegarde absent.") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupError("Lien symbolique refusé pour la sauvegarde.")
        if entry != path and not stat.S_ISDIR(metadata.st_mode):
            raise BackupError("Chemin de sauvegarde invalide.")
    return path


def regular_file(path: Path):
    """Open a regular file without following a final symbolic link."""
    path = checked_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise BackupError("La sauvegarde attend des fichiers ordinaires.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BackupError("Un fichier source a changé pendant sa préparation.")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def database_snapshot(source: Path, target: Path, *, seconds: float = 120) -> None:
    # SQLite needs the original pathname to find a live WAL. Do not copy the
    # database file directly or open it with immutable=1: that could omit commits.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(source) + suffix)
        checked_path(sidecar, allow_missing=True)
        if sidecar.exists() and not stat.S_ISREG(sidecar.lstat().st_mode):
            raise BackupError("Fichier auxiliaire SQLite invalide.")
    with regular_file(source) as guard:
        identity = os.fstat(guard.fileno())
        deadline = time.monotonic() + seconds

        def progress(_status, _remaining, _total):
            if time.monotonic() >= deadline:
                raise BackupError("La base est occupée ; sauvegarde interrompue sans remplacer les précédentes.")

        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
            current = checked_path(source).lstat()
            if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                raise BackupError("La base a changé de fichier pendant sa préparation.")
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=128, progress=progress, sleep=0.05)
                if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise BackupError("La vérification de la copie SQLite a échoué.")
    target.chmod(0o600)


def add_private_file(archive, handle, name: str) -> None:
    info = tarfile.TarInfo(name)
    metadata = os.fstat(handle.fileno())
    info.size = metadata.st_size
    info.mode = 0o600
    info.mtime = int(metadata.st_mtime)
    archive.addfile(info, handle)


def prune(destination: Path) -> None:
    completed = []
    for entry in destination.iterdir():
        if ARCHIVE_RE.fullmatch(entry.name) and stat.S_ISREG(entry.lstat().st_mode):
            completed.append(entry)
    # UTC names include microseconds and sort chronologically, independent of
    # archive reads or administrative changes to filesystem modification dates.
    for entry in sorted(completed, key=lambda p: p.name, reverse=True)[KEEP:]:
        if stat.S_ISREG(entry.lstat().st_mode):
            entry.unlink()


def create_backup(app: Path, destination: Path) -> Path:
    app = checked_path(app)
    if not app.is_dir():
        raise BackupError("Dossier de l’application invalide.")
    database = checked_path(app / "instance" / "duopilot.sqlite3")
    env = checked_path(app / ".env")
    destination = checked_path(destination, allow_missing=True)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = checked_path(destination)
    if not destination.is_dir():
        raise BackupError("Dossier de sauvegarde invalide.")
    destination.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    name = f"duopilot-state-{stamp}-{uuid.uuid4().hex[:12]}.tar.gz"
    final = destination / name
    with tempfile.TemporaryDirectory(prefix=".duopilot-backup-", dir=destination) as scratch:
        working = Path(scratch)
        working.chmod(0o700)
        snapshot = working / "duopilot.sqlite3"
        database_snapshot(database, snapshot)
        pending = working / "archive.part"
        with pending.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                with regular_file(snapshot) as handle:
                    add_private_file(archive, handle, "duopilot/instance/duopilot.sqlite3")
                with regular_file(env) as handle:
                    add_private_file(archive, handle, "duopilot/.env")
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, final)
        directory_fd = os.open(destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    prune(destination)
    return final


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sauvegarder la base et .env de DUO PILOT ; conserver les sept dernières archives.")
    parser.add_argument("--app", type=Path, default=Path("/opt/duopilot/app"))
    parser.add_argument("--destination", type=Path, default=Path("/var/backups/duopilot"))
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.app, args.destination)
    except BackupError as exc:
        print("Sauvegarde non terminée : " + str(exc))
        return 1
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError):
        print("Sauvegarde non terminée : vérifier les chemins, les droits, l’espace disque et la base SQLite.")
        return 1
    print(f"Sauvegarde créée : {result.name}. Les sept dernières archives sont conservées.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
