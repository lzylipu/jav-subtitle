"""全局出站限流器：保证打到翻译接口的请求「不突发、有间隔、429 后全体停手」。

为什么需要它：
- 用户明确要求「不要打流中转站」。多线程 + 多阶段并发时，如果每个 worker 各自
  发请求，很容易在同一秒打出十几个请求，触发 429 甚至被封 key。
- 这里用「固定间隔发车」而不是令牌桶：令牌桶允许一瞬间把攒的令牌全花掉（突发），
  固定间隔则严格保证任意两次请求至少隔 interval 秒，对上游最友好。

两个约束同时生效：
- translate_rpm：每分钟最多几次（换算成 60/rpm 秒间隔）
- translate_min_interval：硬下限，防止 rpm 设得很大时仍然密集

429 处理：
- 收到 429 就把全局「冷却截止时间」推到 now + Retry-After（没有就用配置值），
  所有 worker 一起停下来等，而不是各自轮着去撞墙。
- 连续 429 时冷却时间翻倍（上限 30 分钟），避免在对方限流期内反复试探。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol


class SupportsWait(Protocol):  # pragma: no cover - 仅用于类型提示
    def wait(self, timeout: float | None = None) -> bool: ...


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_slot = 0.0        # 下一次最早可发出的时间戳
        self._cooldown_until = 0.0   # 429 触发的全局冷却截止
        self._strikes = 0            # 连续 429 次数
        self._sent = 0               # 累计发出请求数
        self._recent: list[float] = []  # 最近一分钟的发出时间，用于展示
        self.cooldown_reason = ""

    # ------------------------------------------------------------------ 内部
    def _interval(self, rpm: int, min_interval: float) -> float:
        spacing = 0.0
        if rpm and rpm > 0:
            spacing = 60.0 / float(rpm)
        return max(spacing, max(0.0, float(min_interval)))

    # ------------------------------------------------------------------ 公开
    def acquire(
        self,
        rpm: int,
        min_interval: float,
        stop_event: SupportsWait | None = None,
        max_wait: float = 1800.0,
    ) -> bool:
        """阻塞直到允许发出一次请求。返回 False 表示被 stop_event 打断。"""
        interval = self._interval(rpm, min_interval)
        deadline = time.time() + max_wait
        while True:
            with self._lock:
                now = time.time()
                slot = max(now, self._next_slot, self._cooldown_until)
                wait = slot - now
                if wait <= 0.01:
                    self._next_slot = now + interval
                    self._sent += 1
                    self._recent.append(now)
                    self._recent = [t for t in self._recent if now - t <= 60.0]
                    return True
            if time.time() > deadline:
                return False
            wait = min(wait, 30.0)
            if stop_event is not None:
                if stop_event.wait(wait):
                    return False
            else:
                time.sleep(wait)

    def note_success(self) -> None:
        with self._lock:
            self._strikes = 0
            if self._cooldown_until <= time.time():
                self.cooldown_reason = ""

    def note_429(self, retry_after: float | None, default_backoff: int) -> float:
        """记一次 429，返回本次冷却秒数。所有 worker 共用这个冷却窗口。"""
        with self._lock:
            self._strikes += 1
            base = retry_after if retry_after and retry_after > 0 else float(default_backoff or 120)
            seconds = min(base * (2 ** (self._strikes - 1)), 1800.0)
            until = time.time() + seconds
            if until > self._cooldown_until:
                self._cooldown_until = until
            self._next_slot = max(self._next_slot, until)
            self.cooldown_reason = f"接口限流，冷却 {int(seconds)} 秒（连续 {self._strikes} 次）"
            return seconds

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            recent = [t for t in self._recent if now - t <= 60.0]
            return {
                "sent_total": self._sent,
                "sent_last_minute": len(recent),
                "cooldown_remaining": max(0, round(self._cooldown_until - now)),
                "cooldown_reason": self.cooldown_reason if self._cooldown_until > now else "",
                "strikes": self._strikes,
            }

    def reset(self) -> None:
        with self._lock:
            self._next_slot = 0.0
            self._cooldown_until = 0.0
            self._strikes = 0
            self.cooldown_reason = ""


limiter = RateLimiter()
