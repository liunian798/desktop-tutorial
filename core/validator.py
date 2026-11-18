"""结果校验：渲染结果非空、不残留占位符和语句标记。

和 parser / 其它校验一样，统一返回 (ok: bool, message: str)。
"""

from __future__ import annotations

import re

_RESIDUE_RE = re.compile(r"\{\{|\}\}|\{%|%\}")


def check_required(variables: list[str], values: dict[str, str]) -> tuple[bool, str]:
    """参数级：传进来的变量都必须有非空值（纯空格算空）。

    调用方传的应该是"当前这一行真正会用到的变量"：
    被 {% if %} 跳过的段落里的占位符不算必填。
    """
    missing = [name for name in variables if not str(values.get(name, "")).strip()]
    if missing:
        return False, f"还有 {len(missing)} 个必填项未填：{'、'.join(missing)}"
    return True, ""


def validate_result(rendered: str) -> tuple[bool, str]:
    """结果级：非空，且不残留 {{ }} {% %}。"""
    if not rendered.strip():
        return False, "渲染结果为空：模板只有占位符，或填写的值全为空。"

    match = _RESIDUE_RE.search(rendered)
    if match:
        line = rendered.count("\n", 0, match.start()) + 1
        return False, (
            f"渲染结果残留占位符：第 {line} 行有 {match.group()}，"
            "通常是模板里写了非法变量名（如 {{ 1abc }}）、多余的 }} 或没配对的语句。"
        )
    return True, ""
