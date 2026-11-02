"""Local transactions and an OS-released, cross-process delivery lock."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import threading
from .contracts import Busy


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(version INTEGER NOT NULL);
        INSERT INTO metadata SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM metadata);
        CREATE TABLE IF NOT EXISTS results(
          id TEXT PRIMARY KEY, source TEXT NOT NULL, message_id TEXT NOT NULL,
          input_hash TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(source,message_id));
        CREATE TABLE IF NOT EXISTS deliveries(
          id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES results(id),
          controller_id TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT,
          error TEXT, UNIQUE(result_id,controller_id));
        CREATE TABLE IF NOT EXISTS history(
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL REFERENCES deliveries(id),
          at REAL NOT NULL, event TEXT NOT NULL, value TEXT NOT NULL);
        ''')
        if self.db.execute("SELECT version FROM metadata").fetchone()[0] != 1:
            self.db.close()
            raise ValueError("unsupported store version")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    @contextmanager
    def delivery_lock(self):
        # Kernel locks release after process death; there is no stale PID to clear.
        with open(str(self.path) + ".delivery.lock", "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Busy("one delivery/recovery at a time per store") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def close(self):
        self.db.close()
