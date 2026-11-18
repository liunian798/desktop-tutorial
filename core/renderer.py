"""渲染：用 Jinja2 沙箱把模板变成脚本文档。

- SandboxedEnvironment：模板不能访问对象内部属性、不能调方法
- StrictUndefined：变量没给值就报错，绝不当成空字符串悄悄输出
- 没有 {% %} 的老模板走同一条路径，输出与纯字符串替换一致
"""

from __future__ import annotations

import re

from jinja2 import StrictUndefined
from jinja2.exceptions import SecurityError
from jinja2.exceptions import TemplateError as JinjaTemplateError
from jinja2.exceptions import TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from . import parser

_UNDEFINED_NAME_RE = re.compile(r"'([^']+)' is undefined")
# 条件标签：把比较改成不分大小写（变量值 |lower，字面量一并小写）
_COND_TAG_RE = re.compile(
    r"""\{%\s*if\s+([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(['"])([^'"]*)\2\s*%\}"""
)


class RenderError(Exception):
    """渲染失败，message 直接给用户看。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _make_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.globals.clear()  # 不留 range/dict/lipsum 之类的全局量
    return env


_ENV = _make_env()


def _normalize_source(body: str) -> str:
    """把 {% if x == 'Yes' %} 统一改写成 {% if x|lower == 'yes' %}。

    这样"值不分大小写"在 Jinja2 里真的成立，而不是我们嘴上说说。
    """

    def _fix(match: re.Match[str]) -> str:
        name, _, literal = match.groups()
        canonical = parser.canonical_condition(literal)
        return f"{{% if {name}|lower == '{canonical}' %}}"

    return _COND_TAG_RE.sub(_fix, body)


def check_syntax(body: str) -> tuple[bool, str]:
    """模板级校验（保存时调用）：Jinja2 自己能不能解析这个模板。"""
    try:
        _ENV.parse(body)
    except TemplateSyntaxError as exc:
        return False, f"模板语法错误：第 {exc.lineno} 行 {exc.message}。"
    return True, ""


def render(body: str, values: dict[str, str]) -> str:
    """按 values 渲染；缺变量、语法错、越权访问都抛 RenderError。

    正文里"没有变量的连续行段"会自动套上 if 块，默认输出；values 里给 no 就去掉。
    """
    source_body, segments = parser.wrap_auto_blocks(body)
    try:
        template = parser.tokenize(source_body)
        conditions = set(template.conditions)
    except parser.TemplateError as exc:
        raise RenderError(exc.message) from exc

    effective = dict(values)
    for seg in segments:
        picked = str(effective.get(seg.name, "")).strip()
        if not picked or picked.lower() in ("once", "一次", "只输出一次"):
            effective[seg.name] = "yes"

    # 条件变量的值统一小写，与改写后的标签比较
    prepared = {
        name: (parser.canonical_condition(str(value)) if name in conditions else value)
        for name, value in effective.items()
    }
    source = _normalize_source(source_body)

    try:
        return _ENV.from_string(source).render(**prepared)
    except UndefinedError as exc:
        found = _UNDEFINED_NAME_RE.search(str(exc))
        if found:
            raise RenderError(
                f"缺少变量「{found.group(1)}」的取值，无法生成脚本。"
            ) from exc
        raise RenderError(f"模板里的变量没有取值：{exc}") from exc
    except SecurityError as exc:
        raise RenderError(
            "模板里出现了不允许的写法（不能访问对象内部属性或调用方法），请检查模板。"
        ) from exc
    except TemplateSyntaxError as exc:
        raise RenderError(f"模板语法错误：第 {exc.lineno} 行 {exc.message}。") from exc
    except JinjaTemplateError as exc:
        raise RenderError(f"模板渲染失败：{exc}") from exc