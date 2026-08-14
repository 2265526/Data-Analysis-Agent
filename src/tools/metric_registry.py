"""指标注册器(指标/语义层): 加载、检索、提示词注入、幂等 seed。

设计要点:
- **口径唯一出口**: 聚合逻辑(agg+expr+filter)只来自本注册表, LLM 不自行定义
  指标, 只从目录中选用 —— 消除口径漂移(同一指标不同人算出不同数字)
- 加载: 优先读 DB(metric_definitions, status=active); DB 不可用回退内置默认指标
- 检索: 关键词匹配 name_en/name_cn/alias/description, 命中越多排序越靠前
- 注入: catalog_prompt() 生成最小相关目录片段, 由 coder 拼进提示词
"""
from __future__ import annotations

from typing import Dict, List

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 内置默认指标(演示供应链库口径): DB 无数据/不可用时的兜底
_BUILTIN_METRICS: List[Dict] = [
    {
        "name_en": "sales_7d", "name_cn": "近7天销售额",
        "alias": ["销售额", "近7天销售额", "成交额", "GMV"],
        "description": "最近 7 天内所有已完成订单的销售金额合计",
        "agg": "sum", "expr": "oi.total_item_amount",
        "filter": "o.order_status = '已完成'",
        "unit": "元", "source_tables": ["orders", "order_items"],
        "category": "sales",
    },
    {
        "name_en": "sales_last_week", "name_cn": "上周销售额",
        "alias": ["上周销售额", "前一周销售额"],
        "description": "上一个自然周(7-14 天前)已完成订单销售金额合计",
        "agg": "sum", "expr": "oi.total_item_amount",
        "filter": "o.order_status = '已完成'",
        "unit": "元", "source_tables": ["orders", "order_items"],
        "category": "sales",
    },
    {
        "name_en": "sales_change", "name_cn": "销售额变化",
        "alias": ["销售额变化", "销售额差值", "变化额"],
        "description": "近7天销售额与上周销售额的差值(近7天 - 上周)",
        "agg": "custom", "expr": "sales_7d - sales_last_week",
        "filter": "", "unit": "元", "source_tables": ["orders", "order_items"],
        "category": "sales",
    },
    {
        "name_en": "sales_change_rate", "name_cn": "环比变化率",
        "alias": ["环比变化率", "环比", "变化率", "增长率"],
        "description": "环比变化率(%) = (近7天销售额 - 上周销售额) / 上周销售额 × 100",
        "agg": "custom", "expr": "(sales_7d - sales_last_week) / sales_last_week * 100",
        "filter": "", "unit": "%", "source_tables": ["orders", "order_items"],
        "category": "sales",
    },
    {
        "name_en": "order_count", "name_cn": "订单量",
        "alias": ["订单量", "订单数", "订单总数"],
        "description": "统计周期内去重订单数",
        "agg": "count_distinct", "expr": "o.order_id",
        "filter": "o.order_status = '已完成'",
        "unit": "笔", "source_tables": ["orders"],
        "category": "order",
    },
    {
        "name_en": "avg_order_value", "name_cn": "客单价",
        "alias": ["客单价", "平均订单金额", "客单"],
        "description": "客单价 = 销售额 / 订单量(平均每笔订单金额)",
        "agg": "custom", "expr": "sales_7d / order_count",
        "filter": "", "unit": "元/笔", "source_tables": ["orders", "order_items"],
        "category": "order",
    },
    {
        "name_en": "quantity_sold", "name_cn": "销量",
        "alias": ["销量", "销售数量", "件数"],
        "description": "统计周期内销售商品件数合计",
        "agg": "sum", "expr": "oi.quantity",
        "filter": "o.order_status = '已完成'",
        "unit": "件", "source_tables": ["orders", "order_items"],
        "category": "sales",
    },
]


