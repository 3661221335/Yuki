#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失败重试队列 —— 「一次没解出来」不再等于「这条链接废了」。

为什么需要它
------------
线上失败原因的九成不是票坏，而是上游抖动：metadata 卡一发、step 撞限流、
某条边缘节点返回 500、代理瞬断。这些故障几十秒后就自己好了，但老逻辑把
错误往客户端一扔就结束 —— 用户只能自己再点一次，点不到就认为"解不了"。

现在：失败的票自动进这个队列，服务进程内部一个 daemon 线程按指数退避
反复重解，成功即写进 key 缓存。用户下次拿同一条链接来问，直接命中缓存
（毫秒级）。所以"百分百出卡"的落地方式不是"第一发必然成功"，而是
**永不放弃这一条票，直到它出卡或确认永久无效**。

特性
----
- 落盘（flock + 原子替换）：服务重启不丢队列。
- 单并发（默认 1）：后台重解不跟在线请求抢上游配额。
- 指数退避：5 / 10 / 20 / 40 / 80 / 160 / 300s，上限 5 分钟。
- 无效票立即丢弃：上游明确说 invalid/expired 的，重试一万次也没用。
- 超时丢弃：默认 6 小时，超过就进 dead 列表，不无限占资源。

单进程约束：这个模块只在 server.py 里 new 一次，线程随服务进程退出。
"""
import hashlib
import os
import sys
import threading
import time

try:
    import fcntl
except ImportError:                       # Windows
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))


def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


def _num(name, default):
    try:
        v = float(_env(name) or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _log(msg):
    print(f'[retry {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def default_file():
    return (_env('DELTA_RETRYQ_FILE') or
            os.path.join(HERE, '.delta_retryq.json'))


# 上游明确说"票本身无效"的关键词：这些不重试
_DEAD_MARKERS = (
    'invalid payload', 'invalid session', 'invalid ticket', 'invalid link',
    'expired', 'does not exist', 'not found', '无效链接', '已过期',
)


def is_permanent(err):
    low = str(err or '').lower()
    return any(m in low for m in _DEAD_MARKERS)


class RetryQueue(object):
    """持久化的重试队列。所有公开方法线程安全。"""

    def __init__(self, path=None, max_age=None, base_delay=None,
                 max_delay=None, concurrency=None, tick=1.0, verbose=True):
        self.path = path or default_file()
        self.lock_file = self.path + '.lock'
        self.max_age = float(max_age or _num('DELTA_RETRY_MAX_AGE', 6 * 3600))
        self.base_delay = float(base_delay or _num('DELTA_RETRY_BASE', 5.0))
        self.max_delay = float(max_delay or _num('DELTA_RETRY_MAX_DELAY', 300.0))
        self.concurrency = max(1, int(concurrency or
                                      _num('DELTA_RETRY_CONCURRENCY', 1)))
        self.tick = float(tick)
        self.verbose = verbose

        # 必须是 RLock：save() 内部也要拿同一把锁做快照，而 add/drop/bump
        # 都是"持锁改内存 -> 调 save 落盘"。用普通 Lock 会当场死锁。
        self._lock = threading.RLock()
        self._items = {}            # h -> record
        self._dead = []             # 永久放弃的记录（只留最近 50 条）
        # 删除标记：save() 是"磁盘合并"语义，只从内存 dict 里 pop 掉一条，
        # 下次合并会把它从磁盘上再捡回来（实测：已经解出的票反复复活）。
        # 必须显式记 tombstone，并在 load/save 时都应用。
        self._deleted = []          # 最近删除的 h（最多 500 个）
        self._running = set()
        self._stop = threading.Event()
        self._thread = None
        self._on_success = None
        self.stats = {'enqueued': 0, 'attempts': 0, 'solved': 0,
                      'dropped': 0, 'permanent': 0, 'running': 0}
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = __import__('json').load(f)
            if not isinstance(d, dict):
                return 0
            now = time.time()
            items = d.get('items') or {}
            deleted = {x for x in (d.get('deleted') or []) if isinstance(x, str)}
            # 只接受结构完整的项，避免旧版本/手改文件把 worker 搞崩
            self._items = {h: v for h, v in items.items()
                           if isinstance(v, dict) and v.get('ticket')
                           and isinstance(v.get('tries', 0), int)
                           and h not in deleted}
            self._deleted = list(deleted)[-500:]
            self._dead = (d.get('dead') or [])[:50]
            for h, v in list(self._items.items()):
                if now - float(v.get('created') or 0) > self.max_age:
                    self._items.pop(h, None)
            return len(self._items)
        except Exception:
            return 0

    def save(self):
        """读-合并-原子写，全程持文件锁（多进程也不会互相抹）。"""
        with self._lock:
            snapshot = {'items': dict(self._items), 'dead': list(self._dead),
                        'deleted': list(self._deleted)}
        tmp = '%s.tmp.%d' % (self.path, os.getpid())
        lf = None
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            lf = open(self.lock_file, 'a+')
            if fcntl is not None:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            merged = {'items': {}, 'dead': []}
            try:
                with open(self.path, encoding='utf-8') as f:
                    old = __import__('json').load(f)
                if isinstance(old, dict):
                    merged['items'] = old.get('items') or {}
                    merged['dead'] = old.get('dead') or []
            except Exception:
                pass
            merged['items'].update(snapshot['items'])
            # 关键：删除必须传播到磁盘，否则下一次合并会把已出卡的票捡回来。
            for h in snapshot['deleted']:
                merged['items'].pop(h, None)
            deleted = list(dict.fromkeys(
                list(snapshot['deleted']) +
                [x for x in (merged.get('deleted') or [])
                 if isinstance(x, str)]))[-500:]
            merged['deleted'] = deleted
            # dead 合并去重：本进程的新记录优先，磁盘上别的记录的补在后面
            seen = {x.get('h') for x in snapshot['dead'] if isinstance(x, dict)}
            extra = [x for x in (merged.get('dead') or [])
                     if isinstance(x, dict) and x.get('h') not in seen]
            merged['dead'] = (snapshot['dead'] + extra)[:50]
            with open(tmp, 'w', encoding='utf-8') as f:
                __import__('json').dump(merged, f, ensure_ascii=False,
                                        separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False
        finally:
            if lf is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                    lf.close()
                except Exception:
                    pass

    # ------------------------------------------------------------ 增删查
    @staticmethod
    def h(ticket):
        return hashlib.md5(ticket.encode('utf-8', 'replace')).hexdigest()

    def add(self, ticket, gate='', err='', delay=None, src='api'):
        """失败入队。重复入队会保留"更早的那一条"（重试次数不重置）。"""
        h = self.h(ticket)
        now = time.time()
        with self._lock:
            old = self._items.get(h)
            if old:
                # 已经在队列里：不重置次数，只更新错误和下次时间
                old['last_err'] = str(err or '')[:300]
                old['gate'] = gate or old.get('gate')
                old['last_ts'] = now
                if delay is not None:
                    old['next_ts'] = now + float(delay)
                self.save()
                return h
            self._items[h] = {
                'h': h, 'ticket': ticket, 'tries': 0,
                'created': now, 'last_ts': now,
                'next_ts': now + float(delay if delay is not None else
                                       self.base_delay),
                'gate': gate or '', 'last_err': str(err or '')[:300],
                'src': src,
            }
            self.stats['enqueued'] += 1
        self.save()
        if self.verbose:
            _log(f'入队 {h[:8]}（{gate or "unknown"}），{self.base_delay:.0f}s 后重试'
                 f'  队列 {len(self)} 条')
        return h

    def get(self, h):
        with self._lock:
            v = self._items.get(h)
            return dict(v) if v else None

    def has(self, h):
        with self._lock:
            return h in self._items

    def __len__(self):
        with self._lock:
            return len(self._items)

    def due(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return [(h, dict(v)) for h, v in self._items.items()
                    if float(v.get('next_ts') or 0) <= now and h not in self._running]

    def _tombstone(self, h):
        self._deleted = (self._deleted + [h])[-500:]

    def drop(self, h, reason='done'):
        with self._lock:
            self._items.pop(h, None)
            self._running.discard(h)
            self._tombstone(h)
            self.save()
        if reason != 'silent':
            self.stats['dropped'] += 1

    def done(self, h, key='', solve_time=0.0):
        """重解成功：写缓存由调用方负责（这里不依赖 key_cache，便于测试）。"""
        rec = self.get(h)
        with self._lock:
            self._items.pop(h, None)
            self._running.discard(h)
            self._tombstone(h)
            self.stats['solved'] += 1
            self.save()
        if self.verbose:
            _log(f'出卡 {h[:8]}（第 {(rec or {}).get("tries", 0) + 1} 次尝试，'
                 f'{solve_time:.1f}s），已移出队列')
        if self._on_success:
            try:
                self._on_success(h, rec, key, solve_time)
            except Exception:
                pass

    def bump(self, h, err='', gate=''):
        """重试仍失败：指数退避；确认无效或超龄就丢弃。"""
        now = time.time()
        with self._lock:
            v = self._items.get(h)
            if not v:
                return None
            tries = int(v.get('tries') or 0) + 1
            v['tries'] = tries
            v['last_err'] = str(err or '')[:300]
            v['last_ts'] = now
            v['gate'] = gate or v.get('gate') or ''
            self._running.discard(h)
            if is_permanent(err) or 'invalid-link' in str(err) or \
                    now - float(v.get('created') or 0) > self.max_age:
                self._items.pop(h, None)
                self._tombstone(h)
                dead = {'h': h, 'ticket': v.get('ticket'), 'tries': tries,
                        'err': str(err or '')[:200], 'ts': now}
                self._dead = [dead] + self._dead[:49]
                self.stats['permanent'] += 1
                self.save()
                return {'action': 'drop', 'tries': tries}
            delay = min(self.base_delay * (2 ** min(tries, 12)), self.max_delay)
            v['next_ts'] = now + delay
            self.save()
            return {'action': 'retry', 'tries': tries, 'next_in': round(delay, 1)}

    # ------------------------------------------------------------ worker
    def start(self, solve_fn, on_success=None):
        """启动后台重试线程。solve_fn(ticket) -> (key, err, solve_time)。"""
        self._on_success = on_success
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self._pump(solve_fn)
                except Exception as e:                 # worker 绝不能死
                    _log(f'worker 异常（已忽略）: {type(e).__name__}: {e}')
                self._stop.wait(self.tick)

        self._thread = threading.Thread(target=loop, name='retryq', daemon=True)
        self._thread.start()
        if self.verbose:
            _log(f'worker 已启动：并发 {self.concurrency}，退避 '
                 f'{self.base_delay:.0f}s~{self.max_delay / 60:.0f}min，'
                 f'最长保留 {self.max_age / 3600:.0f}h')
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout)

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _pump(self, solve_fn):
        with self._lock:
            room = self.concurrency - len(self._running)
        if room <= 0:
            return
        for h, rec in self.due()[:room]:
            with self._lock:
                if h in self._running or h not in self._items:
                    continue
                self._running.add(h)
                self.stats['running'] = len(self._running)
                rec['tries'] = int(rec.get('tries') or 0) + 1
                rec['last_ts'] = time.time()
                self._items[h] = rec
                self.save()
            threading.Thread(target=self._run_one,
                             args=(h, rec['ticket'], solve_fn),
                             name=f'retry-{h[:6]}', daemon=True).start()

    def _run_one(self, h, ticket, solve_fn):
        t0 = time.time()
        key, err, st = None, 'retry exception', 0.0
        try:
            result = solve_fn(ticket)
            # solve_fn 的返回元组会随调用方演进（3/4/5 项都出现过）。
            # 这里只取前三项，不让队列跟着求解器的签名改一次坏一次。
            if isinstance(result, (tuple, list)):
                if len(result) >= 3:
                    key, err, st = result[0], result[1], result[2]
                else:
                    err = f'bad solve result: {result!r}'
            else:
                err = f'bad solve result type: {type(result).__name__}'
            key = key or None
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
        st = float(st or (time.time() - t0))
        self.stats['attempts'] += 1
        if key:
            self.done(h, key, st)
        else:
            r = self.bump(h, err or 'unknown')
            if self.verbose and r:
                _log(f'{h[:8]} 第 {r["tries"]} 次仍失败: '
                     f'{str(err)[:60]} → {r["action"]}'
                     + (f' {r["next_in"]}s' if r.get('next_in') else ''))

    # ------------------------------------------------------------ 展示
    def snapshot(self, limit=8):
        now = time.time()
        with self._lock:
            items = sorted(self._items.values(),
                           key=lambda x: float(x.get('next_ts') or 0))
            return {
                'running': self.running(),
                'size': len(self._items),
                'in_flight': len(self._running),
                'stats': dict(self.stats),
                'next_in': (max(0, round(min(
                    (float(v.get('next_ts') or 0) - now) for v in items), 1))
                    if items else None),
                'items': [{
                    'h': v.get('h'), 'tries': v.get('tries'),
                    'gate': v.get('gate'), 'err': (v.get('last_err') or '')[:120],
                    'next_in': round(max(0, float(v.get('next_ts') or 0) - now), 1),
                    'age': round(now - float(v.get('created') or 0), 1),
                } for v in items[:limit]],
                'dead': self._dead[:5],
            }


_default = None
_default_lock = threading.Lock()


def default():
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = RetryQueue()
    return _default


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(description='失败重试队列')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--clear', action='store_true', help='清空队列（谨慎）')
    a = ap.parse_args()
    q = default()
    if a.clear:
        with q._lock:
            n = len(q._items)
            q._items.clear()
            q.save()
        print(f'已清空 {n} 条')
    else:
        print(json.dumps(q.snapshot(limit=50), ensure_ascii=False, indent=2))
