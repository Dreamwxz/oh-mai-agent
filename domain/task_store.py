"""MaiBot Agent 插件任务持久化层。

使用 sqlite3 存储任务和任务历史。每个异步方法创建独立连接，
通过 ``asyncio.to_thread`` 在后台线程执行阻塞 sqlite3 调用，
避免阻塞事件循环。启用 WAL 模式以获得更好的读并发性能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .task_record import TaskLevel, TaskRecord, TaskStatus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# TaskStore — 任务存储
# ═══════════════════════════════════════════════════════════════════════


class TaskStore:
    """基于 SQLite 的任务持久化存储。

    管理两张表：

    - ``tasks``：每个任务一行，带索引筛选列。
    - ``task_history``：每个任务的有序历史条目。

    每次方法调用通过 ``asyncio.to_thread`` 创建并关闭连接，
    确保不会有 sqlite3 连接跨线程使用。
    """

    def __init__(self, db_path: str | Path) -> None:
        """保存数据库文件路径；表结构在 :meth:`init` 中创建。"""
        self._db_path = str(db_path)

    # ── 生命周期 ───────────────────────────────────────────────────

    async def init(self) -> None:
        """创建表结构和索引，启用 WAL 日志模式。"""

        logger.info("初始化 TaskStore 数据库：%s", self._db_path)

        def _init() -> None:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        data TEXT NOT NULL,
                        status TEXT,
                        owner TEXT,
                        stream_id TEXT,
                        level TEXT,
                        trigger_type TEXT,
                        scheduled_at TEXT,
                        created_at TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_owner
                    ON tasks(owner)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_stream_id
                    ON tasks(stream_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_level
                    ON tasks(level)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_trigger_type
                    ON tasks(trigger_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks(created_at)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        entry TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_task_history_task_id
                    ON task_history(task_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_task_history_created
                    ON task_history(created_at)
                """)
                conn.commit()

        await asyncio.to_thread(_init)

    async def close(self) -> None:
        """空操作：连接按方法创建和关闭，无需全局 close。"""
        pass

    # ── 增删改查 ────────────────────────────────────────────────────

    async def save(
        self,
        task: TaskRecord,
        *,
        expected_status: TaskStatus | None = None,
    ) -> bool:
        """插入或更新任务（按 id 做 upsert）。

        完整任务序列化为 JSON 存入 ``data`` 列（读取时反序列化还原），
        各筛选索引列为冗余提取，以支持高效查询。

        Args:
            expected_status: 可选 — 乐观锁守卫。仅当持久化记录的
                ``status`` 仍等于 *expected_status* 时才执行写入
                （WHERE id=? AND status=?）。这关闭了「读取→写入」
                之间的 TOCTOU 窗口：若期间调度器将任务置为终态
                （如超时 FAILED），本次写入被原子拒绝，返回 ``False``，
                旧快照不会覆盖终态记录。``None`` 表示不做守卫
                （全量 upsert，默认行为）。

        Returns:
            是否实际写入。守卫模式下，若持久化状态已在读取与写入
            之间被并发修改，返回 ``False``；未开启守卫时恒为 ``True``。
        """
        data = json.dumps(task.to_dict(), ensure_ascii=False)
        status = task.status.value
        owner = task.owner
        stream_id = task.stream_id
        level = task.level.value
        trigger_type = task.trigger_type.value
        scheduled_at = task.scheduled_at.isoformat() if task.scheduled_at else None
        created_at = task.created_at.isoformat()

        def _save() -> bool:
            with closing(sqlite3.connect(self._db_path)) as conn:
                if expected_status is not None:
                    # 乐观锁守卫：仅当当前 status 与守卫值一致时更新，
                    # 否则 rowcount=0 → 返回 False，不覆盖并发写入的终态。
                    cur = conn.execute("""
                        UPDATE tasks
                        SET data = ?, status = ?, owner = ?, stream_id = ?,
                            level = ?, trigger_type = ?, scheduled_at = ?,
                            created_at = ?
                        WHERE id = ? AND status = ?
                    """, (data, status, owner, stream_id, level, trigger_type,
                          scheduled_at, created_at, task.id, expected_status.value))
                    conn.commit()
                    return cur.rowcount > 0
                conn.execute("""
                    INSERT INTO tasks
                        (id, data, status, owner, stream_id, level,
                         trigger_type, scheduled_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        data = excluded.data,
                        status = excluded.status,
                        owner = excluded.owner,
                        stream_id = excluded.stream_id,
                        level = excluded.level,
                        trigger_type = excluded.trigger_type,
                        scheduled_at = excluded.scheduled_at,
                        created_at = excluded.created_at
                """, (task.id, data, status, owner, stream_id, level,
                      trigger_type, scheduled_at, created_at))
                conn.commit()
                return True

        logger.debug(
            "保存任务：id=%s status=%s guard=%s",
            task.id, task.status.value, expected_status,
        )
        try:
            return await asyncio.to_thread(_save)
        except sqlite3.Error:
            logger.exception("保存任务失败：id=%s", task.id)
            raise

    async def get(self, task_id: str) -> TaskRecord | None:
        """按 id 获取任务，未找到返回 ``None``。"""

        def _get() -> TaskRecord | None:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT data FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            if row is None:
                return None
            return TaskRecord.from_dict(json.loads(row["data"]))

        try:
            return await asyncio.to_thread(_get)
        except sqlite3.Error:
            logger.exception("查询任务失败：id=%s", task_id)
            raise

    async def get_by_prefix(self, prefix: str) -> list[TaskRecord]:
        """按 ID 前缀模糊查询，返回所有匹配任务（0 个或多个）。

        Args:
            prefix: 任务 ID 前缀（至少 1 字符，通常 8 位）。

        Returns:
            匹配的任务列表，按创建时间倒序。
        """
        normalized = str(prefix or "").strip()
        if not normalized:
            return []

        def _query() -> list[TaskRecord]:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT data FROM tasks WHERE id LIKE ? ORDER BY created_at DESC",
                    (normalized + "%",),
                ).fetchall()
            return [TaskRecord.from_dict(json.loads(row["data"])) for row in rows]

        logger.debug("按前缀查询任务：prefix=%s", normalized)
        try:
            return await asyncio.to_thread(_query)
        except sqlite3.Error:
            logger.exception("按前缀查询任务失败：prefix=%s", normalized)
            raise

    async def list(
        self,
        *,
        status: TaskStatus | None = None,
        level: TaskLevel | None = None,
        owner: str | None = None,
        stream_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """按可选条件筛选任务，按 ``created_at DESC`` 排序。"""

        conditions: list[str] = []
        params: list[Any] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if level is not None:
            conditions.append("level = ?")
            params.append(level.value)
        if owner is not None:
            conditions.append("owner = ?")
            params.append(owner)
        if stream_id is not None:
            conditions.append("stream_id = ?")
            params.append(stream_id)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])

        def _list() -> list[TaskRecord]:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT data FROM tasks{where}"
                    " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
            return [TaskRecord.from_dict(json.loads(r["data"])) for r in rows]

        logger.debug(
            "查询任务列表：status=%s level=%s owner=%s stream_id=%s limit=%d offset=%d",
            status.value if status else None,
            level.value if level else None,
            owner,
            stream_id,
            limit,
            offset,
        )
        try:
            return await asyncio.to_thread(_list)
        except sqlite3.Error:
            logger.exception("查询任务列表失败")
            raise

    async def list_active(self) -> list[TaskRecord]:
        """返回所有非终态任务（status 不在 completed/failed/cancelled 中）。"""

        def _list_active() -> list[TaskRecord]:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT data FROM tasks"
                    " WHERE status NOT IN ('completed','failed','cancelled')"
                    " ORDER BY created_at DESC",
                ).fetchall()
            return [TaskRecord.from_dict(json.loads(r["data"])) for r in rows]

        try:
            return await asyncio.to_thread(_list_active)
        except sqlite3.Error:
            logger.exception("查询活动任务列表失败")
            raise

    async def delete(self, task_id: str) -> bool:
        """删除任务及其历史记录。返回 ``True`` 表示实际删除了行。"""

        def _delete() -> bool:
            with closing(sqlite3.connect(self._db_path)) as conn:
                cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                deleted = cursor.rowcount > 0
                conn.execute("DELETE FROM task_history WHERE task_id = ?", (task_id,))
                conn.commit()
            return deleted

        logger.debug("删除任务：id=%s", task_id)
        try:
            deleted = await asyncio.to_thread(_delete)
        except sqlite3.Error:
            logger.exception("删除任务失败：id=%s", task_id)
            raise
        logger.info("任务已删除：id=%s deleted=%s", task_id, deleted)
        return deleted

    # ── 历史 ────────────────────────────────────────────────────────

    async def append_history(self, task_id: str, entry: dict) -> int:
        """为任务追加一条历史条目，返回新条目的自增 id（可作恢复游标）。"""

        entry_json = json.dumps(entry, ensure_ascii=False)
        now = datetime.now().isoformat()

        def _append() -> int:
            with closing(sqlite3.connect(self._db_path)) as conn:
                cur = conn.execute(
                    "INSERT INTO task_history (task_id, entry, created_at)"
                    " VALUES (?, ?, ?)",
                    (task_id, entry_json, now),
                )
                conn.commit()
                return cur.lastrowid

        logger.debug("追加任务历史：id=%s", task_id)
        try:
            return await asyncio.to_thread(_append)
        except sqlite3.Error:
            logger.exception("追加任务历史失败：id=%s", task_id)
            raise

    async def get_history(
        self, task_id: str, limit: int = 200
    ) -> list[dict]:
        """读取任务的历史条目，从旧到新排序。"""

        def _get_history() -> list[dict]:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT entry FROM task_history"
                    " WHERE task_id = ?"
                    " ORDER BY id ASC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            return [json.loads(r["entry"]) for r in rows]

        logger.debug("查询任务历史：id=%s limit=%d", task_id, limit)
        try:
            return await asyncio.to_thread(_get_history)
        except sqlite3.Error:
            logger.exception("查询任务历史失败：id=%s", task_id)
            raise

    async def get_history_after(self, task_id: str, after_id: int) -> list[dict]:
        """返回 id 大于 *after_id* 的历史条目，按 id 升序（回放顺序）。

        :class:`AgentLoop` 恢复时以 0 为起点从头回放全部条目，
        幂等重建对话上下文；任务经 ``TaskRecord.set_last_history_id()``
        记录持久化水位（键 ``META_LAST_HISTORY_ID``，审计 / 未来增量续传锚点）。
        """

        def _get_after() -> list[dict]:
            with closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT entry FROM task_history"
                    " WHERE task_id = ? AND id > ?"
                    " ORDER BY id ASC",
                    (task_id, after_id),
                ).fetchall()
            return [json.loads(r["entry"]) for r in rows]

        logger.debug("增量查询任务历史：id=%s after_id=%d", task_id, after_id)
        try:
            return await asyncio.to_thread(_get_after)
        except sqlite3.Error:
            logger.exception("增量查询任务历史失败：id=%s", task_id)
            raise

    # ── 工具方法 ───────────────────────────────────────────────────

    async def count(self, *, status: TaskStatus | None = None) -> int:
        """统计任务数量，可按状态筛选。"""

        if status is not None:

            def _count() -> int:
                with closing(sqlite3.connect(self._db_path)) as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE status = ?",
                        (status.value,),
                    ).fetchone()
                return row[0] if row else 0

        else:

            def _count() -> int:
                with closing(sqlite3.connect(self._db_path)) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
                return row[0] if row else 0

        logger.debug("统计任务数量：status=%s", status.value if status else None)
        try:
            return await asyncio.to_thread(_count)
        except sqlite3.Error:
            logger.exception("统计任务数量失败")
            raise