class MetricRegistry:
    """指标目录: 加载/检索/提示词注入(口径唯一出口)。"""

    def __init__(self, metrics: List[Dict] | None = None) -> None:
        self._metrics: List[Dict] = metrics if metrics is not None else self._load()

    # ------------------------------------------------------------------
    def _load(self) -> List[Dict]:
        """从 DB 加载 active 指标; DB 不可用/无数据回退内置默认。"""
        try:
            from src.api.deps import SessionLocal
            from src.models.metric_definition import MetricDefinition

            db = SessionLocal()
            try:
                rows = (
                    db.query(MetricDefinition)
                    .filter(MetricDefinition.status == "active")
                    .order_by(MetricDefinition.id.asc())
                    .all()
                )
                if rows:
                    return [
                        {
                            "name_en": r.name_en,
                            "name_cn": r.name_cn,
                            "alias": r.alias or [],
                            "description": r.description,
                            "agg": r.agg,
                            "expr": r.expr,
                            "filter": r.filter or "",
                            "unit": r.unit,
                            "source_tables": r.source_tables or [],
                            "category": r.category,
                        }
                        for r in rows
                    ]
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — 指标库不可用不阻塞
            logger.warning("metric_registry_load_failed", error=str(exc)[:200])
        return list(_BUILTIN_METRICS)

    # ------------------------------------------------------------------
    def search(self, query: str, k: int = 6) -> List[Dict]:
        """关键词召回: 匹配 alias/name_cn/name_en/description, 命中越多越靠前。"""
        q = (query or "").lower()
        if not q:
            return self._metrics[:k]
        scored = []
        for m in self._metrics:
            score = 0
            for a in m.get("alias") or []:
                if a and a.lower() in q:
                    score += 3
            if m["name_cn"] and m["name_cn"].lower() in q:
                score += 3
            if m["name_en"] and m["name_en"].lower() in q:
                score += 2
            if m.get("description") and m["description"].lower() in q:
                score += 1
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:k]]

    # ------------------------------------------------------------------
    def catalog_prompt(self, query: str, k: int = 6) -> str:
        """生成注入 LLM 的指标目录提示词片段(锁定口径)。"""
        hits = self.search(query, k=k)
        if not hits:
            return ""
        lines = [
            "以下为平台已锁定的业务指标口径(生成 SQL 时, 涉及这些指标必须严格遵循其口径, "
            "禁止自行定义或改写聚合逻辑):"
        ]
        for m in hits:
            agg_txt = {
                "sum": f"SUM({m['expr']})",
                "count": f"COUNT({m['expr']})",
                "count_distinct": f"COUNT(DISTINCT {m['expr']})",
                "avg": f"AVG({m['expr']})",
                "custom": f"{m['expr']}",
            }.get(m.get("agg", "sum"), f"{m['agg']}({m['expr']})")
            parts = [f"- {m['name_en']}({m['name_cn']}, 单位{m['unit']}): {agg_txt}"]
            if m.get("filter"):
                parts.append(f" 默认过滤: {m['filter']}")
            if m.get("source_tables"):
                parts.append(f" 涉及表: {', '.join(m['source_tables'])}")
            if m.get("description"):
                parts.append(f" 口径说明: {m['description']}")
            lines.append("\n".join(parts))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def ensure_seed(self) -> int:
        """幂等写入内置默认指标(按 name_en 去重), 返回新增条数。"""
        try:
            from src.api.deps import SessionLocal
            from src.models.metric_definition import MetricDefinition

            db = SessionLocal()
            added = 0
            try:
                existing = {r.name_en for r in db.query(MetricDefinition).all()}
                for m in _BUILTIN_METRICS:
                    if m["name_en"] in existing:
                        continue
                    db.add(
                        MetricDefinition(
                            name_en=m["name_en"],
                            name_cn=m["name_cn"],
                            alias=m.get("alias") or [],
                            description=m.get("description", ""),
                            agg=m.get("agg", "sum"),
                            expr=m.get("expr", ""),
                            filter=m.get("filter", ""),
                            unit=m.get("unit", ""),
                            source_tables=m.get("source_tables") or [],
                            category=m.get("category", "general"),
                        )
                    )
                    added += 1
                db.commit()
            finally:
                db.close()
            return added
        except Exception as exc:  # noqa: BLE001
            logger.warning("metric_seed_failed", error=str(exc)[:200])
            return 0


_registry: MetricRegistry | None = None


def get_metric_registry() -> MetricRegistry:
    """单例注册器(进程内缓存, 指标库变更后重启生效)。"""
    global _registry
    if _registry is None:
        _registry = MetricRegistry()
    return _registry


def reload_metric_registry() -> None:
    """指标库变更后重载: CRUD API 调用, 使口径变更对 LLM 立即生效(无需重启)。"""
    global _registry
    _registry = MetricRegistry()
