"""成本预估(优化方案-熔断时机): Planner 拆解完成后预估任务总成本。

- token 量: planner 自身按 query 长度估算; 后续执行按 plan 步骤数 × coder 历史平均 token
  (无历史时用默认值 1500/500 兜底)
- 单价: 取自 model_routes(按模型名), 无配置时回退 settings 默认单价
- 用途: 预估成本超过 settings.max_estimate_cost 时, 任务转人工审批, 而非执行中熔断
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from src.utils.run_records import get_avg_tokens, get_price
from src.utils.settings import get_settings

settings = get_settings()

# 无历史数据时的默认单步 token 估算(够用即可, 随真实数据自动校准)
_DEFAULT_STEP_PROMPT = 1500
_DEFAULT_STEP_COMPLETION = 500


def estimate_plan_cost(
    plan: list[Dict[str, Any]], user_query: str
) -> Tuple[float, int, int]:
    """预估整个任务的 token 与成本。

    返回: (总成本元, 总prompt tokens, 总completion tokens)
    """
    steps = max(len(plan), 1)

    # 1) Planner 自身: prompt ≈ query 长度×4 + 固定开销, completion ≈ 200
    planner_prompt = len(user_query) * 4 + 500
    planner_completion = 200

    # 2) 执行阶段(每步一次 coder 生成): 历史平均 token 校准
    avg_p, avg_c = get_avg_tokens("coder")
    step_prompt = avg_p if avg_p else float(_DEFAULT_STEP_PROMPT)
    step_completion = avg_c if avg_c else float(_DEFAULT_STEP_COMPLETION)
    exec_prompt = steps * step_prompt
    exec_completion = steps * step_completion

    total_prompt = int(planner_prompt + exec_prompt)
    total_completion = int(planner_completion + exec_completion)

    # 3) 成本: planner 段用 planner 模型价, 执行段用 coder 模型价
    pp = get_price(settings.model_planner)
    cp = get_price(settings.model_coder)
    cost = (
        planner_prompt / 1000 * pp[0]
        + planner_completion / 1000 * pp[1]
        + exec_prompt / 1000 * cp[0]
        + exec_completion / 1000 * cp[1]
    )
    return round(cost, 6), total_prompt, total_completion


def should_escalate_approval(cost: float) -> bool:
    """预估成本是否超上限(熔断时机: 转人工审批而非执行中熔断)。"""
    return cost > settings.max_estimate_cost
