"""辅助任务(开发流程 2.2 表1): 错误分类 / 大结果摘要压缩, 走 qwen-flash(百炼)。

- classify_error_llm: 错误三分类(语法/逻辑/其他), 失败返回 "other" 由调用方规则兜底
- summarize_output: 大结果集压缩为结构化摘要, 失败回退截断
- 两个函数内部都用 make_llm(settings.model_aux)(qwen-flash), 走统一限流/熔断/成本落库
"""
from __future__ import annotations

from typing import Optional

from src.utils.logger import get_logger
from src.utils.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_CLASSIFY_PROMPT = """你是错误分类器。根据数据库/SQL/Python 执行错误信息, 分类为:
- syntax: 语法错误, 代码本身写错, 可原地重写修复
- logic: 逻辑错误, 如表/列不存在、查询逻辑与需求不符, 需要重新规划
- other: 其他(超时/环境/权限等)
只输出一个词: syntax / logic / other"""

_SUMMARIZE_PROMPT = """你是数据分析助手。把下面的执行结果压缩为结构化摘要:
- 保留关键指标、数字、结论
- 去掉重复行与中间过程
- 用中文, 3-6 条要点
执行结果如下:"""


def classify_error_llm(error_log: str, task_id: Optional[str] = None) -> str:
    """LLM 错误三分类(qwen-flash); 失败返回 'other', 调用方用规则引擎兜底。"""
    if not error_log:
        return "other"
    try:
        from src.nodes import make_llm  # 延迟导入避免循环依赖

        llm = make_llm(settings.model_aux, temperature=0, node="aux_classify")
        out = llm.invoke(
            [{"role": "system", "content": _CLASSIFY_PROMPT},
             {"role": "user", "content": error_log[:2000]}],
            task_id=task_id,
        ).content.strip().lower()
        for key in ("logic", "syntax"):
            if key in out:
                return key
        return "other"
    except Exception as exc:  # noqa: BLE001
        logger.warning("classify_llm_failed", error=str(exc))
        return "other"


def summarize_output(
    output: str, max_len: int = 2000, task_id: Optional[str] = None
) -> str:
    """大结果集压缩为摘要(qwen-flash); 失败回退截断前 max_len 字符。"""
    if not output:
        return ""
    try:
        from src.nodes import make_llm  # 延迟导入避免循环依赖

        llm = make_llm(settings.model_aux, temperature=0, node="aux_summarize")
        summary = llm.invoke(
            [{"role": "system", "content": _SUMMARIZE_PROMPT},
             {"role": "user", "content": output[:6000]}],
            task_id=task_id,
        ).content.strip()
        return summary[:max_len]
    except Exception as exc:  # noqa: BLE001
        logger.warning("summarize_failed", error=str(exc))
        return output[:max_len]
