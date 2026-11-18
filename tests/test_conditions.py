"""core 自检：条件段落、必填豁免、L1~L4 各道关卡。

跑法：.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import unittest

import openpyxl

from core import batch, parser, renderer, validator


def render(body, values):
    return renderer.render(body, values)


class LegacyTemplateTest(unittest.TestCase):
    def test_old_template_output_byte_identical(self):
        body = "interface {{ port }}\n port default vlan {{vlan_id}}\n"
        self.assertEqual(
            render(body, {"port": "Gi0/1", "vlan_id": "10"}),
            "interface Gi0/1\n port default vlan 10\n",
        )

    def test_indent_and_newlines_preserved(self):
        body = "system\n  hostname {{ name }}\n  \n  \nend\n"
        self.assertEqual(
            render(body, {"name": "SW1"}),
            "system\n  hostname SW1\n  \n  \nend\n",
        )

    def test_dollar_and_backslash_values_not_interpreted(self):
        out = render("x {{ v }} y", {"v": "\\1 $&"})
        self.assertEqual(out, "x \\1 $& y")


class ParseTest(unittest.TestCase):
    def test_nested_conditions_collect_vars(self):
        body = (
            "{% if a == 'yes' %}\n"
            "{% if b == 'yes' %}{{ x }}{% endif %}\n"
            "{{ y }}\n"
            "{% endif %}\n"
            "{{ z }}\n"
        )
        tpl = parser.tokenize(body)
        self.assertEqual(tpl.placeholders, ["x", "y", "z"])
        self.assertEqual(tpl.conditions, ["a", "b"])
        self.assertEqual(tpl.variables, ["a", "b", "x", "y", "z"])

    def test_needed_vars_skip_untaken_block(self):
        body = "{% if a == 'yes' %}{{ x }}{% endif %}{{ z }}"
        tpl = parser.tokenize(body)
        self.assertEqual(parser.needed_placeholders(tpl, {"a": "no", "z": "z"}), ["z"])
        self.assertEqual(
            parser.needed_placeholders(tpl, {"a": "yes", "x": "x", "z": "z"}), ["x", "z"]
        )

    def test_var_inside_and_outside_always_needed(self):
        body = "{{ x }}{% if a == 'yes' %}{{ x }}{% endif %}"
        tpl = parser.tokenize(body)
        self.assertEqual(parser.needed_placeholders(tpl, {"a": "no"}), ["x"])

    def test_else_branch_vars(self):
        body = "{% if a == 'yes' %}{{ x }}{% else %}{{ y }}{% endif %}"
        tpl = parser.tokenize(body)
        self.assertEqual(parser.needed_placeholders(tpl, {"a": "no"}), ["y"])
        self.assertEqual(parser.needed_placeholders(tpl, {"a": "yes"}), ["x"])

    def test_nested_inner_skipped_but_outer_taken(self):
        body = "{% if a == 'yes' %}{% if b == 'yes' %}{{ x }}{% endif %}{{ y }}{% endif %}"
        tpl = parser.tokenize(body)
        self.assertEqual(
            parser.needed_placeholders(tpl, {"a": "yes", "b": "no"}), ["y"]
        )

    def test_condition_value_case_and_space_insensitive(self):
        body = "{% if a == 'yes' %}on{% endif %}"
        self.assertEqual(render(body, {"a": "  Yes "}).strip(), "on")

    def test_missing_condition_value_is_error(self):
        tpl = parser.tokenize("{% if a == 'yes' %}{{ x }}{% endif %}")
        with self.assertRaises(parser.TemplateError) as ctx:
            parser.needed_placeholders(tpl, {})
        self.assertIn("「a」没有取值", ctx.exception.message)


class L1TemplateCheckTest(unittest.TestCase):
    def check(self, body):
        ok, message = parser.check_template(body)
        if ok:
            ok, message = renderer.check_syntax(body)
        return ok, message

    def test_old_template_passes(self):
        self.assertEqual(self.check("port default vlan {{ vlan_id }}"), (True, ""))

    def test_unclosed_placeholder(self):
        ok, message = self.check("port {{ vlan_id")
        self.assertFalse(ok)
        self.assertIn("占位符未闭合", message)

    def test_if_without_endif(self):
        ok, message = self.check("{% if a == 'yes' %}{{ x }}")
        self.assertFalse(ok)
        self.assertIn("没有对应的 {% endif %}", message)

    def test_endif_without_if(self):
        ok, message = self.check("{{ x }}{% endif %}")
        self.assertFalse(ok)
        self.assertIn("没有对应的 {% if %}", message)

    def test_else_without_if(self):
        ok, message = self.check("{% else %}{{ x }}")
        self.assertFalse(ok)
        self.assertIn("没有对应的 {% if %}", message)

    def test_duplicate_else(self):
        ok, message = self.check("{% if a == 'yes' %}{% else %}{% else %}{% endif %}")
        self.assertFalse(ok)
        self.assertIn("重复出现 {% else %}", message)

    def test_unsupported_statement_has_line_number(self):
        ok, message = self.check("a\nb\n{% for i in x %}{% endfor %}")
        self.assertFalse(ok)
        self.assertIn("第 3 行", message)
        self.assertIn("不支持", message)

    def test_invalid_var_name_blocked_with_line(self):
        ok, message = self.check("a\n{{ 1abc }}")
        self.assertFalse(ok)
        self.assertIn("第 2 行", message)
        self.assertIn("变量名不合法", message)

    def test_condition_syntax_only_equality(self):
        ok, message = self.check("{% if a > 1 %}{% endif %}")
        self.assertFalse(ok)
        self.assertIn("不支持", message)

    def test_supported_shapes_pass(self):
        bodies = [
            "{% if a == 'yes' %}{{ x }}{% endif %}",
            "{% if a == \"yes\" %}{{ x }}{% else %}{{ y }}{% endif %}",
            "{% if a == 'yes' %}{% if b == 'no' %}{{ x }}{% endif %}{% endif %}",
        ]
        for body in bodies:
            self.assertEqual(self.check(body), (True, ""), body)


class RenderGuardTest(unittest.TestCase):
    def test_missing_var_raises_not_empty(self):
        with self.assertRaises(renderer.RenderError) as ctx:
            render("port default vlan {{ vlan_id }}", {})
        self.assertIn("缺少变量「vlan_id」", ctx.exception.message)
        self.assertIn("无法生成脚本", ctx.exception.message)

    def test_sandbox_blocks_attribute_access(self):
        from jinja2.exceptions import SecurityError

        # 沙箱本身是有效的（纵深防御）
        with self.assertRaises(SecurityError):
            renderer._ENV.from_string("{{ v.__class__ }}").render(v="x")
        # 正常入口更早一步：这种写法变量名不合法，L1 就拦掉了
        with self.assertRaises(renderer.RenderError) as ctx:
            render("{{ v.__class__ }}", {"v": "x"})
        self.assertIn("变量名不合法", ctx.exception.message)

    def test_globals_removed(self):
        with self.assertRaises(renderer.RenderError):
            render("{{ range(3) }}", {})

    def test_value_with_double_brace_caught_by_result_check(self):
        out = render("port {{ v }}", {"v": "10 {{ oops"})
        ok, message = validator.validate_result(out)
        self.assertFalse(ok)
        self.assertIn("残留占位符", message)

    def test_condition_false_drops_paragraph(self):
        body = "system\n hostname {{ h }}\n{% if sys == 'yes' %}\n clock timezone BJ\n{% endif %}\nend\n"
        out = render(body, {"h": "SW1", "sys": "No"})
        self.assertNotIn("clock timezone", out)
        self.assertIn("hostname SW1", out)


class BatchConditionTest(unittest.TestCase):
    BODY = (
        "interface {{ port }}\n"
        " port default vlan {{ vlan_id }}\n"
        "{% if sys == 'yes' %}\n"
        " system\n  hostname {{ hostname }}\n"
        "{% endif %}\n"
    )

    def build(self, header, rows, defaults=None):
        return batch.build_results(self.BODY, header, rows, "A楼", defaults)

    def test_condition_no_exempts_guarded_var(self):
        result = self.build(
            ["port", "vlan_id", "sys", "hostname"],
            [(2, ["Gi0/1", "10", "No", ""])],
        )
        self.assertTrue(result.header_ok)
        row = result.rows[0]
        self.assertTrue(row.ok, row.message)
        self.assertNotIn("system", row.output)

    def test_condition_yes_requires_guarded_var(self):
        result = self.build(
            ["port", "vlan_id", "sys", "hostname"],
            [(2, ["Gi0/1", "10", "Yes", ""])],
        )
        row = result.rows[0]
        self.assertFalse(row.ok)
        self.assertIn("hostname", row.message)

    def test_condition_column_optional_uses_default(self):
        result = self.build(
            ["port", "vlan_id", "hostname"],
            [(2, ["Gi0/1", "10", "SW1"])],
            defaults={"sys": "No"},
        )
        self.assertTrue(result.header_ok)
        self.assertTrue(result.rows[0].ok, result.rows[0].message)
        self.assertNotIn("system", result.rows[0].output)

    def test_missing_placeholder_column_still_blocks_sheet(self):
        result = self.build(["port", "sys"], [(2, ["Gi0/1", "No"])])
        self.assertFalse(result.header_ok)
        self.assertIn("vlan_id", result.header_message)

    def test_unknown_condition_value_is_row_error_not_crash(self):
        result = self.build(
            ["port", "vlan_id", "sys", "hostname"],
            [(2, ["Gi0/1", "10", "maybe", ""])],
        )
        self.assertFalse(result.rows[0].ok)

    def test_row_values_still_checked(self):
        result = self.build(
            ["port", "vlan_id", "sys", "hostname"],
            [(2, ["Gi0/1", "", "No", ""])],
        )
        self.assertFalse(result.rows[0].ok)
        self.assertIn("vlan_id", result.rows[0].message)


class FullWorkbookTest(unittest.TestCase):
    def xlsx(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "A楼"
        ws.append(["port", "vlan_id", "sys"])
        ws.append(["Gi0/1", 10, "Yes"])
        ws.append(["Gi0/2", 20, "No"])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        return buf.getvalue()

    def test_two_rows_one_with_system_block(self):
        tables = batch.load_tables(self.xlsx())
        body = (
            "interface {{ port }}\n"
            " port default vlan {{ vlan_id }}\n"
            "{% if sys == 'yes' %}\n"
            " system\n"
            "{% endif %}"
        )
        result = batch.build_results(body, tables[0].header, tables[0].rows)
        outs = [r.output for r in result.rows if r.ok]
        self.assertEqual(len(outs), 2)
        self.assertIn("system", outs[0])
        self.assertNotIn("system", outs[1])


if __name__ == "__main__":
    unittest.main()

class AutoSegmentTest(unittest.TestCase):
    """正文里没有变量的段，不用写 {% %} 也能勾选输出/不输出。"""

    body = (
        "system-view\n"
        "sysname {{ hostname }}\n"
        "\n"
        "interface {{ port }}\n"
        " port default vlan {{ vlan_id }}\n"
        " undo shutdown\n"
    )

    def test_segments_found_only_for_static_lines(self):
        segments = parser.auto_segments(self.body)
        self.assertEqual([s.preview for s in segments], ["system-view", "undo shutdown"])
        self.assertEqual([s.line for s in segments], [1, 6])
        self.assertEqual([s.name for s in segments], ["__auto1", "__auto2"])

    def test_multi_line_segment_label_shows_range(self):
        body = "sys\naaa\nlocal-user {{ user }} password x\nlocal-user\n"
        segments = parser.auto_segments(body)
        self.assertEqual(
            [seg.line_label for seg in segments],
            ["第 1–2 行：sys / aaa", "第 4 行：local-user"],
        )

    def test_manual_block_content_is_not_auto(self):
        body = "{% if sys == 'yes' %}\nsystem-view\n{% endif %}\nend\n"
        segments = parser.auto_segments(body)
        self.assertEqual([s.preview for s in segments], ["end"])

    def test_default_output_keeps_bytes(self):
        values = {"hostname": "SW1", "port": "Gi0/1", "vlan_id": "10"}
        self.assertEqual(render(self.body, values), self.body.replace("{{ hostname }}", "SW1").replace("{{ port }}", "Gi0/1").replace("{{ vlan_id }}", "10"))

    def test_unchecked_segment_disappears_with_its_newline(self):
        values = {"hostname": "SW1", "port": "Gi0/1", "vlan_id": "10", "__auto1": "no"}
        out = render(self.body, values)
        self.assertNotIn("system-view", out)
        self.assertTrue(out.startswith("sysname SW1\n"), out)
        self.assertIn("undo shutdown", out)

    def test_once_outputs_segment_only_on_first_passing_row(self):
        header = ["hostname", "port", "vlan_id"]
        rows = [
            (2, ["SW1", "Gi0/1", "10"]),
            (3, ["", "Gi0/2", "20"]),  # 这行未通过，不应消耗"只输出一次"
            (4, ["SW3", "Gi0/3", "30"]),
            (5, ["SW4", "Gi0/4", "40"]),
        ]
        result = batch.build_results(
            self.body,
            header,
            rows,
            sheet="S",
            condition_defaults={"__auto1": "once", "__auto2": "once"},
        )
        self.assertTrue(result.header_ok, result.header_message)
        first, failed, third, fourth = result.rows
        self.assertTrue(first.ok)
        self.assertIn("system-view", first.output)
        self.assertIn("undo shutdown", first.output)
        self.assertFalse(failed.ok)
        self.assertNotIn("system-view", third.output)  # 已经输出过一次
        self.assertNotIn("undo shutdown", fourth.output)

    def test_batch_column_can_control_segment_per_row(self):
        header = ["__auto1", "hostname", "port", "vlan_id"]
        rows = [
            (2, ["Yes", "SW1", "Gi0/1", "10"]),
            (3, ["No", "SW2", "Gi0/2", "20"]),
        ]
        result = batch.build_results(self.body, header, rows, sheet="S")
        self.assertTrue(result.header_ok, result.header_message)
        self.assertTrue(all(row.ok for row in result.rows), [r.message for r in result.rows])
        self.assertIn("system-view", result.rows[0].output)
        self.assertNotIn("system-view", result.rows[1].output)