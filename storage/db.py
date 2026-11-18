"""SQLite 存储：templates 表的建表与增删改查。

只做存储，不含业务校验（占位符闭合在 core/parser.py）。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    vendor     TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
)
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表；老库缺 updated_at 时补列并用 created_at 回填。"""
    with closing(_connect()) as conn, conn:
        conn.execute(_SCHEMA)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(templates)")
        }
        if "updated_at" not in columns:
            conn.execute(
                "ALTER TABLE templates ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "UPDATE templates SET updated_at = created_at WHERE updated_at = ''"
            )


def list_templates() -> list[dict]:
    """按 id 倒序列出全部模板（不含正文，列表页用不到）。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, vendor, created_at, updated_at FROM templates ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_template(template_id: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, name, vendor, body, created_at, updated_at FROM templates WHERE id = ?",
            (template_id,),
        ).fetchone()
    return dict(row) if row else None


def create_template(name: str, vendor: str, body: str) -> int:
    with closing(_connect()) as conn, conn:
        now = _now()
        cur = conn.execute(
            "INSERT INTO templates (name, vendor, body, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, vendor, body, now, now),
        )
        return int(cur.lastrowid)


def update_template(template_id: int, name: str, vendor: str, body: str) -> None:
    """更新名称/厂商/正文；created_at 保留创建时间，updated_at 刷新。"""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE templates SET name = ?, vendor = ?, body = ?, updated_at = ? WHERE id = ?",
            (name, vendor, body, _now(), template_id),
        )


def delete_template(template_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
