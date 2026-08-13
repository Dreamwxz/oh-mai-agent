"""Transport 协议 + LoopbackTransport — 命令总线传输层。

``Transport`` 协议是通信抽象边界：同一个 ``TaskCommandBus``
可以通过此协议在**进程内**（``LoopbackTransport``）运行，
当前仅支持进程内通信。命令和事件不落库，仅通过总线传递。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Transport — 协议
# ═══════════════════════════════════════════════════════════════════════


class Transport(ABC):
    """命令/事件帧的异步传输抽象协议。

    每次 ``send`` 推送一个 JSON 编码的字节帧。``receive`` 拉取下一个
    可用帧（传输关闭或为空时返回 ``None``）。实现层负责帧边界：
    一次 send 对应一次 receive。

    当前实现：``LoopbackTransport`` — 进程内版本，基于 ``asyncio.Queue``。
    """

    @abstractmethod
    async def send(self, frame: bytes) -> None:
        """推送一个字节帧到传输通道。"""
        ...

    @abstractmethod
    async def receive(self) -> bytes | None:
        """拉取下一个可用字节帧；无帧可读时返回 None。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭传输通道，解除所有阻塞的接收方。"""
        ...


# ═══════════════════════════════════════════════════════════════════════
# LoopbackTransport
# ═══════════════════════════════════════════════════════════════════════


class LoopbackTransport(Transport):
    """进程内回环传输，基于 ``asyncio.Queue``。

    ``send`` 将帧推入队列；``receive`` 从队列拉取下一帧。
    真正的双向通信需要一对实例——每个方向各一个。

    这是当前唯一的传输实现；跨进程传输尚未实现。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed: bool = False

    async def send(self, frame: bytes) -> None:
        if self._closed:
            logger.debug("传输已关闭，跳过投递帧：%r", frame[:80])
            return
        await self._queue.put(frame)
        logger.debug("投递帧到队列：%r", frame[:80])

    async def receive(self) -> bytes | None:
        if self._closed and self._queue.empty():
            return None
        try:
            frame = await self._queue.get()
        except (asyncio.CancelledError, RuntimeError) as exc:
            logger.warning("接收帧异常：%s", exc, exc_info=True)
            return None
        if frame is None:
            logger.debug("收到关闭哨兵，传输结束")
            return None
        logger.debug("收到帧：%r", frame[:80])
        return frame

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            logger.info("LoopbackTransport 关闭，解除阻塞的接收方")
            # 向队列推入哨兵值，解除所有阻塞的 receive() 调用
            await self._queue.put(None)
