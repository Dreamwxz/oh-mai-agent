"""TaskRecovery — 插件重启后活跃任务的恢复逻辑收口。

将原先分散在 ``plugin._recover_active_tasks`` 和 ``scheduler.stop``
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
    - ENQUEUE：SCHEDULED 状态的任务，调用方重新入队等待定时器触发
    - PENDING：原 RUNNING 状态的任务，调用方保存记录后重新入队
    - WAITING：WAITING_INPUT 状态的任务，调用方仅计数（不做状态变更）
    - PAUSED：PAUSED 或终态任务，调用方不做任何操作
    """

    ENQUEUE = "enqueue"     # SCHEDULED — 调用方重新入队等待定时器触发
    PENDING = "pending"     # RUNNING — 调用方保存记录后重新入队
    WAITING = "waiting"     # WAITING_INPUT — 调用方仅计数（不做状态变更）
    PAUSED = "paused"       # PAUSED / 终态 — 调用方不做任何操作


class TaskRecovery:
    """无状态的恢复决策器。

    行为与 ``MaibotAgentPlugin._recover_active_tasks`` 中原有的内联
    逻辑完全一致。
    """

    @staticmethod
    def recover(record: TaskRecord) -> RecoveryAction:
        """返回对单个活跃任务记录的恢复动作。

        对 *record* 的副作用：
          * RUNNING 状态 → 调用 ``record.force(PENDING, …)``
            （写入审计轨迹和元数据标记 ``_recovered_from_running``）。
        """
        if record.status == TaskStatus.SCHEDULED:
            logger.debug("任务 %s 恢复动作: SCHEDULED → ENQUEUE（重新入队等待定时器触发）", record.id)
            return RecoveryAction.ENQUEUE

        if record.status == TaskStatus.RUNNING:
            record.force(
                TaskStatus.PENDING,
                actor="recovery",
                reason="recovered_from_running",
            )
            record.metadata["_recovered_from_running"] = True
            logger.debug("任务 %s 已恢复: RUNNING → PENDING（标记 _recovered_from_running）", record.id)
            return RecoveryAction.PENDING

        if record.status == TaskStatus.WAITING_INPUT:
            # 保持等待——旧的 Event 已随进程消失，等待用户重新唤醒
            logger.debug("任务 %s 保持 %s（等待用户回复唤醒）", record.id, record.status.value)
            return RecoveryAction.WAITING

        # PAUSED 及其他未预期状态：仅支持手动恢复
        logger.debug("任务 %s 保持 %s（仅支持手动恢复）", record.id, record.status.value)
        return RecoveryAction.PAUSED
