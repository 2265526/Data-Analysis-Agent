"""多轮对话上下文窗口管理(三层结构 + token 预算 + 摘要压缩 + 结果集防塞入)。

借鉴 Letta/MemGPT 记忆分层 + LangChain SummarizationMiddleware(trigger+keep) +
本项目分级模型(qwen-flash 做压缩), 把会话历史按节点差异化、受预算约束地注入 LLM。

三层结构:
- L1 固定系统层: 各节点 system prompt + 指标语义层目录(节点内拼接, 此处不管理)
- L2 滑动工作层: 最近 N 轮原始用户消息 + 最近助手结论摘要(截断)
- L3 压缩记忆层: 更早历史摘要(qwen-flash) + 累积筛选条件/口径(规则优先 + qwen-flash 兜底)

三道硬阈值防线:
1. 轮次维度: 滑动层最多保留 context_sliding_turns 轮(用户+助手), 更早的移入 L3
2. Token 维度: L3 内容超过 context_summary_trigger_tokens 时用 qwen-flash 摘要压缩
3. 结果集丢弃: 历史任务报告/report_snapshot 进上下文只截 context_result_max_chars 字符

所有 DB/LLM 访问均降级容忍: 历史读不到/摘要失败时返回空上下文, 绝不阻断主流程。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# 规则提取词表(零 LLM 成本): 累积筛选条件/口径
# ---------------------------------------------------------------------------
_REGION_KEYWORDS = [
    "华南", "华东", "华北", "华中", "西南", "东北", "西北",
    "广东", "广西", "海南", "福建", "江苏", "浙江", "上海", "安徽", "江西", "山东",
    "北京", "天津", "河北", "山西", "河南", "湖北", "湖南", "四川", "重庆", "云南",
    "贵州", "陕西", "辽宁", "吉林", "黑龙江", "新疆", "内蒙", "深圳", "广州", "杭州",
]
_STATUS_KEYWORDS = [
    "已完成", "待支付", "已支付", "已取消", "已发货", "已签收", "退货", "退款", "售后",
    "失效", "有效", "退款中", "待发货",
]
_TIME_KEYWORDS = [
    "近7天", "近30天", "近90天", "近一年", "最近7天", "最近30天", "上周", "本周",
    "上月", "本月", "去年", "今年", "今天", "昨天", "近一周", "近一个月",
]
_DIMENSION_KEYWORDS = [
    "品类", "区域", "地区", "渠道", "门店", "品牌", "城市", "省份", "人群", "客群",
    "一级品类", "二级品类", "产品", "商品",
]


def _extract_filters_rules(texts: List[str]) -> List[str]:
    """规则提取累积筛选条件: 按类别聚合, 去重, 每类最多取前 3 个。

    输入为历史用户消息文本列表(不含当前 query)。零 LLM 成本。
    """
    found: Dict[str, List[str]] = {"地域": [], "状态": [], "时间": [], "维度": []}
    for t in texts:
        if not t:
            continue
        for kw in _REGION_KEYWORDS:
            if kw in t and kw not in found["地域"]:
                found["地域"].append(kw)
        for kw in _STATUS_KEYWORDS:
            if kw in t and kw not in found["状态"]:
                found["状态"].append(kw)
        for kw in _TIME_KEYWORDS:
            if kw in t and kw not in found["时间"]:
                found["时间"].append(kw)
        for kw in _DIMENSION_KEYWORDS:
            if kw in t and kw not in found["维度"]:
                found["维度"].append(kw)
    parts: List[str] = []
    for cat, vals in found.items():
        if vals:
            parts.append(f"{cat}={'、'.join(vals[:3])}")
    return parts


# ---------------------------------------------------------------------------
# token 估算与截断
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """近似 token 估算(中英混合: 1 token ≈ 0.6 字符, 系数可配)。

    不引入 tiktoken(deepseek/qwen tokenizer 各自不同), 用字符数近似足够做预算控制。
    """
    if not text:
        return 0
    return max(1, int(len(text) * settings.context_token_per_char))


def _truncate(text: str, max_chars: int) -> str:
    """截断文本(结果集防塞入), 超长加省略标记。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…(已截断)"


