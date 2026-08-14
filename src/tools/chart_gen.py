"""图表生成: 基于 matplotlib 生成柱状图/折线图, 输出到报告产物目录。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import matplotlib

matplotlib.use("Agg")  # 无界面后端, 兼容沙箱/容器
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 中文字体配置: 显式注册系统 Noto Sans CJK 并置为优先字体。
# 默认 DejaVu Sans 不含 CJK 字形(中文标题/轴标签渲染成方框, 实测报
# "Glyph ... missing from font DejaVu Sans"); SimHei 在 Linux 上不存在。
# ---------------------------------------------------------------------------
try:
    from matplotlib import font_manager as _fm

    _CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if Path(_CJK_FONT).exists():
        _fm.fontManager.addfont(_CJK_FONT)
    # 选一个实际可解析的 CJK 字体名(SC 优先, 退而 JP/HK), 放字体列表首位
    for _cjk_name in ("Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK HK"):
        try:
            if _fm.findfont(_cjk_name, fallback_to_default=False):
                plt.rcParams["font.sans-serif"] = [_cjk_name, "DejaVu Sans"]
                break
        except Exception:  # noqa: BLE001
            continue
except Exception as exc:  # noqa: BLE001 — 字体注册失败不阻塞, 中文最坏退化为方框
    logger.warning("cjk_font_register_failed", error=str(exc)[:200])
plt.rcParams["axes.unicode_minus"] = False

# 基础字号(嵌入 A4 后按物理尺寸缩放, 保证可读)
plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.labelsize": 12,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
})


def _y_axis_fmt(v: float, _pos=None) -> str:
    """y 轴刻度格式: 大数值用 万/亿 中文单位, 避免 matplotlib 默认 1e6 科学计数法。

    matplotlib 对百万级数值默认启用 offset 计数法(坐标轴左上角显示 1e6,
    刻度变成 0/1/2/3), 用户可读性差; 这里按量级动态转换为中文单位。
    """
    av = abs(v)
    if av >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if av >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:,.0f}"


def _setup_y_axis(ax) -> None:
    """y 轴刻度格式化: 用中文单位刻度(FuncFormatter 天然无科学计数 offset)。"""
    ax.yaxis.set_major_formatter(FuncFormatter(_y_axis_fmt))


def generate_bar_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    x_key: str = "label",
    y_key: str = "value",
    title: str = "图表",
    x_label: str = "分类",
    y_label: str = "数值",
) -> Path:
    """生成柱状图并保存为 PNG, 返回输出路径。

    Args:
        data: [{"label": "周一", "value": 12}, ...]
        output_path: 目标文件路径(父目录需存在)
        x_label/y_label: 坐标轴中文语义标签(如 品类 / 近7天销售额)
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    labels = [str(item.get(x_key, "")) for item in data]
    values = [float(item.get(y_key, 0)) for item in data]

    # 适配 A4 报告: 内容区可用宽约 170mm(≈6.7in), 源图 ~1200x630px,
    # 嵌入 PDF 时以 width=620 显示(见 reporter._build_report_html), 避免超大图被截断
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    _setup_y_axis(ax)  # 刻度用 万/亿 中文单位, 避免 1e6 科学计数法
    ax.tick_params(axis="x", rotation=30)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("chart_generated", path=str(output_path), points=len(values))
    return output_path


def generate_line_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    x_key: str = "label",
    y_key: str = "value",
    title: str = "趋势图",
    x_label: str = "分类",
    y_label: str = "数值",
) -> Path:
    """生成折线图并保存为 PNG。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    labels = [str(item.get(x_key, "")) for item in data]
    values = [float(item.get(y_key, 0)) for item in data]

    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(labels, values, marker="o", color="#55A868")
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    _setup_y_axis(ax)  # 刻度用 万/亿 中文单位, 避免 1e6 科学计数法
    ax.tick_params(axis="x", rotation=30)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("line_chart_generated", path=str(output_path), points=len(values))
    return output_path


def generate_pie_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    label_key: str = "label",
    value_key: str = "value",
    title: str = "结构占比",
) -> Path:
    """生成结构占比饼图(品类销售额占比), 用于"窗口内增强"章节。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    labels = [str(item.get(label_key, "")) for item in data]
    values = [max(float(item.get(value_key, 0)), 0) for item in data]
    total = sum(values) or 1.0

    fig, ax = plt.subplots(figsize=(8, 4.2))
    # 占比 < 3% 的合并为"其他", 避免标签拥挤
    show_labels, show_values = [], []
    others = 0.0
    for lbl, v in zip(labels, values):
        if v / total >= 0.03:
            show_labels.append(lbl)
            show_values.append(v)
        else:
            others += v
    if others > 0:
        show_labels.append("其他")
        show_values.append(others)
    ax.pie(
        show_values,
        labels=show_labels,
        autopct=lambda pct: f"{pct:.1f}%",
        startangle=90,
        counterclock=False,
        textprops={"fontsize": 10},
        wedgeprops={"edgecolor": "white", "linewidth": 1.2},
        colors=["#4f46e5", "#0ea5e9", "#f59e0b", "#ef4444", "#10b981", "#8b5cf6", "#64748b"],
    )
    ax.set_title(title)
    ax.axis("equal")
    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("pie_chart_generated", path=str(output_path), points=len(values))
    return output_path


