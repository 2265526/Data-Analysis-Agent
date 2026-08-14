"""Planner 节点: 将自然语言需求拆解为可执行步骤清单(JSON)。"""
from __future__ import annotations

import hashlib
from typing import List

from pydantic import BaseModel, Field

from src.nodes import make_llm
from src.state import PipelineState
from src.utils.logger import get_logger
from src.utils.settings import get_settings
from src.utils.structured_json import invoke_structured

logger = get_logger(__name__)
settings = get_settings()

_SYSTEM_PROMPT = """你是数据分析师, 将用户需求拆解为可执行步骤。
要求:
- **能用一条 SQL 完成的聚合/对比/过滤需求, 只生成 1 个步骤**(直接产出最终结果);
  严禁拆成"提取数据/关联表/聚合计算"等中间步骤——那会导致每条步骤都被重复执行出相同结果
- 仅当需求确实需要多阶段处理(如: 先算 A 再基于 A 分群、多主题分析)时才拆分为多个步骤
- 每步指明依赖(依赖哪些前置步骤)与所需数据表
- 步骤之间可并行(无依赖)则并行, 否则串行
- 若需求缺少关键指标口径(如留存率的分母、时间窗口、维度定义), 请降低 confidence 并在 questions 中列出需要澄清的问题
- 只输出 JSON, 不要多余内容
输出必须严格使用以下字段名与结构(直接按此模板输出, 字段名一个都不能改):

{
  "tasks": [
    {
      "step": "步骤名(简短动词短语)",
      "description": "步骤具体说明",
      "dependencies": ["依赖的前置步骤名"],
      "required_tables": ["所需数据表名"]
    }
  ],
  "confidence": 0.9,
  "questions": []
}

示例(针对"统计近7天各品类销售额,对比上周变化" —— 一条 SQL 可完成, 只生成 1 步):
{
  "tasks": [
    {
      "step": "计算各品类销售额对比",
      "description": "关联订单明细与产品表, 按一级品类聚合近7天与上周销售额并计算变化",
      "dependencies": [],
      "required_tables": ["orders", "order_items", "products"]
    }
  ],
  "confidence": 0.9,
  "questions": []
}"""


class PlanItem(BaseModel):
    """单个可执行步骤。"""

    step: str = Field(description="步骤名, 如 '计算近7天留存率'")
    description: str = Field(description="步骤具体说明")
    dependencies: List[str] = Field(default_factory=list, description="依赖的前置步骤名")
    required_tables: List[str] = Field(default_factory=list, description="所需数据表")


class PlanOutput(BaseModel):
    """结构化输出: 任务拆解清单。"""

    tasks: List[PlanItem]
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="拆解置信度 0-1; 需求歧义/缺口径时 < 0.6 需澄清(OR-02)",
    )
    questions: List[str] = Field(
        default_factory=list, description="需要向用户澄清的问题(缺口径时填写, 否则为空)"
    )


