"""TaskRecovery — 插件重启后活跃任务的恢复逻辑收口。

将原先分散在插件生命周期方法与 ``scheduler.stop``
中的恢复逻辑收敛为单一、可测试的决策函数。调用方根据返回的
``RecoveryAction`` 分别执行调度器入队 / 持久化保存等动作。
"""

from __future__ import annotations

import logging
from enum import Enum

from .task_record import TaskRecord, TaskStatus

logger = logging.getLogger(__name__)


class RecoveryAction(str, Enum):
    """调用方在处理恢复后的任务记录时应执行的动作。

    每个枚举值对应一种恢复策略：
    - ENQUEUE：SCHEDULED / PENDING 状态的任务，调用方重新入队等待调度
    - PENDING：原 RUNNING 或插件关闭时被暂停（paused_by_stop）的任务，
      调用方保存记录后重新入队
    - WAITING：WAITING_INPUT 状态的任务，调用方仅计数（不做状态变更）
    - PAUSED：用户主动暂停或终态任务，调用方不做任何操作
    """

    ENQUEUE = "enqueue"     # SCHEDULED / PENDING — 调用方重新入队等待调度
    PENDING = "pending"     # RUNNING / paused_by_stop — 调用方保存记录后重新入队
    WAITING = "waiting"     # WAITING_INPUT — 调用方仅计数（不做状态变更）
    PAUSED = "paused"       # 用户主动 PAUSED / 终态 — 调用方不做任何操作


class TaskRecovery:
    """无状态的恢复决策器。

    行为与插件原生命周期方法中的内联恢复逻辑完全一致。
    """

    @staticmethod
    def recover(record: TaskRecord) -> RecoveryAction:
        """返回对单个活跃任务记录的恢复动作。

        对 *record* 的副作用：
          * RUNNING 状态 → 调用 ``record.force(PENDING, …)``
            （写入审计轨迹并经 ``mark_recovered_from_running()`` 打标记，
            键 ``META_RECOVERED_FROM_RUNNING``）。
          * 插件关闭时被暂停（``was_paused_by_stop()``）→ 同样降级为
            PENDING 并清除 ``META_PAUSED_BY_STOP`` 标记（与崩溃遗留的
            RUNNING 自动重排对称：优雅停机不应当惩罚任务）。
        """
        if record.status == TaskStatus.SCHEDULED:
            logger.debug("任务 %s 恢复动作: SCHEDULED → ENQUEUE（重新入队等待定时器触发）", record.id)
            return RecoveryAction.ENQUEUE

        if record.status == TaskStatus.PENDING:
            # 崩溃/停机瞬间已落库但尚未派发的任务：重新入队即可（调度器的
            # pending 队列是纯内存的，重启后必须回读 DB 中的 PENDING 行）。
            logger.debug("任务 %s 恢复动作: PENDING → ENQUEUE（重新入队等待调度）", record.id)
            return RecoveryAction.ENQUEUE

        if record.status == TaskStatus.RUNNING:
            record.force(
                TaskStatus.PENDING,
                actor="recovery",
                reason="recovered_from_running",
            )
            record.mark_recovered_from_running()
            logger.debug("任务 %s 已恢复: RUNNING → PENDING（标记 META_RECOVERED_FROM_RUNNING）", record.id)
            return RecoveryAction.PENDING

        if record.status == TaskStatus.WAITING_INPUT:
            # 保持等待——旧的 Event 已随进程消失，等待用户重新唤醒
            logger.debug("任务 %s 保持 %s（等待用户回复唤醒）", record.id, record.status.value)
            return RecoveryAction.WAITING

        if record.status == TaskStatus.PAUSED and record.was_paused_by_stop():
            # 优雅停机（scheduler.stop）时被 force(PAUSED) 的任务：与崩溃遗留
            # 的 RUNNING 对称，重启后自动降级重排；用户主动暂停（无该标记）不受影响。
            record.force(
                TaskStatus.PENDING,
                actor="recovery",
                reason="recovered_from_stop_pause",
            )
            record.clear_paused_by_stop()
            logger.debug("任务 %s 已恢复: PAUSED(paused_by_stop) → PENDING（标记已清除）", record.id)
            return RecoveryAction.PENDING

        # 用户主动暂停及其他未预期状态：仅支持手动恢复
        logger.debug("任务 %s 保持 %s（仅支持手动恢复）", record.id, record.status.value)
        return RecoveryAction.PAUSED
