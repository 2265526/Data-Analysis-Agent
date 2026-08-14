"""Reporter 节点: 汇总所有执行结果, 生成含图表/表格的报告(Markdown + PDF)。

产物写入 static/reports/YYYY/MM/DD/:
- {task_id}.md    —— 前端页面实时渲染报告正文
- {task_id}.pdf   —— 下载用 PDF(weasyprint 渲染, 含图表/表格)
状态中 final_report 指向 PDF 相对路径。
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.nodes import make_llm
from src.state import PipelineState
from src.tools.chart_gen import render_markdown_report
from src.tools.data_source import _table_cn as data_source_table_cn
from src.utils.intent import describe_intent, parse_intent
from src.utils.logger import get_logger
from src.utils.security import mask_sensitive
from src.utils.settings import get_settings
from src.utils.structured_json import invoke_structured

logger = get_logger(__name__)
settings = get_settings()

# 报告结构化输出(LLM): 执行摘要 / 正文 / 口径 / 行动建议
_REPORT_OUTPUT_TMPL = """{
  "executive_summary": "执行摘要(2-4句, 可含换行)",
  "body": "**背景**\\n一段...\\n\\n**关键结论**\\n- 结论1...\\n- 结论2...\\n\\n**风险提示**\\n- 风险1...",
  "data_notes": "- **数据来源**：xxx\\n- **指标定义**：xxx\\n- **统计周期**：xxx\\n- **局限性**：xxx",
  "action_items": "- **P0**：xxx\\n- **P1**：xxx"
}"""


class ReportContent(BaseModel):
    """Reporter LLM 结构化输出。"""

    executive_summary: str = Field(description="执行摘要, 面向决策者 2-4 句")
    body: str = Field(description="报告正文: 背景/关键结论/风险提示")
    data_notes: str = Field(description="数据口径说明: 来源/指标/周期/局限")
    action_items: str = Field(description="行动建议, 带 P0/P1/P2 优先级")

_SYSTEM_PROMPT = """你是数据报告撰写专家, 基于执行结果撰写专业经营分析报告。
- 结构化输出 4 个字段(见输出模板), 全部用 Markdown
- **格式要求(重要): 需要分点表述的内容必须换行, 严禁挤成一段**
- 执行摘要: 面向决策者的 2-4 句浓缩(最关键结论 + 最优先行动), 倒金字塔
- 正文: 严格包含三个小节, 每小节标题独占一行:
  **背景**(一段即可)
  **关键结论**(每条结论独占一行, 用 "- " 列表; 带数据支撑)
  **风险提示**(每条独占一行, 用 "- " 列表)
- 数据口径: 每项独占一行, 用 "- **数据来源**：xxx" 格式列出(数据来源/指标定义/统计周期/局限性)
- 行动建议: 每条独占一行, 用 "- **P0**：xxx" 格式(带优先级, 具体可执行)
- 数据明细与图表由系统自动生成, 你无需重复"""


def _split_top_level(s: str) -> list[str]:
    """按顶层逗号切分, 忽略括号/引号内的逗号(支持 datetime.date(2026, 8, 7) 等嵌套)。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _parse_row_values(line: str) -> list:
    """解析沙箱输出的一行数据元组: ('服饰鞋包', Decimal('266411290.16')) -> ['服饰鞋包', 266411290.16]。

    兼容: 引号字符串 / Decimal('...') / None / True / 纯数字 / datetime.date(2026, 8, 7)(嵌套括号)。
    """
    m = re.match(r"^\((.*)\)$", line.strip(), re.DOTALL)
    if not m:
        return []
    parts = [p.strip() for p in _split_top_level(m.group(1))]
    vals: list = []
    for p in parts:
        if not p:
            continue
        dm = re.fullmatch(r"Decimal\(\s*['\"]([^'\"]+)['\"]\s*\)", p)
        if dm:
            try:
                vals.append(float(dm.group(1)))
            except ValueError:
                vals.append(dm.group(1))
            continue
        # datetime.date(2026, 8, 7) -> '2026-08-07'(嵌套括号内含逗号, 简单切分会拆坏)
        dd = re.fullmatch(r"datetime\.date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", p)
        if dd:
            vals.append(f"{int(dd.group(1)):04d}-{int(dd.group(2)):02d}-{int(dd.group(3)):02d}")
            continue
        try:
            vals.append(ast.literal_eval(p))
        except Exception:  # noqa: BLE001
            vals.append(p.strip("'\" "))
    return vals


def _parse_exec_result(exec_result: str, stats: dict | None = None) -> List[Dict[str, Any]]:
    """尽力解析执行结果为结构化数据, 供图表生成。

    支持:
    1. JSON 数组: [{"label": ..., "value": ...}, ...]
    2. 沙箱文本格式(含多步骤 [步骤N] 拼接): 按 "rows=N" 分块,
       每块取前两列作为 label / value。

    与 _exec_result_to_md_table 保持一致的表头过滤与去重逻辑:
    planner 可能拆出多个表头相同的重复步骤(实测 6 步都是同 8 品类数据),
    若直接合并所有块会导致同一品类出现 6 次、单项占比骤降, 图表被污染
    (饼图 100% 其他 / TOP5 同色堆叠)。因此仅保留表头一致的数据块并去重。
    """
    if not exec_result:
        return []
    try:
        data = json.loads(exec_result)
        if isinstance(data, list):
            return data
    except Exception:  # noqa: BLE001
        pass

    rows: List[Dict[str, Any]] = []
    all_headers_raw: list[str] | None = None
    seen: set = set()
    # 按 "rows=N" 切块(兼容 [步骤N] 标题/多步拼接)
    for block in re.split(r"^rows=\d+\s*$", exec_result, flags=re.M):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        header_line = _find_header_line(lines)
        if not header_line:
            continue
        headers = [h.strip() for h in header_line.split(",")]
        if not headers:
            continue
        # 维度/值列索引(传 STATS 数值列信息; 排除日期列)
        dim_idx, val_idx = _infer_axes(headers, stats)
        if all_headers_raw is None:
            all_headers_raw = headers
        elif headers != all_headers_raw:  # 表头不同则跳过该块(保持与数据明细表一致)
            continue
        for l in lines:
            if not (l.startswith("(") and l.endswith(")")):
                continue
            vals = _parse_row_values(l)
            if not vals:
                continue
            # 单列结果(COUNT/AVG 等 KPI 查询): value = 唯一列, label = 列名中文
            if len(vals) == 1:
                try:
                    value = float(vals[0])
                except (TypeError, ValueError):
                    continue
                label = _col_cn(headers[0]) if headers else "值"
                key = (label, value)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"label": label, "value": value})
                continue
            # value 列: 优先与 _infer_axes 一致 —— 最后一个出现在 STATS 数值列的列。
            # 多指标结果(如 品类/销售额/订单数)若取第一个数值列会把"销售额"当成
            # "订单数"展示, 报告/图表/看板/明细全部错列(回归根因);
            # 无 STATS 时取最后一列(与 _infer_axes 相同), 回退第一个数值列
            vi = None
            if 0 <= val_idx < len(vals) and val_idx != dim_idx:
                try:
                    float(vals[val_idx])
                    vi = val_idx
                except (TypeError, ValueError):
                    vi = None
            if vi is None:
                for i in range(len(vals)):
                    if i == dim_idx:
                        continue
                    try:
                        float(vals[i])
                        vi = i
                        break
                    except (TypeError, ValueError):
                        continue
            if vi is None:
                continue
            # label = 维度列(headers[dim_idx])的值(排除日期列), 如品类/客户名
            if dim_idx < len(vals):
                label = str(vals[dim_idx])
            else:
                label = str(vals[0])
            try:
                value = float(vals[vi])
            except (TypeError, ValueError):
                continue
            key = (label, value)
            if key in seen:  # 重复行去重(同表头多块场景)
                continue
            seen.add(key)
            rows.append({"label": label, "value": value})
    return rows


# 数据明细表头: 英文原始列名 → 用户可读的中文(从用户出发展示)
COLUMN_CN = {
    "category": "品类",
    "category_l1": "一级品类",
    "category_l2": "二级品类",
    "product_name": "商品名称",
    "product_id": "商品ID",
    "order_id": "订单号",
    "order_status": "订单状态",
    "order_count": "订单数",
    "orders": "订单数",
    "sales": "销售额",
    "total_item_amount": "销售额",
    "recent_sales": "近7天销售额",
    "recent_7_days_sales": "近7天销售额",
    "recent_7days_sales": "近7天销售额",  # LLM 生成 SQL 列名变体(无下划线)
    "current_week_sales": "近7天销售额",
    "last_week_sales": "上周销售额",
    "sales_change": "销售额变化",
    "sales_diff": "销售额变化",
    "sales_difference": "销售额变化",
    "difference": "销售额变化",
    "change_rate": "环比变化率",
    "growth_rate": "环比增长率",
    "growth_rate_percent": "环比增长率(%)",
    "growth_rate_pct": "环比增长率(%)",
    "amount": "金额",
    "quantity": "数量",
    "sold_quantity": "销量",
    "total_quantity": "总销量",
    "carrier": "承运商",
    "avg_days": "平均时效(天)",
    "transit_days": "运输天数",
    "count": "数量",
    "total": "总计",
    "price": "价格",
    "status": "状态",
    "date": "日期",
    "region": "地区",
    "province": "省份",
    "city": "城市",
    "user_count": "用户数",
    "customer_id": "客户",
    "customer_name": "客户名称",
    "total_spending": "总消费金额",
    "total_spend": "总消费金额",
    "total_amount": "总金额",
    "spending": "消费金额",
    "sales_amount": "销售额",
    "total_sales": "总销售额",
    "daily_sales": "销售额",
    "sales": "销售额",
    "sales_7d": "近7天销售额",
}


def _col_cn(h: str) -> str:
    """列名中文化: COLUMN_CN 优先, 其次指标目录 name_en(LLM 用指标标识作列名时兜底)。"""
    if h in COLUMN_CN:
        return COLUMN_CN[h]
    try:
        from src.tools.metric_registry import get_metric_registry

        for m in get_metric_registry()._load():
            if m.get("name_en") == h:
                return m.get("name_cn") or h
    except Exception:  # noqa: BLE001 — 指标库不可用不阻塞
        pass
    return h