def _trim_to_budget(text: str, token_budget: int) -> str:
    """按 token 预算裁剪文本(从头保留, 超出部分丢弃)。"""
    if not text or token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    # 按比例截断到预算内(留 5% 余量)
    max_chars = int(token_budget / settings.context_token_per_char * 0.95)
    return _truncate(text, max_chars)


# ---------------------------------------------------------------------------
# qwen-flash 摘要/提取(兜底, 延迟导入避免循环依赖)
# ---------------------------------------------------------------------------
def _llm_summarize_conversation(history_text: str, task_id: Optional[str] = None) -> str:
    """qwen-flash 把较早对话历史压缩为要点摘要(L3 压缩层)。失败回退截断。"""
    try:
        from src.nodes import make_llm  # 延迟导入, 避免循环依赖

        llm = make_llm(settings.model_aux, temperature=0, node="aux_context_summarize")
        out = llm.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "你是数据分析对话的记忆压缩器。把下面的对话历史压缩成要点摘要, 保留:\n"
                        "- 用户的分析目标\n"
                        "- 已使用/累积的筛选条件(地区/时间/状态/品类/渠道等)\n"
                        "- 已得出的关键结论或数字\n"
                        "用中文, 3-6 条要点, 简洁。"
                    ),
                },
                {"role": "user", "content": history_text[:6000]},
            ],
            task_id=task_id,
        ).content.strip()
        return out[:1200]
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_summarize_failed", error=str(exc)[:200])
        return _truncate(history_text, 1200)


def _llm_extract_filters(texts: List[str], task_id: Optional[str] = None) -> List[str]:
    """qwen-flash 兜底: 从历史用户消息中抽取累积筛选条件(规则未命中时)。"""
    try:
        from src.nodes import make_llm  # 延迟导入, 避免循环依赖

        llm = make_llm(settings.model_aux, temperature=0, node="aux_context_filters")
        joined = "\n".join(f"- {t[:200]}" for t in texts[-8:] if t)
        out = llm.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "从用户的历史提问中抽取仍在生效的筛选条件/口径(如 地域、时间范围、"
                        "订单状态、品类/渠道等维度)。每行输出一条, 格式: 类别=值。"
                        "若无有效筛选条件, 输出空。"
                    ),
                },
                {"role": "user", "content": joined},
            ],
            task_id=task_id,
        ).content.strip()
        lines = [ln.strip(" -•") for ln in out.splitlines() if ln.strip()]
        return [ln for ln in lines if "=" in ln or "：" in ln][:6]
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_filters_llm_failed", error=str(exc)[:200])
        return []


# ---------------------------------------------------------------------------
# 历史加载
# ---------------------------------------------------------------------------
def load_session_history(session_id: int, current_query: str = "") -> List[Dict[str, Any]]:
    """读取会话历史消息(时间正序), 排除与当前 query 相同的用户消息。

    结果集防塞入: assistant 任务消息(报告正文)只取前 context_result_max_chars 字符。
    DB 不可用/会话不存在返回空列表。
    """
    if not session_id:
        return []
    try:
        from src.api.deps import SessionLocal
        from src.models.chat_session import ChatMessage

        db = SessionLocal()
        try:
            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            messages: List[Dict[str, Any]] = []
            for m in rows:
                content = (m.content or "").strip()
                if m.role == "user" and content == current_query.strip():
                    continue  # 排除当前这轮输入, 它不属于"历史"
                if not content:
                    continue
                # 结果集防塞入: 历史报告/长回复只保留开头
                if m.role == "assistant":
                    content = _truncate(content, settings.context_result_max_chars)
                else:
                    content = _truncate(content, 2000)
                messages.append({"role": m.role, "type": m.type or "text", "content": content})
            return messages
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("context_history_load_failed", session_id=session_id, error=str(exc)[:200])
        return []


