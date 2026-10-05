from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class CaptionStore:
    """Optional single-machine caption store. Audio files are not kept."""

    def __init__(self, path: str | Path | None):
        text = "" if path is None else str(path).strip()
        self.path = Path(text) if text else None
        self.enabled = self.path is not None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        """
                        create table if not exists captions (
                            id text primary key,
                            room_id text not null,
                            session_id text not null,
                            seq integer not null,
                            version integer not null,
                            zh text,
                            zh_raw text,
                            en text,
                            status text,
                            t0_ms integer,
                            t1_ms integer,
                            updated_at real not null
                        )
                        """
                    )
                    conn.execute("create index if not exists captions_room_session_seq on captions (room_id, session_id, seq)")
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        assert self.path is not None
        return sqlite3.connect(self.path)

    def _run(self, fn):
        conn = self._conn()
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    def save(self, event: dict) -> None:
        if not self.enabled or not event.get("id"):
            return
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                insert into captions (id, room_id, session_id, seq, version, zh, zh_raw, en, status, t0_ms, t1_ms, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    version=excluded.version,
                    zh=excluded.zh,
                    zh_raw=excluded.zh_raw,
                    en=excluded.en,
                    status=excluded.status,
                    t0_ms=excluded.t0_ms,
                    t1_ms=excluded.t1_ms,
                    updated_at=excluded.updated_at
                where excluded.version >= captions.version
                """,
                (
                    event.get("id"), event.get("room_id"), event.get("session_id"), int(event.get("seq") or 0),
                    int(event.get("version") or 1), event.get("zh") or "", event.get("zh_raw") or "",
                    event.get("en") or "", event.get("status") or "", event.get("t0_ms"), event.get("t1_ms"),
                    time.time(),
                ),
            )
        self._run(write)

    def delete_room(self, room_id: str) -> int:
        if not self.enabled:
            return 0
        def remove(conn: sqlite3.Connection) -> int:
            cur = conn.execute("delete from captions where room_id = ?", (room_id,))
            return cur.rowcount
        return self._run(remove)

    def purge_expired(self, ttl_s: float) -> int:
        if not self.enabled:
            return 0
        cutoff = time.time() - ttl_s
        def purge(conn: sqlite3.Connection) -> int:
            cur = conn.execute("delete from captions where updated_at < ?", (cutoff,))
            return cur.rowcount
        return self._run(purge)

    def room_rows(self, room_id: str) -> list[dict]:
        if not self.enabled:
            return []
        def read(conn: sqlite3.Connection) -> list[dict]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                with sessions as (
                    select session_id, min(rowid) as session_ord
                    from captions where room_id = ? group by session_id
                )
                select captions.*, sessions.session_ord
                from captions join sessions on captions.session_id = sessions.session_id
                where captions.room_id = ?
                order by sessions.session_ord, captions.seq
                """,
                (room_id, room_id),
            ).fetchall()
            return [dict(row) for row in rows]
        return self._run(read)