# 相对时间语义列名(近7天/上周等): 查询为具体日期范围(单日/区间)时, 这些列名
# 会被 coder 误用为别名(如对"8月7日"查询起名 sales_7d), 展示成"近7天销售额"
# 会误导 —— 此时应归一化为通用"销售额"(回归根因)。
_REL_TIME_COLS = {
    "sales_7d", "recent_sales", "recent_7_days_sales", "recent_7days_sales",
    "current_week_sales", "daily_sales", "sales_last_week", "last_week_sales",
}


def _col_cn_override(h: str, kpi: dict | None) -> str:
    """列名中文化, 支持"具体日期查询"时的相对列名归一化(kpi["_rel_col_override"])。

    单日查询(如 2026-08-07)时 SQL 列名 sales_7d 不代表"近7天销售额",
    应展示为通用"销售额"; 相对窗口查询(近7天/上周)不覆盖, 保留原语义。
    """
    ov = (kpi or {}).get("_rel_col_override") or {}
    if h in ov:
        return ov[h]
    return _col_cn(h)


def _extract_time_range(sql: str | None) -> dict | None:
    """从主 SQL 的 order_date 过滤解析**实际统计时间范围**(报告"统计周期"的事实依据)。

    返回 {"kind": "single_day"|"range"|"relative"|None, "start", "end", "desc"};
    支持 `= 'date'`(单日)、`>=`/`>` 下界 + `<`/`<=` 上界、`BETWEEN a AND b`、
    相对窗口(NOW()/CURRENT_TIMESTAMP/INTERVAL); 单边范围 desc 用"自 X 起"。
    LLM 报告生成不能自由编造统计周期 —— 把单日查询写成"近7天/7月31日至8月7日"
    是回归根因, 报告"统计周期"必须以 SQL 事实为准。
    """
    if not sql:
        return None
    low = sql.lower()
    # 日期可带时间部分: '2026-08-07' / '2026-08-07 00:00:00' / '2026-08-07T00:00:00'
    _date = r"(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
    # 归一化常见写法(LLM 自由发挥): CAST('2026-08-05' AS DATE) / to_date('2026-08-05',...)
    # / DATE '2026-08-05' / ('2026-08-05') 字面量 —— 统一成裸 '2026-08-05' 供下方正则匹配
    norm = re.sub(
        rf"cast\s*\(\s*'{_date}'\s*as\s+date\s*\)", r"'\1'", low
    )
    norm = re.sub(r"to_date\s*\(\s*'(\d{4}-\d{2}-\d{2})'[^)]*\)", r"'\1'", norm)
    norm = re.sub(r"date\s+'(\d{4}-\d{2}-\d{2})'", r"'\1'", norm)
    start = end = None
    start_op = end_op = None
    m_between = re.search(
        rf"order_date(?:::date)?\s+between\s+'{_date}'\s+and\s+'{_date}'", norm
    )
    if m_between:
        start, end = m_between.group(1), m_between.group(2)
        start_op, end_op = ">=", "<="  # BETWEEN 闭区间
    else:
        for op, d in re.findall(
            rf"order_date(?:::date)?\s*(>=|>|<|<=|=)\s*'{_date}'", norm
        ):
            if op == "=":
                start = end = d
                start_op = end_op = "="
            elif op in (">=", ">"):
                if start is None or d < start:
                    start, start_op = d, op
            else:  # <=, <
                if end is None or d > end:
                    end, end_op = d, op
    if start is None and end is None:
        if "now()" in low or "current_timestamp" in low or "interval" in low:
            return {"kind": "relative", "start": None, "end": None, "start_op": None, "end_op": None,
                    "desc": "相对窗口(近N天/上周等)"}
        return None
    try:
        from datetime import date, timedelta

        sd = date.fromisoformat(start) if start else None
        ed = date.fromisoformat(end) if end else None
        # 单日归一化: `=` / 同天(闭转半开) / 半开相邻(end_op="<") 才判单日 ——
        # 闭区间相邻(BETWEEN '08-07' AND '08-08' 两天 / <=)保留 range, 否则下钻丢一天(回归根因)
        if start and end and start == end:
            end = (sd + timedelta(days=1)).isoformat()
            ed = date.fromisoformat(end)  # end 已更新, ed 需重新解析(否则 single_day 判定失败)
            start_op, end_op = ">=", "<"
        is_single = (start and end and start == end) or (
            sd and ed and ed == sd + timedelta(days=1) and end_op == "<"
        )
        if is_single:
            return {"kind": "single_day", "start": start, "end": end,
                    "start_op": start_op or ">=", "end_op": end_op or "<",
                    "desc": f"{start}(单日)"}
        return {
            "kind": "range", "start": start, "end": end,
            "start_op": start_op or ">=", "end_op": end_op or "<",
            "desc": (
                f"自 {start} 起" if end is None else
                (f"截至 {end}" if start is None else f"{start} ~ {end}")
            ),
        }
    except ValueError:
        return {"kind": "range", "start": start, "end": end,
                "start_op": start_op or ">=", "end_op": end_op or "<",
                "desc": f"{start} ~ {end}"}


def _find_header_line(lines: list[str]) -> str:
    """取数据块中第一行"非数据行、非 STATS 统计行"的表头。

    - 数据行以 ( 开头; STATS 行是沙箱聚合统计(JSON, 含逗号), 均需排除
    - 单列结果(COUNT/AVG)表头不含逗号也应识别
    """
    for l in lines:
        if l.startswith("(") or l.startswith("STATS:"):
            continue
        return l
    return ""


def _parse_exec_blocks(exec_result: str) -> list[dict]:
    """按 [步骤N] 标题切块解析执行结果(多步骤并行场景, graph._aggregate 拼接)。

    返回 [{"step": 1, "headers": [...], "rows": [[...], ...]}, ...];
    无数据/解析失败的块被过滤。兼容无步骤标题的单块结果。
    """
    blocks: list[dict] = []
    markers = [
        (m.start(), int(m.group(1)))
        for m in re.finditer(r"^\[步骤\s*(\d+)\s*\]\s*$", exec_result, flags=re.M)
    ]
    if not markers:
        blk = _parse_one_block(exec_result)
        blk["step"] = 1
        if blk["rows"]:
            blocks.append(blk)
        return blocks
    for i, (pos, no) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(exec_result)
        blk = _parse_one_block(exec_result[pos:end])
        blk["step"] = no
        if blk["rows"]:
            blocks.append(blk)
    return blocks


def _parse_one_block(text: str) -> dict:
    """解析单个数据块: rows=N 之后的第一行逗号行为表头, (..) 行为数据行。"""
    headers: list[str] = []
    rows: list[list] = []
    m = re.search(r"^rows=\d+\s*$", text, flags=re.M)
    body = text[m.end():] if m else text
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    header_line = _find_header_line(lines)
    if header_line:
        headers = [h.strip() for h in header_line.split(",")]
    for l in lines:
        if not (l.startswith("(") and l.endswith(")")):
            continue
        vals = _parse_row_values(l)
        if vals:
            rows.append(vals)
    return {"headers": headers, "rows": rows}


def _parse_simple_pairs(output: str) -> list[dict]:
    """解析补查输出的 rows=N 文本为 [{"label": ..., "value": ...}]。"""
    data: list[dict] = []
    m = re.search(r"^rows=\d+\s*$", output, flags=re.M)
    if not m:
        return data
    lines = [l.strip() for l in output[m.end():].splitlines() if l.strip()]
    for l in lines:
        if not (l.startswith("(") and l.endswith(")")):
            continue
        vals = _parse_row_values(l)
        if len(vals) >= 2:
            try:
                data.append({"label": str(vals[0]), "value": float(vals[1])})
            except (TypeError, ValueError):
                continue
    return data


# 补查 SQL(基于演示供应链库 orders/order_items, 只读; 生产换库时探测失败自动降级)
# 未识别时间窗口时兜底: 近 8 周按周(原行为)
_TREND_SQL = (
    "SELECT to_char(date_trunc('week', o.order_date), 'MM-DD') AS week_start, "
    "COALESCE(SUM(oi.total_item_amount), 0) AS sales "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "WHERE o.order_date >= NOW() - INTERVAL '56 days' "
    "GROUP BY 1 ORDER BY 1"
)
_YOY_SQL = (
    "SELECT COALESCE(SUM(oi.total_item_amount), 0) AS sales "
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "WHERE o.order_date >= NOW() - INTERVAL '371 days' "
    "AND o.order_date < NOW() - INTERVAL '364 days'"
)


def _build_trend_sql(time_window: str | None) -> tuple[str, str, str, str]:
    """按用户时间窗口生成趋势 SQL, 返回 (sql, 图表标题, x轴标签, 粒度)。

    窗口必须与主查询一致: 用户查"近7天"趋势图却出 6~8 月的近 8 周数据
    (回归根因) —— 窗口/分组粒度随 intent.time_window 变化:
    - 7d / last_week / 30d: 按天
    - 90d: 按周(避免 90 个点)
    - 1y: 按月(12 个点)
    - None(未识别): 保持原"近 8 周按周"兜底

    显式日期区间(如 "2026年8月5日到8月11日")由 intent.time_range 提供,
    趋势窗口=查询区间(调用方传入 _build_trend_sql_for_range)。
    """
    day_sql = (
        "SELECT to_char(date_trunc('day', o.order_date), 'MM-DD') AS day_start, "
        "COALESCE(SUM(oi.total_item_amount), 0) AS sales "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
        "WHERE {where} "
        "GROUP BY 1 ORDER BY 1"
    )
    if time_window == "7d":
        return (day_sql.format(where="o.order_date >= NOW() - INTERVAL '7 days'"),
                "近 7 天销售趋势", "日期", "day")
    if time_window == "last_week":
        return (day_sql.format(
                    where="o.order_date >= date_trunc('week', NOW()) - INTERVAL '7 days' "
                          "AND o.order_date < date_trunc('week', NOW())"),
                "上周销售趋势", "日期", "day")
    if time_window == "30d":
        return (day_sql.format(where="o.order_date >= NOW() - INTERVAL '30 days'"),
                "近 30 天销售趋势", "日期", "day")
    if time_window == "90d":
        return ("SELECT to_char(date_trunc('week', o.order_date), 'MM-DD') AS week_start, "
                "COALESCE(SUM(oi.total_item_amount), 0) AS sales "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                "WHERE o.order_date >= NOW() - INTERVAL '90 days' "
                "GROUP BY 1 ORDER BY 1",
                "近 90 天销售趋势(按周)", "周(周一)", "week")
    if time_window == "1y":
        return ("SELECT to_char(date_trunc('month', o.order_date), 'YYYY-MM') AS month_start, "
                "COALESCE(SUM(oi.total_item_amount), 0) AS sales "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                "WHERE o.order_date >= NOW() - INTERVAL '1 year' "
                "GROUP BY 1 ORDER BY 1",
                "近 1 年销售趋势(按月)", "月份", "month")
    return _TREND_SQL, "近 8 周销售趋势", "周(周一)", "week"


