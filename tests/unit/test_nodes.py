"""节点层单元测试: supervisor 路由与降级 / coder 错误分类与重试上限 / human_approval。

通过 monkeypatch mock make_llm / interrupt, 验证路由、降级、错误分类等确定性逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.nodes.coder import classify_error
from src.state import MAX_RETRY


class _Resp:
    def __init__(self, content: str):
        self.content = content


def _fake_llm(content: str):
    class L:
        def invoke(self, messages, task_id=None, **kw):
            return _Resp(content)

    return L()


# ---------- coder 错误分类(规则引擎) ----------
def test_classify_error_syntax():
    assert classify_error("syntax error near SELECT") == "syntax"
    assert classify_error("NameError: name 'x' is not defined") == "syntax"
    assert classify_error("TypeError: ...") == "syntax"


def test_classify_error_logic():
    assert classify_error('relation "users" does not exist') == "logic"
    assert classify_error("no such table: users") == "logic"
    assert classify_error("division by zero") == "logic"


def test_classify_error_other():
    assert classify_error("connection timeout") == "other"
    assert classify_error("") == "other"


# ---------- coder 重试上限 ----------
def test_coder_max_retry_routes_failed():
    from src.nodes.coder import coder_node

    out = coder_node({
        "user_query": "q",
        "plan": [{"description": "d", "required_tables": []}],
        "current_task_index": 0,
        "error_log": "syntax error near x",  # 语法错误 -> 不路由回 planner
        "retry_count": MAX_RETRY,  # 已达上限
    })
    # 重试超限直接判失败(不再挂起审批, 避免"批准后又挂起"循环)
    assert out["route"] == "failed"
    assert out["status"] == "failed"
    assert out["error_log"] == "syntax error near x"
    assert "重试" in out["error"]


# ---------- supervisor 路由 ----------
def test_supervisor_routes_planner(monkeypatch):
    from src.nodes import supervisor as S

    monkeypatch.setattr(S, "make_llm", lambda *a, **k: _fake_llm('{"route":"planner","reason":"分析需求"}'))
    out = S.supervisor_node({"user_query": "统计销售额", "task_id": "t"})
    assert out["route"] == "planner"


def test_supervisor_routes_finish_chat(monkeypatch):
    from src.nodes import supervisor as S

    monkeypatch.setattr(S, "make_llm", lambda *a, **k: _fake_llm('{"route":"FINISH","reason":"闲聊"}'))
    out = S.supervisor_node({"user_query": "你好", "task_id": "t"})
    assert out["route"] == "FINISH"
    assert out["status"] == "completed"
    assert out["chat_reply"]  # 有闲聊回复


def test_supervisor_fallback_on_invoke_error(monkeypatch):
    """LLM invoke 失败时降级为 planner, 不崩溃(默认走完整流水线)。"""
    from src.nodes import supervisor as S

    class BoomLLM:
        def invoke(self, messages, task_id=None, **kw):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(S, "make_llm", lambda *a, **k: BoomLLM())
    out = S.supervisor_node({"user_query": "统计", "task_id": "t"})
    assert out["route"] == "planner"


def test_supervisor_constructor_failure_should_fallback(monkeypatch):
    """make_llm 构造失败也应降级(已知缺陷: make_llm 在 try 块外, 构造异常会冒泡崩溃)。"""
    from src.nodes import supervisor as S

    def boom(*a, **k):
        raise RuntimeError("constructor down")

    monkeypatch.setattr(S, "make_llm", boom)
    out = S.supervisor_node({"user_query": "统计", "task_id": "t"})  # 期望不抛
    assert out["route"] == "planner"


# ---------- human_approval ----------
def test_human_approval_approved(monkeypatch):
    from src.nodes import human_approval as H

    monkeypatch.setattr(H, "interrupt", lambda payload: {"approved": True})
    out = H.human_approval_node({"task_id": "t", "user_query": "q"})
    assert out["human_approval"] is True
    assert out["status"] == "running"


def test_human_approval_rejected(monkeypatch):
    from src.nodes import human_approval as H

    monkeypatch.setattr(H, "interrupt", lambda payload: {"approved": False})
    out = H.human_approval_node({"task_id": "t", "user_query": "q"})
    assert out["human_approval"] is False
    assert out["status"] == "failed"
    assert "拒绝" in out["error_log"]


# ---------- clarifier 降级路径 ----------
def test_clarifier_default_questions(monkeypatch):
    """无 clarify_questions 时用默认问题(不崩溃)。"""
    from src.nodes import clarifier as C

    captured = {}

    def fake_interrupt(payload):
        captured["questions"] = payload["questions"]
        return {"approved": True, "clarify_answer": "近30天"}

    monkeypatch.setattr(C, "interrupt", fake_interrupt)
    out = C.clarifier_node({"task_id": "t", "user_query": "q"})
    assert out["route"] == "planner"
    assert out["clarify_answer"] == "近30天"
    assert captured["questions"]  # 有默认问题兜底


# ---------- 多轮上下文场景: cache_key 未定义不再抛 UnboundLocalError ----------

def test_planner_with_context_does_not_crash(monkeypatch):
    """回归: 多轮会话(has_context=True)时 planner 写缓存分支不再引用未定义 cache_key。"""
    from types import SimpleNamespace

    from src.nodes import planner as planner_mod
    from src.nodes.planner import PlanOutput, planner_node

    # mock LLM 结构化输出(tasks 元素需支持 .model_dump())
    class _Task:
        def __init__(self, d):
            self.d = d

        def model_dump(self):
            return self.d

    fake_out = SimpleNamespace(
        tasks=[_Task({"step": "x", "description": "统计销售额", "dependencies": [], "required_tables": []})],
        confidence=0.9,
        questions=[],
    )
    monkeypatch.setattr(planner_mod, "make_llm", lambda *a, **k: SimpleNamespace(invoke=lambda *a, **k: fake_out))
    monkeypatch.setattr(planner_mod, "invoke_structured", lambda *a, **k: fake_out)
    monkeypatch.setattr("src.utils.context_window.format_context", lambda *a, **k: "上文摘要")

    state = {
        "user_query": "统计数码产品上周的销售额是多少",
        "conversation_context": {"history": [{"role": "user", "content": "x"}]},
    }
    out = planner_node(state)  # 修复前: UnboundLocalError: cache_key
    assert out.get("plan") and out["plan"][0]["description"] == "统计销售额"


def test_coder_with_context_does_not_crash(monkeypatch):
    """回归: 多轮会话(has_context=True)时 coder 写缓存分支不再引用未定义 cache_key。"""
    from src.nodes import coder as coder_mod
    from src.nodes.coder import coder_node

    monkeypatch.setattr(coder_mod, "make_llm", lambda *a, **k: _fake_llm("SELECT 1"))
    monkeypatch.setattr(coder_mod, "fetch_schema_sql", lambda *a, **k: "")
    monkeypatch.setattr(coder_mod, "_retrieve_history", lambda *a, **k: "")
    monkeypatch.setattr("src.utils.context_window.format_context", lambda *a, **k: "")

    state = {
        "user_query": "统计数码产品上周的销售额是多少",
        "plan": [{"description": "统计数码产品上周销售额", "required_tables": []}],
        "current_task_index": 0,
        "conversation_context": {"history": [{"role": "user", "content": "x"}]},
    }
    out = coder_node(state)  # 修复前: UnboundLocalError: cache_key
    assert out.get("code") == "SELECT 1"


class SimpleMetricRegistry:
    def catalog_prompt(self, *a, **k):
        return ""


# ---------- 审批恢复路由: 执行前审批不再产出空报告 ----------

def test_route_after_approval():
    """审批通过后的路由: 有结果->reporter; 执行前审批(空结果+有计划)->coder 继续执行。"""
    from src.graph import _route_after_approval

    # 拒绝 -> 终止
    assert _route_after_approval({"human_approval": False}) == "finish"
    # executor 后审批(大结果集/敏感表): 有执行结果 -> 直接出报告
    assert _route_after_approval({"human_approval": True, "exec_result": "rows=1\n(1,)"}) == "reporter"
    assert _route_after_approval({"human_approval": True, "exec_result": "", "code": "SELECT 1"}) == "reporter"
    # 执行前审批(coder 重试超限/成本超限): 空结果+有计划 -> 回 coder 继续执行
    assert (
        _route_after_approval(
            {"human_approval": True, "exec_result": "", "code": "", "plan": [{"description": "x"}]}
        )
        == "coder"
    )
    # 兜底: 空结果且无计划 -> reporter(报告标注无数据)
    assert _route_after_approval({"human_approval": True, "exec_result": "", "code": "", "plan": []}) == "reporter"


def test_human_approval_reset_retry(monkeypatch):
    """审批通过后重置 retry_count/error_log, 恢复路径不会因重试已满再次立即挂起。"""
    from src.nodes import human_approval as ha

    # 打桩 interrupt(monkeypatch 自动恢复, 避免污染后续审批流程测试)
    monkeypatch.setattr(ha, "interrupt", lambda *a, **k: {"approved": True})
    out = ha.human_approval_node({"task_id": "t1", "retry_count": 3, "error_log": "重试超过 3 次"})
    assert out["human_approval"] is True
    assert out["retry_count"] == 0
    assert out["error_log"] == ""


# ---------- supervisor 数据意图规则 + 定时任务永久审批 ----------

def test_supervisor_data_intent_routes_to_planner():
    from src.nodes.supervisor import _looks_like_data_query, supervisor_node

    # 数据意图词 -> 强制 planner(不调 LLM)
    for q in ("统计客户数量是多少", "各品类上周销售额", "查询用户数量", "订单趋势", "统计一下利润"):
        assert _looks_like_data_query(q), q
    # 纯闲聊 -> 不强制
    assert not _looks_like_data_query("你好")
    assert not _looks_like_data_query("谢谢你的帮助")

    import src.nodes.supervisor as sv

    orig_llm = sv.make_llm
    sv.make_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM"))
    try:
        out = supervisor_node({"user_query": "统计客户数量是多少"})
        assert out["route"] == "planner"
    finally:
        sv.make_llm = orig_llm
