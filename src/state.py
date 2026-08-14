"""全局状态定义(LangGraph State Schema)。

所有节点共享同一个 TypedDict 状态,通过 Checkpointer 持久化,
支持任务中断后从上次节点恢复(断点续跑)。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, TypedDict


class PipelineState(TypedDict, total=False):
    """一次数据分析任务的完整共享状态。

    - user_query:         用户自然语言需求
    - plan:               任务拆解清单, 每项含 step/依赖/所需表
    - current_task_index: 当前执行到第几步
    - code:               当前待执行的 SQL/Python 代码
    - exec_result:        沙箱执行输出(摘要, 大结果集只传摘要)
    - error_log:          最近一次错误信息(供 Coder 修复)
    - retry_count:        当前步骤重试次数
    - final_report:       最终报告内容/相对路径
    - human_approval:     人机协同审批结果(True 通过 / False 拒绝)
    - route:              Supervisor 输出的路由目标(planner/reporter/FINISH)
    - progress:           节点级进度事件(如 coder_retry_2 / executor_running)
    - progress_detail:    进度明细文本(如"正在执行 SQL 查询(已耗时 12s/30s)")
    - progress_percent:   进度百分比 0-100(按节点阶段权重估算)
    - chat_reply:         Supervisor 识别闲聊(FINISH)时的直接回复文本
    """

    user_query: str
    plan: List[Dict[str, Any]]
    current_task_index: int
    code: str
    exec_result: str
    error_log: str
    retry_count: int
    final_report: str
    human_approval: bool
    route: str
    chat_reply: str

    # 多轮上下文(上下文窗口管理): 入口构建一次, 各节点按预算消费
    session_id: int
    conversation_context: Dict[str, Any]

    # 任务元信息(与外部系统对接)
    task_id: str
    status: str
    progress: str
    progress_detail: str
    progress_percent: int

    # 数据权限: 任务提交者(数据级权限按用户/角色生效)
    actor: str
    # 数据源: 任务指定的数据源 id(空=主库)
    data_source_id: int
    # 审批放行: 人工已批准过(approval_passed)或定时任务自动执行(auto_approve), 不再反复挂起审批
    approval_passed: bool
    auto_approve: bool

    # OR-03 并行执行: Send API 各分支返回的子结果(按 sub_task_id 聚合)
    sub_results: Annotated[List[Dict[str, Any]], operator.add]
    # OR-02 需求澄清: Planner 的候选问题 / 用户回填口径
    clarify_questions: List[str]
    clarify_answer: str
    estimated_cost: float
    approval_reason: str


# 路由常量:Supervisor 的候选目标
ROUTE_PLANNER = "planner"
ROUTE_REPORTER = "reporter"
ROUTE_FINISH = "FINISH"

# 执行结果状态
EXEC_OK = "success"
EXEC_ERROR = "error"

# 重试上限
MAX_RETRY = 3