def _build_trend_sql_for_range(time_range: dict | None) -> tuple[str, str, str, str]:
    """按**统一时间范围**(intent.time_range)生成趋势 SQL, 返回 (sql, 标题, x轴标签, 粒度)。

    显式日期区间("2026年8月5日到8月11日")趋势窗口=查询区间 [start, end),
    粒度按区间宽度(≤31天按天/≤120天按周/更久按月)—— 不再落到"近8周"兜底(回归根因)。
    """
    if not time_range or not time_range.get("start") or not time_range.get("end"):
        return _build_trend_sql((time_range or {}).get("window"))
    g = time_range.get("granularity") or "day"
    bucket = {"day": "day", "week": "week", "month": "month"}[g]
    col = {"day": "day_start", "week": "week_start", "month": "month_start"}[g]
    fmt = {"day": "'MM-DD'", "week": "'MM-DD'", "month": "'YYYY-MM'"}[g]
    title = {"day": "销售趋势", "week": "销售趋势(按周)", "month": "销售趋势(按月)"}[g]
    x_label = {"day": "日期", "week": "周(周一)", "month": "月份"}[g]
    sql = (
        f"SELECT to_char(date_trunc('{bucket}', o.order_date), {fmt}) AS {col}, "
        f"COALESCE(SUM(oi.total_item_amount), 0) AS sales "
        f"FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
        f"WHERE o.order_date >= '{time_range['start']}' AND o.order_date < '{time_range['end']}' "
        f"GROUP BY 1 ORDER BY 1"
    )
    return sql, title, x_label, g


def _run_extension_sql(sql: str, task_id: str) -> str | None:
    """在沙箱执行补查 SQL(趋势/同比), 成功返回 output; 失败/业务表不存在返回 None。"""
    try:
        from src.sandbox.docker_sandbox import run_in_sandbox

        res = run_in_sandbox(sql, backend="auto")
        if res.get("status") == "success" and res.get("output"):
            return res["output"]
        logger.info("extension_query_empty", task_id=task_id, error=(res.get("error") or "")[:120])
    except Exception as exc:  # noqa: BLE001 — 补查失败不阻塞报告
        logger.warning("extension_query_failed", task_id=task_id, error=str(exc)[:200])
    return None


def _fetch_extension_data(intent: dict, task_id: str) -> dict:
    """按意图补查跨窗口数据: 趋势(窗口随主查询)/同比(去年同周)。

    趋势窗口/粒度由 intent.time_window 决定(近7天->按天, 近90天->按周等),
    不能固定近 8 周 —— 否则"近7天+趋势图"会画出 6~8 月的数据(回归根因)。
    仅当用户意图触发(提到趋势/同比)才查询; 失败降级返回空, 报告自动跳过对应章节。
    """
    ext: dict = {"trend": [], "yoy": None}
    if intent.get("want_trend"):
        # 统一时间范围优先(显式区间 -> 趋势窗口=查询区间); 否则按相对窗口
        tr = intent.get("time_range")
        if tr and tr.get("start") and tr.get("end"):
            sql, title, x_label, granularity = _build_trend_sql_for_range(tr)
        else:
            sql, title, x_label, granularity = _build_trend_sql(intent.get("time_window"))
        out = _run_extension_sql(sql, task_id)
        if out:
            ext["trend"] = _parse_simple_pairs(out)
            ext["trend_window"] = {"title": title, "x_label": x_label, "granularity": granularity}
    if intent.get("want_yoy"):
        out = _run_extension_sql(_YOY_SQL, task_id)
        if out:
            pairs = _parse_simple_pairs(out)
            ext["yoy"] = pairs[0]["value"] if pairs else None
    return ext


def _fmt_num(v) -> str:
    """数值千分位格式化(报告正文用)。"""
    try:
        f = float(v)
        return f"{f:,.2f}" if f != int(f) else f"{int(f):,}"
    except (TypeError, ValueError):
        return str(v)


def _compute_kpis(main_block: dict, val_idx: int = 1) -> dict:
    """从主数据块计算核心指标(KPI 总览)。

    支持 2 列(品类/本期) 与 ≥3 列(品类/本期/上期/变化) 两种主查询结果。
    val_idx: 值列索引(多列如 品类/金额/订单数 时, 合计/TOP 基于该列, 而非固定 r[1]);
    仅当值列是第 2 列(headers[1])**且第 3 列有上期/对比语义**(列名含 last/prev/环比/
    上周/同比等)时才把第 3 列当"上期"计算环比 —— 否则如 [品类, 订单数, 合计列] 会把
    合计列误当"上周销售额", KPI 卡出现"上周销售额 16,664 笔"(回归根因)。
    """
    rows = main_block.get("rows", [])
    headers = main_block.get("headers", [])
    kpi: dict = {
        "total_sales": 0.0, "total_last": None, "change": None, "change_pct": None,
        "top": None, "bottom": None, "top_contrib_pct": None,
    }
    if not rows:
        return kpi
    last_col = (headers[2] if len(headers) >= 3 else "").lower()
    # 变化率/占比列(rate/pct/percent/环比变化率)不是"上期值", 不能当上期求和
    if any(k in last_col for k in ("rate", "pct", "percent")):
        has_last = False
    else:
        _LAST_LIKE = ("last", "prev", "previous", "change", "diff", "yoy", "环比",
                      "同比", "上周", "上期", "较上", "growth", "mom")
        has_last = any(k in last_col for k in _LAST_LIKE)
    current, last = 0.0, None
    for r in rows:
        try:
            val = float(r[val_idx] if val_idx < len(r) else r[0])  # 单列取 r[0]
            current += val
        except (TypeError, ValueError, IndexError):
            continue
        if val_idx == 1 and len(r) >= 3 and has_last:
            try:
                lv = float(r[2])
                last = (last or 0.0) + lv
            except (TypeError, ValueError):
                pass
    kpi["total_sales"] = current
    if last is not None:
        kpi["total_last"] = last
        kpi["change"] = current - last
        if last:
            kpi["change_pct"] = (current - last) / last * 100
    # Top/Bottom 维度
    items = []
    for r in rows:
        try:
            items.append((str(r[0]), float(r[val_idx] if val_idx < len(r) else r[0])))
        except (TypeError, ValueError, IndexError):
            continue
    if items:
        items.sort(key=lambda x: x[1], reverse=True)
        kpi["top"] = items[0]
        kpi["bottom"] = items[-1]
        kpi["top_contrib_pct"] = items[0][1] / current * 100 if current else None
    return kpi


def _fmt_cell(v) -> str:
    """数据明细单元格格式化: None -> '-'; float 千分位(销售额等); 其余原样。"""
    if v is None:
        return "-"
    if isinstance(v, float):
        # 整数值(如 1.0)去掉小数位, 更符合阅读习惯
        return f"{int(v):,}" if v == int(v) else f"{v:,}"
    return str(v)


def _infer_axis_labels(exec_result: str, stats: dict | None = None) -> tuple[str, str]:
    """从执行结果的首个数据块表头推断坐标轴中文语义标签。

    LLM 生成的 SQL 列名不固定(如 recent_7days_sales / current_week_sales), 经
    COLUMN_CN 映射为用户可读中文; 映射不到则回退原始列名, 再退默认值。
    值列与 _infer_axes 保持一致(指标口径/恒值排除), 否则多指标结果
    (品类/销售额/订单数)会出现"y 轴标签是销售额、数据却是订单数"的错位。
    返回 (x_label, y_label)。
    """
    for block in re.split(r"^rows=\d+\s*$", exec_result, flags=re.M):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        header_line = _find_header_line(lines)
        if not header_line:
            continue
        headers = [h.strip() for h in header_line.split(",")]
        if len(headers) >= 2:
            dim_idx, val_idx = _infer_axes(headers, stats)
            return (
                _col_cn(headers[dim_idx]) or "分类",
                _col_cn(headers[val_idx]) or "数值",
            )
    return "分类", "数值"


_STATS_RE = re.compile(r"^STATS: (\{.*\})$", re.M)


def _parse_stats(output: str) -> dict:
    """解析沙箱输出的 STATS 聚合行(JSON, 规则统计, 全量行数的真实聚合)。

    payload: {"count": N, "cols": {列名: {"sum": .., "max": .., "top": "label:val; ..."}}}
    每个数值列独立统计 —— 3 列数据(客户/名称/金额)时"第2列是字符串"不影响金额列统计。
    """
    if not output:
        return {}
    m = _STATS_RE.search(output)
    if not m:
        return {}
    try:
        s = json.loads(m.group(1))
        if isinstance(s, dict) and s.get("count") is not None:
            return s
    except Exception:  # noqa: BLE001 - 解析失败按无统计处理
        pass
    return {}


def _pick_value_col(stats: dict, headers: list[str]) -> str | None:
    """从 STATS 的数值列中, 选择报告当前使用的值列(与 _infer_axes 完全一致)。

    指标口径优先 + 排除恒值合计列 —— 否则 [category_l1, order_count, total_orders]
    会从后往前选中 total_orders(sum=16664), 把 KPI 合计覆盖成 8×订单总数,
    而图表/明细(经 _infer_axes)是真实 2083, 看板自相矛盾(回归根因)。
    """
    if not headers:
        return None
    try:
        _, val_idx = _infer_axes(headers, stats)
        h = headers[val_idx]
        cols = stats.get("cols") or {}
        if h in cols and cols[h].get("sum") is not None:
            return h
    except Exception:  # noqa: BLE001 — 推断失败回退原有逻辑
        pass
    cols = stats.get("cols") or {}
    for h in reversed(headers):
        if h in cols and cols[h].get("sum") is not None:
            return h
    return None


