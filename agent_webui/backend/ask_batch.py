# -*- coding: utf-8 -*-
"""ask 多卡共享窗口（纯逻辑，可单测）。

问题：`ask_user` 允许同批并行提问 —— 一个回合里同时挂 N 张卡（编排器把它们放在
同一层并行执行）。但「等多久」这件事不能按卡各算各的：

  · 每张卡各算 N 秒 → 用户答完第 1 张时，第 2、3 张往往已经到点。**人明明就在跟前**，
    卡却"死了"：答复投给一个已被结算的等待者 → 静默吞掉（这正是本模块要消灭的形态）。
  · 无限等 → 用户走开后整个回合永久挂住。

于是引入**活跃窗口**：一批并行卡共享一条 deadline，**只要用户还在答题就往后续**，
连续 `window` 秒没有任何人作答，才算"没人应答"。另设硬上限（born + window*n + grace），
防止一直续窗把回合挂死。

语义：
    join(window)  新卡加入当前批次（批里没卡就开新的一批）；返回该批的快照
    touch()       有人作答：deadline = min(now + window, born + window * n + grace)
    leave()       某张卡出局（答完/超时/回合结束）
    remaining()   距 deadline 还剩多少秒（0 表示窗口已关）
    reset()       整个批次作废（回合终止/桥重启）

批次的判定不需要调用方传 batch_id：**批里没有活卡时的第一个 join 就是新批**。
同一时刻只有一个回合在跑（bridge 的 _TURN_GATE 保证），所以全局单批次足够。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


def batch_timeout(n_cards: int, window: float, margin: float = 10.0) -> int:
    """编排器一批工具的等待上限（秒）。

    含 ask 的批次由 bridge 把 timeout 抬到这个值，否则编排器会按 30s 抢先结算，
    把晚到的答复投给一个已被判死的等待者（静默吞掉）。

    **按卡数放宽**：n 张卡是并行挂着的，用户要一张张答，共享同一段时间。
    n=1 时结果与历史行为完全一致（window+margin，不多等一秒）；n=3 时给足三倍余量。
    """
    return int(max(1, int(n_cards or 1)) * float(window) + float(margin))


class WindowPool:
    """一批并行 ask 卡共享的活跃窗口（线程安全）。"""

    def __init__(self, grace: float = 60.0) -> None:
        self._lock = threading.RLock()
        self._grace = float(grace)     # 硬上限在 window*n 之外的额外余量
        self._batch_id = 0
        self._born = 0.0
        self._deadline = 0.0
        self._window = 0.0
        self._n = 0                    # 本批累计加入过的卡数（含已出局的）
        self._live = 0                 # 此刻仍在等的卡数
        self._answers = 0              # 本批被作答的次数

    # ---------- 卡生命周期 ----------
    def join(self, window: float, now: Optional[float] = None) -> Dict[str, Any]:
        """新卡加入（批里没活卡则开新批）。返回批次快照。"""
        now = time.time() if now is None else now
        w = max(1.0, float(window or 0))
        with self._lock:
            if self._live <= 0:                       # 上一批已经空了 → 这是新的一批
                self._batch_id += 1
                self._born = now
                self._n = 0
                self._answers = 0
                self._window = w
                self._deadline = now + w
            else:
                # 后来的卡若窗口更长，就把共同 deadline 往后推（不会提前）
                self._deadline = max(self._deadline, now + w)
            self._live += 1
            self._n += 1
            return self._snapshot(now)

    def touch(self, now: Optional[float] = None, window: Optional[float] = None) -> Dict[str, Any]:
        """有人作答 → 续窗（封顶在 born + window*n + grace）。"""
        now = time.time() if now is None else now
        with self._lock:
            w = float(window or self._window or 0)
            self._answers += 1
            if w > 0:
                hard = self._born + w * max(1, self._n) + self._grace
                self._deadline = min(now + w, hard)
            return self._snapshot(now)

    def leave(self) -> Dict[str, Any]:
        """某张卡出局。返回批次快照（live 归零时下一张 join 会开新批）。"""
        with self._lock:
            self._live = max(0, self._live - 1)
            if self._live == 0:
                self._deadline = 0.0                  # 批内没卡了：窗口同时关掉
            return self._snapshot(time.time())

    def reset(self) -> None:
        """整批作废（回合终止、回合收尾）。"""
        with self._lock:
            self._live = 0
            self._n = 0
            self._window = 0.0
            self._deadline = 0.0
            self._born = 0.0
            self._answers = 0

    # ---------- 读取 ----------
    def remaining(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            if self._live <= 0:
                return 0.0
            return max(0.0, self._deadline - now)

    def expired(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= 0

    def live(self) -> int:
        with self._lock:
            return self._live

    def snapshot(self) -> Dict[str, Any]:
        return self._snapshot(time.time())

    def _snapshot(self, now: float) -> Dict[str, Any]:
        return {
            "batch_id": self._batch_id,
            "size": self._n,                 # 本批一共出现过几张卡
            "live": self._live,              # 还有几张在等
            "answers": self._answers,
            "window": self._window,
            "deadline": self._deadline,
            "remaining": (max(0.0, self._deadline - now) if self._live > 0 else 0.0),
        }
