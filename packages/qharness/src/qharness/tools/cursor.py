# -*- coding: utf-8 -*-
"""工具运行时使用的短游标内存存储。"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


CursorValue = TypeVar("CursorValue")


@dataclass(slots=True)
class _CursorEntry(Generic[CursorValue]):
    """保存一个游标的业务状态和单调时钟过期时间。"""

    value: CursorValue
    expires_at: float


class ExpiringCursorStore(Generic[CursorValue]):
    """保存短随机游标，并在成功读取时刷新其过期时间。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30 * 60,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化有界内存存储；时钟参数主要用于确定性验证。"""

        if isinstance(ttl_seconds, bool) or not isinstance(
            ttl_seconds,
            (int, float),
        ):
            raise ValueError("cursor ttl_seconds 必须是数字。")
        if ttl_seconds <= 0:
            raise ValueError("cursor ttl_seconds 必须大于 0。")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError("cursor max_entries 必须是整数。")
        if max_entries <= 0:
            raise ValueError("cursor max_entries 必须大于 0。")

        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _CursorEntry[CursorValue]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def create(self, value: CursorValue) -> str:
        """保存业务状态并返回适合模型传递的短随机游标。"""

        now = self._clock()
        with self._lock:
            self._remove_expired(now)
            while len(self._entries) >= self._max_entries:
                # 达到容量上限时，优先淘汰最久没有被访问的游标。
                self._entries.popitem(last=False)

            cursor = self._new_cursor()
            while cursor in self._entries:
                cursor = self._new_cursor()
            self._entries[cursor] = _CursorEntry(
                value=value,
                expires_at=now + self._ttl_seconds,
            )
            return cursor

    def get(self, cursor: str) -> CursorValue | None:
        """读取有效游标并滑动续期；失效或过期时返回 ``None``。"""

        now = self._clock()
        with self._lock:
            entry = self._entries.get(cursor)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del self._entries[cursor]
                return None

            entry.expires_at = now + self._ttl_seconds
            self._entries.move_to_end(cursor)
            return entry.value

    def _remove_expired(self, now: float) -> None:
        """删除全部已过期游标；调用方必须持有实例锁。"""

        expired = [
            cursor
            for cursor, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for cursor in expired:
            del self._entries[cursor]

    @staticmethod
    def _new_cursor() -> str:
        """生成带类型前缀的 96 位随机游标，通常总长 20 字符。"""

        return f"cur_{secrets.token_urlsafe(12)}"
