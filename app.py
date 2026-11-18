"""交换机脚本生成工具 —— Streamlit 入口。

两个页签：模板管理 / 生成。
MVP 只实现 PRD 中的 F1~F4：模板管理、占位符与条件段落、三类校验、Excel 批量生成与导出。
不内置厂商命令，不判断命令语义，不做设备下发。
"""

import io
import json
import zipfile
from datetime import datetime

import streamlit as st

from core import batch, parser, renderer, validator
from storage import db

st.set_page_config(
    page_title="交换机脚本生成工具",
    page_icon="🛠",
    layout="wide",
)

db.init_db()


def _friendly(timestamp: str) -> str:
    """把 ISO 时间显示成 2026-11-18 11:20:33。"""
    return timestamp.replace("T", " ") if timestamp else ""


def check_template_body(body: str) -> bool:
    """保存前做 L1 校验：占位符闭合、语句配对、变量名合法、Jinja2 能解析。

    任一条不过都在原地报错（带行号）并阻断保存，不写库、不静默降级。
    """
    ok, message = parser.check_template(body)
    if not ok:
        st.error(message)
        return False
    ok, message = renderer.check_syntax(body)
    if not ok:
        st.error(message)
        return False
    return True


def condition_inputs(
    template_id: int,
    template: parser.Template,
    auto: list[parser.AutoSegment],
) -> dict[str, str]:
    """条件变量与静态段落的取值，作为整批默认值传给 build_results。"""
    defaults: dict[str, str] = {}
    domains = parser.condition_domains(template)
    if template.conditions:
        st.markdown("**条件变量**")
        st.caption("Excel 里有同名列就按行取值；没有这一列时，整批都用这里选的值。")
        columns = st.columns(min(len(template.conditions), 3))
        for index, name in enumerate(template.conditions):
            options = domains.get(name) or ["yes", "no"]
            with columns[index % len(columns)]:
                if options[:2] == ["yes", "no"]:
                    picked = st.selectbox(
                        name,
                        ["否（跳过这一段）", "是（输出这一段）"],
                        index=0,
                        key=f"cond_{template_id}_{name}",
                    )
                    defaults[name] = "yes" if picked.startswith("是") else "no"
                else:
                    picked = st.selectbox(
                        name, options, index=0, key=f"cond_{template_id}_{name}"
                    )
                    defaults[name] = picked

    if auto:
        st.markdown("**静态段落**（正文里没有变量的连续几行）")
        st.caption(
            "勾选 = 每一行都输出这一段；取消勾选 = 整批只在第一个通过的行输出一次，"
            "后面的行不再输出（不是整段删掉）。"
        )
        for seg in auto:
            included = st.checkbox(
                seg.line_label,
                value=True,
                key=f"auto_{template_id}_{seg.name}",
            )
            defaults[seg.name] = "yes" if included else "once"
    return defaults


def copy_button_html(script: str, enabled: bool) -> str:
    """复制按钮：嵌在 iframe 里，点击时写剪贴板，失败回退 execCommand。"""
    payload = json.dumps(script, ensure_ascii=False).replace("</", "<\\/")
    label = "复制脚本" if enabled else "复制脚本（校验未通过）"
    disabled = "" if enabled else "disabled"
    style = (
        "padding:.4rem .9rem;border-radius:.5rem;border:1px solid rgba(49,51,63,.2);"
        "background:#fff;cursor:pointer;font-size:.95rem"
        if enabled
        else
        "padding:.4rem .9rem;border-radius:.5rem;border:1px solid rgba(49,51,63,.1);"
        "background:#f0f2f6;color:#a3a8b4;cursor:not-allowed;font-size:.95rem"
    )
    return f"""<body style="margin:0;font-family:sans-serif">
<button id="copy" {disabled} style="{style}">{label}</button>
<span id="msg" style="margin-left:.6rem;font-size:.85rem"></span>
<script>
const text = {payload};
const btn = document.getElementById('copy');
const msg = document.getElementById('msg');
function done(ok) {{
  msg.textContent = ok ? '已复制到剪贴板' : '复制失败：请选中代码块，用右上角图标复制';
  msg.style.color = ok ? '#09ab3b' : '#d93025';
}}
btn.addEventListener('click', async () => {{
  try {{
    await navigator.clipboard.writeText(text);
    done(true);
  }} catch (err) {{
    try {{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      done(ok);
    }} catch (err2) {{
      done(false);
    }}
  }}
}});
</script>
</body>"""