def _load_exec_full(task_id: str, out_dir: Path) -> str:
    """读取大结果集落盘的全量输出(executor 审批挂起/中等结果集时写入); 无则返回空串。

    大结果集审批通过后, reporter 必须基于全量数据做统计/图表,
    而不是 exec_result 截断(前 10 行) —— 否则 KPI/TOP 数字失真。
    路径按 task_id 唯一(与 executor._persist_exec_full 对应), 不依赖日期目录,
    避免审批跨午夜后按"当天"目录读不到全量(回归根因)。
    """
    try:
        f = settings.reports_dir / f"{task_id}.exec_full.txt"
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 读取失败按无全量处理
        pass
    return ""


def _parse_stats_top(top_txt: str) -> list[tuple[str, float]]:
    """解析 STATS top 串 'A:1.0; B:2.0' -> [(A,1.0),(B,2.0)]。"""
    out: list[tuple[str, float]] = []
    for part in top_txt.split(";"):
        part = part.strip()
        if ":" in part:
            k, _, v = part.rpartition(":")
            try:
                out.append((k.strip(), float(v)))
            except (TypeError, ValueError):
                continue
    return out


def _aggregate_labels(data: list[dict]) -> list[dict]:
    """按 label 聚合求和(多维主查询如 日期×品类 时, 同一品类多天出现需合并为一条)。

    客户消费(label=客户 唯一)不受影响; 品类×日期场景避免同一品类重复出柱/饼块。
    """
    agg: dict[str, float] = {}
    for d in data:
        agg[d["label"]] = agg.get(d["label"], 0.0) + d["value"]
    return [{"label": k, "value": v} for k, v in agg.items()]


def _stat_text(output: str, headers: list[str] | None = None) -> str:
    """全量数据统计摘要(供 LLM 上下文; 大结果集上千行不能全塞 prompt)。

    优先用沙箱 STATS 聚合行(全量真实统计), 无 STATS 时回退解析样例。
    """
    stats = _parse_stats(output)
    if stats:
        col = _pick_value_col(stats, headers or [])
        if col:
            st = stats["cols"][col]
            top_txt = st.get("top") or ""
            return f"共 {stats['count']} 条记录, 合计 {_fmt_num(st['sum'])}" + (f"; TOP: {top_txt}" if top_txt else "")
    data = _parse_exec_result(output, stats)
    if not data:
        return ""
    total = sum(d["value"] for d in data)
    top = sorted(data, key=lambda x: x["value"], reverse=True)[:5]
    top_txt = "; ".join(f"{d['label']}:{_fmt_num(d['value'])}" for d in top)
    return f"共 {len(data)} 条记录, 合计 {_fmt_num(total)}; TOP5: {top_txt}"


def _exec_result_to_md_table(exec_result: str) -> str:
    """把沙箱执行结果文本转为 Markdown 表格(数据明细, 真实数据)。

    支持多步骤 [步骤N] 拼接: 跨块合并相同表头的数据并去重, 输出一张干净表格;
    列名经 COLUMN_CN 映射为中文(从用户出发)。解析失败时降级为截断文本。
    """
    if not exec_result.strip():
        return ""
    blocks = re.split(r"^rows=\d+\s*$", exec_result, flags=re.M)
    all_headers_raw: list[str] | None = None
    all_headers: list[str] | None = None
    all_rows: list[tuple] = []
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if lines and lines[0].startswith("[步骤"):
            lines = lines[1:]
        if not lines:
            continue
        header_line = _find_header_line(lines)
        if not header_line:
            continue
        headers = [h.strip() for h in header_line.split(",")]
        if all_headers_raw is None:
            all_headers_raw = headers
            all_headers = [_col_cn(h) for h in headers]
        if headers != all_headers_raw:  # 表头不同则跳过该块(保持单表)
            continue
        for l in lines:
            if not (l.startswith("(") and l.endswith(")")):
                continue
            vals = _parse_row_values(l)
            if len(vals) != len(headers):
                continue
            row = tuple(_fmt_cell(v) for v in vals)
            if row not in all_rows:
                all_rows.append(row)
    if not all_headers:
        return exec_result[:2000]
    md = [
        "| " + " | ".join(all_headers) + " |",
        "| " + " | ".join(["---"] * len(all_headers)) + " |",
    ]
    for row in all_rows[:50]:
        md.append("| " + " | ".join(row) + " |")
    if len(all_rows) > 50:
        md.append("| ... |")
    return "\n".join(md)


def _add_table_widths(html_body: str) -> str:
    """为表格表头设置绝对像素列宽(th 上), 避免 LibreOffice 窄列强制换行。

    LibreOffice 6.4 的 HTML 导入器对 <colgroup> 与 th 百分比宽度支持均有限
    (实测均不生效, "近7天销售额"仍被拆行), 绝对像素 <th width="N"> 最可靠。
    A4 可用宽约 170mm ≈ 620px@96dpi: 首列(分类)固定 110px, 其余列均分余量。
    """
    def _repl(m: re.Match) -> str:
        table_open, inner = m.group(1), m.group(2)
        n = len(re.findall(r"<th[^>]*>", inner))
        if n < 2:
            return m.group(0)
        first = 110
        rest = max((620 - first) // (n - 1), 90)
        widths = iter([first] + [rest] * (n - 1))
        inner2 = re.sub(r"<th>", lambda m2: f'<th width="{next(widths)}">', inner, count=n)
        return f"{table_open}{inner2}"

    return re.sub(r"(<table[^>]*>)(.*?</table>)", _repl, html_body, flags=re.S)


def _build_report_html(md_file: Path) -> str:
    """Markdown -> 带样式的 HTML(供 LibreOffice/weasyprint 转 PDF 共用)。"""
    import markdown as md_lib

    md_text = md_file.read_text(encoding="utf-8")
    html_body = md_lib.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])
    # LibreOffice 的 HTML 导入器不实现 CSS max-width(见 Bugzilla #151033), 图片会按原始
    # 像素尺寸嵌入 A4 页面导致超宽截断; 显式 width 属性(HTML 4 标准)最可靠。
    # A4 可用宽约 170mm ≈ 640px@96dpi, 取 620 留安全边距(源图 1200px 缩到 620 显示依然清晰)。
    html_body = re.sub(r"<img(?=[^>]*\bsrc=)", '<img width="620"', html_body)
    # 表格宽度用属性 + CSS 双保险(LibreOffice 对 CSS width 支持有限, 属性最稳)
    html_body = re.sub(r"<table(?=[^>]*>)", '<table width="100%"', html_body)
    # 表头百分比列宽: 防止 LibreOffice 把表格列压窄导致内容拆行
    html_body = _add_table_widths(html_body)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="GENERATOR" content="LibreOffice">
<style>
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
       margin: 20mm 16mm; color: #1a1a1a; line-height: 1.7; font-size: 14px; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #4f46e5; padding-bottom: 8px; }}
