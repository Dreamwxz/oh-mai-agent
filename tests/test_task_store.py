"""oh_mai_agent.domain.task_store 的测试 —— SQLite CRUD、历史、过滤、删除。

使用 pytest 的 tmp_path fixture 创建隔离的 SQLite 数据库。
"""

from __future__ import annotations

import logging

import pytest
from conftest import make_task
from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore


@pytest.mark.asyncio
class TestTaskStoreInit:
    async def test_init_creates_db(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        store = TaskStore(str(db_path))
        await store.init()
        assert db_path.exists()
        await store.close()

    async def test_init_idempotent(self, tmp_path) -> None:
        db_path = tmp_path / "test2.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.init()  # 重复 init 不应报错
        await store.close()


@pytest.mark.asyncio
class TestTaskStoreCRUD:
    async def test_save_and_get(self, tmp_path) -> None:
        db_path = tmp_path / "crud.db"
        store = TaskStore(str(db_path))
        await store.init()

        task = make_task("task-001", title="测试", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await store.save(task)

        retrieved = await store.get("task-001")
        assert retrieved is not None
        assert retrieved.title == "测试"
        assert retrieved.level == TaskLevel.AGENT

        await store.close()

    async def test_get_nonexistent(self, tmp_path) -> None:
        db_path = tmp_path / "crud.db"
        store = TaskStore(str(db_path))
        await store.init()
        assert await store.get("no-such-task") is None
        await store.close()

    async def test_save_upsert(self, tmp_path) -> None:
        db_path = tmp_path / "crud.db"
        store = TaskStore(str(db_path))
        await store.init()

        task = make_task("task-001", title="旧标题", level=TaskLevel.INSTANT)
        await store.save(task)

        task.title = "新标题"
        task.level = TaskLevel.AGENT
        await store.save(task)

        retrieved = await store.get("task-001")
        assert retrieved is not None
        assert retrieved.title == "新标题"
        assert retrieved.level == TaskLevel.AGENT
        await store.close()

    async def test_delete(self, tmp_path) -> None:
        db_path = tmp_path / "crud.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("task-001"))
        assert await store.delete("task-001") is True
        assert await store.get("task-001") is None
        assert await store.delete("no-such-task") is False
        await store.close()

    async def test_count(self, tmp_path) -> None:
        db_path = tmp_path / "crud.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", status=TaskStatus.PENDING))
        await store.save(make_task("t2", status=TaskStatus.RUNNING))
        await store.save(make_task("t3", status=TaskStatus.COMPLETED))

        assert await store.count() == 3
        assert await store.count(status=TaskStatus.PENDING) == 1
        assert await store.count(status=TaskStatus.RUNNING) == 1
        assert await store.count(status=TaskStatus.COMPLETED) == 1
        await store.close()