def _first_line(text: str) -> str:
    """预览用：取第一行非空内容，过长截断。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= 60 else f"{stripped[:60]}…"
    return ""


def generate_batch(
    tpl: dict,
    template: parser.Template,
    condition_defaults: dict[str, str],
) -> None:
    """Excel 批量：一个工作表 = 一份脚本文档，只拼通过校验的行。"""
    variables = template.placeholders
    conditions = template.conditions
    uploaded = st.file_uploader(
        "上传 Excel（.xlsx，每个工作表第一行写变量名）",
        type=["xlsx"],
        key=f"batch_file_{tpl['id']}",
    )
    st.caption(
        "模板需要这些列："
        + ("、".join(variables) if variables else "无")
        + (f"；条件列（可选）：{'、'.join(conditions)}" if conditions else "")
    )
    if not variables:
        st.warning("这个模板没有合法占位符，每行都会直接输出模板原文。")

    if uploaded is None:
        st.caption("还没有上传文件。")
        return

    try:
        tables = batch.load_tables(uploaded.getvalue())
    except ValueError as exc:
        st.error(f"读取失败：{exc}")
        return

    all_rows: list[batch.BatchRow] = []
    blocked: list[str] = []
    ignored: list[str] = []
    for table in tables:
        result = batch.build_results(
            tpl["body"],
            table.header,
            table.rows,
            sheet=table.name,
            condition_defaults=condition_defaults,
        )
        if not result.header_ok:
            # 一个工作表表头不合规，只让这一张不参与，其它工作表照常
            blocked.append(f"工作表「{table.name}」：{result.header_message}")
            continue
        ignored.extend(f"「{table.name}」{note}" for note in result.ignored_columns)
        all_rows.extend(result.rows)

    if ignored:
        st.warning("已忽略这些列：" + "；".join(ignored))
    if blocked:
        st.error("这些工作表整表不参与输出：\n\n" + "\n\n".join(f"- {b}" for b in blocked))

    if not all_rows:
        st.info("没有可生成的数据行。")
        return

    passed = [row for row in all_rows if row.ok]
    st.caption(
        f"工作表 {len(tables)} 个，数据行 {len(all_rows)} 行，"
        f"通过 {len(passed)} 行，未通过 {len(all_rows) - len(passed)} 行。"
    )

    # 每个变量一列，被条件跳过的格子直接标出来，避免用户以为漏填
    shown_vars = variables + [
        name
        for name in conditions
        if name not in variables and not name.startswith("__auto")
    ]
    table_rows: list[dict[str, object]] = []
    for row in all_rows:
        cells: dict[str, object] = {
            "工作表": row.sheet,
            "行号": row.row_no,
            "状态": "通过" if row.ok else "未通过",
        }
        for name in shown_vars:
            if name in row.skipped:
                cells[name] = "（本行不需要）"
            else:
                cells[name] = row.values.get(name, "")
        cells["预览首行"] = _first_line(row.output) if row.ok else ""
        cells["说明"] = row.message
        table_rows.append(cells)

    st.dataframe(table_rows, hide_index=True)
    if any(row.skipped for row in all_rows):
        st.caption("标「（本行不需要）」的变量，所在段落被条件跳过了，留空也算通过。")

    if not passed:
        st.info("没有通过校验的行，无法生成脚本。")
        return

    # 一个工作表 = 一份脚本文档：该表通过的行按行号拼成一份，段间空一行
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    groups: dict[str, list[batch.BatchRow]] = {}
    for row in passed:
        groups.setdefault(row.sheet, []).append(row)

    def to_script(rows: list[batch.BatchRow]) -> str:
        return "\n\n".join(row.output.rstrip("\n") for row in rows) + "\n"

    st.markdown("**生成的脚本文档**")
    if len(groups) > 1:
        combined = "\n\n".join(to_script(rows).rstrip("\n") for rows in groups.values()) + "\n"
        st.download_button(
            f"合并全部工作表下载（txt，{len(passed)} 段）",
            data=combined.encode("utf-8"),
            file_name=batch.suggested_filename(tpl["name"], timestamp, ".txt"),
            mime="text/plain",
            type="primary",
        )

    for sheet_name, rows in groups.items():
        script = to_script(rows)
        st.markdown(f"#### 工作表「{sheet_name}」")
        st.caption(f"{len(rows)} 段，按行号顺序拼成一份；段与段之间空一行。")
        st.code(script, language="text")
        col_copy, col_export, _ = st.columns([1, 1, 3])
        with col_copy:
            st.iframe(copy_button_html(script, True), height=48)
        with col_export:
            st.download_button(
                f"下载「{sheet_name}」（txt，{len(rows)} 段）",
                data=script.encode("utf-8"),
                file_name=batch.suggested_filename(
                    tpl["name"], timestamp, f"_{batch.safe_part(sheet_name)}.txt"
                ),
                mime="text/plain",
                type="primary" if len(groups) == 1 else "secondary",
                key=f"dl_sheet_{tpl['id']}_{sheet_name}",
            )

    with st.expander("按行单独下载（一般用不到）"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for row in passed:
                archive.writestr(
                    batch.suggested_filename(
                        tpl["name"],
                        timestamp,
                        f"_{batch.safe_part(row.sheet)}_第{row.row_no}行.txt",
                    ),
                    row.output,
                )
        st.download_button(
            "打包下载全部通过行（zip）",
            data=buffer.getvalue(),
            file_name=batch.suggested_filename(tpl["name"], timestamp, ".zip"),
            mime="application/zip",
        )
        options = {f"{row.sheet} 第 {row.row_no} 行": row for row in passed}
        picked_label = st.selectbox(
            "选择要单独下载的行", list(options), key=f"batch_pick_{tpl['id']}"
        )
        picked = options[picked_label]
        st.download_button(
            f"下载 {picked_label}（txt）",
            data=picked.output.encode("utf-8"),
            file_name=batch.suggested_filename(
                tpl["name"],
                timestamp,
                f"_{batch.safe_part(picked.sheet)}_第{picked.row_no}行.txt",
            ),
            mime="text/plain",
        )


# 保存 / 删除后 st.rerun()，用一次性提示把结果带到下一轮
flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)

tab_manage, tab_generate = st.tabs(["模板管理", "生成"])

with tab_manage:
    templates = db.list_templates()

    st.subheader("模板列表")
    if templates:
        st.dataframe(
            [
                {
                    "ID": t["id"],
                    "名称": t["name"],
                    "厂商": t["vendor"],
                    "创建时间": _friendly(t["created_at"]),
                    "更新时间": _friendly(t["updated_at"]),
                }
                for t in templates
            ],
            hide_index=True,
        )
    else:
        st.info("还没有模板，先在下面新建一个。")

    st.divider()
    st.subheader("新建模板")
    # 每次创建成功后换一组 widget key，等于清空表单；校验失败时保留用户输入
    create_token = st.session_state.setdefault("create_token", 0)
    with st.form("create_form"):
        col_name, col_vendor = st.columns([2, 1])
        new_name = col_name.text_input(
            "模板名称",
            key=f"new_name_{create_token}",
            placeholder="例如：接入端口 VLAN 配置",
        )
        new_vendor = col_vendor.text_input(
            "厂商标签",
            key=f"new_vendor_{create_token}",
            placeholder="自由文本，例如：华为",
        )
        new_body = st.text_area(
            "模板正文",
            key=f"new_body_{create_token}",
            height=220,
            placeholder="interface {{ port }}\n port default vlan {{ vlan_id }}",
        )
        create_clicked = st.form_submit_button("保存模板", type="primary")

    if create_clicked:
        if not new_name.strip():
            st.error("模板名称不能为空，未保存。")
        elif check_template_body(new_body):
            db.create_template(new_name.strip(), new_vendor.strip(), new_body)
            st.session_state["flash"] = f"已新建模板「{new_name.strip()}」。"
            st.session_state["create_token"] = create_token + 1
            st.rerun()

    st.divider()
    st.subheader("编辑 / 删除")
    if not templates:
        st.caption("暂无可编辑的模板。")
    else:
        label_to_id = {f"#{t['id']}  {t['name']}": t["id"] for t in templates}
        picked = st.selectbox("选择模板", list(label_to_id))
        template_id = label_to_id[picked]
        tpl = db.get_template(template_id)
        try:
            tpl_parsed = parser.tokenize(tpl["body"])
            detected, conds = tpl_parsed.placeholders, tpl_parsed.conditions
        except parser.TemplateError:
            detected, conds = parser.extract_vars(tpl["body"]), []
        summary = "识别到占位符：" + ("、".join(detected) if detected else "无")
        if conds:
            summary += f"；条件变量：{'、'.join(conds)}"
        st.caption(summary)

        with st.form(f"edit_form_{template_id}"):
            col_name, col_vendor = st.columns([2, 1])
            edit_name = col_name.text_input(
                "模板名称", value=tpl["name"], key=f"edit_name_{template_id}"
            )
            edit_vendor = col_vendor.text_input(
                "厂商标签", value=tpl["vendor"], key=f"edit_vendor_{template_id}"
            )
            edit_body = st.text_area(
                "模板正文",
                value=tpl["body"],
                height=220,
                key=f"edit_body_{template_id}",
            )
            save_clicked = st.form_submit_button("保存修改")

        if save_clicked:
            if not edit_name.strip():
                st.error("模板名称不能为空，未保存。")
            elif check_template_body(edit_body):
                db.update_template(
                    template_id, edit_name.strip(), edit_vendor.strip(), edit_body
                )
                st.session_state["flash"] = f"已更新模板「{edit_name.strip()}」。"
                st.rerun()

        with st.expander("删除这个模板"):
            confirmed = st.checkbox(
                f"我确认删除「{tpl['name']}」（ID {template_id}），删除后不可恢复",
                key=f"delete_confirm_{template_id}",
            )
            if st.button(
                "删除",
                disabled=not confirmed,
                key=f"delete_button_{template_id}",
            ):
                db.delete_template(template_id)
                st.session_state["flash"] = f"已删除模板「{tpl['name']}」。"
                st.rerun()

with tab_generate:
    st.subheader("生成")
    if not templates:
        st.info("还没有模板，先去「模板管理」新建一个。")
    else:
        gen_labels = {}
        for t in templates:
            label = f"#{t['id']}  {t['name']}"
            if t["vendor"]:
                label = f"{label}（{t['vendor']}）"
            gen_labels[label] = t["id"]

        gen_picked = st.selectbox("选择模板", list(gen_labels), key="gen_template")
        gen_tpl = db.get_template(gen_labels[gen_picked])

        try:
            gen_parsed = parser.tokenize(gen_tpl["body"])
        except parser.TemplateError as exc:
            st.error(f"这个模板的正文不合规，无法生成：{exc.message}")
            st.stop()

        with st.expander("查看模板正文"):
            st.code(gen_tpl["body"] or "（空模板）", language="text")

        auto_segments = parser.auto_segments(gen_tpl["body"])
        condition_defaults = condition_inputs(gen_tpl["id"], gen_parsed, auto_segments)
        generate_batch(gen_tpl, gen_parsed, condition_defaults)