def generate_topn_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    label_key: str = "label",
    value_key: str = "value",
    title: str = "排名",
    top: int = 5,
    bottom: bool = False,
    x_label: str = "数值",
) -> Path:
    """生成 Top/Bottom N 水平条形图(排名一目了然)。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    items = [(str(item.get(label_key, "")), float(item.get(value_key, 0))) for item in data]
    items.sort(key=lambda x: x[1], reverse=not bottom)
    items = items[:top][::-1]  # 水平条形自下而上, 翻转让最大的在顶部

    labels = [it[0] for it in items]
    values = [it[1] for it in items]
    color = "#ef4444" if bottom else "#4f46e5"

    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.barh(labels, values, color=color, height=0.62)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.xaxis.set_major_formatter(FuncFormatter(_y_axis_fmt))
    for i, v in enumerate(values):
        ax.text(v, i, f" {_y_axis_fmt(v)}", va="center", fontsize=10, color="#374151")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("topn_chart_generated", path=str(output_path), top=len(values))
    return output_path


def generate_pareto_chart(
    data: List[Dict[str, Any]],
    output_path: Path,
    label_key: str = "label",
    value_key: str = "value",
    title: str = "品类贡献度(帕累托)",
    x_label: str = "品类",
    y_label: str = "数值",
) -> Path:
    """生成帕累托图: 柱 = 各维度值(降序), 线 = 累计占比%(80/20 定位主力)。

    x_label/y_label: 坐标轴语义(如 品类/订单数); 不能写死"销售额"——
    查询指标可能是订单数/销量(回归根因)。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    items = sorted(
        ((str(item.get(label_key, "")), float(item.get(value_key, 0))) for item in data),
        key=lambda x: x[1],
        reverse=True,
    )
    labels = [it[0] for it in items]
    values = [it[1] for it in items]
    total = sum(values) or 1.0
    cum = []
    acc = 0.0
    for v in values:
        acc += v
        cum.append(acc / total * 100)

    fig, ax = plt.subplots(figsize=(8, 4.2))
    bars = ax.bar(labels, values, color="#4C72B0", width=0.6)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    _setup_y_axis(ax)
    ax.tick_params(axis="x", rotation=30)

    ax2 = ax.twinx()
    ax2.plot(labels, cum, marker="o", color="#ef4444", linewidth=1.8, label="累计占比")
    ax2.set_ylabel("累计占比 (%)")
    ax2.set_ylim(0, 105)
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    # 80% 参考线
    ax2.axhline(80, color="#ef4444", linestyle="--", linewidth=1, alpha=0.6)
    ax2.legend(loc="lower right", fontsize=10)

    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v, _y_axis_fmt(v), ha="center", va="bottom", fontsize=9)

    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("pareto_chart_generated", path=str(output_path), points=len(values))
    return output_path


def generate_grouped_bar_chart(
    categories: List[str],
    series: List[Dict[str, Any]],
    output_path: Path,
    title: str = "多期对比",
    x_label: str = "分类",
    y_label: str = "销售额",
) -> Path:
    """生成多基期分组柱状图(如 近7天/上周/去年同周 并排对比)。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    n_cat = len(categories)
    n_ser = len(series)
    bar_w = 0.7 / max(n_ser, 1)
    colors = ["#4f46e5", "#0ea5e9", "#f59e0b", "#10b981", "#ef4444"]

    fig, ax = plt.subplots(figsize=(8, 4.2))
    x = list(range(n_cat))
    for si, s in enumerate(series):
        vals = [float(v) for v in s.get("values", [])]
        offset = (si - (n_ser - 1) / 2) * bar_w
        ax.bar(
            [xi + offset for xi in x],
            vals,
            width=bar_w * 0.9,
            label=s.get("name", f"系列{si + 1}"),
            color=colors[si % len(colors)],
        )
    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=10)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    _setup_y_axis(ax)
    ax.tick_params(axis="x", rotation=30)
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(pad=1.0)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info("grouped_chart_generated", path=str(output_path), series=len(series))
    return output_path


def render_markdown_report(
    title: str,
    sections: List[Dict[str, str]],
    output_path: Path,
) -> Path:
    """将结构化内容渲染为 Markdown 报告(后续可由 weasyprint 转 PDF)。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# {title}", ""]
    for section in sections:
        lines.append(f"## {section.get('heading', '')}")
        lines.append("")
        lines.append(section.get("body", ""))
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("report_rendered", path=str(output_path))
    return output_path
