"""Schema 向量检索: 基于 Chroma 缓存表结构 / 历史成功代码段, 提升首次生成质量。

向量库不可用时, 调用方应降级为"无 Schema 检索模式", 不影响主流程。
"""
from __future__ import annotations

from typing import List, Optional

import chromadb
from chromadb.api.client import Client

from src.utils.logger import get_logger
from src.utils.metrics import metrics
from src.utils.settings import get_settings

logger = get_logger(__name__)

settings = get_settings()

# 检索结果上限(防止上下文膨胀)
DEFAULT_TOP_K = 3


class SchemaRetriever:
    """Chroma 检索器: collection = schema_history。"""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self._client: Optional[Client] = None
        self._collection = None
        self._host = host or settings.chroma_host
        self._port = port or settings.chroma_port

    @property
    def collection(self):
        """惰性连接: 首次访问时建立(向量库未就绪则抛错, 由调用方降级)。"""
        if self._collection is None:
            self._client = chromadb.HttpClient(host=self._host, port=self._port)
            self._collection = self._client.get_or_create_collection(settings.chroma_collection)
        return self._collection

    def upsert(self, documents: List[str], metadatas: List[dict], ids: List[str]) -> None:
        """写入/更新文档(表结构说明 / 成功代码段)。"""
        self.collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
        logger.info("chroma_upserted", count=len(documents))

    def query(
        self,
        text: str,
        top_k: int = DEFAULT_TOP_K,
        status: str | None = "success",
        required_tables: List[str] | None = None,
    ) -> List[dict]:
        """相似度检索, 返回 [{id, document, metadata, distance}]。

        Args:
            text: 检索文本(必填, 非空)
            top_k: 返回条数, 1 <= top_k <= 20; 非法参数直接拒绝并计入监控指标
            status: 只取指定 metadata.status 的片段; 默认 "success"(负向过滤: 排除历史错误代码, OR-01)
            required_tables: 非空时剔除与所需表无交集的片段(表结构匹配度过滤, OR-01)

        过滤后结果可能少于 top_k(匹配度过滤剔除), 调用方按返回数量使用。
        """
        if not text or not text.strip():
            metrics.inc(
                "tool_param_rejections_total",
                labels={"tool": "schema_retriever.query", "reason": "empty_query"},
            )
            raise ValueError("query text must not be empty")
        if not 1 <= top_k <= 20:
            metrics.inc(
                "tool_param_rejections_total",
                labels={"tool": "schema_retriever.query", "reason": "invalid_top_k"},
            )
            raise ValueError("top_k must be within [1, 20]")

        where = {"status": status} if status else None
        result = self.collection.query(query_texts=[text], n_results=top_k, where=where)
        docs, metas, dists = result["documents"][0], result["metadatas"][0], result["distances"][0]
        ids = result["ids"][0]
        hits = [
            {"id": ids[i], "document": docs[i], "metadata": metas[i] or {}, "distance": dists[i]}
            for i in range(len(docs))
        ]

        # 表结构匹配度过滤(OR-01): 剔除与 required_tables 无交集的片段, 防止复用错误逻辑
        if required_tables:
            rt = set(required_tables)
            hits = [
                h for h in hits
                if rt & set((h["metadata"] or {}).get("required_tables", "").split(","))
            ]
        return hits[:top_k]

    def upsert_success_code(
        self,
        code: str,
        plan_step: str = "",
        required_tables: List[str] | None = None,
        task_id: str = "",
    ) -> None:
        """写入成功代码段(仅 status=success, 供后续任务负向过滤复用)。失败静默降级。"""
        try:
            import hashlib
            import uuid

            doc_id = f"code-{hashlib.md5(code.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:6]}"
            self.upsert(
                documents=[code[:4000]],
                metadatas=[{
                    "status": "success",
                    "step": plan_step[:200],
                    "required_tables": ",".join(required_tables or []),
                    "task_id": task_id,
                }],
                ids=[doc_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma_upsert_success_code_failed", error=str(exc))

    def health(self) -> bool:
        """探测向量库可用性(供降级决策)。"""
        try:
            self.collection.count()
            return True
        except Exception:  # noqa: BLE001
            return False


_retriever: Optional[SchemaRetriever] = None


def get_schema_retriever() -> SchemaRetriever:
    """全局单例。"""
    global _retriever
    if _retriever is None:
        _retriever = SchemaRetriever()
    return _retriever
