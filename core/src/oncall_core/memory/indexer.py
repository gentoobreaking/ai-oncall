"""入库路徑（algs/knowledge-flywheel.md §D.2 三來源 + §D.5 遮蔽）。

三個來源各有專屬方法（介面明確、各自可測）：
  - index_postmortem()：resolved 後人工修訂結論
  - index_override()：人類否決 AI 建議時的一句話「實際做法/原因」
  - index_runbook()/reindex_runbooks()：runbook 目錄檔更新觸發重新索引

所有內容入庫前強制過 redact_text()（§D.5）。
"""

from __future__ import annotations

from pathlib import Path

from oncall_core.memory.embeddings import EmbeddingProvider, HashEmbeddingProvider
from oncall_core.redact import redact_text
from oncall_core.store import KnowledgeChunk, Store


class KnowledgeIndexer:
    def __init__(self, store: Store, provider: EmbeddingProvider | None = None) -> None:
        self._store = store
        self._provider = provider or HashEmbeddingProvider()

    def _index(
        self,
        *,
        source: str,
        text: str,
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
        ref_id: str | None = None,
    ) -> KnowledgeChunk:
        # §D.5：入库前強制遮蔽——金鑰樣式不得進入向量庫
        safe_text = redact_text(text)
        vec = self._provider.embed(safe_text)
        return self._store.insert_knowledge_chunk(
            source=source,
            text=safe_text,
            embedding=vec,
            embedding_provider=self._provider.name,
            service=service,
            cluster=cluster,
            severity=severity,
            ref_id=ref_id,
        )

    # ------------------------------------------------------------------
    # §D.2 來源一：postmortem 定稿（F8/F9）
    # ------------------------------------------------------------------

    def index_postmortem(
        self,
        *,
        incident_id: str,
        conclusion: str,
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
    ) -> KnowledgeChunk:
        return self._index(
            source="postmortem",
            text=conclusion,
            service=service,
            cluster=cluster,
            severity=severity,
            ref_id=incident_id,
        )

    # ------------------------------------------------------------------
    # §D.2 來源二：即時 override（人類否決 AI 建議，F9 最貴養分）
    # ------------------------------------------------------------------

    def index_override(
        self,
        *,
        incident_id: str,
        actual_action: str,
        service: str | None = None,
        cluster: str | None = None,
        severity: str | None = None,
    ) -> KnowledgeChunk:
        """拒絕當下的一句話「實際做法/原因」——即時入库。"""
        return self._index(
            source="override",
            text=f"override: {actual_action}",
            service=service,
            cluster=cluster,
            severity=severity,
            ref_id=incident_id,
        )

    # ------------------------------------------------------------------
    # §D.2 來源三：runbook 變更（目錄掃描重新索引）
    # ------------------------------------------------------------------

    def index_runbook(self, *, name: str, content: str) -> KnowledgeChunk:
        return self._index(source="runbook", text=content, ref_id=name)

    def reindex_runbooks(self, runbook_dir: str | Path) -> list[KnowledgeChunk]:
        """掃描 runbook 目錄（*.yml/*.yaml/*.md），全量重新索引。"""
        root = Path(runbook_dir)
        if not root.is_dir():
            return []
        chunks: list[KnowledgeChunk] = []
        for path in sorted(root.iterdir()):
            if path.suffix.lower() not in {".yml", ".yaml", ".md"}:
                continue
            self._store.delete_knowledge_by_ref("runbook", path.stem)
            content = path.read_text(encoding="utf-8")
            if content.strip():
                chunks.append(self.index_runbook(name=path.stem, content=content))
        return chunks
