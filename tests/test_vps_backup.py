"""Real SQLite/tar backups, retention, permissions, and unsafe path rejection."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup.py"
spec = importlib.util.spec_from_file_location("duopilot_vps_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture
def source(tmp_path):
    app = tmp_path / "app"
    (app / "instance").mkdir(parents=True)
    (app / ".env").write_text("SECRET_KEY=never-print-secret\nGITHUB_TOKEN=private-token\n")
    db = sqlite3.connect(app / "instance" / "duopilot.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY, message TEXT)")
    db.execute("INSERT INTO conversations(message) VALUES(?)", ("Premier échange",))
    db.commit()
    yield app, db
    db.close()


def read_database(archive, target):
    with tarfile.open(archive, "r:gz") as saved:
        target.write_bytes(saved.extractfile("duopilot/instance/duopilot.sqlite3").read())
    with sqlite3.connect(target) as restored:
        return restored.execute("SELECT message FROM conversations ORDER BY id").fetchall()


def test_live_wal_backup_restores_committed_rows_and_excludes_other_data(source, tmp_path):
    app, db = source
    (app / "instance" / "executions").mkdir()
    (app / "instance" / "executions" / "work.txt").write_text("Not a backup of worktrees")
    (app / ".codex").mkdir()
    (app / ".codex" / "auth.json").write_text("CLI-credentials-do-not-archive")
    db.execute("INSERT INTO conversations(message) VALUES(?)", ("Échange validé dans WAL",))
    db.commit()
    assert (app / "instance" / "duopilot.sqlite3-wal").stat().st_size > 0
    db.execute("INSERT INTO conversations(message) VALUES(?)", ("Encore non validé",))
    archive = backup.create_backup(app, tmp_path / "backups")
    assert read_database(archive, tmp_path / "restored.sqlite3") == [("Premier échange",), ("Échange validé dans WAL",)]
    with tarfile.open(archive, "r:gz") as saved:
        assert set(saved.getnames()) == {"duopilot/.env", "duopilot/instance/duopilot.sqlite3"}
        assert saved.extractfile("duopilot/.env").read() == (app / ".env").read_bytes()
        assert all(item.mode == 0o600 and item.isfile() for item in saved.getmembers())
    db.rollback()


@pytest.mark.skipif(os.name != "posix", reason="VPS permission model is POSIX")
def test_output_directory_and_archive_are_private(source, tmp_path):
    app, _ = source
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o777)
    destination.chmod(0o777)
    result = backup.create_backup(app, destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert list(destination.glob(".duopilot-backup-*")) == []


def test_retains_seven_completed_archives_and_no_other_paths_are_deleted(source, tmp_path):
    app, _ = source
    destination = tmp_path / "backups"
    destination.mkdir()
    old = []
    for day in range(1, 10):
        item = destination / f"duopilot-state-202501{day:02d}T120000.000000Z-000000000000.tar.gz"
        item.write_bytes(b"older completed archive")
        old.append(item)
    other = destination / "personal.tar.gz"
    other.write_bytes(b"leave this alone")
    incomplete = destination / ".duopilot-backup-unfinished"
    incomplete.mkdir()
    new = backup.create_backup(app, destination)
    assert new.is_file()
    assert all(not item.exists() for item in old[:3])
    assert all(item.is_file() for item in old[3:])
    assert other.read_bytes() == b"leave this alone"
    assert incomplete.is_dir()


@pytest.mark.parametrize("kind", ["env", "database", "wal", "app", "destination", "destination_parent"])
@pytest.mark.skipif(os.name != "posix", reason="Symlink setup on VPS")
def test_rejects_symlink_sources_and_destinations(source, tmp_path, kind):
    app, db = source
    destination = tmp_path / "backups"
    if kind in ("env", "database", "wal"):
        original = app / ".env" if kind == "env" else app / "instance" / ("duopilot.sqlite3-wal" if kind == "wal" else "duopilot.sqlite3")
        real = tmp_path / (kind + "-real")
        original.rename(real)
        original.symlink_to(real)
    elif kind == "app":
        alias = tmp_path / "app-link"
        alias.symlink_to(app, target_is_directory=True)
        app = alias
    elif kind == "destination":
        real = tmp_path / "backup-real"
        real.mkdir()
        destination.symlink_to(real, target_is_directory=True)
    else:
        real = tmp_path / "backup-parent-real"
        real.mkdir()
        alias = tmp_path / "backup-parent-link"
        alias.symlink_to(real, target_is_directory=True)
        destination = alias / "child"
    with pytest.raises(backup.BackupError, match="Lien symbolique"):
        backup.create_backup(app, destination)


def test_bad_database_leaves_previous_archive_and_no_temporary_output(source, tmp_path):
    app, db = source
    destination = tmp_path / "backups"
    first = backup.create_backup(app, destination)
    db.close()
    (app / "instance" / "duopilot.sqlite3").write_bytes(b"not sqlite never-print-secret private-token")
    result = subprocess.run([sys.executable, str(SCRIPT), "--app", str(app), "--destination", str(destination)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "never-print-secret" not in result.stdout + result.stderr
    assert "private-token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert list(destination.iterdir()) == [first]


def test_cli_success_does_not_print_secrets(source, tmp_path):
    app, _ = source
    result = subprocess.run([sys.executable, str(SCRIPT), "--app", str(app), "--destination", str(tmp_path / "backups")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "Sauvegarde créée" in result.stdout
    assert "never-print-secret" not in result.stdout + result.stderr
    assert "private-token" not in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "posix", reason="Symlink setup on VPS")
def test_retention_leaves_symlinks_untouched(source, tmp_path):
    app, _ = source
    destination = tmp_path / "backups"
    destination.mkdir()
    elsewhere = tmp_path / "precious.txt"
    elsewhere.write_text("keep")
    link = destination / "duopilot-state-20200101T120000.000000Z-000000000000.tar.gz"
    link.symlink_to(elsewhere)
    backup.create_backup(app, destination)
    assert link.is_symlink()
    assert elsewhere.read_text() == "keep"