def planner_node(state: PipelineState) -> dict:
    """拆解任务, 写入 plan 并初始化执行游标。"""
    user_query = state.get("user_query", "")
    # 从状态里已有的错误上下文(逻辑错误重规划时带回来)
    error_context = state.get("error_log", "")
    # OR-02: 澄清回填(用户补充的口径), 存在时重新拆解且不读缓存/不再触发澄清
    clarify_answer = (state.get("clarify_answer") or "").strip()
    # 多轮上下文(上下文窗口管理): 有历史上下文时跳过缓存(上下文跨会话动态, 缓存会串)
    conversation_context = state.get("conversation_context") or {}
    has_context = bool(conversation_context)

    # OR-06 结果缓存: 同需求(且非重规划/非澄清重拆/无多轮上下文)直接复用拆解结果, 省一次 LLM 调用
    # 缓存 key 带 PLAN_CACHE_VERSION: 提示词/策略变更时 bump 版本, 避免命中旧 plan
    # (如: 单步骤策略上线后, 旧 6 步 plan 缓存必须作废)
    PLAN_CACHE_VERSION = "v2"
    plan: list[dict] | None = None
    out: "PlanOutput | None" = None
    cache_key: str | None = None
    query_hash = ""
    if not error_context and not clarify_answer and not has_context:
        from src.utils.cache import cache_get_json, cache_set_json

        query_hash = hashlib.md5(user_query.encode("utf-8")).hexdigest()
        cache_key = f"planner:{PLAN_CACHE_VERSION}:{query_hash}"
        plan = cache_get_json(cache_key)
        if plan is not None:
            logger.info("planner_cache_hit", query_hash=query_hash[:8])

    if plan is None:
        user_content = f"需求: {user_query}"
        if error_context:
            user_content += f"\n\n上次执行失败, 请重新规划: {error_context[:1000]}"
        if clarify_answer:
            user_content += f"\n\n用户补充的口径(澄清): {clarify_answer[:500]}"
        # 多轮上下文: 注入累积筛选条件 + 最近上文 + 更早摘要(理解追问意图)
        if has_context:
            from src.utils.context_window import format_context

            ctx_text = format_context(conversation_context, node="planner")
            if ctx_text:
                user_content += f"\n\n{ctx_text}"

        try:
            llm = make_llm(settings.model_planner, temperature=0.1, node="planner")
            out: PlanOutput = invoke_structured(
                llm,
                PlanOutput,
                [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                task_id=state.get("task_id"),
            )
            plan = [item.model_dump() for item in out.tasks]
            logger.info("planner_finished", task_count=len(plan), confidence=out.confidence)
        except Exception as exc:  # noqa: BLE001
            logger.error("planner_failed", error=str(exc))
            # 结构化输出连续失败: 降级为单步骤默认计划, 保证流水线可继续
            plan = [
                {
                    "step": "执行用户需求",
                    "description": user_query,
                    "dependencies": [],
                    "required_tables": [],
                }
            ]
            out = None

        # 写缓存(仅首次生成且非重规划/非澄清重拆; 多轮上下文场景 cache_key 未定义, 不写缓存)
        if plan is not None and not error_context and not clarify_answer and cache_key is not None:
            from src.utils.cache import cache_set_json

            cache_set_json(cache_key, plan)
            logger.info("planner_cache_set", query_hash=query_hash[:8])

    # OR-02 需求澄清: confidence < 0.6 或 LLM 认为缺口径 -> 路由 Clarifier 向用户提问
    need_clarify = (
        not error_context
        and not clarify_answer
        and out is not None
        and (out.confidence < 0.6 or bool(out.questions))
    )
    if need_clarify:
        questions = out.questions or ["请补充关键指标口径(如时间窗口、口径定义、维度)"]
        logger.info("planner_need_clarify", confidence=out.confidence, questions=questions)
        return {
            "plan": plan,
            "current_task_index": 0,
            "retry_count": 0,
            "error_log": "",
            # 澄清挂起等同待审批: 前端识别 awaiting_approval 才显示等待态, 审批中心可处理
            "status": "awaiting_approval",
            "route": "clarifier",
            "clarify_questions": questions,
            "progress": "awaiting_clarify",
            "progress_detail": "需求存在歧义, 等待用户澄清",
            "progress_percent": 15,
        }

    # 熔断时机(优化方案-成本核算): Planner 后按单价+历史平均token预估总成本,
    # 超上限转人工审批, 而非执行中熔断
    from src.utils.costing import estimate_plan_cost, should_escalate_approval
    from src.utils.run_records import record_estimate

    estimated_cost, est_prompt, est_completion = estimate_plan_cost(plan, user_query)
    task_id = state.get("task_id")
    if task_id:
        record_estimate(task_id, "planner", settings.model_planner, est_prompt, est_completion)

    if should_escalate_approval(estimated_cost):
        logger.info("planner_cost_escalated", estimated_cost=estimated_cost)
        return {
            "plan": plan,
            "current_task_index": 0,
            "retry_count": 0,
            "error_log": "",
            "status": "awaiting_approval",
            "approval_reason": "cost_estimate",
            "estimated_cost": estimated_cost,
            "progress": "awaiting_approval",
            "progress_detail": f"预估成本 ¥{estimated_cost} 超过上限 ¥{settings.max_estimate_cost}, 等待人工审批",
            "progress_percent": 30,
        }

    return {
        "plan": plan,
        "current_task_index": 0,
        "retry_count": 0,
        "error_log": "",
        "status": "running",
    }