h2 {{ font-size: 17px; margin-top: 24px; color: #4f46e5; }}
h3 {{ font-size: 15px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
table, th, td {{ border: 1px solid #d1d5db; }}
th, td {{ padding: 5px 8px; font-size: 12.5px; }}
th {{ background: #eef2ff; }}
img {{ max-width: 100%; height: auto; margin: 12px 0; }}
code {{ background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }}
</style></head><body>{html_body}</body></html>"""


def _render_pdf(md_file: Path, pdf_file: Path, chart_name: str = "") -> bool:
    """Markdown -> HTML -> PDF。

    首选 weasyprint(HTML+CSS 渲染引擎): 完整支持 CSS, 表格百分比列宽/图片尺寸/
    页边距均可控(实测 LibreOffice 6.4 的 HTML 导入器对表格宽度控制全部失效,
    "近7天销售额"等内容被拆行); 失败则回退 LibreOffice headless 转换。
    图表用相对路径嵌入, HTML 与 md 同目录。
    """
    html = _build_report_html(md_file)
    html_file = pdf_file.with_suffix(".html")

    # ---- 方案 1: weasyprint(首选) ----
    try:
        from weasyprint import HTML

        HTML(string=html, base_url=str(md_file.parent)).write_pdf(str(pdf_file))
        if pdf_file.exists() and pdf_file.stat().st_size > 0:
            logger.info("report_pdf_rendered", path=str(pdf_file), engine="weasyprint")
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_weasyprint_failed", error=str(exc)[:300])

    # ---- 方案 2: LibreOffice headless 转换(回退) ----
    try:
        import subprocess

        html_file.write_text(html, encoding="utf-8")
        # 唯一 UserInstallation 避免并发任务争用 soffice 锁
        profile = f"file:///tmp/lo_profile_{pdf_file.stem[:8]}"
        subprocess.run(
            [
                "soffice", "--headless", "--norestore",
                "-env:UserInstallation=" + profile,
                # 强制以 Writer 模式导入 HTML: headless 默认走 Web 模式(无固定页面概念),
                # 会导致转换结果第一页是空白页(实测复现, 见 SO #36845426 / AskLO #2928)
                "--infilter=HTML (StarWriter)",
                "--convert-to", "pdf",
                "--outdir", str(pdf_file.parent),
                str(html_file),
            ],
            capture_output=True,
            timeout=120,
        )
        if pdf_file.exists() and pdf_file.stat().st_size > 0:
            logger.info("report_pdf_rendered", path=str(pdf_file), engine="libreoffice")
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_libreoffice_failed", error=str(exc)[:300])

    # PDF 生成失败不阻塞, 保留 md 供前端展示/下载
    return False


# 日期/时间语义列名: 多维推断维度列时排除(日期是"非数值"但不应作为聚合维度)
_DATE_COL_NAMES = {
    "order_day", "order_date", "date", "day", "week", "week_start", "month",
    "year", "time", "created_at", "updated_at", "paid_at", "delivered_at",
}


def _infer_axes(headers: list[str], stats: dict | None = None) -> tuple[int, int]:
    """推断 (维度列索引, 值列索引)。

    - 值列: 优先命中"指标口径目录"的列(用户设置的口径, 如 order_count/订单量)——
      多指标结果(品类/销售额/订单数)按口径取列, 而非碰运气取最后一个;
      其次取最后一个出现在 STATS 数值列的列, 但排除"恒值合计列"
      (sum≈max×count, 即每行值相同的总数列, 如 total_orders 每行都是 2083,
      会被误当成值列导致图表/明细/KPI 全部失真 —— 回归根因);
      无 STATS 时取最后一列
    - 维度列: 非数值列中第一个"非日期语义"列(日期 order_day 不应作聚合维度);
      无合适列则取值列左侧第一列
    """
    if not headers:
        return 0, 0
    cols = (stats or {}).get("cols") or {}
    count = (stats or {}).get("count") or 0
    val_idx = None
    metric_col = _match_metric_col(headers, cols)
    if metric_col is not None:
        st = cols.get(headers[metric_col]) or {}
        s, mx = st.get("sum"), st.get("max")
        # 指标命中的列同样要过恒值检测: 若自定义指标恰好注册了合计列名, 不能选中
        if not (count > 1 and mx and s is not None and abs(s - mx * count) < max(1.0, abs(s) * 1e-9)):
            val_idx = metric_col
    if val_idx is None:
        for i in range(len(headers) - 1, -1, -1):
            if headers[i] in cols and cols[headers[i]].get("sum") is not None:
                st = cols[headers[i]]
                s, mx = st.get("sum"), st.get("max")
                # 恒值列: 所有行取值相同(sum = max × count), 是"合计列"而非维度值
                if count > 1 and mx and s is not None and abs(s - mx * count) < max(1.0, abs(s) * 1e-9):
                    continue
                val_idx = i
                break
    if val_idx is None:
        val_idx = len(headers) - 1
    dim_idx = None
    for i in range(len(headers)):
        if i == val_idx:
            continue
        h = (headers[i] or "").lower()
        if headers[i] in cols:  # 数值列(如 customer_id)不作维度
            continue
        if h in _DATE_COL_NAMES:
            continue
        dim_idx = i
        break
    if dim_idx is None:
        dim_idx = val_idx - 1 if val_idx >= 1 else 0
    return dim_idx, val_idx


def _match_metric_col(headers: list[str], cols: dict) -> int | None:
    """headers 中命中"指标口径目录且为数值列"的**最后**一个列索引。

    用户配置的指标口径(metric_definitions)应决定图表/明细取哪列、用什么单位;
    LLM 生成的 SQL 列名常与指标 name_en 一致(如 order_count), 中文别名亦可命中。
    取"最后命中"而非目录序第一个: 多指标混出时(品类/销售额/订单数)值列通常在
    靠后, 目录序优先会把 sales_7d 误选为用户要的 order_count(回归根因)。
    """
    try:
        from src.tools.metric_registry import get_metric_registry

        hit: int | None = None
        for m in get_metric_registry()._load():
            names = {m.get("name_en"), m.get("name_cn")} | set(m.get("alias") or [])
            for i, h in enumerate(headers):
                if h in cols and cols[h].get("sum") is not None and h in names:
                    hit = i  # 记录, 继续找更靠后的命中列
        return hit
    except Exception:  # noqa: BLE001 — 指标库不可用不阻塞
        pass
    return None


def _metric_unit(h: str) -> str:
    """按列名查指标口径目录的单位(命中 name_en/别名); 无则空串。

    看板 KPI 卡单位不能硬编码"元"—— 订单数指标单位是"笔"(回归根因)。
    """
    if not h:
        return ""
    try:
        from src.tools.metric_registry import get_metric_registry

        for m in get_metric_registry()._load():
            names = {m.get("name_en"), m.get("name_cn")} | set(m.get("alias") or [])
            if h in names:
                return m.get("unit") or ""
    except Exception:  # noqa: BLE001 — 指标库不可用不阻塞
        pass
    return ""


def _labels_md(data: list[dict], dim_cn: str, val_cn: str) -> str:
    """聚合后的 (label, value) 列表 -> 两列 Markdown 表格。"""
    if not data:
        return ""
    lines = [f"| {dim_cn} | {val_cn} |", "| --- | --- |"]
    for d in data[:50]:
        lines.append(f"| {d['label']} | {_fmt_num(d['value'])} |")
    return "\n".join(lines)


def _kpi_md(kpi: dict, main_block: dict | None, stats: dict | None = None) -> str:
    """核心指标总览(窗口内增强, 不扩大数据范围)。"""
    if not kpi or not kpi.get("total_sales"):
        return "_数据不足_"
    headers = (main_block or {}).get("headers", [])
    dim_idx, val_idx = _infer_axes(headers, stats)
    dim_cn = _col_cn(headers[dim_idx]) if headers else "指标"
    val_cn = _col_cn_override(headers[val_idx] if headers else "", kpi) or "数值"
    val_unit = _metric_unit(headers[val_idx]) if headers else ""
    # 单列结果(COUNT/AVG): 直接展示列名 + 值, 不编造"合计/TOP/元"
    if len(headers) <= 1:
        lines = ["| 指标 | 数值 |", "| --- | --- |"]
        lines.append(f"| {_col_cn_override(headers[dim_idx] if headers else '', kpi) or '数值'} | {_fmt_num(kpi['total_sales'])}{val_unit} |")
        return "\n".join(lines)
    lines = ["| 指标 | 数值 |", "| --- | --- |"]
    lines.append(f"| {val_cn}合计 | {_fmt_num(kpi['total_sales'])}{val_unit} |")
    if kpi.get("total_last") is not None:
        lines.append(f"| 上周销售额 | {_fmt_num(kpi['total_last'])}{val_unit} |")
        lines.append(f"| 环比变化 | {_fmt_num(kpi['change'])} ({kpi['change_pct']:+.1f}%) |")
    if kpi.get("top"):
        lines.append(f"| {val_cn} TOP {dim_cn} | {kpi['top'][0]} ({_fmt_num(kpi['top'][1])}{val_unit}, 占 {kpi['top_contrib_pct']:.1f}%) |")
    if kpi.get("bottom"):
        lines.append(f"| {val_cn}最低 {dim_cn} | {kpi['bottom'][0]} ({_fmt_num(kpi['bottom'][1])}{val_unit}) |")
    return "\n".join(lines)


def _multi_baseline_md(kpi: dict, ext: dict) -> str:
    """多基期对比表(跨窗口扩展): 近7天 / 上周 / 近4周均值 / 去年同周。"""
    lines = ["| 指标 | 数值 |", "| --- | --- |"]
    if kpi.get("total_sales"):
        lines.append(f"| 近7天总销售额 | {_fmt_num(kpi['total_sales'])} |")
    if kpi.get("total_last") is not None:
        lines.append(f"| 上周总销售额 | {_fmt_num(kpi['total_last'])} |")
        if kpi.get("change_pct") is not None:
            lines.append(f"| 环比变化率 | {kpi['change_pct']:+.1f}% |")
    trend = ext.get("trend") or []
    if len(trend) >= 4:
        avg4 = sum(t["value"] for t in trend[-4:]) / 4
        g = (ext.get("trend_window") or {}).get("granularity")
        label = "近 4 日日均销售额" if g == "day" else "近 4 周周均销售额"
        lines.append(f"| {label} | {_fmt_num(avg4)} |")
    if ext.get("yoy"):
        yoy = ext["yoy"]
        lines.append(f"| 去年同周销售额 | {_fmt_num(yoy)} |")
        cur = kpi.get("total_sales")
        if cur and yoy:
            lines.append(f"| 同比变化率 | {(cur - yoy) / yoy * 100:+.1f}% |")
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def _lineage_to_md(runs: list[dict]) -> str:
    """血缘溯源附录: 每次 SQL 执行的 涉及表(中文)/行数/耗时/SQL 摘要。"""
    if not runs:
        return "_本次任务无 SQL 执行记录(可能由缓存/模板生成)_"
    lines = ["| # | 涉及表 | 返回行数 | 耗时 | SQL(截断) |", "| --- | --- | --- | --- | --- |"]
    for r in runs:
        sql_short = (r.get("sql_text") or "").replace("|", "\\|").replace("\n", " ")[:80]
        tables = []
        for t in r.get("tables") or []:
            cn = data_source_table_cn(t)
            tables.append(f"{cn}({t})" if cn else t)
        lines.append(
            f"| {r.get('run_order', 0) + 1} | {', '.join(tables)} | "
            f"{r.get('rows_returned', 0)} | {r.get('duration_ms', 0) / 1000:.1f}s | `{sql_short}` |"
        )
    return "\n".join(lines)


def _metrics_table(metrics: list) -> str:
    """指标口径 markdown 表格。"""
    if not metrics:
        return ""
    lines = ["| 指标 | 口径(聚合表达式) | 默认过滤 | 单位 | 涉及表 |", "| --- | --- | --- | --- | --- |"]
    for m in metrics:
        agg_txt = {
            "sum": f"SUM({m['expr']})",
            "count": f"COUNT({m['expr']})",
            "count_distinct": f"COUNT(DISTINCT {m['expr']})",
            "avg": f"AVG({m['expr']})",
            "custom": f"{m['expr']}",
        }.get(m.get("agg", "sum"), f"{m['agg']}({m['expr']})")
        lines.append(
            f"| {m['name_cn']}({m['name_en']}) | `{agg_txt}` | {m.get('filter') or '-'} | "
            f"{m.get('unit') or '-'} | {', '.join(_fmt_table(t) for t in (m.get('source_tables') or []))} |"
        )
    return "\n".join(lines)


def _fmt_table(t: str) -> str:
    """表名中文展示: 有映射显示 '中文(原名)', 无映射保留原名(溯源可核对)。"""
    cn = data_source_table_cn(t)
    return f"{cn}({t})" if cn else t


def _used_metrics_md(task_id: str) -> str:
    """本任务**实际用到**的平台指标口径清单; 无则空串。

    匹配优先级(强→弱), 避免把整表涉及的指标全部误列:
    1. SQL 的 SELECT 列名/别名精确命中指标 name_en(如 order_count 列 -> 订单量指标)
    2. 指标 name_en/别名作为词出现在 SQL 文本中
    3. 兜底: source_tables 与 SQL 涉及表有交集(仅当上面都无命中, 防止"8月7日单日
       查询"因涉及 orders 表就把 sales_7d(近7天销售额)误列为本次口径 —— 回归根因)
    """
    import re as _re

    from src.tools.lineage import get_task_runs
    from src.tools.metric_registry import get_metric_registry

    runs = get_task_runs(task_id)
    used_tables: set = set()
    sql_lower = ""
    for r in runs:
        used_tables.update(r.get("tables") or [])
        sql_lower += (r.get("sql_text") or "") + "\n"
    sql_lower = sql_lower.lower()

    # 1) SELECT 列名/别名精确命中(AS xxx 或 SELECT 后第一列)
    col_names = set(_re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", sql_lower))
    col_names |= set(_re.findall(r"select\s+(?:distinct\s+)?([a-z_][a-z0-9_]*)", sql_lower))

    # 具体日期范围(单日/区间)查询: 相对时间指标(sales_7d 近7天销售额、sales_change
    # 环比变化等)只是 coder 误用的列别名/衍生, 不是本次口径 —— 排除, 否则
    # "8月7日单日查询"口径清单仍列出"近7天销售额"类指标, 与统计周期事实矛盾(回归根因)
    tr = _extract_time_range(sql_lower)
    exclude_rel = bool(tr and tr.get("kind") in ("single_day", "range"))

    def _is_rel_time_metric(m: dict) -> bool:
        """指标是否依赖相对时间窗口(近N天/上周/环比/同比)。

        按指标名称/别名的语义关键词判定(而非聚合表达式引用) —— 否则客单价
        avg_order_value(expr 为 sales_7d / order_count)会被误排除, 单日查询
        的绝对指标口径不应消失(回归根因)。
        """
        if (m.get("name_en") or "").lower() in _REL_TIME_COLS:
            return True
        txt = " ".join(
            str(x) for x in
            ((m.get("name_cn") or ""), (m.get("name_en") or "")) + tuple(m.get("alias") or [])
        )
        return any(k in txt for k in ("上周", "环比", "同比", "变化", "近7天", "近30天", "前一周", "周均"))

    metrics = get_metric_registry()._load()
    used: list = []
    for m in metrics:
        if exclude_rel and _is_rel_time_metric(m):
            continue
        ne = (m.get("name_en") or "").lower()
        if ne and ne in col_names:
            used.append(m)
    if not used:
        for m in metrics:
            if exclude_rel and _is_rel_time_metric(m):
                continue
            ne = (m.get("name_en") or "").lower()
            if ne and ne in sql_lower:
                used.append(m)
                continue
            if any((a or "").lower() and (a or "").lower() in sql_lower for a in (m.get("alias") or [])):
                used.append(m)
    if not used:
        for m in metrics:
            if exclude_rel and _is_rel_time_metric(m):
                continue
            if set(m.get("source_tables") or []) & used_tables:
                used.append(m)
    return _metrics_table(used)


def _build_board_json(
    state_meta: dict,
    kpi: dict,
    data: list,
    main_block: dict | None,
    ext: dict,
    runs: list[dict],
    time_range: dict | None = None,
) -> dict:
    """构建交互式看板 JSON(前端 ECharts 渲染 + 点击品类联动过滤)。

    结构: kpis(指标卡) / charts(柱/饼/趋势) / table(明细, 下钻目标) / lineage(溯源)
    time_range: 主查询实际时间范围(_extract_time_range 结果), 持久化到 board,
    供看板下钻按同一时间口径聚合(单日查询下钻不能再显示近7天/上周 —— 回归根因)。
    """
    kpis: list[dict] = []
    headers = (main_block or {}).get("headers", [])
    dim_idx, val_idx = _infer_axes(headers, kpi.get("_stats"))
    dim_cn = _col_cn(headers[dim_idx]) if headers else "指标"
    val_cn = _col_cn_override(headers[val_idx] if headers else "", kpi) or "数值"
    val_unit = _metric_unit(headers[val_idx]) if headers else ""  # 指标口径单位(订单数=笔), 不硬编码"元"
    # 单列结果(COUNT/AVG): 单指标卡, 无"合计/元/TOP"冗余
    if len(headers) <= 1:
        kpis.append({"label": _col_cn_override(headers[dim_idx] if headers else "", kpi) or "数值",
                     "value": _fmt_num(kpi.get("total_sales", 0)), "unit": val_unit})
    else:
        if kpi.get("total_sales"):
            kpis.append({"label": f"{val_cn}合计", "value": _fmt_num(kpi["total_sales"]), "unit": val_unit})
        if kpi.get("total_last") is not None:
            kpis.append({"label": "上周销售额", "value": _fmt_num(kpi["total_last"]), "unit": val_unit})
        if kpi.get("change_pct") is not None:
            kpis.append({"label": "环比变化", "value": f"{kpi['change_pct']:+.1f}%", "unit": ""})
        if kpi.get("top"):
            kpis.append({"label": f"TOP {dim_cn}", "value": kpi["top"][0], "unit": _fmt_num(kpi["top"][1])})

    charts: list[dict] = []
    # 柱状图 + 饼图(主数据, 维度列中文名为联动维度, 不再写死"品类")
    if len(data) >= 2:
        charts.append({"id": "c_bar", "type": "bar", "title": f"{dim_cn} {val_cn} 分布",
                       "x_label": dim_cn, "y_label": val_cn, "data": data, "dim": dim_cn})
        charts.append({"id": "c_pie", "type": "pie", "title": f"{dim_cn}结构占比",
                       "x_label": "", "y_label": "", "data": data, "dim": dim_cn})
    # 趋势折线(跨窗口扩展, 无品类维度不参与联动)
    if ext.get("trend"):
        tw = ext.get("trend_window") or {}
        charts.append({"id": "c_trend", "type": "line",
                       "title": tw.get("title", "近 8 周销售趋势"),
                       "x_label": tw.get("x_label", "周"),
                       "y_label": val_cn or "销售额", "data": ext["trend"], "dim": None})

    # 明细表(下钻目标): 主数据按维度聚合(多维 品类×日期 不再重复同一品类多天)
    columns, rows = [], []
    if data:
        columns = [dim_cn, val_cn]
        rows = [[d["label"], d["value"]] for d in data[:500]]

    # 血缘摘要
    lineage = None
    if runs:
        r0 = runs[0]
        lineage = {
            "tables": r0.get("tables", []),
            "rows": r0.get("rows_returned", 0),
            "duration_ms": r0.get("duration_ms", 0),
            "sql": (r0.get("sql_text") or "")[:500],
            "count": len(runs),
        }

    return {
        "task_id": state_meta.get("task_id"),
        "title": state_meta.get("title", ""),
        "intent_text": state_meta.get("intent_text", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kpis": kpis,
        "charts": charts,
        "table": {"columns": columns, "rows": rows},
        "drill_dim": dim_cn,
        "drill_key": headers[dim_idx] if headers else None,  # 维度列原始列名(下钻动态化依据; 不用 headers[0], 日期在前时会是 order_day)
        "metric_col": headers[val_idx] if headers else None,  # 值列原始列名(下钻按此指标聚合, 不再写死销售额)
        "time_range": (  # 主查询时间范围事实(下钻复用同一口径; 仅存必要字段, 含边界运算符)
            {k: time_range[k] for k in ("kind", "start", "end", "start_op", "end_op", "desc") if k in time_range}
            if time_range else None
        ),
        "lineage": lineage,
    }


def _answer_only_report(
    state: PipelineState, user_query: str, exec_result: str, task_id: str, out_dir: Path
) -> dict:
    """简洁问答模式: 一句话直接回答, 不生成图表/看板/PDF(只产出极简 md)。

    用户明确"只要答案/不用报告"时(parse_intent.answer_only), 跳过完整报告链路,
    由 LLM 基于执行结果生成一句话答案; md 标题固定为"直接回答"供前端识别。
    """
    try:
        llm = make_llm(settings.model_reporter, temperature=0.1, node="reporter")
        answer = (
            llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "你是数据分析助手。基于执行结果, 用一句话直接回答用户的问题。"
                        "只输出答案本身(中文), 不要解释、不要列表、不要markdown标题、不要多余内容。",
                    },
                    {"role": "user", "content": f"问题: {user_query}\n执行结果:\n{exec_result[:4000]}"},
                ],
                task_id=state.get("task_id"),
            )
            .content.strip()
        )
    except Exception as exc:  # noqa: BLE001 — 答案生成失败用模板兜底
        logger.error("answer_only_llm_failed", error=str(exc))
        answer = ""
    if not answer:
        answer = "基于数据未能生成答案, 请稍后重试。"

    sections = [
        {"heading": "任务", "body": user_query},
        {"heading": "直接回答", "body": answer},
    ]
    used_md = _used_metrics_md(task_id)
    if used_md:
        sections.append(
            {
                "heading": "数据口径",
                "body": (
                    "- 数据来源: 业务数据库, 经安全沙箱只读查询\n\n"
                    f"### 本次用到的指标口径\n\n{used_md}"
                ),
            }
        )
    md_file = out_dir / f"{task_id}.md"
    render_markdown_report("直接回答", sections, md_file)
    rel_path = f"/static/reports/{datetime.now().strftime('%Y/%m/%d')}/{task_id}.md"
    logger.info("answer_only_finished", report=rel_path)
    return {"final_report": rel_path, "status": "completed", "progress": "报告生成完成"}


def _board_only_report(
    state: PipelineState,
    user_query: str,
    exec_result: str,
    task_id: str,
    out_dir: Path,
    kpi: dict,
    data: list,
    main_block: dict | None,
    ext: dict,
    intent: dict,
    time_range: dict | None = None,
) -> dict:
    """看板模式: 生成交互式看板(board.json), 不生成 PDF/完整报告; md 极简。

    用户"只要看板"时(parse_intent.want_board): 复用 _build_board_json 产出看板数据,
    报告 md 只含一句话答案与数据口径, 前端任务卡片显示"看板"按钮、无"下载 PDF"。
    """
    from src.tools.lineage import get_task_runs

    runs = get_task_runs(task_id)
    board = _build_board_json(
        state_meta={"task_id": task_id, "title": user_query, "intent_text": describe_intent(intent)},
        kpi=kpi,
        data=data,
        main_block=main_block,
        ext=ext,
        runs=runs,
        time_range=time_range,
    )
    (out_dir / f"{task_id}.board.json").write_text(
        json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 一句话答案(失败用模板)
    try:
        llm = make_llm(settings.model_reporter, temperature=0.1, node="reporter")
        answer = (
            llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "你是数据分析助手。基于执行结果, 用一句话直接回答用户的问题。"
                        "只输出答案本身(中文), 不要解释、不要列表、不要markdown标题。",
                    },
                    {"role": "user", "content": f"问题: {user_query}\n执行结果:\n{exec_result[:4000]}"},
                ],
                task_id=state.get("task_id"),
            )
            .content.strip()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("board_only_llm_failed", error=str(exc))
        answer = ""
    if not answer:
        answer = "分析完成, 点击上方'📊 交互式看板'查看详情。"

    sections = [
        {"heading": "任务", "body": user_query},
        {"heading": "直接回答", "body": answer},
    ]
    used_md = _used_metrics_md(task_id)
    if used_md:
        sections.append(
            {
                "heading": "数据口径",
                "body": (
                    "- 数据来源: 业务数据库, 经安全沙箱只读查询\n\n"
                    f"### 本次用到的指标口径\n\n{used_md}"
                ),
            }
        )
    md_file = out_dir / f"{task_id}.md"
    render_markdown_report("看板已生成", sections, md_file)
    rel_path = f"/static/reports/{datetime.now().strftime('%Y/%m/%d')}/{task_id}.md"
    logger.info("board_only_finished", report=rel_path, charts=len(board["charts"]))
    return {"final_report": rel_path, "status": "completed", "progress": "报告生成完成"}


def _fix_data_notes_facts(data_notes: str, time_range: dict | None) -> str:
    """规则校验(确定性): 报告"数据口径/统计周期"必须与 SQL 事实一致。

    LLM 会凭指标目录把单日查询编造成"近7天/7月31日至8月7日"(回归根因);
    这里把 data_notes 中的"统计周期"行强制替换为 SQL 提取的事实
    (如 2026-08-07(单日)), 无该行则插入 —— 不依赖 LLM 判断, 100% 对齐事实。
    """
    if not data_notes:
        return data_notes
    if not time_range or time_range.get("kind") not in ("single_day", "range"):
        return data_notes
    fact = time_range.get("desc") or ""
    # 兼容 "- **统计周期**：xxx" / "- 统计周期: xxx" / "统计周期为 xxx" 等形态
    m = re.search(r"(统计周期[^\n]*?(?:[：:]|为)\s*)[^\n]*", data_notes)
    if m:
        return data_notes[: m.start()] + m.group(1) + fact + data_notes[m.end():]
    return data_notes.rstrip() + f"\n- **统计周期**：{fact}"


class _QualityVerdict(BaseModel):
    """LLM 质量门禁的结构化输出: 报告与查询/数据是否一致及具体问题。"""

    consistent: bool = Field(description="报告是否与用户查询/实际数据一致")
    issues: list[str] = Field(default_factory=list, description="不一致的具体问题(给重生成作为修正反馈)")


_QC_SYSTEM_PROMPT = """你是数据分析报告的质量校验员。比对"用户查询/数据事实"与"已生成报告", 逐项检查:
- 统计周期: 报告描述的时间范围是否与"实际统计时间范围"一致(单日查询绝不能写成近7天/上周/上月等)
- 数据忠实: 报告引用的数字是否与"核心指标/执行结果"一致, 是否编造未查询的内容
- 口径: 指标名称/单位是否与数据一致(订单数不能用销售额/元; 品类数/数值不能与数据矛盾)
只输出 JSON: {"consistent": bool, "issues": [问题1, 问题2...]}。一致时 issues 为空数组。"""


def _quality_gate(
    user_query: str,
    time_range: dict | None,
    kpi: dict,
    exec_result: str,
    rc: "ReportContent",
    llm,
) -> list[str]:
    """LLM 质量门禁: 校验报告与查询/数据事实一致, 返回不一致问题列表(空=通过)。

    校验器自身可能有误判, 由调用方最多重生成一次; LLM 校验失败按通过处理(不阻塞)。
    """
    if not rc or llm is None:
        return []
    time_fact = ""
    if time_range and time_range.get("kind") in ("single_day", "range"):
        time_fact = f"实际统计时间范围: {time_range['desc']}"
    # 门禁 prompt 排除 kpi 内部字段(_stats 全量统计/归一化映射), 避免膨胀
    kpi_shown = {k: v for k, v in (kpi or {}).items() if not k.startswith("_")}
    content = (
        f"用户查询: {user_query}\n{time_fact}\n"
        f"核心指标: {kpi_shown}\n执行结果(截断): {exec_result[:3000]}\n"
        f"报告执行摘要: {rc.executive_summary}\n报告正文(截断): {rc.body[:2000]}\n"
        f"数据口径: {rc.data_notes}"
    )
    try:
        v: _QualityVerdict = invoke_structured(
            llm,
            _QualityVerdict,
            [
                {"role": "system", "content": _QC_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        return v.issues if not v.consistent else []
    except Exception as exc:  # noqa: BLE001 — 门禁自身失败不阻塞报告
        logger.warning("quality_gate_failed", error=str(exc)[:150])
        return []


def reporter_node(state: PipelineState) -> dict:
    """汇总执行结果, 生成含多章节/多图表的报告(Markdown + PDF)并落盘。

    意图感知章节装配:
    - 窗口内增强(执行摘要/KPI/结构占比/贡献度/TopN/数据口径/行动建议): 默认生成
    - 跨窗口扩展(趋势折线/同比/多基期对比): 仅当 parse_intent 触发且补查数据成功
    - 用户"只要/仅"时 strict_only 抑制跨窗口扩展
    """
    user_query = state.get("user_query", "")
    # CR-01 二次脱敏: 数据入库已脱敏, 报告输出前再兜底一次(手机号/身份证)
    exec_result = mask_sensitive(state.get("exec_result", ""))
    task_id = state.get("task_id", "unknown")
    today = datetime.now().strftime("%Y/%m/%d")
    out_dir = settings.reports_dir / today

    # 1) 意图解析 + 数据准备
    intent = parse_intent(user_query)
    # 大结果集审批场景: 优先用 executor 落盘的全量输出(9927 行), 保证 KPI/图表/明细数字真实;
    # exec_result 仅为结构保真截断(rows=N + 表头 + 前 10 行)
    full_output = _load_exec_full(task_id, out_dir) or exec_result
    blocks = _parse_exec_blocks(full_output)
    main_block = blocks[0] if blocks else None
    stats = _parse_stats(full_output)
    data = _aggregate_labels(_parse_exec_result(full_output, stats))[:200]  # 图表/看板/明细共用(聚合去重)
    dheaders = (main_block or {}).get("headers", [])
    dim_idx, val_idx = _infer_axes(dheaders, stats)
    _, kpi_val_idx = _infer_axes(dheaders, stats)
    kpi = _compute_kpis(main_block, kpi_val_idx) if main_block else {}
    # 大结果集/多列场景: 沙箱只展示前 10 行样例, 直接求和会失真(669.7万 而非全量 ~4亿),
    # 且多列(品类/金额/订单数)时样例求和基于错误列; 一律用 STATS 全量聚合行校正
    # (规则统计, 无 LLM 幻觉), 值列从表头自动挑选
    block_headers = dheaders
    if stats and block_headers:
        vcol = _pick_value_col(stats, block_headers)
        if vcol and stats["cols"][vcol].get("sum") is not None:
            st = stats["cols"][vcol]
            kpi["total_sales"] = st["sum"]
            top_items = _parse_stats_top(st.get("top") or "")
            if top_items:
                kpi["top"] = top_items[0]
                kpi["bottom"] = top_items[-1]
                if kpi["total_sales"]:
                    kpi["top_contrib_pct"] = top_items[0][1] / kpi["total_sales"] * 100
    kpi["_stats"] = stats  # 供 _kpi_md/_build_board_json 推断真实值列(多列时不取错列)

    # 实际统计时间范围(从主 SQL 的 order_date 过滤提取): 报告"统计周期"的事实依据,
    # 同时决定"具体日期查询"时相对列名(sales_7d)是否归一化为通用"销售额"
    # (单日查询被 LLM 编造成"近7天/7月31日至8月7日" —— 回归根因)
    from src.tools.lineage import get_task_runs

    # 统一时间范围: 优先 intent.time_range(用户查询解析, 一等公民) —— 显式区间/
    # 相对窗口都在意图层锚定为绝对区间, 不再依赖 SQL 正则猜测; SQL 提取仅作兜底。
    # 列名归一化(sales_7d->销售额)仅对**显式日期**查询生效: 相对窗口(近7天)保留
    # "近7天销售额"语义, 否则语义丢失(回归根因)。
    intent_tr = intent.get("time_range")
    if intent_tr and intent_tr.get("type") == "explicit" and intent_tr.get("start") and intent_tr.get("end"):
        from datetime import date as _date

        sd = _date.fromisoformat(intent_tr["start"])
        ed = _date.fromisoformat(intent_tr["end"])
        time_range = {
            "kind": "single_day" if (ed - sd).days <= 1 else "range",
            "start": intent_tr["start"], "end": intent_tr["end"],
            "start_op": ">=", "end_op": "<",
            "desc": intent_tr["desc"],
        }
        kpi["_rel_col_override"] = {c: "销售额" for c in _REL_TIME_COLS}
    else:
        runs_for_range = get_task_runs(task_id)
        main_sql = (runs_for_range[0].get("sql_text") if runs_for_range else None) or ""
        time_range = _extract_time_range(main_sql)
        if time_range and time_range["kind"] in ("single_day", "range"):
            kpi["_rel_col_override"] = {c: "销售额" for c in _REL_TIME_COLS}
    val_cn = _col_cn_override(dheaders[val_idx] if dheaders else "", kpi) or "数值"
    # 数据明细: 按维度聚合(品类×日期 -> 品类合计), 避免同一品类多天重复展示
    table_md = _labels_md(
        data,
        _col_cn(dheaders[dim_idx]) if dheaders else "维度",
        val_cn,
    ) or _exec_result_to_md_table(full_output)
    want_cross = intent.get("want_trend") or intent.get("want_yoy")
    ext = _fetch_extension_data(intent, task_id) if want_cross else {"trend": [], "yoy": None}

    # 产出模式分发(互斥, 由 parse_intent 保证):
    #   只要看板 -> 生成 board.json + 极简 md, 不生成 PDF
    if intent.get("want_board"):
        return _board_only_report(
            state, user_query, exec_result, task_id, out_dir, kpi, data, main_block, ext, intent,
            time_range=time_range,
        )
    #   只要答案 -> 一句话回答, 不生成图表/看板/PDF
    if intent.get("answer_only"):
        return _answer_only_report(state, user_query, exec_result, task_id, out_dir)
    #   只要 PDF -> 完整报告 + PDF, 不生成看板
    skip_board = bool(intent.get("want_pdf"))

    # 2) 图表生成(窗口内增强固定; 跨窗口按意图)
    from src.tools.chart_gen import (
        generate_bar_chart,
        generate_line_chart,
        generate_pareto_chart,
        generate_pie_chart,
        generate_topn_chart,
    )

    charts: list[str] = []

    def _chart(fn, source, **kw) -> str:
        try:
            p = out_dir / f"{task_id}_{len(charts) + 1}.png"
            return fn(source, p, **kw).name
        except Exception as exc:  # noqa: BLE001 — 单张图失败不阻塞整份报告
            logger.warning("chart_generation_failed", error=str(exc)[:200])
            return ""

    if len(data) >= 2:
        x_label, y_label = _infer_axis_labels(full_output or exec_result, stats)
        # 具体日期查询时相对列名(sales_7d)归一化为通用"销售额"(回归根因)
        if dheaders and val_idx < len(dheaders):
            y_label = _col_cn_override(dheaders[val_idx], kpi) or y_label
        dim, val = x_label or "分类", y_label or "数值"
        p = _chart(generate_bar_chart, data, x_key="label", y_key="value",
                   title=user_query[:30], x_label=dim, y_label=val)
        if p:
            charts.append(p)
        p = _chart(generate_pie_chart, data, label_key="label", value_key="value",
                   title=f"{dim}结构占比")
        if p:
            charts.append(p)
        p = _chart(generate_topn_chart, data, label_key="label", value_key="value",
                   title=f"{val} TOP 5 {dim}", top=5, x_label=val)
        if p:
            charts.append(p)
        p = _chart(generate_pareto_chart, data, label_key="label", value_key="value",
                   title=f"{dim}贡献度(帕累托)", x_label=dim, y_label=val)
        if p:
            charts.append(p)

    # 跨窗口扩展: 趋势折线(用户提到趋势/走势等)
    if intent.get("want_trend") and ext.get("trend"):
        tw = ext.get("trend_window") or {}
        p = _chart(generate_line_chart, ext["trend"], x_key="label", y_key="value",
                   title=tw.get("title", "近 8 周销售趋势"),
                   x_label=tw.get("x_label", "周(周一)"), y_label=val)
        if p:
            charts.append(p)

    # 3) LLM 结构化生成(执行摘要/正文/口径/行动建议); 失败用模板兜底
    # 指标/语义层: 注入锁定口径目录, 约束 LLM 对指标口径的描述
    from src.tools.metric_registry import get_metric_registry

    metric_catalog = get_metric_registry().catalog_prompt(user_query)
    # 时间范围事实注入: 约束 LLM 的"统计周期/指标定义"必须基于实际 SQL 范围,
    # 禁止把单日查询编造成"近7天/上月"等周期(回归根因: 报告口径出现
    # "统计周期 2026年7月31日至8月7日"而实际只是 8月7日单日)
    time_fact = ""
    if time_range and time_range["kind"] in ("single_day", "range"):
        time_fact = (
            f"\n实际统计时间范围(事实, 报告'数据口径/统计周期'必须照抄此值, "
            f"禁止写成近7天/上周/上月等其他周期): {time_range['desc']}"
        )
    llm = None
    try:
        llm = make_llm(settings.model_reporter, temperature=0.2, node="reporter")
        rc: ReportContent = invoke_structured(
            llm,
            ReportContent,
            [
                {"role": "system", "content": _SYSTEM_PROMPT + "\n输出模板(严格 JSON):\n" + _REPORT_OUTPUT_TMPL},
                {
                    "role": "user",
                    "content": (
                        f"需求: {user_query}\n核心指标:\n{kpi}\n{metric_catalog}\n执行结果:\n{exec_result[:6000]}"
                        + time_fact
                        + (f"\n全量统计(数据共 {stats['count']} 行, 仅列统计与TOP):\n{_stat_text(full_output, block_headers)}"
                           if stats else "")
                    ),
                },
            ],
            task_id=state.get("task_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("reporter_llm_failed", error=str(exc))
        headers = (main_block or {}).get("headers", [])
        dim_cn = _col_cn(headers[0]) if headers else "指标"
        val_cn = _col_cn(headers[1]) if len(headers) > 1 else "数值"
        top_txt = f"{val_cn} TOP {dim_cn}为 {kpi.get('top', ['未知', 0])[0]}" if kpi.get("top") else "暂无数据"
        rc = ReportContent(
            executive_summary=f"{val_cn}合计 {_fmt_num(kpi.get('total_sales', 0))}; {top_txt}。建议关注异常动因排查。(由模板生成, 可重试)",
            body=f"### 执行结果\n\n```\n{exec_result[:4000]}\n```",
            data_notes="数据来源: 业务数据库(经沙箱只读查询)。指标口径见报告附录, 仅列出本次查询实际用到的指标。",
            action_items="- P1: 结合业务实际排查数据动因(当前报告由模板生成, 建议重新生成)",
        )

    # 4) 质量校验(两道关卡, 产出可信报告):
    #    a. 规则校验: "统计周期"与 SQL 事实强制对齐(确定性, 不依赖 LLM)
    #    b. LLM 质量门禁: 比对 用户查询 vs 报告 vs 数据; 不一致带反馈重生成一次
    rc.data_notes = _fix_data_notes_facts(rc.data_notes, time_range)
    qc_issues = _quality_gate(user_query, time_range, kpi, exec_result, rc, llm)
    if qc_issues and llm is not None:
        logger.info("reporter_quality_retry", issues=qc_issues[:3])
        try:
            feedback = (
                "上次报告经质量校验发现问题, 请逐条修正(只改表述/口径/周期, 不得改动数据):\n"
                + "\n".join(f"- {i}" for i in qc_issues[:5])
            )
            rc = invoke_structured(
                llm,
                ReportContent,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT + "\n输出模板(严格 JSON):\n" + _REPORT_OUTPUT_TMPL},
                    {
                        "role": "user",
                        "content": (
                            f"需求: {user_query}\n核心指标:\n{kpi}\n{metric_catalog}\n执行结果:\n{exec_result[:6000]}"
                            + time_fact
                            + (f"\n全量统计(数据共 {stats['count']} 行, 仅列统计与TOP):\n{_stat_text(full_output, block_headers)}"
                               if stats else "")
                            + f"\n\n{feedback}"
                        ),
                    },
                ],
                task_id=state.get("task_id"),
            )
            rc.data_notes = _fix_data_notes_facts(rc.data_notes, time_range)
        except Exception as exc:  # noqa: BLE001 — 重生成失败沿用首版(已过规则校验)
            logger.warning("reporter_quality_retry_failed", error=str(exc)[:150])

    # 4) 章节组装(意图感知)
    # 本次实际用到的平台指标口径: 有才展示"数据口径"section 与附录指标清单
    used_metrics_md = _used_metrics_md(task_id)
    kpi_md = _kpi_md(kpi, main_block, stats)
    multi_md = _multi_baseline_md(kpi, ext) if want_cross else ""
    chart_md = "\n\n".join(f"![图表 {i + 1}]({p})" for i, p in enumerate(charts))
    sections: List[dict] = [
        {"heading": "执行摘要", "body": rc.executive_summary},
        {"heading": "任务", "body": f"{user_query}\n\n> 分析范围: {describe_intent(intent)}"},
        {"heading": "报告正文", "body": rc.body},
        {"heading": "核心指标", "body": kpi_md},
        {"heading": "数据图表", "body": chart_md or "_数据不足, 未生成图表_"},
    ]
    if used_metrics_md:
        # 数据口径: 仅当本次查询实际用到平台指标时才展示(口径只列用到的; 无则整段隐藏)
        sections.insert(3, {"heading": "数据口径", "body": rc.data_notes})
    if multi_md:
        sections.append({"heading": "多基期对比", "body": multi_md})
    # 血缘/溯源: 本任务 SQL 执行记录 + 本次实际用到的平台指标口径(附录权威来源)
    from src.tools.lineage import get_task_runs

    runs = get_task_runs(task_id)
    lineage_md = _lineage_to_md(runs)

    # 交互式看板: 生成 board.json(前端看板渲染 + 点击品类下钻联动)
    # 仅"只要 PDF"模式跳过看板(skip_board)
    if not skip_board:
        try:
            board = _build_board_json(
                state_meta={"task_id": task_id, "title": user_query, "intent_text": describe_intent(intent)},
                kpi=kpi,
                data=data,
                main_block=main_block,
                ext=ext,
                runs=runs,
                time_range=time_range,
            )
            (out_dir / f"{task_id}.board.json").write_text(
                json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("board_json_generated", task_id=task_id, charts=len(board["charts"]))
        except Exception as exc:  # noqa: BLE001 — 看板生成失败不影响报告主体
            logger.warning("board_json_failed", error=str(exc)[:200])

    appendix_body = (
        "- 数据来源: 业务数据库, 经安全沙箱只读查询\n"
        "- 图表: 由 matplotlib 生成, 嵌入 PDF 缩放适配 A4\n"
        "- 局限: 趋势/同比为系统按用户意图补查, 若业务表缺失将自动省略对应章节\n\n"
        "### 数据来源与溯源(数字可解释: 每条查询的执行来源)\n\n"
        f"{lineage_md}"
    )
    if used_metrics_md:
        appendix_body += (
            "\n\n### 本次用到的指标口径(口径唯一出口, 报告数字均按此口径计算)\n\n"
            f"{used_metrics_md}"
        )
    sections += [
        {"heading": "数据明细", "body": table_md or "_无数据_"},
        {"heading": "行动建议", "body": rc.action_items},
        {"heading": "附录", "body": appendix_body},
    ]

    md_file = out_dir / f"{task_id}.md"
    render_markdown_report(f"分析报告 — {task_id[:8]}", sections, md_file)

    # 5) 生成 PDF(失败则下载回退 md)
    pdf_file = out_dir / f"{task_id}.pdf"
    ok = _render_pdf(md_file, pdf_file, charts[0] if charts else "")

    rel_path = f"/static/reports/{today}/{task_id}.pdf" if ok else f"/static/reports/{today}/{task_id}.md"
    logger.info("reporter_finished", report=rel_path, pdf=ok, sections=len(sections), charts=len(charts))
    return {"final_report": rel_path, "status": "completed", "progress": "报告生成完成"}
