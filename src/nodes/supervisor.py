"""Supervisor 节点: 根据用户意图路由到 Planner / Reporter / FINISH。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.nodes import make_llm
from src.state import PipelineState, ROUTE_FINISH, ROUTE_PLANNER, ROUTE_REPORTER
from src.utils.logger import get_logger
from src.utils.settings import get_settings
from src.utils.structured_json import invoke_structured

logger = get_logger(__name__)
settings = get_settings()

_SYSTEM_PROMPT = """你是数据分析平台的路由器(Supervisor)。
判断用户需求属于哪类任务:
- planner: 需要**查询数据库/执行分析**的任务。只要用户要"统计/查询/计算/分析/看数据",涉及任何业务数字(销售额/数量/占比/趋势/TOP/对比/最近/上周/本月等),都必须是 planner —— 平台必须真的去查库,不能凭空回答
- reporter: 仅对**本任务已生成的结果**做二次汇总/追问(如"把报告精简一下"),或用户明确说"只要答案不用报告"且该答案已由前面步骤产出
- FINISH: 纯闲聊/问候/与数据无关的话题(如"你好""谢谢""今天天气")
判定原则:拿不准就选 planner(宁可多查,不可漏查)。
只输出 JSON, 不要多余内容。"""


def _looks_like_data_query(query: str) -> bool:
    """数据意图词规则前置: 命中即强制走 planner(LLM 分类不可靠, 防止'统计X'被误判为 reporter)。"""
    if not query:
        return False
    triggers = (
        "统计", "多少", "几个", "查询", "分析", "计算", "销售额", "销量", "数量", "金额",
        "占比", "趋势", "top", "对比", "环比", "同比", "排名", "排行", "最高", "最低",
        "最近", "上周", "本周", "本月", "上个月", "今年", "平均", "总和", "合计", "客户", "订单",
        "商品", "品类", "库存", "利润", "成本", "毛利", "增长", "下降", "分布", "明细", "列表",
    )
    q = query.lower()
    return any(t in q for t in triggers)


class SupervisorOutput(BaseModel):
    """结构化输出约束: 强制 JSON, 杜绝解析报错。"""

    route: Literal[ROUTE_PLANNER, ROUTE_REPORTER, ROUTE_FINISH] = Field(
        description="路由目标: planner / reporter / FINISH"
    )
    reason: str = Field(default="", description="路由决策依据(一句话)")


def supervisor_node(state: PipelineState) -> dict:
    """根据 user_query 输出下一步路由目标。"""
    user_query = state.get("user_query", "").strip()

    # 数据意图词规则前置: 命中即强制走完整流水线(防止"统计X"被 LLM 误判为 reporter 空答)
    if _looks_like_data_query(user_query):
        logger.info("supervisor_data_intent", reason="命中数据意图词, 强制走 planner")
        return {"route": ROUTE_PLANNER}

    # 低成本模型做三分类(输入多输出少, 最省); make_llm 构造失败同样降级, 不阻断流水线
    try:
        llm = make_llm(settings.model_supervisor, temperature=0, node="supervisor")
        out: SupervisorOutput = invoke_structured(
            llm,
            SupervisorOutput,
            [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_query}],
            task_id=state.get("task_id"),
        )
        route = out.route
        logger.info("supervisor_routed", route=route, reason=out.reason)
    except Exception as exc:  # noqa: BLE001 — LLM 不可用时降级
        logger.warning("supervisor_fallback", error=str(exc))
        route = ROUTE_PLANNER  # 降级: 默认走完整流水线

    # 闲聊(FINISH): 直接返回回复并完成, 不再进入 planner/coder/executor,
    # 避免闲聊任务产出空白报告或挂起澄清
    if route == ROUTE_FINISH:
        from src.utils.chat_gate import chat_reply

        return {
            "route": route,
            "chat_reply": chat_reply(user_query),
            "status": "completed",
            "progress": "task_completed",
            "progress_detail": "已识别为闲聊, 直接回复",
            "progress_percent": 100,
        }

    return {"route": route}
