# subjetunes/tagging/masterdb.py
"""Write comment tags straight into rekordbox's master.db (rekordbox must be closed).

Same safety rules as TAG's write-back:
  * refuses to run while rekordbox is running (pyrekordbox checks again on commit);
  * copies master.db, -wal, -shm and masterPlaylists6.xml to a timestamped backup first;
  * changes only DjmdContent.Commnt, and only our {st ...} block inside it;
  * one commit, rollback on any error, then re-opens the database and verifies.

pyrekordbox tracks attribute changes and bumps rekordbox's USN counters on commit,
so rekordbox sees the edit as a normal change. Tracks are matched by file path.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .comments import BlockFormat, merge_comment, normalise_path

DB_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")


class WriteError(RuntimeError):
    pass


@dataclass
class Change:
    key: str
    content_id: str
    old: str
    new: str


@dataclass
class DbReport:
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0
    not_found: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    written: bool = False


def open_db(db_path: Path | None = None, key: str = "") -> Any:
    try:
        try:
            from pyrekordbox import MasterDatabase as Db
        except ImportError:  # older pyrekordbox
            from pyrekordbox import Rekordbox6Database as Db
    except ImportError as e:
        raise WriteError("pyrekordbox is not installed: pip install pyrekordbox") from e
    kw: dict[str, Any] = {}
    if not db_path:
        # pyrekordbox's own lookup needs rekordbox in /Applications or Program Files and fails
        # when its settings files disagree; ours reads the same files more forgivingly.
        try:
            from ..desktop.rblocate import candidates, find_master_db
            db_path = find_master_db()
            if db_path is None:
                looked = "\n".join(f"  {c.path}  ({c.how})" for c in candidates()) or "  (no Pioneer folder on this system)"
                raise WriteError("Couldn't find your rekordbox library (master.db). Looked in:\n" + looked +
                                 "\nOpen it by hand in Settings > Library, or start rekordbox once so it creates it.")
        except ImportError:
            pass
    if db_path:
        kw["path"] = str(db_path)
    if key:
        kw["key"] = key
    return Db(**kw)


def rekordbox_running() -> bool:
    try:
        from pyrekordbox.utils import get_rekordbox_pid
        return bool(get_rekordbox_pid())
    except Exception:
        return False


def backup(db_dir: Path, backups_root: Path) -> Path:
    dest = backups_root / f"master_{datetime.now():%Y%m%d_%H%M%S}"
    dest.mkdir(parents=True, exist_ok=False)
    for name in DB_FILES:
        if (db_dir / name).exists():
            shutil.copy2(db_dir / name, dest / name)
    if not (dest / "master.db").exists():
        raise WriteError(f"backup failed: no master.db in {db_dir}")
    return dest


def plan(db: Any, new_tags: Mapping[str, list[str]], fmt: BlockFormat = BlockFormat()) -> DbReport:
    """Dry run: what would change. Doesn't modify anything."""
    by_path: dict[str, Any] = {}
    for c in db.get_content():
        p = getattr(c, "FolderPath", None)
        if p:
            by_path[normalise_path(p)] = c
    rep = DbReport()
    for raw_key, tags in new_tags.items():
        c = by_path.get(normalise_path(raw_key))
        if c is None:
            rep.not_found.append(raw_key)
            continue
        old = getattr(c, "Commnt", None) or ""
        new = merge_comment(old, tags, fmt)
        if new == old:
            rep.unchanged += 1
        else:
            rep.changes.append(Change(normalise_path(raw_key), str(c.ID), old, new))
    return rep


def write_comments(new_tags: Mapping[str, list[str]], backups_root: Path, db_path: Path | None = None,
                   key: str = "", fmt: BlockFormat = BlockFormat(), dry_run: bool = True,
                   opener: Callable[..., Any] = open_db) -> DbReport:
    """Apply tags to master.db. dry_run=True (default) only reports."""
    if not dry_run and rekordbox_running():
        raise WriteError("Close rekordbox first: comments are only written while it is closed.")
    db = opener(db_path, key)
    try:
        rep = plan(db, new_tags, fmt)
        if dry_run or not rep.changes:
            return rep
        db_dir = Path(getattr(db, "db_directory", None) or (db_path.parent if db_path else "."))
        rep.backup_dir = backup(db_dir, backups_root)
        try:
            for ch in rep.changes:
                c = db.get_content(ID=ch.content_id)
                c = c.first() if hasattr(c, "first") else c
                c.Commnt = ch.new
            db.commit()
        except Exception as e:
            db.rollback()
            raise WriteError(f"write failed and was rolled back; library unchanged. Detail: {e}") from e
        rep.written = True
    finally:
        db.close()

    check = opener(db_path, key)
    try:
        bad = []
        for ch in rep.changes:
            c = check.get_content(ID=ch.content_id)
            c = c.first() if hasattr(c, "first") else c
            if (getattr(c, "Commnt", None) or "") != ch.new:
                bad.append(ch.key)
        if bad:
            raise WriteError(f"{len(bad)} comments didn't verify (e.g. {bad[0]}). Backup: {rep.backup_dir}")
    finally:
        check.close()
    return rep


def restore(backup_dir: Path, db_dir: Path) -> None:
    if rekordbox_running():
        raise WriteError("Close rekordbox before restoring.")
    if not (backup_dir / "master.db").exists():
        raise WriteError(f"no master.db in {backup_dir}")
    for name in DB_FILES:
        src, dst = backup_dir / name, db_dir / name
        if src.exists():
            shutil.copy2(src, dst)
        elif dst.exists() and name.endswith(("-wal", "-shm")):
            dst.unlink()  # a WAL newer than the backup would be replayed over it
