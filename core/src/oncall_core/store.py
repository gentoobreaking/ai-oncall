"""SQLite store：WAL 模式、有序 migration、incidents/timeline/predictions 表。

- WAL：併發讀寫安全（gate 的 ReportIncident 與 UI readapi 同時存取）
- busy_timeout：寫入鎖競爭時等待而非立即報錯
- migration：schema_migrations 記錄已套用版本，啟動時依序補齊
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from oncall_core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Migrations（只追加，不修改既有條目）
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_incidents",
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id            TEXT PRIMARY KEY,
            fingerprint   TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open',
            severity      INTEGER NOT NULL DEFAULT 0,
            title         TEXT NOT NULL DEFAULT '',
            labels_json   TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_fingerprint_status
            ON incidents (fingerprint, status);
        CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
        """,
    ),
    (
        "0002_timeline",
        """
        CREATE TABLE IF NOT EXISTS timeline (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id  TEXT NOT NULL REFERENCES incidents (id),
            kind         TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_incident ON timeline (incident_id, id);
        """,
    ),
    (
        "0003_predictions",
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id              TEXT PRIMARY KEY,
            incident_id     TEXT NOT NULL REFERENCES incidents (id),
            prompt_version  TEXT NOT NULL DEFAULT 'v0',
            hypotheses_json TEXT NOT NULL DEFAULT '[]',
            actions_json    TEXT NOT NULL DEFAULT '[]',
            missing_context_json TEXT NOT NULL DEFAULT '[]',
            tokens_used     INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_incident
            ON predictions (incident_id, created_at);
        """,
    ),
    (
        "0004_timeline_hashchain",
        """
        ALTER TABLE timeline ADD COLUMN prev_hash TEXT;
        ALTER TABLE timeline ADD COLUMN hash TEXT;
        """,
    ),
    (
        "0005_knowledge_chunks",
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id            TEXT PRIMARY KEY,
            source        TEXT NOT NULL,
            ref_id        TEXT,
            text          TEXT NOT NULL,
            embedding     TEXT NOT NULL,
            embedding_provider TEXT NOT NULL DEFAULT 'hash',
            service       TEXT,
            cluster       TEXT,
            severity      TEXT,
            created_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kc_meta ON knowledge_chunks (service, cluster, severity);
        CREATE INDEX IF NOT EXISTS idx_kc_source ON knowledge_chunks (source, ref_id);
        """,
    ),
]


@dataclass(slots=True)
class Incident:
    """事故領域模型的最小骨架（T006 擴充）。"""

    id: str
    fingerprint: str
    status: str
    severity: int
    title: str
    labels: dict[str, str]
    created_at: float
    updated_at: float


@dataclass(slots=True)
class KnowledgeChunk:
    """RAG 知識庫片段（memory §D.1/D.2）。"""

    id: str
    source: str
    ref_id: str | None
    text: str
    embedding_vector: list[float]
    embedding_provider: str
    service: str | None
    cluster: str | None
    severity: str | None
    created_at: float