# ---------------------------------------------------------------------------
# 上下文构建(入口一次) 与 节点格式化(按预算)
# ---------------------------------------------------------------------------
def build_context_raw(
    session_id: Optional[int],
    current_query: str = "",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """在任务入口调用一次: 读历史 -> 分层 -> 提取 -> 返回原料(不按节点裁剪)。

    返回 dict(可 JSON 序列化, 供 LangGraph state 持久化):
      {
        "recent_user": [...],       # L2 滑动层: 最近 N 条用户消息原文
        "recent_assistant": [...],  # L2 滑动层: 最近 N 条助手结论(已截断)
        "filters": [...],           # L3 累积筛选条件/口径(规则优先 + qwen-flash 兜底)
        "summary": str,             # L3 更早历史摘要(qwen-flash, 超阈值触发)
      }
    """
    if not session_id or not settings.context_window_enabled:
        return {}
    try:
        messages = load_session_history(session_id, current_query)
        if not messages:
            return {}

        turns = max(1, settings.context_sliding_turns)
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        assistant_msgs = [m["content"] for m in messages if m["role"] == "assistant"]

        # L2 滑动层: 最近 N 条; 更早的移入 L3
        recent_user = user_msgs[-turns:]
        recent_assistant = assistant_msgs[-turns:]
        older_user = user_msgs[:-turns]
        older_assistant = assistant_msgs[:-turns]

        # L3 累积筛选条件: 规则优先(全部历史用户消息), 未命中且有跨轮历史时 qwen-flash 兜底
        filters = _extract_filters_rules(user_msgs)
        if not filters and len(user_msgs) >= 2:
            filters = _llm_extract_filters(user_msgs, task_id=task_id)

        # L3 更早历史摘要: 超出滑动窗口的部分; 超阈值用 qwen-flash 压缩, 否则截断保留
        summary = ""
        older_parts = []
        for u in older_user:
            older_parts.append(f"用户: {u}")
        for a in older_assistant:
            older_parts.append(f"助手: {a}")
        if older_parts:
            older_text = "\n".join(older_parts)
            if estimate_tokens(older_text) > settings.context_summary_trigger_tokens:
                summary = _llm_summarize_conversation(older_text, task_id=task_id)
            else:
                summary = _truncate(older_text, 3000)

        return {
            "recent_user": recent_user,
            "recent_assistant": recent_assistant,
            "filters": filters,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001 — 上下文构建失败不影响主流程
        logger.warning("context_build_failed", session_id=session_id, error=str(exc)[:200])
        return {}


def _budget_for(node: str) -> int:
    """节点 -> 对话上下文注入的 token 预算。"""
    budgets = {
        "planner": settings.context_budget_planner_tokens,
        "coder": settings.context_budget_coder_tokens,
        "supervisor": settings.context_budget_supervisor_tokens,
        "reporter": settings.context_budget_reporter_tokens,
    }
    return int(budgets.get(node, settings.context_budget_planner_tokens))


def format_context(
    raw: Optional[Dict[str, Any]],
    node: str = "planner",
    task_id: Optional[str] = None,  # noqa: ARG001 — 预留: 后续按节点二次裁剪
) -> str:
    """把入口构建的原料按节点预算拼成可注入 user_content 的文本。

    节点差异(谁需要谁才带):
    - planner: 累积筛选条件 + 最近用户消息 + 更早摘要(理解追问意图)
    - coder:   以上 + 最近助手结论(上轮结果), 预算更大
    - reporter/supervisor: 基本不注入(靠 exec_result/单条 query 即可)
    """
    if not raw:
        return ""

    parts: List[str] = []

    # L3 累积筛选条件/口径 —— 跨轮记忆的核心, 所有需要它的节点都带上
    filters = raw.get("filters") or []
    if filters:
        parts.append(
            "【历史累积的筛选条件/口径(继续沿用, 除非用户本轮明确更改)】\n"
            + "\n".join(f"- {f}" for f in filters)
        )

    # L2 滑动层: 最近用户消息(理解"再/接着/改成..."等追问)
    recent_user = raw.get("recent_user") or []
    if recent_user:
        parts.append(
            "【最近对话上文】\n" + "\n".join(f"- 用户: {u}" for u in recent_user)
        )

    # coder 额外带最近助手结论(上轮结果/口径参考)
    if node == "coder":
        recent_assistant = raw.get("recent_assistant") or []
        if recent_assistant:
            parts.append(
                "【上轮分析结论】\n" + "\n".join(f"- {a}" for a in recent_assistant)
            )

    # L3 更早历史摘要
    summary = raw.get("summary") or ""
    if summary:
        parts.append("【更早对话摘要】\n" + summary)

    text = "\n\n".join(parts)
    return _trim_to_budget(text, _budget_for(node))
