"""Postmortem 草稿與 action items 追蹤（F8/F19、§D.2）。

- resolved 後彙整時間線 → Markdown 草稿（時間線／根因人工修正欄／動作紀錄／影響範圍）
- 草稿修正事項自動建追蹤清單（負責人/期限/狀態），逾期未結提醒
- 定稿：Markdown commit 至 incidents repo ＋ 結論入库 RAG（知識飛輪）
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from oncall_core.incident.hashchain import HashChain
from oncall_core.logging import get_logger
from oncall_core.memory.indexer import KnowledgeIndexer
from oncall_core.store import Store

log = get_logger(__name__)


class ActionItemStatus:
    OPEN = "open"
    DONE = "done"
    WONT_DO = "wont_do"


@dataclass(slots=True)
class ActionItem:
    id: str
    incident_id: str
    description: str
    owner: str
    due_ts: float | None
    status: str
    created_at: float


class PostmortemWriter:
    def __init__(
        self,
        store: Store,
        indexer: KnowledgeIndexer,
        *,
        incidents_repo_dir: str | Path | None = None,
    ) -> None:
        self._store = store
        self._chain = HashChain(store)
        self._indexer = indexer
        self._repo_dir = Path(incidents_repo_dir) if incidents_repo_dir else None

    # ------------------------------------------------------------------
    # 草稿生成
    # ------------------------------------------------------------------

    def draft(
        self,
        incident_id: str,
        *,
        impact: str = "",
        root_cause_manual: str = "",
    ) -> Path:
        """彙整時間線與分診紀錄產生 Markdown 草稿；回傳草稿路徑。"""
        incident = self._store.get_incident(incident_id)
        if incident is None:
            raise ValueError(f"unknown incident {incident_id}")

        lines: list[str] = [
            f"# Postmortem draft - {incident_id}",
            f"- date: {time.strftime('%Y-%m-%d', time.gmtime(incident.created_at))}",
            f"- service: {incident.labels.get('service', 'unknown')}",
            f"- status: {incident.status}",
            "",
            "## Timeline",
        ]
        for row in self._store.timeline(incident_id):
            ts = time.strftime("%H:%M:%S", time.gmtime(row["created_at"]))
            payload = json.loads(row["payload_json"])
            summary = payload.get("summary") or payload.get("reason") or payload.get("to") or ""
            lines.append(f"- `{ts}` {row['kind']} {summary}".rstrip())

        # 最新分診報告（根因假設）
        prediction = self._store.latest_prediction(incident_id)
        lines += ["", "## Root cause hypotheses (AI, 需人工確認)"]
        if prediction is not None:
            for h in json.loads(prediction["hypotheses_json"]):
                conf = h.get("confidence", "?")
                lines.append(f"- {h.get('cause')} (confidence={conf})")
        else:
            lines.append("- (無分診紀錄)")
        # §F8：人工修正欄——定稿前必填
        lines += [
            "",
            "## Root cause (manual)",
            root_cause_manual or "<TODO-人工修正後的根因>",
        ]

        # 動作紀錄（執行器步驟）
        lines += ["", "## Actions taken"]
        exec_events = [
            r
            for r in self._store.timeline(incident_id)
            if r["kind"].startswith("step_") or r["kind"] == "execution_started"
        ]
        if exec_events:
            lines.extend(
                f"- {r['kind']}: {json.loads(r['payload_json']).get('step', '')}"
                for r in exec_events
            )
        else:
            lines.append("- (無自動執行動作)")

        lines += ["", "## Impact", impact or "<TODO-影響範圍>"]

        # 關聯 action items
        items = self.list_action_items(incident_id)
        if items:
            lines += ["", "## Action items"]
            lines.extend(f"- [{it.status}] {it.description} (owner={it.owner})" for it in items)

        path = self._draft_path(incident_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("postmortem drafted", incident_id=incident_id, path=str(path))
        return path

    def _draft_path(self, incident_id: str) -> Path:
        base = self._repo_dir or Path("incidents_repo")
        return base / f"{incident_id}.md"

    # ------------------------------------------------------------------
    # Action items CRUD（F19）
    # ------------------------------------------------------------------

    def add_action_item(
        self, incident_id: str, *, description: str, owner: str, due_ts: float | None = None
    ) -> str:
        item_id = f"act-{uuid.uuid4().hex[:10]}"
        with self._store._write() as conn:
            conn.execute(
                "INSERT INTO action_items (id, incident_id, description, owner,"
                " due_ts, status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (item_id, incident_id, description, owner, due_ts, time.time()),
            )
        return item_id

    def update_action_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        due_ts: float | None = None,
    ) -> bool:
        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if owner is not None:
            sets.append("owner = ?")
            params.append(owner)
        if due_ts is not None:
            sets.append("due_ts = ?")
            params.append(due_ts)
        if not sets:
            return False
        with self._store._write() as conn:
            cur = conn.execute(
                f"UPDATE action_items SET {', '.join(sets)} WHERE id = ?",
                (*params, item_id),
            )
            return cur.rowcount > 0

    def get_action_item(self, item_id: str) -> ActionItem | None:
        row = self._store.get_action_item(item_id)
        return self._row_to_item(row) if row else None

    def list_action_items(self, incident_id: str | None = None) -> list[ActionItem]:
        rows = self._store.list_action_items(incident_id)
        return [self._row_to_item(r) for r in rows]

    def overdue_items(self, now: float | None = None) -> list[ActionItem]:
        now = now or time.time()
        return [self._row_to_item(r) for r in self._store.overdue_action_items(now)]

    def remind_overdue(self, notify: object = None) -> list[ActionItem]:
        """逾期未結提醒：回傳逾期清單；notify 為 callable(item) 時逐一提醒。"""
        overdue = self.overdue_items()
        if callable(notify):
            for item in overdue:
                notify(item)
        log.info("overdue reminders sent", count=len(overdue))
        return overdue

    @staticmethod
    def _row_to_item(r) -> ActionItem:
        return ActionItem(
            id=r["id"],
            incident_id=r["incident_id"],
            description=r["description"],
            owner=r["owner"],
            due_ts=r["due_ts"],
            status=r["status"],
            created_at=r["created_at"],
        )

    # ------------------------------------------------------------------
    # 定稿：commit 至 incidents repo ＋ 入庫 RAG（§D.2）
    # ------------------------------------------------------------------

    def finalize(
        self,
        incident_id: str,
        *,
        conclusion: str,
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
    ) -> str | None:
        """結論入库 RAG；若設定 incidents repo 則 git commit 定稿檔。"""
        chunk = self._indexer.index_postmortem(
            incident_id=incident_id,
            conclusion=conclusion,
            service=service,
            cluster=cluster,
            severity=severity,
        )
        self._chain.append(incident_id, "postmortem_finalized", {"ref": chunk.id})
        commit_hash: str | None = None
        if self._repo_dir is not None and self._repo_dir.is_dir():
            path = self._draft_path(incident_id)
            if path.exists():
                commit_hash = self._git_commit(path, f"postmortem: {incident_id}")
        return commit_hash

    def _git_commit(self, path: Path, message: str) -> str | None:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(self._repo_dir), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("add", str(path.name))
        try:
            git("commit", "-m", message)
        except subprocess.CalledProcessError:
            return None  # 無變更可提交（冪等）
        return git("rev-parse", "HEAD")