@pytest.mark.asyncio
class TestTaskStoreList:
    async def test_list_all(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", status=TaskStatus.PENDING))
        await store.save(make_task("t2", status=TaskStatus.RUNNING))
        await store.save(make_task("t3", status=TaskStatus.COMPLETED))

        result = await store.list()
        assert len(result) == 3
        await store.close()

    async def test_list_filter_by_status(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", status=TaskStatus.PENDING))
        await store.save(make_task("t2", status=TaskStatus.RUNNING))

        result = await store.list(status=TaskStatus.RUNNING)
        assert len(result) == 1
        assert result[0].id == "t2"
        await store.close()

    async def test_list_filter_by_level(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", level=TaskLevel.INSTANT))
        await store.save(make_task("t2", level=TaskLevel.AGENT))
        await store.save(make_task("t3", level=TaskLevel.AGENT))

        result = await store.list(level=TaskLevel.AGENT)
        assert len(result) == 2
        await store.close()

    async def test_list_filter_by_owner(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", owner="qq:10001"))
        await store.save(make_task("t2", owner="qq:10002"))

        result = await store.list(owner="qq:10001")
        assert len(result) == 1
        assert result[0].owner == "qq:10001"
        await store.close()

    async def test_list_filter_by_stream_id(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", stream_id="qq:group:1"))
        await store.save(make_task("t2", stream_id="qq:group:2"))

        result = await store.list(stream_id="qq:group:1")
        assert len(result) == 1
        assert result[0].stream_id == "qq:group:1"
        await store.close()

    async def test_list_active(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("t1", status=TaskStatus.PENDING))
        await store.save(make_task("t2", status=TaskStatus.RUNNING))
        await store.save(make_task("t3", status=TaskStatus.WAITING_INPUT))
        await store.save(make_task("t4", status=TaskStatus.SCHEDULED))
        await store.save(make_task("t5", status=TaskStatus.COMPLETED))
        await store.save(make_task("t6", status=TaskStatus.FAILED))
        await store.save(make_task("t7", status=TaskStatus.CANCELLED))

        active = await store.list_active()
        assert len(active) == 4  # 活跃任务为 t1-t4
        active_ids = {t.id for t in active}
        assert "t5" not in active_ids
        assert "t6" not in active_ids
        assert "t7" not in active_ids
        await store.close()

    async def test_list_limit_and_offset(self, tmp_path) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()

        for i in range(5):
            await store.save(make_task(f"t{i}"))

        result = await store.list(limit=2)
        assert len(result) == 2

        result2 = await store.list(limit=2, offset=2)
        assert len(result2) == 2

        assert result[0].id != result2[0].id  # 两次结果首条不同，说明 offset 生效
        await store.close()

    async def test_list_active_quiet_at_debug(self, tmp_path, caplog) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()
        caplog.set_level(logging.DEBUG, logger="oh_mai_agent.domain.task_store")

        await store.save(make_task("t1", status=TaskStatus.PENDING))
        await store.list_active()

        assert not any("查询活动任务列表" in r.message for r in caplog.records)
        await store.close()

    async def test_get_quiet_at_debug(self, tmp_path, caplog) -> None:
        db_path = tmp_path / "list.db"
        store = TaskStore(str(db_path))
        await store.init()
        caplog.set_level(logging.DEBUG, logger="oh_mai_agent.domain.task_store")

        task = make_task("task-001", title="测试", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await store.save(task)
        await store.get(task.id)

        assert not any(r.message.startswith("查询任务：") for r in caplog.records)
        await store.close()


@pytest.mark.asyncio
class TestTaskStoreHistory:
    async def test_append_and_get_history(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("task-001"))
        await store.append_history("task-001", {"round": 1, "msg": "hello"})
        await store.append_history("task-001", {"round": 2, "msg": "world"})

        history = await store.get_history("task-001")
        assert len(history) == 2
        assert history[0]["round"] == 1
        assert history[1]["round"] == 2
        await store.close()

    async def test_get_history_empty(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()
        history = await store.get_history("no-such-task")
        assert history == []
        await store.close()

    async def test_get_history_with_limit(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("task-001"))
        for i in range(10):
            await store.append_history("task-001", {"i": i})

        history = await store.get_history("task-001", limit=5)
        assert len(history) == 5
        assert history[0]["i"] == 0  # 最早的在前
        assert history[4]["i"] == 4
        await store.close()

    async def test_delete_removes_history(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()

        await store.save(make_task("task-001"))
        await store.append_history("task-001", {"r": 1})
        await store.delete("task-001")

        history = await store.get_history("task-001")
        assert history == []
        await store.close()

    async def test_append_history_returns_incrementing_id(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("task-001"))

        id1 = await store.append_history("task-001", {"round": 1})
        id2 = await store.append_history("task-001", {"round": 2})
        assert isinstance(id1, int) and id1 > 0
        assert id2 == id1 + 1
        await store.close()

    async def test_get_history_after(self, tmp_path) -> None:
        db_path = tmp_path / "hist.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("task-001"))

        ids = []
        for i in range(5):
            ids.append(await store.append_history("task-001", {"i": i}))

        after = await store.get_history_after("task-001", ids[2])
        assert [e["i"] for e in after] == [3, 4]

        # after_id=0 表示不设下限，返回全部
        all_entries = await store.get_history_after("task-001", 0)
        assert [e["i"] for e in all_entries] == [0, 1, 2, 3, 4]

        tail = await store.get_history_after("task-001", ids[4])
        assert tail == []
        await store.close()


@pytest.mark.asyncio
class TestGetByPrefix:
    async def test_unique_prefix(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B"))
        await store.save(make_task("cccccccc-1111-2222-3333-444444444444", title="C"))
        hits = await store.get_by_prefix("cccccccc")
        assert len(hits) == 1
        assert hits[0].title == "C"
        await store.close()

    async def test_multiple_prefix(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B"))
        await store.save(make_task("cccccccc-1111-2222-3333-444444444444", title="C"))
        hits = await store.get_by_prefix("aaaa")
        assert len(hits) == 2
        titles = {t.title for t in hits}
        assert titles == {"A", "B"}
        await store.close()

    async def test_no_match(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        result = await store.get_by_prefix("zzzz")
        assert result == []
        await store.close()

    async def test_empty_prefix(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        result = await store.get_by_prefix("")
        assert result == []
        result2 = await store.get_by_prefix(None)  # type: ignore[arg-type]
        assert result2 == []
        await store.close()

    async def test_exact_id_works(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        task = make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A")
        await store.save(task)
        # 完整 ID 也能通过前缀匹配
        hits = await store.get_by_prefix(task.id)
        assert len(hits) == 1
        assert hits[0].id == task.id
        await store.close()

    async def test_short_prefix(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        # 8 字符前缀（`/task list` 常见场景）
        await store.save(make_task("9bcd9c0b-1111-2222-3333-444444444444", title="X"))
        await store.save(make_task("9bcd9c0c-1111-2222-3333-444444444444", title="Y"))
        hits = await store.get_by_prefix("9bcd9c0b")
        assert len(hits) == 1
        assert hits[0].title == "X"
        # 1 字符前缀
        hits = await store.get_by_prefix("9")
        assert len(hits) == 2
        await store.close()

    async def test_whitespace_stripped(self, tmp_path) -> None:
        db_path = tmp_path / "prefix.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        hits = await store.get_by_prefix("  aaaa  ")
        assert len(hits) == 1
        hits = await store.get_by_prefix("   ")
        assert hits == []
        await store.close()


@pytest.mark.asyncio
class TestGetByTitle:
    async def test_exact_title(self, tmp_path) -> None:
        db_path = tmp_path / "title.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="系统环境检查"))
        await store.save(make_task("bbbbbbbb-1111-2222-3333-444444444444", title="其他任务"))
        hits = await store.get_by_title("系统环境检查")
        assert len(hits) == 1
        assert hits[0].id == "aaaaaaaa-1111-2222-3333-444444444444"
        await store.close()

    async def test_multiple_same_title(self, tmp_path) -> None:
        db_path = tmp_path / "title.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="重复标题"))
        await store.save(make_task("bbbbbbbb-1111-2222-3333-444444444444", title="重复标题"))
        hits = await store.get_by_title("重复标题")
        assert len(hits) == 2
        await store.close()

    async def test_no_match(self, tmp_path) -> None:
        db_path = tmp_path / "title.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        hits = await store.get_by_title("不存在的标题")
        assert hits == []
        await store.close()

    async def test_empty_title(self, tmp_path) -> None:
        db_path = tmp_path / "title.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        hits = await store.get_by_title("")
        assert hits == []
        hits2 = await store.get_by_title(None)  # type: ignore[arg-type]
        assert hits2 == []
        await store.close()

    async def test_whitespace_stripped(self, tmp_path) -> None:
        db_path = tmp_path / "title.db"
        store = TaskStore(str(db_path))
        await store.init()
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="系统环境检查"))
        hits = await store.get_by_title("  系统环境检查  ")
        assert len(hits) == 1
        await store.close()

