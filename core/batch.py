"""Excel 批量：读 .xlsx → 表头映射变量 → 逐行渲染 + 逐行校验 → 拼成一份脚本。

只读表格数据，不做厂商命令判断，不做下发。
渲染复用 core.renderer，校验复用 core.validator，保证与单份生成一致。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import openpyxl

from . import parser, renderer, validator


@dataclass
class SheetTable:
    name: str
    header: list[str]
    rows: list[tuple[int, list[str]]]  # (Excel 行号, 单元格文本)


@dataclass
class BatchRow:
    sheet: str
    row_no: int  # Excel 里的行号，表头是第 1 行
    values: dict[str, str]
    output: str
    ok: bool
    message: str
    skipped: list[str] = field(default_factory=list)  # 被条件跳过、本行不需要的变量


@dataclass
class BatchResult:
    header_ok: bool
    header_message: str
    variables: list[str]  # 需要 Excel 提供的列（占位符变量）
    rows: list[BatchRow] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)  # 条件变量，列可选


def cell_text(value: object) -> str:
    """单元格转文本：None → 空串；整数值的浮点去掉 .0；其它去首尾空格。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_tables(data: bytes) -> list[SheetTable]:
    """读取工作簿里所有非空工作表，按 sheet 顺序返回。

    每个工作表首行是表头；跳过全空行；列数按实际有内容的最右列截断。
    """
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # openpyxl 抛的异常类型很杂
        raise ValueError(f"不是有效的 .xlsx 文件（{exc}）") from exc

    tables: list[SheetTable] = []
    try:
        for sheet in workbook.worksheets:
            table: list[tuple[int, list[str]]] = []
            for row_no, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = [cell_text(cell) for cell in row]
                if any(cells):
                    table.append((row_no, cells))
            if not table:
                continue  # 空工作表直接跳过

            width = max(len(cells) for _, cells in table)
            for _, cells in table:
                cells.extend([""] * (width - len(cells)))

            header = table[0][1]
            while header and header[-1] == "":
                header.pop()
            rows = [(row_no, cells[: len(header)]) for row_no, cells in table[1:]]
            tables.append(SheetTable(sheet.title, header, rows))
    finally:
        workbook.close()

    if not tables:
        raise ValueError("工作簿里没有内容：至少一个工作表的首行要写变量名表头")
    if all(not table.rows for table in tables):
        raise ValueError("只有表头，没有数据行")
    return tables


def build_results(
    body: str,
    header: list[str],
    rows: list[tuple[int, list[str]]],
    sheet: str = "",
    condition_defaults: dict[str, str] | None = None,
) -> BatchResult:
    """按表头映射变量，逐行渲染并跑参数/结果校验。

    condition_defaults：条件变量没有对应列时的取值（来自生成页的下拉框）。
    """
    try:
        template = parser.tokenize(body)
    except parser.TemplateError as exc:
        return BatchResult(False, exc.message, [])

    variables = template.placeholders
    auto = parser.auto_segments(body)
    auto_names = [seg.name for seg in auto]
    auto_set = set(auto_names)
    emitted_once: set[str] = set()  # 已经输出过的"只输出一次"段落
    conditions = template.conditions + auto_names
    defaults = condition_defaults or {}

    column_of: dict[str, int] = {}
    ignored: list[str] = []
    for index, name in enumerate(header):
        if not name:
            ignored.append(f"第 {index + 1} 列（空列名）")
        elif not parser.VAR_NAME_RE.match(name):
            ignored.append(f"{name}（变量名不合法）")
        elif name in column_of:
            ignored.append(f"{name}（重复列，用第一次出现的）")
        else:
            column_of[name] = index

    # 表头里模板用不到的合法列：忽略但提示
    for name in column_of:
        if name not in variables and name not in conditions:
            ignored.append(f"{name}（模板没有这个占位符）")

    missing = [name for name in variables if name not in column_of]
    if missing:
        return BatchResult(
            header_ok=False,
            header_message=f"表头缺少模板需要的列：{'、'.join(missing)}",
            variables=variables,
            ignored_columns=ignored,
            conditions=conditions,
        )

    result = BatchResult(
        header_ok=True,
        header_message="",
        variables=variables,
        ignored_columns=ignored,
        conditions=conditions,
    )
    for row_no, cells in rows:
        values: dict[str, str] = {}
        for name in variables:
            index = column_of[name]
            values[name] = cells[index] if index < len(cells) else ""
        for name in conditions:
            index = column_of.get(name)
            if index is not None:
                values[name] = cells[index] if index < len(cells) else ""
            else:
                picked = defaults.get(name)
                if picked is None or not str(picked).strip():
                    # 静态段落默认输出；条件变量没给值就留空，让校验报出来
                    picked = "yes" if name in auto_names else ""
                values[name] = picked

        # 静态段落：once = 只在第一个通过的行输出一次，后续行按 no 处理
        pending_once: list[str] = []
        for name in auto_names:
            if _once_values(values.get(name, "")):
                if name in emitted_once:
                    values[name] = "no"
                else:
                    values[name] = "yes"
                    pending_once.append(name)

        # 参数级：只要求"这一行真正会用到"的变量
        try:
            needed = parser.needed_placeholders(template, values)
        except parser.TemplateError as exc:
            result.rows.append(
                BatchRow(sheet, row_no, values, "", False, exc.message)
            )
            continue
        skipped = [name for name in variables if name not in needed]
        required_ok, required_message = validator.check_required(needed, values)
        if not required_ok:
            result.rows.append(
                BatchRow(sheet, row_no, values, "", False, required_message, skipped)
            )
            continue

        # 渲染级：缺变量 / 语法错 / 越权访问都变成"这一行未通过"
        try:
            output = renderer.render(body, values)
        except renderer.RenderError as exc:
            result.rows.append(
                BatchRow(sheet, row_no, values, "", False, exc.message, skipped)
            )
            continue

        # 结果级
        result_ok, result_message = validator.validate_result(output)
        if result_ok:
            emitted_once.update(pending_once)
        result.rows.append(
            BatchRow(
                sheet,
                row_no,
                values,
                output,
                result_ok,
                "" if result_ok else result_message,
                skipped,
            )
        )
    return result


def _once_values(value: str) -> bool:
    return str(value).strip().lower() in ("once", "一次", "只输出一次")


def safe_part(text: str) -> str:
    """去掉文件系统不接受的字符。"""
    return (
        "".join("_" if ch in '\\/:*?"<>|' or ch.isspace() else ch for ch in text)
        .strip("_")
        or "template"
    )


def suggested_filename(template_name: str, timestamp: str, suffix: str = "") -> str:
    """模板名 + 时间戳 组成的文件名。"""
    return f"{safe_part(template_name)}_{timestamp}{suffix}"