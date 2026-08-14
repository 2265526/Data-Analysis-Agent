"""阶段 4 智能体能力增强: OR-01 检索过滤 / OR-02 Clarifier / OR-03 并行 单元测试。"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command


# ---------- OR-01 检索过滤 ----------

def test_query_filters_success_status_and_tables():
    from src.tools.schema_retriever import SchemaRetriever

    class FakeCollection:
        def query(self, query_texts, n_results, where=None):
            assert where == {"status": "success"}  # 负向过滤: 只取 success
            return {
                "documents": [["SELECT 1", "SELECT 2"]],
                "metadatas": [[{"status": "success", "required_tables": "orders"},
                               {"status": "success", "required_tables": "users"}]],
                "distances": [[0.1, 0.2]],
                "ids": [["id-orders", "id-users"]],
            }

    r = SchemaRetriever()
    r._collection = FakeCollection()
    hits = r.query("销量", top_k=2, status="success", required_tables=["orders"])
    # 表结构匹配过滤: 剔除无交集(users)片段
    assert len(hits) == 1
    assert hits[0]["id"] == "id-orders"


def test_query_no_required_tables_keeps_all():
    from src.tools.schema_retriever import SchemaRetriever

    class FakeCollection:
        def query(self, query_texts, n_results, where=None):
            return {
                "documents": [["SELECT 1"]],
                "metadatas": [[{"status": "success", "required_tables": "orders"}]],
                "distances": [[0.1]],
                "ids": [["id-1"]],
            }

    r = SchemaRetriever()
    r._collection = FakeCollection()
    hits = r.query("销量", top_k=2, status="success", required_tables=None)
    assert len(hits) == 1


def test_upsert_success_code_metadata(monkeypatch):
    from src.tools.schema_retriever import SchemaRetriever

    captured = {}

    class FakeCollection:
        def upsert(self, documents, metadatas, ids):
            captured["meta"] = metadatas[0]
            captured["ids"] = ids

    r = SchemaRetriever()
    r._collection = FakeCollection()
    r.upsert_success_code("SELECT 1", plan_step="统计", required_tables=["orders"], task_id="t1")
    assert captured["meta"]["status"] == "success"
    assert captured["meta"]["required_tables"] == "orders"
    assert captured["ids"][0].startswith("code-")


# ---------- OR-02 Clarifier ----------

class _ClarifyState(TypedDict, total=False):
    task_id: str
    user_query: str
    clarify_questions: list
    route: str
    clarify_answer: str
    status: str
    progress: str
    progress_detail: str


def _build_clarify_app():
    from src.nodes.clarifier import clarifier_node

    g = StateGraph(_ClarifyState)
    g.add_node("clarifier", clarifier_node)
    g.add_edge(START, "clarifier")
    g.add_edge("clarifier", END)
    return g.compile(checkpointer=InMemorySaver())


def test_clarifier_pauses_and_resumes_with_answer():
    app = _build_clarify_app()
    cfg = {"configurable": {"thread_id": "c1"}}
    result = app.invoke(
        {"task_id": "t1", "user_query": "求留存率", "clarify_questions": ["时间窗口?"]}, cfg
    )
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "clarify"
    assert payload["questions"] == ["时间窗口?"]

    out = app.invoke(Command(resume={"approved": True, "clarify_answer": "近30天"}), cfg)
    assert out["route"] == "planner"
    assert out["clarify_answer"] == "近30天"


def test_clarifier_skip_when_not_answered():
    app = _build_clarify_app()
    cfg = {"configurable": {"thread_id": "c2"}}
    app.invoke({"task_id": "t2", "clarify_questions": ["口径?"]}, cfg)
    out = app.invoke(Command(resume={"approved": False, "clarify_answer": ""}), cfg)
    assert out["route"] == "planner"
    assert out["clarify_answer"] == ""


# ---------- OR-03 并行 ----------

def test_route_planner_fan_out():
    from src.graph import _route_after_planner

    multi = _route_after_planner({"plan": [{"step": "a"}, {"step": "b"}], "status": "running"})
    assert isinstance(multi, list) and len(multi) == 2
    assert multi[0].node == "step_exec"
    assert multi[1].arg["sub_task_id"] == 1

    assert _route_after_planner({"plan": [{"step": "a"}], "status": "running"}) == "coder"
    assert _route_after_planner({"plan": [{"step": "a"}], "status": "awaiting_approval"}) == "human_approval"
    assert _route_after_planner({"plan": [{"step": "a"}], "status": "running", "route": "clarifier"}) == "clarifier"


def test_step_exec_runs_coder_and_executor(monkeypatch):
    from src import graph as G

    def fake_coder(s):
        return {"code": f"print({s['plan'][0]['step']})", "retry_count": 1}

    def fake_executor(s):
        return {"exec_result": f"结果:{s['plan'][0]['step']}", "status": "running", "route": "reporter", "error_log": ""}

    monkeypatch.setattr(G, "coder_node", fake_coder)
    monkeypatch.setattr(G, "executor_node", fake_executor)

    out = G._step_exec({
        "task_id": "t", "user_query": "q",
        "plan_step": {"step": "s1", "description": "d1", "required_tables": []},
        "sub_task_id": 0,
    })
    result = out["sub_results"][0]
    assert result["sub_task_id"] == 0
    assert result["needs_approval"] is False
    assert "s1" in result["result"]


def test_step_exec_marks_approval(monkeypatch):
    from src import graph as G

    monkeypatch.setattr(G, "coder_node", lambda s: {"code": "x"})
    monkeypatch.setattr(G, "executor_node", lambda s: {
        "exec_result": "摘要", "status": "awaiting_approval", "progress_detail": "结果 20000 行, 超过阈值",
    })

    out = G._step_exec({
        "task_id": "t", "user_query": "q",
        "plan_step": {"step": "s1", "required_tables": []},
        "sub_task_id": 2,
    })
    r = out["sub_results"][0]
    assert r["needs_approval"] is True
    assert "20000" in r["reason"]


def test_aggregate_orders_and_merges():
    from src import graph as G

    out = G._aggregate({"sub_results": [
        {"sub_task_id": 1, "needs_approval": False, "result": "B", "error": ""},
        {"sub_task_id": 0, "needs_approval": False, "result": "A", "error": ""},
    ]})
    assert out["route"] == "reporter"
    # 按 plan 顺序(sub_task_id)合并
    assert out["exec_result"].index("[步骤1]") < out["exec_result"].index("[步骤2]")
    assert "A" in out["exec_result"] and "B" in out["exec_result"]


def test_aggregate_unified_approval():
    from src import graph as G

    out = G._aggregate({"sub_results": [
        {"sub_task_id": 0, "needs_approval": True, "reason": "敏感表", "result": "", "error": ""},
        {"sub_task_id": 1, "needs_approval": False, "result": "ok", "error": ""},
    ]})
    assert out["route"] == "human_approval"
    assert out["status"] == "awaiting_approval"
    assert "敏感表" in out["error_log"]


def test_parallel_graph_fan_in_aggregates():
    """端到端: 2 个子任务 Send 派发 -> aggregate 合并(无 LLM, 纯逻辑图)。"""
    from langgraph.types import Send

    class PState(TypedDict, total=False):
        plan: list
        merged: list
        sub_results: Annotated[list, operator.add]

    def fan(state):
        return [Send("step", {"sub_task_id": i, "val": state["plan"][i]}) for i in range(2)]

    def step(state):
        return {"sub_results": [{"sub_task_id": state["sub_task_id"], "result": state["val"] * 2}]}

    def agg(state):
        return {"merged": sorted(s["sub_task_id"] for s in state["sub_results"])}

    g = StateGraph(PState)
    g.add_node("start", lambda s: {})
    g.add_node("step", step)
    g.add_node("agg", agg)
    g.add_edge(START, "start")
    g.add_conditional_edges("start", fan)
    g.add_edge("step", "agg")
    g.add_edge("agg", END)
    app = g.compile(checkpointer=InMemorySaver())

    result = app.invoke({"plan": ["a", "b"]}, {"configurable": {"thread_id": "p1"}})
    assert result["merged"] == [0, 1]
    assert "aa" in [s["result"] for s in result["sub_results"]]


def test_sandbox_sql_output_has_header():
    """回归: 服务器端游标 description 在 fetch 前为 None, 必须取数后再取列名,
    否则沙箱输出缺表头, 报告表格/图表全部退化。"""
    from src.sandbox.local_sandbox import LocalSandbox
    from src.nodes.reporter import _exec_result_to_md_table, _parse_exec_blocks

    sql = (
        "SELECT p.category_l1 AS category_l1, SUM(oi.total_item_amount) AS sales_last_week "
        "FROM order_items oi JOIN orders o ON oi.order_id=o.order_id "
        "JOIN products p ON oi.product_id=p.product_id "
        "WHERE o.order_status='已完成' GROUP BY p.category_l1 ORDER BY 2 DESC"
    )
    r = LocalSandbox().execute(sql)
    assert r["status"] == "success"
    out = r["output"]
    assert "rows=8" in out
    lines = out.splitlines()
    assert lines[1] == "category_l1, sales_last_week"  # 表头必须存在(回归点)
    # reporter 能解析出表格与图表数据
    md = _exec_result_to_md_table(out)
    assert "| 一级品类 | 上周销售额 |" in md
    blocks = _parse_exec_blocks(out)
    assert blocks and len(blocks[0]["rows"]) == 8


def test_sandbox_stats_line_and_kpi_correction():
    """回归: 大结果集沙箱输出须带 STATS 全量聚合行(规则统计, 无 LLM 幻觉),
    reporter 用其校正 KPI —— 否则样例前10行求和会严重失真(669万 vs 全量19.6亿),
    且报告/看板可能被 LLM 摘要幻觉成"品类"维度, 完全不对题。"""
    from src.nodes.reporter import _parse_stats, _parse_stats_top, _compute_kpis, _parse_exec_blocks
    from src.sandbox.local_sandbox import LocalSandbox

    sql = (
        "SELECT c.customer_id, SUM(oi.total_item_amount) AS total_spending "
        "FROM customers c JOIN orders o ON c.customer_id=o.customer_id "
        "JOIN order_items oi ON oi.order_id=o.order_id "
        "GROUP BY c.customer_id ORDER BY 2 DESC"
    )
    r = LocalSandbox().execute(sql)
    out = r["output"]
    assert '"count": 9927' in out  # 新 JSON 格式: STATS: {"count": 9927, "cols": {...}}
    stats = _parse_stats(out)
    assert stats["count"] == 9927
    # 新格式: sum 在所选值列(cols.total_spending.sum)中
    assert stats["cols"]["total_spending"]["sum"] > 1_000_000_000  # 全量 ~19.6亿(而非样例求和 669万)
    top = _parse_stats_top(stats["cols"]["total_spending"]["top"])
    assert top and top[0][0] == "346"
    # 修复后: 沙箱对 ≤审批阈值 的结果直接全量输出(不再截断前 10 行),
    # _compute_kpis 基于全量行求和 —— 旧行为只有 10 行样例, 求和失真
    # (669万 vs 全量 19.6亿); 现在 KPI 直接等于全量, 与 STATS 一致
    block = _parse_exec_blocks(out)[0]
    kpi = _compute_kpis(block)
    assert kpi["total_sales"] > 1_000_000_000  # 全量求和(而非样例失真)
    assert abs(kpi["total_sales"] - stats["cols"]["total_spending"]["sum"]) < 1.0  # 与 STATS sum 一致
    kpi["top"] = top[0]
    assert kpi["total_sales"] > 1_000_000_000  # 校正后为全量


def test_single_column_and_nested_paren_parsing():
    """回归(测试用例 BUG2/3/1):
    - 单列结果(COUNT/AVG)必须能解析出 KPI/数据, 否则报告"数据不足"且 LLM 自由发挥
    - 嵌套括号(datetime.date(2026, 8, 7))必须整体解析, 不能拆成 3 段
    - STATS 行不能被当成表头
    """
    from src.nodes.reporter import (
        _compute_kpis, _exec_result_to_md_table, _parse_exec_blocks,
        _parse_exec_result, _parse_row_values, _aggregate_labels,
    )

    # 单列 COUNT
    out = "rows=1\norder_count\n(50000,)\nSTATS: {\"count\": 1, \"cols\": {\"order_count\": {\"sum\": 50000.0}}}"
    blk = _parse_exec_blocks(out)[0]
    assert blk["headers"] == ["order_count"]  # 表头不吞 STATS 行
    kpi = _compute_kpis(blk)
    assert kpi["total_sales"] == 50000.0
    assert _parse_exec_result(out) == [{"label": "订单数", "value": 50000.0}]
    assert "| 订单数 |" in _exec_result_to_md_table(out)

    # 嵌套括号 + 日期格式化
    row = "(datetime.date(2026, 8, 7), '服饰鞋包', Decimal('246833.74'))"
    vals = _parse_row_values(row)
    assert vals == ["2026-08-07", "服饰鞋包", 246833.74], vals

    # 多维数据聚合(同一品类多天合并; 聚合维度 = 该行第一个非数值列, 真实列序 品类在前)
    multi = (
        "rows=4\ncategory, order_day, sales\n"
        "('服饰鞋包', datetime.date(2026, 8, 7), Decimal('10'))\n"
        "('服饰鞋包', datetime.date(2026, 8, 8), Decimal('20'))\n"
        "('家居生活', datetime.date(2026, 8, 7), Decimal('5'))\n"
        "('家居生活', datetime.date(2026, 8, 8), Decimal('7'))\n"
    )
    data = _aggregate_labels(_parse_exec_result(multi))
    by_label = {d["label"]: d["value"] for d in data}
    assert by_label["服饰鞋包"] == 30.0 and by_label["家居生活"] == 12.0


def test_axis_inference_excludes_date_column():
    """回归: 多维查询(日期×品类×金额)时, 维度列必须是品类而非日期
    (STATS top / KPI TOP / 明细聚合都不能显示"2026-08-10"当维度)。"""
    from src.nodes.reporter import (
        _aggregate_labels, _infer_axes, _parse_exec_blocks, _parse_exec_result, _parse_stats,
    )
    from src.sandbox.local_sandbox import LocalSandbox

    sql = (
        "SELECT o.order_date::date AS order_day, p.category_l1 AS category, "
        "SUM(oi.total_item_amount) AS daily_sales "
        "FROM orders o JOIN order_items oi ON oi.order_id=o.order_id "
        "JOIN products p ON p.product_id=oi.product_id "
        "WHERE o.order_date >= NOW() - INTERVAL '7 days' GROUP BY 1,2 ORDER BY 1,2"
    )
    out = LocalSandbox().execute(sql)["output"]
    blk = _parse_exec_blocks(out)[0]
    st = _parse_stats(out)
    dim_idx, val_idx = _infer_axes(blk["headers"], st)
    assert blk["headers"][dim_idx] == "category"  # 维度是品类, 不是 order_day
    assert blk["headers"][val_idx] == "daily_sales"
    # STATS top 的 label 是品类(非日期)
    assert "2026-08-" not in st["cols"]["daily_sales"]["top"].split(":")[0]
    # 聚合维度是品类
    data = _aggregate_labels(_parse_exec_result(out, st))
    assert any(d["label"] == "服饰鞋包" for d in data)


def test_sandbox_outputs_full_small_resultset():
    """回归(bug 修复): 沙箱对"小结果集"必须全量输出, 不能只给前 10 行样本。

    多维结果(8 品类 × 5 天 = 39 行)按 ORDER BY category, order_day 排序时,
    前 10 行恰好只覆盖前 2 个品类 —— 修复前 reporter 基于样本构建图表/看板/
    明细导致"只有两个品类"。修复后 ≤500 行直接全量输出, 解析聚合应覆盖全部品类。
    """
    from src.nodes.reporter import _aggregate_labels, _parse_exec_result, _parse_stats
    from src.sandbox.local_sandbox import LocalSandbox

    sql = (
        "SELECT p.category_l1 AS category, DATE(o.order_date) AS order_day, "
        "SUM(oi.total_item_amount) AS daily_sales "
        "FROM orders o JOIN order_items oi ON oi.order_id=o.order_id "
        "JOIN products p ON p.product_id=oi.product_id "
        "WHERE o.order_date >= NOW() - INTERVAL '7 days' AND o.order_status='已完成' "
        "GROUP BY 1,2 ORDER BY 1,2"
    )
    out = LocalSandbox().execute(sql)["output"]
    lines = out.splitlines()
    assert lines[0].startswith("rows=")
    total = int(lines[0].split("=")[1])
    assert total > 10  # 多天多维结果, 行数必然大于样本截断阈值
    # 修复点: 小结果集全量输出(输出行 = rows=N + 表头 + total 行数据 + STATS)
    assert not any("仅显示前 10 行" in l for l in lines)
    assert sum(1 for l in lines[2:] if l.startswith("(")) == total

    st = _parse_stats(out)
    data = _aggregate_labels(_parse_exec_result(out, st))
    labels = {d["label"] for d in data}
    # 修复前只有 2 个品类(服饰鞋包/家居生活); 修复后覆盖全部品类
    assert len(labels) >= 3, labels
    assert "服饰鞋包" in labels and "家居生活" in labels


def test_parse_exec_result_picks_last_metric_column():
    """回归(bug 修复): 多指标结果(品类/销售额/订单数)图表取值必须选"订单数"列。

    表头 [category, total_sales, order_count] 时, 修复前 _parse_exec_result 取
    "第一个数值列" total_sales(销售额), 与 _infer_axes(最后一个数值列 order_count)
    不一致, 导致"上月订单总数"任务的图表/明细/看板显示品类销售额而非品类订单数。
    """
    from src.nodes.reporter import _aggregate_labels, _parse_exec_result, _parse_stats

    out = (
        "rows=3\ncategory, total_sales, order_count\n"
        "('运动户外', Decimal('677243.73'), 46)\n"
        "('食品饮料', Decimal('675122.10'), 39)\n"
        "('服饰鞋包', Decimal('610910.88'), 40)\n"
        "STATS: {\"count\": 3, \"cols\": {\"total_sales\": {\"sum\": 1963276.71}, "
        "\"order_count\": {\"sum\": 125.0}}}"
    )
    st = _parse_stats(out)
    data = _aggregate_labels(_parse_exec_result(out, st))
    by_label = {d["label"]: d["value"] for d in data}
    # 值必须是订单数列(order_count), 而非销售额列(total_sales)
    assert by_label["运动户外"] == 46.0
    assert by_label["食品饮料"] == 39.0
    assert by_label["服饰鞋包"] == 40.0
    assert sum(by_label.values()) == 125.0  # 订单数合计, 不是 196 万销售额



def test_exec_full_roundtrip_task_id_path(monkeypatch, tmp_path):
    """回归(bug 修复): exec_full 落盘/读取必须按 task_id 唯一路径(而非当天日期目录)。

    审批可经 interrupt 挂起跨午夜: 若按"当天日期"目录落盘, 恢复后 reporter
    在新日期目录找不到全量, 静默回退截断文本导致 KPI/图表/明细失真。
    """
    from src.nodes import executor as E
    from src.nodes.reporter import _load_exec_full

    class FakeSettings:
        reports_dir = tmp_path

    monkeypatch.setattr(E, "settings", FakeSettings())
    monkeypatch.setattr("src.nodes.reporter.settings", FakeSettings())
    tid = "roundtrip-task"
    out = "rows=39\ncategory, order_day, daily_sales\n('服饰鞋包', '2026-08-07', Decimal('10'))\nSTATS: {\"count\": 39, \"cols\": {}}"

    E._persist_exec_full(tid, out)
    f = tmp_path / f"{tid}.exec_full.txt"
    assert f.exists()
    # 任意"日期目录"下都能读到(不依赖当天): 模拟跨午夜后 reporter 运行
    full = _load_exec_full(tid, tmp_path / "2099/12/31")
    assert full == out


def test_infer_axes_skips_constant_total_column():
    """回归(bug 修复): 多指标结果含"合计列"时, 图表/明细/KPI 不得取合计列。

    表头 [category_l1, order_count, total_orders] 中 total_orders 是每行重复的
    总数列(sum = max × count): 修复前 _infer_axes 取"最后一个数值列"选中它,
    导致每品类图表值都是 2083、KPI 合计 8×2083=16664(真实订单总数是 2083)。
    修复后按指标口径优先选 order_count(订单量, 单位"笔")。
    """
    from src.nodes.reporter import (
        _aggregate_labels, _compute_kpis, _infer_axes, _metric_unit,
        _parse_exec_blocks, _parse_exec_result, _parse_stats,
    )
    from src.sandbox.local_sandbox import LocalSandbox

    sql = (
        "WITH monthly_orders AS (SELECT o.order_id, p.category_l1 FROM orders AS o "
        "JOIN order_items AS oi ON o.order_id = oi.order_id JOIN products AS p ON oi.product_id = p.product_id "
        "WHERE o.order_status = '已完成' AND o.order_date >= CURRENT_TIMESTAMP - INTERVAL '1 MONTH' - INTERVAL '1 DAY' "
        "AND o.order_date < CURRENT_TIMESTAMP - INTERVAL '1 DAY'), "
        "category_order_counts AS (SELECT category_l1, COUNT(DISTINCT order_id) AS order_count "
        "FROM monthly_orders GROUP BY category_l1), "
        "total_order_count AS (SELECT SUM(order_count) AS total_orders FROM category_order_counts) "
        "SELECT c.category_l1, c.order_count, t.total_orders FROM category_order_counts AS c "
        "CROSS JOIN total_order_count AS t ORDER BY c.order_count DESC"
    )
    out = LocalSandbox().execute(sql)["output"]
    stats = _parse_stats(out)
    blk = _parse_exec_blocks(out)[0]
    dim_idx, val_idx = _infer_axes(blk["headers"], stats)
    assert blk["headers"][dim_idx] == "category_l1"
    assert blk["headers"][val_idx] == "order_count"  # 非合计列 total_orders
    assert _metric_unit(blk["headers"][val_idx]) == "笔"

    data = _aggregate_labels(_parse_exec_result(out, stats))
    by_label = {d["label"]: d["value"] for d in data}
    # 各品类订单数各不相同, 不能全是 2083(合计列值)
    assert len({v for v in by_label.values()}) > 1
    # 合计是真实订单总数(约 2083), 而非 8×2083=16664 的合计列求和
    total = sum(by_label.values())
    assert 0 < total < 10_000, total

    kpi = _compute_kpis(blk, val_idx)
    assert abs(kpi["total_sales"] - total) < 1.0  # KPI 与图表同源一致
    assert kpi["total_sales"] < 10_000  # 非 16664(合计列覆盖)


def test_drill_sql_follows_metric_col():
    """回归(bug 修复): 下钻必须按看板值列指标聚合, 不能写死销售额。

    主查询是"各品类订单数量"时, 二级品类下钻应 COUNT(DISTINCT order_id)
    而不是 SUM(total_item_amount)(回归根因: 下钻明细显示销售额)。
    """
    from src.api.routes import _build_drill_sql

    dim = {
        "label": "二级品类",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.category_l2",
        "filter_col": "p.category_l1",
        "columns": ["二级品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    }
    sql, cols = _build_drill_sql(dim, "服饰鞋包", "order_count")
    assert "COUNT(DISTINCT o.order_id)" in sql
    assert "total_item_amount" not in sql
    assert cols == ["二级品类", "订单数"]

    # 默认/旧看板(无 metric_col): 仍走销售额聚合, 向后兼容
    sql2, cols2 = _build_drill_sql(dim, "服饰鞋包", None)
    assert "sales_7d" in sql2 and "COUNT(DISTINCT" not in sql2
    assert cols2 == dim["columns"]


def test_pick_value_col_consistent_with_infer_axes():
    """回归(bug 修复): KPI 的 STATS 校正必须与图表/明细同列(排除合计列)。

    _pick_value_col 原先"从后往前第一个数值列"会选中 total_orders(sum=16664),
    把 KPI 合计覆盖成 8×订单总数, 而图表是真实 2083 —— 看板自相矛盾。
    """
    from src.nodes.reporter import _pick_value_col

    stats = {
        "count": 8,
        "cols": {
            "category_l1": {"sum": None, "max": None},
            "order_count": {"sum": 2083.0, "max": 282.0},
            "total_orders": {"sum": 16664.0, "max": 2083.0},
        },
    }
    headers = ["category_l1", "order_count", "total_orders"]
    assert _pick_value_col(stats, headers) == "order_count"
    # 无合计列时仍取最后一个数值列(兼容: 客户/名称/总消费金额 -> total_spending)
    stats2 = {"count": 3, "cols": {
        "customer_id": {"sum": 12345.0, "max": 5000.0},
        "customer_name": {"sum": None, "max": None},
        "total_spending": {"sum": 1964609284.86, "max": 774280.27},
    }}
    assert _pick_value_col(stats2, ["customer_id", "customer_name", "total_spending"]) == "total_spending"


def test_compute_kpis_ignores_non_comparison_third_column():
    """回归(bug 修复): 第 3 列无"上期/对比"语义时不得当上期算环比。

    [品类, 订单数, total_orders(合计列)] 若把第 3 列当上期, KPI 卡会出现
    "上周销售额 16,664 笔"(合计列求和)与 -87.5% 的虚假环比(回归根因)。
    """
    from src.nodes.reporter import _compute_kpis

    blk = {
        "headers": ["category_l1", "order_count", "total_orders"],
        "rows": [
            ["服饰鞋包", 282, 2083], ["运动户外", 273, 2083], ["食品饮料", 270, 2083],
        ],
    }
    kpi = _compute_kpis(blk, val_idx=1)
    assert kpi["total_sales"] == 825.0
    assert kpi["total_last"] is None  # 合计列不当作上期
    assert kpi["change_pct"] is None

    # 真实对比查询(第 3 列 sales_last_week 含 last): 仍计算环比
    blk2 = {
        "headers": ["category", "sales_7d", "sales_last_week"],
        "rows": [["服饰鞋包", 100.0, 80.0], ["食品饮料", 50.0, 50.0]],
    }
    kpi2 = _compute_kpis(blk2, val_idx=1)
    assert kpi2["total_last"] == 130.0 and abs(kpi2["change_pct"] - (150 - 130) / 130 * 100) < 1e-9


def test_infer_axis_labels_matches_value_col():
    """回归(bug 修复): 图表 y 轴标签必须与值列一致(经 _infer_axes 而非固定第 2 列)。

    多指标结果 [category, total_sales, order_count] 修复前 y 轴标签取 headers[1]
    (总销售额), 而数据列是 order_count(订单数) —— 标签与数据错位。
    """
    from src.nodes.reporter import _infer_axis_labels, _parse_stats

    out = (
        "rows=2\ncategory, total_sales, order_count\n"
        "('运动户外', Decimal('677243.73'), 46)\n"
        "('食品饮料', Decimal('675122.10'), 39)\n"
        "STATS: {\"count\": 2, \"cols\": {\"total_sales\": {\"sum\": 1352365.83}, "
        "\"order_count\": {\"sum\": 85.0}}}"
    )
    stats = _parse_stats(out)
    x, y = _infer_axis_labels(out, stats)
    assert x == "品类"
    assert y == "订单数"  # 非"总销售额"(headers[1] 错位)


def test_trend_sql_follows_time_window():
    """回归(bug 修复): 趋势图窗口/粒度必须与主查询一致, 不能固定近 8 周。

    "统计近7天...并给出趋势图"(time_window=7d)修复前趋势 SQL 固定
    INTERVAL '56 days' 按周分组, 趋势图画出 6~8 月数据(回归根因);
    修复后近 7 天按天分组, 数据点落在最近一周内。
    """
    from src.nodes.reporter import _build_trend_sql, _parse_simple_pairs
    from src.sandbox.local_sandbox import LocalSandbox
    from src.utils.intent import parse_intent

    intent = parse_intent("统计近7天各品类销售额，并给出趋势图")
    assert intent.get("time_window") == "7d" and intent.get("want_trend")

    sql, title, x_label, g = _build_trend_sql(intent.get("time_window"))
    assert "INTERVAL '7 days'" in sql
    assert "date_trunc('day'" in sql
    assert title == "近 7 天销售趋势" and g == "day"

    out = LocalSandbox().execute(sql)["output"]
    pairs = _parse_simple_pairs(out)
    assert pairs, "7 天窗口应有趋势数据"
    # 数据点必须落在最近 10 天内(08-0x), 不能是 06/07 月
    for p in pairs:
        assert p["label"].startswith("08-"), p["label"]

    # 1 年窗口: 按月分组
    sql_y, title_y, x_y, g_y = _build_trend_sql("1y")
    assert "date_trunc('month'" in sql_y and "INTERVAL '1 year'" in sql_y
    assert g_y == "month" and "按月" in title_y

    # 未识别窗口: 兜底近 8 周(兼容原行为)
    sql_n, title_n, _, g_n = _build_trend_sql(None)
    assert "56 days" in sql_n and g_n == "week"


def test_single_day_query_time_range_and_label():
    """回归(bug 修复): 单日查询(2026-08-07)报告不得显示"近7天"口径/标签。

    ① LLM 报告"统计周期"必须基于 SQL 事实(2026-08-07 单日), 不能编造成
       "7月31日至8月7日/近7天"; ② coder 对单日查询误起的 sales_7d 列名
       在具体日期范围内归一化为"销售额", 不再显示"近7天销售额"(回归根因)。
    """
    from src.nodes.reporter import (_REL_TIME_COLS, _col_cn_override, _extract_time_range)

    sql_single = (
        "SELECT p.category_l1 AS category, SUM(oi.total_item_amount) AS sales_7d "
        "FROM order_items AS oi JOIN orders AS o ON oi.order_id = o.order_id "
        "JOIN products AS p ON oi.product_id = p.product_id "
        "WHERE o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08' "
        "AND o.order_status = '已完成' GROUP BY 1 ORDER BY 2 DESC"
    )
    tr = _extract_time_range(sql_single)
    assert tr["kind"] == "single_day"
    assert tr["desc"] == "2026-08-07(单日)"  # LLM 事实注入值: 禁止编造成近7天

    # 具体日期范围: 相对列名归一化
    kpi = {"_rel_col_override": {c: "销售额" for c in _REL_TIME_COLS}}
    assert _col_cn_override("sales_7d", kpi) == "销售额"
    # 相对窗口查询(近7天): 不归一化, 保留"近7天销售额"语义
    assert _col_cn_override("sales_7d", {}) == "近7天销售额"
    # 区间查询: 同样归一化
    tr2 = _extract_time_range("WHERE o.order_date >= '2026-08-08' AND o.order_date < '2026-08-15'")
    assert tr2["kind"] == "range" and tr2["desc"] == "2026-08-08 ~ 2026-08-15"
    # 相对窗口(近7天): 保持相对, 不触发归一化
    tr3 = _extract_time_range("WHERE o.order_date >= NOW() - INTERVAL '7 DAYS'")
    assert tr3["kind"] == "relative"


def test_extract_time_range_more_forms():
    """回归(bug 修复): 时间范围解析覆盖 = 单日(含 ::date)、> / <= 组合、单边范围。

    修复前只认 `>=` + `<` 和 BETWEEN, `order_date::date = '2026-08-07'` 这类
    单日形态漏判 -> 无 time_fact 注入, "单日被报成近7天"根因原样存在。
    """
    from src.nodes.reporter import _extract_time_range

    r = _extract_time_range("WHERE order_date::date = '2026-08-07' AND o.order_status='已完成'")
    assert r["kind"] == "single_day" and r["desc"] == "2026-08-07(单日)"

    r2 = _extract_time_range("WHERE o.order_date > '2026-08-01' AND o.order_date <= '2026-08-14'")
    assert r2["kind"] == "range" and r2["desc"] == "2026-08-01 ~ 2026-08-14"

    r3 = _extract_time_range("WHERE o.order_date >= '2026-08-01'")
    assert r3["kind"] == "range" and r3["desc"] == "自 2026-08-01 起"  # 无 "~ None"

    r4 = _extract_time_range("WHERE o.order_date BETWEEN '2026-08-07' AND '2026-08-08'")
    assert r4["kind"] == "range"  # 闭区间相邻两天保留 range(不压缩成单日丢一天)
    assert r4["end_op"] == "<=" and r4["desc"] == "2026-08-07 ~ 2026-08-08"

    # 半开相邻(>= a AND < a+1)才是单日
    r5 = _extract_time_range("WHERE o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08'")
    assert r5["kind"] == "single_day" and r5["desc"] == "2026-08-07(单日)"


def test_used_metrics_excludes_rel_time_for_specific_date():
    """回归(bug 修复): 单日查询的"数据口径"清单不得列出相对时间指标。

    coder 对"8月7日单日"查询误起列名 sales_7d, 修复前口径清单命中 sales_7d
    (近7天销售额)及其衍生(sales_change 环比), 与"统计周期 2026-08-07(单日)"
    自相矛盾; 修复后单日/区间查询排除依赖相对窗口的指标, 近7天查询不受影响。
    """
    from src.nodes.reporter import _used_metrics_md
    from src.tools.lineage import record_query_run

    tid = "verify-used-metrics-x"
    record_query_run(
        task_id=tid, run_order=0, rows_returned=8, duration_ms=5,
        sql_text=(
            "SELECT p.category_l1 AS category, SUM(oi.total_item_amount) AS sales_7d "
            "FROM order_items AS oi JOIN orders AS o ON oi.order_id = o.order_id "
            "JOIN products AS p ON oi.product_id = p.product_id "
            "WHERE o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08' "
            "AND o.order_status = '已完成' GROUP BY 1 ORDER BY 2 DESC"
        ),
    )
    md = _used_metrics_md(tid)
    assert "近7天销售额" not in md  # sales_7d 指标行被排除
    assert "环比变化率" not in md and "销售额变化" not in md  # 相对时间衍生指标排除
    assert "订单量" in md  # 非相对时间指标保留


def test_fix_data_notes_facts_overrides_llm_period():
    """回归(规则校验器): 报告"统计周期"必须与 SQL 事实强制对齐, 不依赖 LLM。

    单日查询被 LLM 编造成"2026年7月31日至8月7日/近7天"时, 规则校验器
    确定性替换为事实"2026-08-07(单日)"; 相对窗口查询不强制。
    """
    from src.nodes.reporter import _extract_time_range, _fix_data_notes_facts

    tr = _extract_time_range("WHERE o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08'")
    notes = (
        "- **数据来源**：orders 表\n"
        "- **指标定义**：sales_7d（近7天销售额）\n"
        "- **统计周期**：2026年7月31日至2026年8月7日\n"
        "- **局限性**：未包含已取消订单"
    )
    fixed = _fix_data_notes_facts(notes, tr)
    assert "2026-08-07(单日)" in fixed
    assert "7月31日" not in fixed and "近7天" not in fixed.split("统计周期")[-1]
    assert fixed.count("统计周期") == 1  # 不重复追加

    # 无统计周期行 -> 插入
    f2 = _fix_data_notes_facts("- **数据来源**：orders 表", tr)
    assert "- **统计周期**：2026-08-07(单日)" in f2

    # 相对窗口查询不强制(保留 LLM 原文)
    tr_rel = _extract_time_range("WHERE o.order_date >= NOW() - INTERVAL '7 DAYS'")
    n3 = "- **统计周期**：最近 7 天"
    assert _fix_data_notes_facts(n3, tr_rel) == n3


def test_quality_gate_branches(monkeypatch):
    """回归(LLM 质量门禁): 校验分支与降级路径。

    consistent=False -> 返回 issues(触发重生成); consistent=True -> 空;
    llm=None(模板兜底)或门禁异常 -> 按通过处理, 不阻塞报告。
    """
    from src.nodes import reporter as R

    tr = R._extract_time_range("WHERE o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08'")
    rc = R.ReportContent(executive_summary="近7天销售额184万", body="周期7月31日至8月7日",
                         data_notes="- **统计周期**：近7天", action_items="-")

    # 1) 判定不一致 -> 返回 issues
    def fake_invoke_consistent(llm, model, msgs, **kw):
        return R._QualityVerdict(consistent=False, issues=["统计周期错误", "口径矛盾"])
    monkeypatch.setattr(R, "invoke_structured", fake_invoke_consistent)
    issues = R._quality_gate("统计8月7日销售额", tr, {"total_sales": 1841973.89}, "data", rc, object())
    assert issues == ["统计周期错误", "口径矛盾"]

    # 2) 判定一致 -> 空列表
    def fake_invoke_good(llm, model, msgs, **kw):
        return R._QualityVerdict(consistent=True, issues=[])
    monkeypatch.setattr(R, "invoke_structured", fake_invoke_good)
    assert R._quality_gate("统计8月7日销售额", tr, {}, "data", rc, object()) == []

    # 3) llm=None(模板兜底) -> 跳过门禁
    assert R._quality_gate("统计8月7日销售额", tr, {}, "data", rc, None) == []

    # 4) 门禁自身异常 -> 降级通过, 不阻塞
    def fake_invoke_fail(llm, model, msgs, **kw):
        raise RuntimeError("qc down")
    monkeypatch.setattr(R, "invoke_structured", fake_invoke_fail)
    assert R._quality_gate("统计8月7日销售额", tr, {}, "data", rc, object()) == []

    # 5) prompt 不注入 kpi 内部字段(_stats 不膨胀)
    def fake_invoke_check_prompt(llm, model, msgs, **kw):
        body = msgs[-1]["content"]
        assert "_stats" not in body and "_rel_col_override" not in body
        return R._QualityVerdict(consistent=True, issues=[])
    monkeypatch.setattr(R, "invoke_structured", fake_invoke_check_prompt)
    R._quality_gate("q", tr, {"_stats": {"big": 1}, "total_sales": 1}, "data", rc, object())


def test_drill_sql_follows_time_range():
    """回归(bug 修复): 下钻必须与主查询同一时间口径。

    查"2026年8月7日各品类销售额"时下钻仍显示近7天/上周销售额(回归根因):
    board 持久化 time_range 后, 单日查询下钻按同一日期过滤且只显示单列销售额;
    无 time_range(旧看板/相对窗口)保持近7天/上周/环比兜底。
    """
    from src.api.routes import _build_drill_sql

    dim = {
        "label": "二级品类",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.category_l2",
        "filter_col": "p.category_l1",
        "columns": ["二级品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    }
    tr = {"kind": "single_day", "start": "2026-08-07", "end": "2026-08-08", "desc": "2026-08-07(单日)"}

    # 单日销售额下钻: 同口径过滤, 单列销售额, 无近7天/上周/环比
    sql, cols = _build_drill_sql(dim, "服饰鞋包", "sales_7d", tr)
    assert "o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08'" in sql
    assert "sales_7d" not in sql and "sales_last_week" not in sql and "change_rate" not in sql
    assert cols == ["二级品类", "销售额"]

    # 单日订单数下钻: 同样加时间过滤
    sql2, cols2 = _build_drill_sql(dim, "服饰鞋包", "order_count", tr)
    assert "COUNT(DISTINCT o.order_id)" in sql2 and "order_date >= '2026-08-07'" in sql2
    assert cols2 == ["二级品类", "订单数"]

    # 无 time_range(旧 board/相对窗口): 近7天/上周/环比兜底
    sql3, cols3 = _build_drill_sql(dim, "服饰鞋包", "sales_7d", None)
    assert "sales_7d" in sql3 and "sales_last_week" in sql3 and "change_rate_pct" in sql3
    assert cols3 == dim["columns"]


def test_extract_time_range_operator_semantics():
    """回归(bug 修复): time_range 保留边界运算符, 下钻不与 KPI 差边界日。

    `> '2026-08-01'` 下钻不能变成 `>=`(多含一天); `<=`/BETWEEN 不能漏上界;
    `::date = '2026-08-07'` 单日归一化为半开 [08-07, 08-08), 下钻非空。
    """
    from src.api.routes import _build_drill_sql
    from src.nodes.reporter import _extract_time_range

    dim = {
        "label": "二级品类",
        "join": "JOIN products p ON p.product_id = oi.product_id",
        "group_col": "p.category_l2",
        "filter_col": "p.category_l1",
        "columns": ["二级品类", "近7天销售额", "上周销售额", "环比增长率(%)"],
    }

    # > / <= 组合: 运算符原样保留
    tr = _extract_time_range("WHERE o.order_date > '2026-08-01' AND o.order_date <= '2026-08-14'")
    assert tr["start_op"] == ">" and tr["end_op"] == "<="
    sql, _ = _build_drill_sql(dim, "服饰鞋包", "sales_7d", tr)
    assert "o.order_date > '2026-08-01'" in sql and "o.order_date <= '2026-08-14'" in sql
    assert ">= '2026-08-01'" not in sql

    # ::date = 单日: 半开区间, 下钻非空
    tr_eq = _extract_time_range("WHERE order_date::date = '2026-08-07'")
    assert tr_eq["kind"] == "single_day" and tr_eq["end"] == "2026-08-08"
    sql_eq, _ = _build_drill_sql(dim, "服饰鞋包", "sales_7d", tr_eq)
    assert "o.order_date >= '2026-08-07' AND o.order_date < '2026-08-08'" in sql_eq

    # relative: 不落入单日化, 近7天/上周兜底
    tr_rel = _extract_time_range("WHERE o.order_date >= NOW() - INTERVAL '7 DAYS'")
    assert tr_rel["kind"] == "relative"
    sql_rel, cols_rel = _build_drill_sql(dim, "服饰鞋包", "sales_7d", tr_rel)
    assert "sales_7d" in sql_rel and "sales_last_week" in sql_rel
    assert cols_rel == dim["columns"]


def test_extract_time_range_with_time_part():
    """回归(bug 修复): 时间范围解析支持 '2026-08-07 00:00:00' 带时间部分。

    coder 常生成 order_date >= '2026-08-07 00:00:00' AND < '2026-08-08 00:00:00'
    (单日), 修复前正则只认纯日期 -> time_range=None -> 下钻兜底显示近7天/上周(回归根因)。
    """
    from src.nodes.reporter import _extract_time_range

    r = _extract_time_range(
        "WHERE o.order_date >= '2026-08-07 00:00:00' AND o.order_date < '2026-08-08 00:00:00'"
    )
    assert r["kind"] == "single_day" and r["desc"] == "2026-08-07(单日)"
    assert r["start"] == "2026-08-07" and r["end"] == "2026-08-08"

    r2 = _extract_time_range("WHERE o.order_date >= '2026-08-07 00:00:00.123'")
    assert r2["kind"] == "range" and r2["start"] == "2026-08-07"


def test_executor_rejects_relative_window_for_specific_date(monkeypatch):
    """回归(bug 修复): 用户指定具体日期时, SQL 误用相对窗口(NOW()/INTERVAL)被拦截打回 coder。

    coder 复用"近7天/近1月"历史代码把"2026年8月7日"整成 INTERVAL '1 MONTH'(回归根因);
    executor 在沙箱执行前确定性拦截, 带明确修正提示重生成; 具体日期 SQL 正常执行。
    """
    from src.nodes.executor import _query_has_specific_date, _sql_uses_relative_window

    assert _query_has_specific_date("统计2026年8月7日各品类销售额")
    assert _query_has_specific_date("查 2026-08-07 数据")
    assert not _query_has_specific_date("统计近7天各品类销售额")

    sql_bad = "SELECT ... WHERE o.order_date >= CURRENT_TIMESTAMP - INTERVAL '1 MONTH' AND o.order_date < CURRENT_TIMESTAMP"
    sql_good = "SELECT ... WHERE o.order_date >= '2026-08-07 00:00:00' AND o.order_date < '2026-08-08 00:00:00'"
    assert _sql_uses_relative_window(sql_bad)
    assert not _sql_uses_relative_window(sql_good)

    from src.nodes.executor import executor_node

    # 拦截路径(不实际执行): mock run_in_sandbox 确认不会被调用
    called = {"n": 0}
    monkeypatch.setattr("src.nodes.executor.run_in_sandbox", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"status": "ok", "output": "rows=0"})
    sql_bad_full = (
        "SELECT p.category_l1 AS category, SUM(oi.total_item_amount) AS sales "
        "FROM orders AS o JOIN order_items AS oi ON oi.order_id = oi.order_id "
        "JOIN products AS p ON oi.product_id = p.product_id "
        "WHERE o.order_status = '已完成' "
        "AND o.order_date >= CURRENT_TIMESTAMP - INTERVAL '1 MONTH' "
        "AND o.order_date < CURRENT_TIMESTAMP GROUP BY p.category_l1"
    )
    out = executor_node({
        "code": sql_bad_full,
        "user_query": "统计2026年8月7日各品类销售额",
        "task_id": "t-sem", "current_task_index": 0, "retry_count": 0,
        "error_log": "", "actor": "admin", "data_source_id": None,
        "approval_passed": True, "auto_approve": False,
    })
    assert out["route"] == "coder" and "具体日期" in out.get("error_log", "")
    assert called["n"] == 0  # 未进入沙箱


def test_parse_time_range_explicit_interval():
    """回归(统一时间范围): "2026年8月5日到2026年8月11日"解析为绝对区间。

    修复前 parse_intent 只认相对关键词(近7天/上周), 显式区间 time_window=None
    -> 趋势图兜底近8周、下钻兜底近7天/上周(回归根因)。现在显式区间/相对窗口
    统一解析为 {type,start,end,granularity,desc}, 全链路消费。
    """
    from src.utils.intent import parse_intent, parse_time_range

    r = parse_time_range("统计2026年8月5日到2026年8月11日各品类销售额，并给出趋势图")
    assert r["type"] == "explicit" and r["start"] == "2026-08-05" and r["end"] == "2026-08-12"
    assert r["desc"] == "2026-08-05 ~ 2026-08-11" and r["granularity"] == "day"

    # ISO 区间 / 单日 / 相对窗口锚定 / 上月
    assert parse_time_range("查 2026-08-05 到 2026-08-11 数据")["start"] == "2026-08-05"
    r3 = parse_time_range("统计2026年8月7日各品类销售额")
    assert r3["type"] == "explicit" and r3["start"] == "2026-08-07" and r3["end"] == "2026-08-08"
    assert parse_time_range("统计近7天销售额")["type"] == "relative"
    assert parse_time_range("上月订单总数")["window"] == "last_month"

    # intent 接入
    it = parse_intent("统计2026年8月5日到2026年8月11日各品类销售额，并给出趋势图")
    assert it["time_range"]["desc"] == "2026-08-05 ~ 2026-08-11"


def test_trend_and_board_follow_time_range():
    """回归(统一时间范围): 趋势图窗口=查询区间, board.time_range 来自 intent。

    "8月5日到8月11日"查询趋势图必须是 08-05~08-11 按天(非近8周);
    _extract_time_range 兼容 CAST/to_date/date 字面量(LLM 自由写法)。
    """
    from src.utils.intent import parse_intent
    from src.nodes.reporter import _build_trend_sql_for_range, _extract_time_range

    intent = parse_intent("统计2026年8月5日到2026年8月11日各品类销售额，并给出趋势图")
    sql, title, x_label, g = _build_trend_sql_for_range(intent["time_range"])
    assert "o.order_date >= '2026-08-05' AND o.order_date < '2026-08-12'" in sql
    assert "56 days" not in sql  # 不再近8周
    assert title == "销售趋势" and g == "day"

    # CAST / to_date / DATE 字面量归一化
    r1 = _extract_time_range(
        "WHERE o.order_date >= CAST('2026-08-05' AS DATE) AND o.order_date < CAST('2026-08-12' AS DATE)"
    )
    assert r1 and r1["start"] == "2026-08-05" and r1["end"] == "2026-08-12"
    r2 = _extract_time_range(
        "WHERE o.order_date >= to_date('2026-08-05','YYYY-MM-DD') AND o.order_date < DATE '2026-08-12'"
    )
    assert r2 and r2["start"] == "2026-08-05" and r2["end"] == "2026-08-12"

    # 相对窗口(近7天)趋势仍按锚定区间
    sql7, t7, _, g7 = _build_trend_sql_for_range(parse_intent("统计近7天销售额并给趋势图")["time_range"])
    assert "INTERVAL '7 days'" in sql7 or "order_date >=" in sql7


def test_parse_time_range_baseline_protection():
    """回归(Blocking): 对比基准词("与上周/与上月/同比")不得把主查询范围锚定成基准期。

    "本周销售额与上周对比" 含"上周" -> 修复前 time_range=last_week 把主查询
    限死在上周(回归根因); 现在对比语境不锚定, 交还 LLM 按语义生成。
    """
    from src.utils.intent import parse_intent, parse_time_range

    assert parse_time_range("本周销售额与上周对比") is None
    assert parse_time_range("各品类销售额与上月相比") is None
    assert parse_time_range("销售额同比去年") is None
    # 纯"上周"查询仍锚定
    r = parse_time_range("统计上周各品类销售额")
    assert r and r["window"] == "last_week"


def test_describe_intent_keeps_baseline_and_trend():
    """回归(Should-fix): describe_intent 不因 time_range 提前 return 丢失对比/趋势信息。"""
    from src.utils.intent import describe_intent, parse_intent

    d = describe_intent(parse_intent("统计近7天销售额，与上周对比，并给出趋势图"))
    assert "与上周对比" in d and "含趋势分析" in d and "最近 7 天" in d

    d2 = describe_intent(parse_intent("统计2026年8月5日到2026年8月11日各品类销售额，并给出趋势图"))
    assert "2026-08-05 ~ 2026-08-11" in d2 and "含趋势分析" in d2
    assert d2.count("用户指定范围") == 1  # 不重复
