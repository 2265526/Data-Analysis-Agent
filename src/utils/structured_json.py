"""结构化 JSON 输出辅助: 不依赖模型 response_format 参数, 全模型兼容。

背景: langchain 的 with_structured_output(PydanticModel) 会向 OpenAI 兼容端点
发送 response_format={"type": "json_schema", ...}; 部分模型(如 deepseek-v4-flash)
不支持该类型, 返回 400 "This response_format type is unavailable now", 导致
Supervisor / Planner 节点直接失败。

本模块改为: 提示词约束 JSON + 文本解析 + Pydantic 校验, 任何模型都能用;
解析失败抛异常, 由调用方已有的 fallback 逻辑兜底(不影响流水线)。
"""
from __future__ import annotations

import json
import re
from typing import Any, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# 匹配 ```json ... ``` / ``` ... ``` 代码围栏
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_content(content: str) -> Any:
    """从 LLM 输出中提取 JSON(容错: 剥离代码围栏 / 前后多余文字)。

    依次尝试:
    1. ```json ... ``` 代码围栏内的内容
    2. 整体直接 JSON.parse
    3. 截取第一个 "{" 到最后一个 "}" 之间的子串
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("LLM 输出为空, 无法解析 JSON")

    m = _FENCE_RE.search(text)
    if m:
        return json.loads(m.group(1).strip())

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 输出中解析 JSON: {text[:200]!r}")


def _build_schema_hint(schema: Type[T]) -> str:
    """从 Pydantic 模型生成 JSON 结构提示, 让 LLM 输出严格一致的字段名。"""
    import json

    try:
        js = schema.model_json_schema()
    except Exception:  # noqa: BLE001
        return ""
    props = js.get("properties", {})
    required = js.get("required", [])
    lines = ["你输出的 JSON 必须严格使用以下字段名(键名与类型一一对应, 不要改名字、不要加字段):", "```json"]
    # 顶层对象
    lines.append("{")
    entries = []
    for name, p in props.items():
        t = p.get("type", "any")
        if p.get("$ref") or t == "array" or p.get("anyOf"):
            t = "值(见说明)"
        desc = p.get("description", "")
        note = "必填" if name in required else "可选"
        entry = f'  "{name}": {t}  ({note})'
        if desc:
            entry += f"  // {desc}"
        entries.append(entry)
    lines.append(",\n".join(entries))
    lines.append("}")
    # 数组元素说明(如 PlanOutput.tasks)
    for name, p in props.items():
        items = p.get("items", {})
        if p.get("type") == "array" and items.get("properties"):
            lines.append("")
            lines.append(f'其中 "{name}" 数组的每个元素结构:')
            lines.append("{")
            sub = []
            for n2, p2 in items["properties"].items():
                line = f'  "{n2}": {p2.get("type", "any")}'
                if p2.get("description"):
                    line += f"  // {p2['description']}"
                sub.append(line)
            lines.append(",\n".join(sub))
            lines.append("}")
    lines.append("```")
    return "\n".join(lines)


def invoke_structured(
    llm: Any,
    schema: Type[T],
    messages: list[dict],
    task_id: str | None = None,
    max_retries: int = 1,
) -> T:
    """调用 LLM 并将输出强制解析为 Pydantic 模型。

    Args:
        llm: src.nodes.make_llm 返回的包装对象(支持 invoke(messages, task_id=...))
        schema: Pydantic 模型类, 用于校验与类型转换
        messages: OpenAI 风格消息列表
        task_id: 任务 ID, 用于 token/成本落库
        max_retries: 校验失败时的自动重试次数(把错误回传 LLM 修正), 默认 1

    Returns:
        校验通过的模型实例

    Raises:
        ValueError: 重试耗尽仍失败, 由调用方兜底
    """
    hint = _build_schema_hint(schema)
    last_err: str | None = None

    for attempt in range(max_retries + 1):
        msgs = messages
        if hint:
            last = msgs[-1]
            content = f"{last['content']}\n\n{hint}"
            if last_err:
                content += (
                    f"\n\n上次输出不符合要求, 校验错误: {last_err}。"
                    "请严格按照上面的字段名与类型修正后, 只重新输出 JSON。"
                )
            msgs = [*msgs[:-1], {**last, "content": content}]

        out = llm.invoke(msgs, task_id=task_id)
        content = getattr(out, "content", None)
        if content is None:
            content = str(out)
        try:
            data = parse_json_content(content)
            return schema.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — 交给重试或最终抛出
            # 精简错误: 只保留前 2 条, 便于 LLM 理解并修正
            err_lines = [l for l in str(exc).splitlines() if "Input should" in l or "Field required" in l][:2]
            last_err = "; ".join(err_lines) if err_lines else "输出 JSON 不符合结构"

    raise ValueError(
        f"结构化输出在 {max_retries + 1} 次尝试后仍失败: {last_err}"
    )
