"""用户意图解析: 从自然语言需求中提取结构化分析意图(规则引擎, 零 LLM 成本)。

用于报告的"意图感知章节装配"(见产品方案):
- **窗口内增强**(结构占比/贡献度/TopN/KPI): 不扩大数据范围, 默认启用, 不违反用户
  明确的 7 天等时间约束。
- **跨窗口扩展**(趋势折线/同比/近4周均值/多基期对比): 需要查询更多周期的数据,
  **必须**由用户意图触发(提到"趋势/走势/同比/近30天"等), 否则不生成, 避免画蛇添足。
- **strict_only**: 用户明确"只要/仅"时, 抑制跨窗口扩展, 只输出窗口内内容。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict

# 时间窗口: (窗口标识, 关键词列表)
_TIME_WINDOWS: list[tuple[str, list[str]]] = [
    ("7d", ["近7天", "最近7天", "近7日", "最近7日", "7天内", "7日内", "近一周", "最近一周", "一周内"]),
    ("30d", ["近30天", "最近30天", "近30日", "近一个月", "最近一个月", "本月", "当月"]),
    ("90d", ["近90天", "近三个月", "最近三个月", "本季度", "当季"]),
    ("1y", ["近一年", "近12个月", "最近一年", "本年度", "今年以来"]),
    # 上周/上一周等相对日历窗口(注意放最后: "对比上周"等基准词不抢占, 时间窗口优先匹配)
    ("last_week", ["上周", "上一周", "上个星期", "上星期", "前一周", "上周一"]),
]

# 对比基准: (基准标识, 关键词列表) —— "对比上周" 等
_BASELINES: list[tuple[str, list[str]]] = [
    ("last_week", ["对比上周", "较上周", "与上周", "比上周", "环比上周", "上周相比", "上周对比"]),
    ("last_month", ["对比上月", "较上月", "与上月", "比上月", "环比上月", "上月相比"]),
    ("yoy", ["同比", "去年同期", "去年同周", "去年同月", "较去年", "与去年", "比去年"]),
]

# 跨窗口扩展触发词
_TREND_KEYWORDS = ["趋势", "走势", "逐周", "逐日", "逐月", "变化趋势", "时间序列", "折线",
                   "近8周", "近12周", "近8天", "近14天", "近4周", "周度", "月度", "日趋势",
                   "增长率变化", "变化过程", "过程"]
_YOY_KEYWORDS = ["同比", "去年同期", "去年同周", "去年同月", "较去年", "与去年", "比去年", "年度对比"]

# 严格限定词: 用户明确缩小范围, 抑制扩展
_STRICT_KEYWORDS = ["只要", "仅需", "仅看", "只需", "只统计", "只关注", "只要看", "就够了", "即可", "只要这"]

# 简洁问答模式: 用户只要答案, 不要完整报告/看板/PDF
_ANSWER_ONLY_KEYWORDS = [
    "只要答案", "只要结果", "给我答案", "直接告诉我", "直接说", "直接回答",
    "不用报告", "不要报告", "不需要报告", "不生成报告", "不做报告", "不用出报告",
    "不用分析", "不要分析", "不用做分析", "不用详细", "不要详细", "不用长篇",
    "不用看板", "不要看板", "不生成看板", "不用图表", "不要图表",
    "不用pdf", "不要pdf", "不生成pdf", "不用下载",
    "一句话", "简单说", "简要说", "简短回答", "简单回答", "就告诉我结果", "只要告诉我",
]

# 只要看板模式: 生成交互式看板, 不生成 PDF/完整报告
_BOARD_ONLY_KEYWORDS = [
    "只要看板", "只需要看板", "只看板", "要看板", "生成看板", "出个看板",
    "做看板", "给我看板", "看板就行", "看板就够了",
]

# 只要 PDF 模式: 生成 PDF 报告, 不生成看板
_PDF_ONLY_KEYWORDS = [
    "只要pdf", "只需要pdf", "要pdf", "生成pdf", "出pdf", "下载pdf",
    "只要报告", "只需要报告", "要报告文件", "只要pdf报告", "导成pdf", "导出pdf",
]

# 对比意图(环比)触发词
_COMPARE_KEYWORDS = ["对比", "比较", "相比", "环比", "变化", "差异", "增减"]


def _match(keywords: list[str], text: str) -> bool:
    """任一关键词命中即 True(中文无词边界, 直接子串匹配)。"""
    return any(kw in text for kw in keywords)


def parse_time_range(user_query: str) -> dict | None:
    """从用户查询解析**统一时间范围**(相对窗口锚定 + 显式日期区间), 返回:
    {"type": "relative"|"explicit"|None, "window": "7d"|...|None,
     "start": "YYYY-MM-DD"|None, "end": "YYYY-MM-DD"|None,
     "granularity": "day"|"week"|"month"|None, "desc": str}

    业界标准(参考斯坦福 SUTime TIMEX3 规范化): 相对窗口("近7天/上周/近30天/上月")
    在此**锚定为绝对区间** [start, end)(闭开), 显式日期("2026年8月5日到8月11日"/单日)
    解析为绝对区间 —— 全链路(趋势图窗口/下钻口径/报告统计周期/SQL 校验)只消费
    此对象, 不再各处从 SQL 正则或关键词猜测(回归根因: 趋势图固定近8周/下钻近7天)。
    """
    if not user_query:
        return None
    text = user_query.strip()
    # 1) 显式日期优先: 用户明确给了日期区间/单日, 不能用相对窗口猜测
    r = _parse_explicit_dates(text)
    if r:
        return r
    # 2) 对比语境保护: "本周销售额与上周对比" 的"上周"是**基准期**不是主查询范围,
    #    锚定成主查询范围会把本周数据限死在上周(回归根因)。命中对比基准词时
    #    不锚定, 交还 LLM 按查询语义自由生成。
    if _match([k for _bid, kws in _BASELINES for k in kws], text):
        return None
    # 3) 相对窗口锚定
    today = date.today()
    for wid, kws in _TIME_WINDOWS:
        if _match(kws, text):
            return _anchor_relative_window(wid, today)
    if _match(["上月", "上个月", "前一月"], text):
        first_this = today.replace(day=1)
        last_month_end = first_this
        last_month_start = (last_month_end - timedelta(days=1)).replace(day=1)
        return {
            "type": "relative", "window": "last_month",
            "start": last_month_start.isoformat(), "end": last_month_end.isoformat(),
            "granularity": "day", "desc": f"{last_month_start} ~ {last_month_end - timedelta(days=1)}",
        }
    return None


def _parse_explicit_dates(text: str) -> dict | None:
    """解析显式日期: 区间(2026年8月5日到8月11日 / 2026-08-05~2026-08-11 / 8月5日至8月11日)
    与单日(2026年8月7日 / 2026-08-07)。返回与 parse_time_range 相同结构; 无则 None。
    """
    today = date.today()
    year = today.year

    def _mk(start: date, end: date) -> dict:
        desc = f"{start}(单日)" if end == start + timedelta(days=1) else f"{start} ~ {end - timedelta(days=1)}"
        width = (end - start).days
        granularity = "day" if width <= 31 else ("week" if width <= 120 else "month")
        return {
            "type": "explicit", "window": None,
            "start": start.isoformat(), "end": end.isoformat(),
            "granularity": granularity, "desc": desc,
        }

    def _d(y: int, mo: int, d: int) -> date | None:
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # --- 区间 ---
    # 2026年8月5日到2026年8月11日 / 至
    m = re.search(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:到|至|~|-|—|－)\s*"
        r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日", text
    )
    if m:
        y2 = int(m.group(4)) if m.group(4) else year
        s, e = _d(int(m.group(1)), int(m.group(2)), int(m.group(3))), _d(y2, int(m.group(5)), int(m.group(6)))
        if s and e and e >= s:
            return _mk(s, e + timedelta(days=1))
    # 2026-08-05 到 2026-08-11 / ~
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s*(?:到|至|~|-)\s*(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        s, e = _d(int(m.group(1)), int(m.group(2)), int(m.group(3))), _d(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        if s and e and e >= s:
            return _mk(s, e + timedelta(days=1))
    # 8月5日到8月11日(默认当年, 仅当查询中有月份无年份区间)
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:到|至|~|-)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m and not re.search(r"\d{4}\s*年", text):
        s, e = _d(year, int(m.group(1)), int(m.group(2))), _d(year, int(m.group(3)), int(m.group(4)))
        if s and e and e >= s:
            return _mk(s, e + timedelta(days=1))

    # --- 单日 ---
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        d = _d(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return _mk(d, d + timedelta(days=1))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        d = _d(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return _mk(d, d + timedelta(days=1))
    if not re.search(r"\d{4}\s*年", text):
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if m:
            d = _d(year, int(m.group(1)), int(m.group(2)))
            if d:
                return _mk(d, d + timedelta(days=1))
    return None


def _anchor_relative_window(wid: str, today: date) -> dict:
    """相对窗口 -> 绝对区间 [start, end)(闭开), 与 _date_range_desc 口径一致。"""
    if wid == "7d":
        start = today - timedelta(days=6)
        return {"type": "relative", "window": "7d", "start": start.isoformat(),
                "end": (today + timedelta(days=1)).isoformat(), "granularity": "day",
                "desc": f"{start} ~ {today}"}
    if wid == "30d":
        start = today - timedelta(days=29)
        return {"type": "relative", "window": "30d", "start": start.isoformat(),
                "end": (today + timedelta(days=1)).isoformat(), "granularity": "day",
                "desc": f"{start} ~ {today}"}
    if wid == "90d":
        start = today - timedelta(days=89)
        return {"type": "relative", "window": "90d", "start": start.isoformat(),
                "end": (today + timedelta(days=1)).isoformat(), "granularity": "week",
                "desc": f"{start} ~ {today}"}
    if wid == "1y":
        start = today - timedelta(days=364)
        return {"type": "relative", "window": "1y", "start": start.isoformat(),
                "end": (today + timedelta(days=1)).isoformat(), "granularity": "month",
                "desc": f"{start} ~ {today}"}
    if wid == "last_week":
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(days=7)
        return {"type": "relative", "window": "last_week", "start": start.isoformat(),
                "end": monday.isoformat(), "granularity": "day",
                "desc": f"{start} ~ {monday - timedelta(days=1)}"}
    return {"type": "relative", "window": wid, "start": None, "end": None,
            "granularity": None, "desc": "用户指定范围"}


def parse_intent(user_query: str) -> Dict:
    """解析用户需求, 返回结构化意图。

    Returns:
        {
          "time_window": "7d|30d|90d|1y|None",   # 识别到的时间窗口
          "baseline": "last_week|last_month|yoy|None",  # 对比基准
          "want_trend": bool,   # 是否要做趋势分析(跨窗口)
          "want_yoy": bool,     # 是否要做同比对比(跨窗口)
          "want_compare": bool, # 是否要做环比/对比
          "strict_only": bool,  # 用户只要特定内容, 抑制扩展
          "answer_only": bool,  # 只要简洁答案, 不生成完整报告/看板/PDF
          "want_board": bool,   # 只要看板(不生成 PDF/完整报告)
          "want_pdf": bool,     # 只要 PDF 报告(不生成看板)
        }
    """
    text = (user_query or "").strip().lower()  # 统一小写(英文关键词如 pdf/sql 大小写不敏感)
    intent: Dict = {
        "time_window": None,
        "time_range": None,  # 统一时间范围(相对锚定/显式区间): 全链路消费, 见 parse_time_range
        "baseline": None,
        "want_trend": False,
        "want_yoy": False,
        "want_compare": False,
        "strict_only": False,
        "answer_only": False,
        "want_board": False,
        "want_pdf": False,
    }
    if not text:
        return intent

    # 1) 统一时间范围(一等公民): 显式日期区间优先, 其次相对窗口锚定
    intent["time_range"] = parse_time_range(user_query)

    # 1b) 时间窗口标识(兼容: describe_intent/旧逻辑用; 显式日期时保持 None)
    for wid, kws in _TIME_WINDOWS:
        if _match(kws, text):
            intent["time_window"] = wid
            break

    # 2) 对比基准
    for bid, kws in _BASELINES:
        if _match(kws, text):
            intent["baseline"] = bid
            break

    # 3) 跨窗口扩展触发(纯关键词: 只有用户明确提到趋势/同比才扩展, 避免画蛇添足)
    intent["want_yoy"] = _match(_YOY_KEYWORDS, text)
    intent["want_trend"] = _match(_TREND_KEYWORDS, text)

    # 4) 对比意图
    intent["want_compare"] = _match(_COMPARE_KEYWORDS, text)

    # 5) 严格限定: 只给用户要的, 抑制跨窗口扩展
    if _match(_STRICT_KEYWORDS, text):
        intent["strict_only"] = True
        intent["want_trend"] = False
        intent["want_yoy"] = False

    # 6) 产出模式(互斥, 优先级: 看板 > PDF > 简洁答案):
    #    "不用分析报告, 只需要看板" -> 看板模式; "只要答案" -> 简洁问答
    if _match(_BOARD_ONLY_KEYWORDS, text):
        intent["want_board"] = True
    elif _match(_PDF_ONLY_KEYWORDS, text):
        intent["want_pdf"] = True
    elif _match(_ANSWER_ONLY_KEYWORDS, text):
        intent["answer_only"] = True

    return intent


def _date_range_desc(window: str) -> str:
    """相对时间窗口 -> 具体起止日期(供报告"分析范围"展示, 从用户视角落具体日期)。

    示例: 上周 -> "2026-08-04 ~ 2026-08-10"; 最近7天 -> "2026-08-08 ~ 2026-08-14"。
    """
    from datetime import date, timedelta

    today = date.today()
    if window == "7d":
        return f"{today - timedelta(days=6)} ~ {today}"
    if window == "30d":
        return f"{today - timedelta(days=29)} ~ {today}"
    if window == "90d":
        return f"{today - timedelta(days=89)} ~ {today}"
    if window == "1y":
        return f"{today - timedelta(days=364)} ~ {today}"
    if window == "last_week":
        # 上周一 ~ 上周日: 本周一往前推 7 天
        monday = today - timedelta(days=today.weekday())
        return f"{monday - timedelta(days=7)} ~ {monday - timedelta(days=1)}"
    return ""


def describe_intent(intent: Dict) -> str:
    """意图的中文描述(供报告"数据口径/分析范围"章节使用)。

    相对时间窗口一律落到具体日期范围(用户给了"上周"就要写清是哪几天),
    而不是只写"用户指定范围"。
    """
    parts: list[str] = []
    tr = intent.get("time_range")
    if tr and tr.get("desc"):
        # 统一时间范围优先(含显式区间: "2026-08-05 ~ 2026-08-11"/"2026-08-07(单日)")
        if tr.get("type") == "explicit":
            parts.append(f"用户指定范围({tr['desc']})")
        else:
            parts.append(f"{tr['desc']}")
        # 不 return: 继续拼对比基准/趋势/同比, 避免丢弃"与上周对比/含趋势分析"
    elif intent.get("time_window"):
        tw = intent.get("time_window")
        dates = _date_range_desc(tw)
        label = {
            "7d": "最近 7 天",
            "30d": "最近 30 天",
            "90d": "最近 90 天",
            "1y": "近一年",
            "last_week": "上周",
        }.get(tw, "用户指定范围")
        parts.append(f"{label}({dates})" if dates else label)
    else:
        parts.append("用户指定范围")
    bl = intent.get("baseline")
    if bl == "last_week":
        parts.append("与上周对比")
    elif bl == "last_month":
        parts.append("与上月对比")
    elif bl == "yoy":
        parts.append("与去年同期对比")
    if intent.get("want_trend"):
        parts.append("含趋势分析")
    if intent.get("want_yoy"):
        parts.append("含同比分析")
    return "、".join(parts)
