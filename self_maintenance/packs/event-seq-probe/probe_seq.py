# 对照实验：新版 EventBus（已修）vs 旧写法（seq += 1 后二次读，无锁）
import queue
import sys
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'agent_webui' / 'backend'))
import bridge


class OldBus:
    """复刻修复前的 emit 关键两行（其余无关部分照抄）。"""

    def __init__(self):
        self.seq = 0
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100000)
        with self._lock:
            self._subs.append(q)
        return q

    def emit(self, etype, payload=None):
        self.seq += 1
        evt = dict(payload or {})
        evt.update({'seq': self.seq, 'type': etype, 'ts': 0})
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass


def hammer(bus, n_threads=16, per=500):
    q = bus.subscribe()
    seen = []

    def consume():
        while True:
            try:
                seen.append(q.get(timeout=1.0)['seq'])
            except queue.Empty:
                return

    c = threading.Thread(target=consume)
    c.start()
    bar = threading.Barrier(n_threads)

    def pump(k):
        bar.wait()
        for i in range(per):
            bus.emit('pipeline', {'tc_id': 't%d-%d' % (k, i)})

    ts = [threading.Thread(target=pump, args=(k,)) for k in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    c.join()
    uniq = len(set(seen)) == len(seen)
    dense = (max(seen) == len(seen)) if seen else False
    return len(seen), n_threads * per, uniq, dense


for name, bus in (('NEW(已修)', bridge.EventBus()), ('OLD(修复前)', OldBus())):
    got, exp, uniq, dense = hammer(bus)
    print('%-12s received=%d/%d unique=%s dense=%s' % (name, got, exp, uniq, dense))