class Store:
    """SQLite 持久持久層。執行緒安全：單一連線 + 寫入鎖。"""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._write_lock = threading.Lock()
        self._conn = self._connect()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        if self._path == ":memory:":
            conn = sqlite3.connect(self._path, check_same_thread=False)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 允許「寫入進行中」的併發讀取；busy_timeout 等鎖 5 秒
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """寫入交易：跨執行緒互斥，避免 SQLITE_BUSY。"""
        with self._write_lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """讀取：與寫入共用鎖——單一 sqlite3.Connection 跨執行緒共用時，
        讀寫交錯使用同一 cursor pool 會觸發 InterfaceError。"""
        with self._write_lock:
            yield self._conn

    def migrate(self) -> list[str]:
        """套用未執行的 migrations，回傳本次套用的版本名。"""
        with self._write_lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  name TEXT PRIMARY KEY,"
                "  applied_at REAL NOT NULL)"
            )
            applied = {r["name"] for r in self._conn.execute("SELECT name FROM schema_migrations")}
        ran: list[str] = []
        for name, script in MIGRATIONS:
            if name in applied:
                continue
            with self._write() as conn:
                conn.executescript(script)
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (name, time.time()),
                )
            ran.append(name)
            log.info("migration applied", version=name)
        return ran

    # ------------------------------------------------------------------
    # incidents
    # ------------------------------------------------------------------

    def create_incident(
        self,
        *,
        fingerprint: str,
        severity: int = 0,
        title: str = "",
        labels: dict[str, str] | None = None,
    ) -> tuple[Incident, bool]:
        """建立 incident；冪等命中回 (既有, False)。"""
        import json as _json

        now = time.time()
        inc_id = f"inc-{uuid.uuid4().hex[:12]}"
        labels_json = _json.dumps(labels or {}, ensure_ascii=False)
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is not None:
                return self._row_to_incident(row), False
            conn.execute(
                "INSERT INTO incidents (id, fingerprint, status, severity, title,"
                " labels_json, created_at, updated_at)"
                " VALUES (?, ?, 'open', ?, ?, ?, ?, ?)",
                (inc_id, fingerprint, severity, title, labels_json, now, now),
            )
        created = self.get_incident_by_fingerprint(fingerprint)
        assert created is not None
        return created, True

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return self._row_to_incident(row) if row else None

    def get_incident_by_fingerprint(self, fingerprint: str) -> Incident | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def update_status(self, incident_id: str, status: str) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE incidents SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), incident_id),
            )
            return cur.rowcount > 0

    def list_incidents(self, status: str | None = None, limit: int = 100) -> list[Incident]:
        q = "SELECT * FROM incidents"
        params: tuple[str, ...] = ()
        if status:
            q += " WHERE status = ?"
            params = (status,)
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._read() as conn:
            rows = conn.execute(q, (*params, limit)).fetchall()
        return [self._row_to_incident(r) for r in rows]

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        import json as _json

        return Incident(
            id=row["id"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            severity=row["severity"],
            title=row["title"],
            labels=_json.loads(row["labels_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # timeline（雜湊鏈欄位由 T006 hashchain 接手擴充）
    # ------------------------------------------------------------------

    def append_timeline(self, incident_id: str, kind: str, payload: dict[str, object]) -> int:
        import json as _json

        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO timeline (incident_id, kind, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (incident_id, kind, _json.dumps(payload, ensure_ascii=False), time.time()),
            )
            lastrow = cur.lastrowid
            # AUTOINCREMENT INSERT 必有 rowid
            assert lastrow is not None
            return int(lastrow)

    def timeline(self, incident_id: str) -> list[sqlite3.Row]:
        with self._read() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM timeline WHERE incident_id = ? ORDER BY id",
                    (incident_id,),
                )
            )

    # ------------------------------------------------------------------
    # timeline 雜湊鏈（T006，§E.3）
    # ------------------------------------------------------------------

    def last_chained_hash(self, incident_id: str) -> str | None:
        """回傳該 incident 最後一筆雜湊鏈事件的 hash；無則 None。"""
        with self._read() as conn:
            row = conn.execute(
                "SELECT hash FROM timeline"
                " WHERE incident_id = ? AND hash IS NOT NULL"
                " ORDER BY id DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
        return row["hash"] if row else None

    def append_chained_event(
        self,
        incident_id: str,
        kind: str,
        payload: dict[str, object],
        *,
        prev_hash: str | None = None,
        event_hash: str | None = None,
    ) -> int:
        """寫入含雜湊欄位的時間線事件。

        prev_hash/event_hash 未提供時為未鏈結事件（舊路徑相容）；
        正式路徑一律經 HashChain.append() 計算後傳入。
        """
        import json as _json

        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO timeline (incident_id, kind, payload_json, created_at,"
                " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    incident_id,
                    kind,
                    _json.dumps(payload, ensure_ascii=False),
                    time.time(),
                    prev_hash,
                    event_hash,
                ),
            )
            lastrow = cur.lastrowid
            # AUTOINCREMENT INSERT 必有 rowid
            assert lastrow is not None
            return int(lastrow)

    def tamper_timeline_payload(self, event_id: int, new_payload: dict[str, object]) -> None:
        """直接改寫事件 payload——僅供竄改偵測測試使用。"""
        import json as _json

        with self._write() as conn:
            conn.execute(
                "UPDATE timeline SET payload_json = ? WHERE id = ?",
                (_json.dumps(new_payload, ensure_ascii=False), event_id),
            )

    # ------------------------------------------------------------------
    # correlate 聚合查詢（§A.2）
    # ------------------------------------------------------------------

    def recent_unresolved_incidents(self, window_seconds: float) -> list[Incident]:
        """過去 window_seconds 內更新過、且尚未 resolved 的 incidents。"""
        import time as _time

        cutoff = _time.time() - window_seconds
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents"
                " WHERE status != 'resolved' AND updated_at >= ?"
                " ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [self._row_to_incident(r) for r in rows]

    def touch_incident(self, incident_id: str) -> bool:
        """更新 updated_at（聚合併入時刷新時間窗）。"""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE incidents SET updated_at = ? WHERE id = ?",
                (time.time(), incident_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # predictions（分診紀錄；prompt_version 綁定 F16）
    # ------------------------------------------------------------------

    def save_prediction(
        self,
        *,
        incident_id: str,
        prompt_version: str,
        hypotheses: list[dict[str, object]],
        actions: list[dict[str, object]],
        missing_context: list[str],
        tokens_used: int = 0,
    ) -> str:
        import json as _json

        pred_id = f"pred-{uuid.uuid4().hex[:12]}"
        with self._write() as conn:
            conn.execute(
                "INSERT INTO predictions (id, incident_id, prompt_version,"
                " hypotheses_json, actions_json, missing_context_json, tokens_used, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pred_id,
                    incident_id,
                    prompt_version,
                    _json.dumps(hypotheses, ensure_ascii=False),
                    _json.dumps(actions, ensure_ascii=False),
                    _json.dumps(missing_context),
                    tokens_used,
                    time.time(),
                ),
            )
        return pred_id

    # ------------------------------------------------------------------
    # knowledge chunks（memory RAG，§D.1/D.2）
    # ------------------------------------------------------------------

    def insert_knowledge_chunk(
        self,
        *,
        source: str,
        text: str,
        embedding: list[float],
        embedding_provider: str = "hash",
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
        ref_id: str | None = None,
    ) -> KnowledgeChunk:
        import json as _json

        chunk_id = f"kc-{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO knowledge_chunks (id, source, ref_id, text, embedding,"
                " embedding_provider, service, cluster, severity, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    source,
                    ref_id,
                    text,
                    _json.dumps(embedding),
                    embedding_provider,
                    service,
                    cluster,
                    severity,
                    now,
                ),
            )
        return KnowledgeChunk(
            id=chunk_id,
            source=source,
            ref_id=ref_id,
            text=text,
            embedding_vector=embedding,
            embedding_provider=embedding_provider,
            service=service,
            cluster=cluster,
            severity=severity,
            created_at=now,
        )

    def query_knowledge_chunks(
        self,
        *,
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
        time_range: tuple[float, float] | None = None,
        sources: list[str] | None = None,
    ) -> list[KnowledgeChunk]:
        """metadata 等值/區間過濾（§D.1）；相似度計算交給呼叫端。"""
        import json as _json

        clauses = []
        params: list[object] = []
        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if cluster is not None:
            clauses.append("cluster = ?")
            params.append(cluster)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if time_range is not None:
            clauses.append("created_at >= ? AND created_at <= ?")
            params.extend(time_range)
        if sources:
            clauses.append(f"source IN ({','.join('?' for _ in sources)})")
            params.extend(sources)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM knowledge_chunks {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [
            KnowledgeChunk(
                id=r["id"],
                source=r["source"],
                ref_id=r["ref_id"],
                text=r["text"],
                embedding_vector=_json.loads(r["embedding"]),
                embedding_provider=r["embedding_provider"],
                service=r["service"],
                cluster=r["cluster"],
                severity=r["severity"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def delete_knowledge_by_ref(self, source: str, ref_id: str) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_chunks WHERE source = ? AND ref_id = ?",
                (source, ref_id),
            )
            return cur.rowcount

    def count_knowledge_chunks(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()
        return int(row["c"])

    def close(self) -> None:
        self._conn.close()
