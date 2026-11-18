"""占位符与语句扫描：变量提取、闭合检查、条件块解析。

只做字符串扫描，不渲染、不判断命令语义。
变量名正则：^[A-Za-z_][A-Za-z0-9_]*$

条件语法（MVP 只支持这一种）：
    {% if 变量名 == '值' %} ... {% else %} ... {% endif %}
可以嵌套。其它 {% ... %} 一律视为非法，保存时由这里拦住。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 严格占位符（ARCHITECTURE.md 定义），只匹配合法变量名
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# 宽松占位符，用于找出"形状像占位符但变量名不合法"的片段
LOOSE_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
DELIMITER_RE = re.compile(r"\{\{|\}\}")
# 扫描起点：{{ 或 {%
TAG_START_RE = re.compile(r"\{\{|\{%")
# 条件只支持 变量名 == '字面量'
IF_RE = re.compile(
    r"""^if\s+([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?:'([^']*)'|"([^"]*)")$"""
)


class TemplateError(Exception):
    """模板写错时的可读错误，message 直接给用户看。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class Token:
    kind: str  # text / var / if / else / endif
    line: int
    text: str = ""   # text 片段，或占位符/条件变量名
    value: str = ""  # 条件要比较的字面量


@dataclass
class Template:
    tokens: list[Token]

    @property
    def placeholders(self) -> list[str]:
        """正文里 {{ }} 的变量名，按首次出现排序、去重。"""
        names: list[str] = []
        for tok in self.tokens:
            if tok.kind == "var" and tok.text not in names:
                names.append(tok.text)
        return names

    @property
    def conditions(self) -> list[str]:
        """条件里用到的变量名，按首次出现排序、去重。"""
        names: list[str] = []
        for tok in self.tokens:
            if tok.kind == "if" and tok.text not in names:
                names.append(tok.text)
        return names

    @property
    def variables(self) -> list[str]:
        """需要用户提供的变量：占位符变量 + 条件变量，按首次出现排序。"""
        names: list[str] = []
        for tok in self.tokens:
            if tok.kind in ("var", "if") and tok.text not in names:
                names.append(tok.text)
        return names


def _line_no(body: str, pos: int) -> int:
    return body.count("\n", 0, pos) + 1


def extract_vars(body: str) -> list[str]:
    """按出现顺序返回去重后的合法变量名列表（只看 {{ }}）。"""
    names: list[str] = []
    for match in PLACEHOLDER_RE.finditer(body):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def find_invalid_placeholders(body: str) -> list[tuple[int, str]]:
    """返回 [(行号, 原始片段)]：形状像占位符但变量名不合法的片段。"""
    invalid: list[tuple[int, str]] = []
    for match in LOOSE_PLACEHOLDER_RE.finditer(body):
        if not VAR_NAME_RE.match(match.group(1).strip()):
            invalid.append((_line_no(body, match.start()), match.group(0)))
    return invalid


def check_placeholders_closed(body: str) -> tuple[bool, str]:
    """模板级校验：每个 {{ 必须有对应的 }}。

    多余的 }} 不属于这条校验（结果级由 validator 负责）。
    """
    depth = 0
    first_open_line = 0
    for match in DELIMITER_RE.finditer(body):
        if match.group() == "{{":
            if depth == 0:
                first_open_line = _line_no(body, match.start())
            depth += 1
        elif depth > 0:
            depth -= 1
    if depth:
        return False, f"占位符未闭合：第 {first_open_line} 行的 {{{{ 没有对应的 }}}}。"
    return True, ""


def _tag_token(tag: str, line: int) -> Token:
    if tag == "else":
        return Token("else", line)
    if tag == "endif":
        return Token("endif", line)
    match = IF_RE.match(tag)
    if match:
        literal = match.group(2) if match.group(2) is not None else match.group(3)
        return Token("if", line, match.group(1), literal or "")
    raise TemplateError(
        f"第 {line} 行的语句 {{% {tag} %}} 不支持："
        "只支持 {% if 变量名 == '值' %}、{% else %}、{% endif %} 这三种。"
    )


def _check_blocks(tokens: list[Token]) -> None:
    stack: list[Token] = []
    else_seen: set[int] = set()
    for tok in tokens:
        if tok.kind == "if":
            stack.append(tok)
        elif tok.kind == "else":
            if not stack:
                raise TemplateError(
                    f"第 {tok.line} 行的 {{% else %}} 没有对应的 {{% if %}}。"
                )
            if id(stack[-1]) in else_seen:
                raise TemplateError(f"第 {tok.line} 行重复出现 {{% else %}}，一个 if 只能有一个。")
            else_seen.add(id(stack[-1]))
        elif tok.kind == "endif":
            if not stack:
                raise TemplateError(
                    f"第 {tok.line} 行的 {{% endif %}} 没有对应的 {{% if %}}。"
                )
            stack.pop()
    if stack:
        raise TemplateError(
            f"第 {stack[-1].line} 行的 {{% if %}} 没有对应的 {{% endif %}}。"
        )


def tokenize(body: str) -> Template:
    """把正文切成 text / var / if / else / endif 片段，写错时抛 TemplateError。"""
    tokens: list[Token] = []
    pos = 0
    while True:
        match = TAG_START_RE.search(body, pos)
        if match is None:
            if body[pos:]:
                tokens.append(Token("text", _line_no(body, pos), body[pos:]))
            break
        if match.start() > pos:
            tokens.append(Token("text", _line_no(body, pos), body[pos : match.start()]))
        if match.group() == "{{":
            end = body.find("}}", match.end())
            line = _line_no(body, match.start())
            if end == -1:
                raise TemplateError(f"占位符未闭合：第 {line} 行的 {{{{ 没有对应的 }}}}。")
            name = body[match.end() : end].strip()
            if not VAR_NAME_RE.match(name):
                raise TemplateError(
                    f"第 {line} 行的占位符变量名不合法：{{{{ {name} }}}}。"
                    "变量名要以字母或下划线开头，只能包含字母、数字、下划线。"
                )
            tokens.append(Token("var", line, name))
            pos = end + 2
        else:
            end = body.find("%}", match.end())
            line = _line_no(body, match.start())
            if end == -1:
                raise TemplateError(f"语句未闭合：第 {line} 行的 {{% 没有对应的 %}}。")
            tokens.append(_tag_token(body[match.end() : end].strip(), line))
            pos = end + 2
    _check_blocks(tokens)
    return Template(tokens)


def check_template(body: str) -> tuple[bool, str]:
    """模板级校验（保存时调用）：占位符闭合 + 语句匹配 + 变量名合法。"""
    ok, message = check_placeholders_closed(body)
    if not ok:
        return ok, message
    try:
        tokenize(body)
    except TemplateError as exc:
        return False, exc.message
    return True, ""


_TRUE_WORDS = {"yes", "true", "1", "y", "是", "是。"}
_FALSE_WORDS = {"no", "false", "0", "n", "否"}


def canonical_condition(value: str) -> str:
    """把常见的真假写法归一成 yes / no；其它值原样小写。

    Excel 里填 Yes / yes / TRUE / 1 都算同一个意思，大小写不敏感。
    """
    text = str(value).strip().casefold()
    if text in _TRUE_WORDS:
        return "yes"
    if text in _FALSE_WORDS:
        return "no"
    return text


def condition_domains(template: Template) -> dict[str, list[str]]:
    """每个条件变量在模板里出现过的取值（字面量），按首次出现去重。"""
    domains: dict[str, list[str]] = {}
    for tok in template.tokens:
        if tok.kind != "if":
            continue
        values = domains.setdefault(tok.text, [])
        canonical = canonical_condition(tok.value)
        # yes / no 是一对：写 yes 分支就默认也允许 no
        candidates = ["yes", "no"] if canonical in ("yes", "no") else [canonical]
        for item in candidates:
            if item not in values:
                values.append(item)
    return domains


def condition_holds(token: Token, values: dict[str, str], allowed: list[str]) -> bool:
    """判断一个条件是否成立；没给值或给了模板里没有的值就直接报错，不猜。"""
    raw = values.get(token.text)
    if raw is None or not str(raw).strip():
        raise TemplateError(
            f"第 {token.line} 行的条件变量「{token.text}」没有取值，"
            "无法判断这一段要不要输出。"
        )
    value = canonical_condition(raw)
    allowed_set = [canonical_condition(item) for item in allowed]
    if value not in allowed_set:
        hint = " / ".join(allowed_set)
        raise TemplateError(
            f"第 {token.line} 行的条件变量「{token.text}」填的是「{str(raw).strip()}」，"
            f"模板里只认 {hint}，请改成其中一个。"
        )
    return value == canonical_condition(token.value)


def needed_placeholders(template: Template, values: dict[str, str]) -> list[str]:
    """按当前取值走一遍条件块，返回真正需要填值的占位符变量（按出现顺序、去重）。

    被跳过的 {% if %} 分支里的占位符不会出现在结果里，因此不算必填。
    """
    needed: list[str] = []
    domains = condition_domains(template)
    # 每一层 if 记一条：[条件是否成立, 是否已进入 else, 当前分支是否生效]
    frames: list[list[bool]] = []

    def active() -> bool:
        return frames[-1][2] if frames else True

    for tok in template.tokens:
        if tok.kind == "if":
            holds = condition_holds(tok, values, domains.get(tok.text, []))
            frames.append([holds, False, active() and holds])
        elif tok.kind == "else":
            holds = frames[-1][0]
            parent = frames[-2][2] if len(frames) > 1 else True
            frames[-1][1] = True
            frames[-1][2] = parent and not holds
        elif tok.kind == "endif":
            frames.pop()
        elif tok.kind == "var" and active() and tok.text not in needed:
            needed.append(tok.text)
    return needed

@dataclass
class AutoSegment:
    """正文里连续几行"没有变量"的静态段落。

    不用写任何 {% %} 语法，生成页会给每段一个"输出 / 不输出"的勾选。
    """

    name: str
    line: int  # 起始行号（1 起）
    preview: str  # 段内各行文字，用 " / " 连起来，给勾选框当标签
    text: str
    start_index: int = 0
    end_index: int = 0

    @property
    def end_line(self) -> int:
        return self.line + max(0, self.end_index - self.start_index)

    @property
    def line_label(self) -> str:
        """勾选框文案：单行「第 6 行：local-user」，多行「第 1–2 行：sys / aaa」。"""
        if self.end_line == self.line:
            return f"第 {self.line} 行：{self.preview}"
        return f"第 {self.line}–{self.end_line} 行：{self.preview}"


def _is_static_line(line: str) -> bool:
    return not any(tag in line for tag in ("{{", "}}", "{%", "%}"))


def auto_segments(body: str) -> list[AutoSegment]:
    """找连续的、整行都不含占位符/语句的文字段；手动 {% if %} 块内部不算。

    段落之间用空行分隔；段内行原样保留（连行尾换行）。
    """
    lines = body.splitlines(keepends=True)
    segments: list[AutoSegment] = []
    run: list[int] = []
    depth = 0

    def flush() -> None:
        nonlocal run
        if run:
            start, end = run[0], run[-1]
            text = "".join(lines[start : end + 1])
            if text.strip():
                preview = " / ".join(
                    line.strip() for line in lines[start : end + 1] if line.strip()
                )
                if len(preview) > 40:
                    preview = preview[:40] + "…"
                segments.append(
                    AutoSegment(
                        name=f"__auto{len(segments) + 1}",
                        line=start + 1,
                        preview=preview,
                        text=text,
                        start_index=start,
                        end_index=end,
                    )
                )
        run = []

    for index, line in enumerate(lines):
        if not line.strip():
            flush()
            continue
        if "{%" in line or "%}" in line:
            flush()
            if re.match(r"\{%\s*if\b", line):
                depth += 1
            elif re.match(r"\{%\s*endif\b", line):
                depth = max(0, depth - 1)
            continue
        if depth == 0 and _is_static_line(line):
            run.append(index)
        else:
            flush()
    flush()
    return segments


def wrap_auto_blocks(body: str) -> tuple[str, list[AutoSegment]]:
    """给静态段落套上 {% if __autoN == 'yes' %}，返回 (改写后正文, 段落列表)。

    只在行首行尾插标签，不增删空行；勾选"Yes"时输出与原文逐字节一致。
    """
    segments = auto_segments(body)
    if not segments:
        return body, []
    lines = body.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    for seg in segments:
        out.extend(lines[pos : seg.start_index])
        out.append(f"{{% if {seg.name} == 'yes' %}}")
        out.extend(lines[seg.start_index : seg.end_index + 1])
        out.append("{% endif %}")
        pos = seg.end_index + 1
    out.extend(lines[pos:])
    return "".join(out), segments