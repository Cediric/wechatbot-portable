"""Durable FIFO of incoming OneBot events. SQLite commits precede acceptance."""
import json
import os
import sqlite3
import threading
import time

class Outbox:
    def __init__(self, path, max_events=10000):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.max_events = max_events
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, key TEXT UNIQUE, payload TEXT NOT NULL, created REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS media_leases (path TEXT PRIMARY KEY, expires REAL NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_media_leases_expires ON media_leases(expires)")
        self._prune_media_leases_locked()
        self.db.commit()

    def put(self, event):
        payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
        key = f"{event.get('self_id')}:{event.get('message_id')}:{event.get('time')}"
        with self.lock:
            if self.db.execute("SELECT 1 FROM events WHERE key=?", (key,)).fetchone():
                return True
            if self.db.execute("SELECT count(*) FROM events").fetchone()[0] >= self.max_events:
                return False
            try:
                self.db.execute("INSERT INTO events(key,payload,created) VALUES(?,?,?)", (key,payload,time.time()))
                self.db.commit()
                self.ready.set()
            except BaseException:
                self.db.rollback()
                raise
        return True

    def peek(self, wait_seconds=0):
        with self.lock:
            row = self.db.execute("SELECT id,payload FROM events ORDER BY id LIMIT 1").fetchone()
            if row:
                return row[0], json.loads(row[1])
            # Clear under the same lock as put/commit so a new event cannot
            # disappear between the empty read and beginning the wait.
            self.ready.clear()
        if wait_seconds and self.ready.wait(wait_seconds):
            return self.peek()
        return None

    def acknowledge(self, row_id):
        with self.lock:
            try:
                self._prune_media_leases_locked()
                row=self.db.execute("SELECT payload FROM events WHERE id=?",(row_id,)).fetchone()
                if row:
                    for path in self._media_paths(json.loads(row[0])):
                        self.db.execute("INSERT OR REPLACE INTO media_leases(path,expires) VALUES(?,?)",(path,time.time()+3600))
                self.db.execute("DELETE FROM events WHERE id=?", (row_id,))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _prune_media_leases_locked(self, now=None):
        self.db.execute("DELETE FROM media_leases WHERE expires<=?", (time.time() if now is None else now,))

    @staticmethod
    def _media_paths(event):
        paths=set()
        for segment in event.get("message",[]):
            if isinstance(segment,dict) and segment.get("type") in ("record","voice","image","file","video"):
                path=segment.get("data",{}).get("file","")
                if isinstance(path,str) and os.path.isabs(path):
                    paths.add(os.path.realpath(path))
        return paths

    def referenced_files(self):
        with self.lock:
            try:
                now = time.time()
                self._prune_media_leases_locked(now)
                paths={row[0] for row in self.db.execute("SELECT path FROM media_leases WHERE expires>?",(now,))}
                for row in self.db.execute("SELECT payload FROM events"):
                    paths.update(self._media_paths(json.loads(row[0])))
                self.db.commit()
                return paths
            except BaseException:
                self.db.rollback()
                raise

    def stats(self):
        with self.lock:
            count, oldest = self.db.execute("SELECT count(*),min(created) FROM events").fetchone()
        return {"pending": count, "oldest_age_seconds": round(time.time()-oldest,1) if oldest else 0, "limit": self.max_events}

    def close(self):
        with self.lock:
            self.db.close()
