#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""key 本地缓存 —— server.py 与 main.py 共用。

为什么单独抽一个模块：server.py 要 `from main import solve_chain`，如果缓存
写在 main.py 里，server.py 再 import 缓存就会绕回自己。抽到无依赖的独立模块，
两边都能用，也不会循环导入。

缓存键 = md5(ticket)，值 = {key, ts, solve_time}。

为什么复跑要查缓存：单进程每次启动都要重新做 DNS + TCP + TLS 握手（走代理时
实测 ~4s，占单次求解的一半以上）。已出过 key 的链接再解一遍纯属浪费——上游
对同一 identifier 会直接回 `a key for this identifier already exists.`，本来
就只是让客户端去读。直接读本地缓存，整条链路（metadata + step + poll）全跳过。
"""

import os
import json
import time
import hashlib
import tempfile
import threading

try:
    import fcntl
except ImportError:          # Windows 没有 fcntl，退回进程内锁（CLI 够用）
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))

# key 24h 内会被消耗，缓存也就设 24h。可用 DELTA_CACHE_TTL 覆盖（秒）。
def _ttl():
    try:
        v = float(os.environ.get('DELTA_CACHE_TTL') or 0)
        return v if v > 0 else 24 * 3600
    except (TypeError, ValueError):
        return 24 * 3600


CACHE_TTL = _ttl()


def _writable(path):
    """真去写一个探针文件，别只靠 os.access（root 下会骗人）。"""
    try:
        d = os.path.dirname(os.path.abspath(path)) or '.'
        os.makedirs(d, exist_ok=True)
        probe = path + '.probe'
        with open(probe, 'w') as f:
            f.write('{}')
        os.remove(probe)
        return True
    except Exception:
        return False


def pick_cache_file():
    """按优先级选第一个真能写的落点，返回 (path, source)。"""
    cands = [os.path.join(HERE, '.key_cache.json')]
    if os.name == 'posix':
        # 只有 Linux/Unix 才走系统路径；Windows 上 "/var/lib/..." 会被解释成
        # 当前盘符下的 \var\lib\...，平白建一堆目录。
        cands.append('/var/lib/delta/keys.json')
    cands.append(os.path.expanduser('~/.cache/delta/keys.json'))
    cands.append(os.path.join(tempfile.gettempdir(), 'delta-keys.json'))

    want = (os.environ.get('DELTA_CACHE_FILE') or '').strip()
    if want:
        if _writable(want):
            return want, 'DELTA_CACHE_FILE'
        # 显式指定却写不了（只读挂载、目录无权）—— 不静默忽略，把原因带回启动日志
        for p in cands[1:]:
            if _writable(p):
                return p, 'DELTA_CACHE_FILE(%s) 不可写，已回退' % want
        return cands[-1], 'DELTA_CACHE_FILE(%s) 不可写，回退到临时目录' % want
    for p in cands:
        if _writable(p):
            return p, 'auto'
    return cands[-1], 'fallback'


class KeyCache:
    """进程内内存 + 磁盘 JSON 双层缓存。

    - CLI：put 之后显式 save_now()，进程退出即落盘，下次启动直接命中。
    - server：start_writer() 起后台 debounce 线程，合并写盘不阻塞请求。
    """

    def __init__(self, path=None, ttl=None):
        if path:
            self.path = path
            self.source = 'explicit'
        else:
            self.path, self.source = pick_cache_file()
        self.ttl = ttl or CACHE_TTL
        self.lock_file = self.path + '.lock'
        self.data = {}
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._writer = None
        self.last_error = None

    # ---------------- 磁盘 ----------------
    def _read_disk(self):
        """读盘并丢掉过期项。文件坏了就当空，绝不抛。"""
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                return {}
            now = time.time()
            return {h: v for h, v in d.items()
                    if isinstance(v, dict) and now - v.get('ts', 0) < self.ttl}
        except Exception:
            return {}

    def load(self):
        # 原地 clear+update 而不是重新赋值 self.data —— 外部（server.py 的
        # key_cache 别名）持有的是同一个 dict 对象，重新赋值会让它们脱钩。
        fresh = self._read_disk()
        with self._lock:
            self.data.clear()
            self.data.update(fresh)
        return len(self.data)

    def save_now(self):
        """读-合并-写 全程持文件锁，多进程并发也不会互相抹掉。

        为什么必须合并而不是直接写自己那份：gunicorn / uvicorn --workers N
        起多进程时，每个进程只持有自己内存里的那份。若各自覆盖写，A 刚存的 key
        会被 B 下一次落盘直接抹掉——表现为"明明出过 key，再查又没了"。
        """
        with self._lock:
            now = time.time()
            for h in [h for h, v in self.data.items()
                      if now - v.get('ts', 0) >= self.ttl]:
                self.data.pop(h, None)
            snapshot = dict(self.data)

        tmp = '%s.tmp.%d' % (self.path, os.getpid())
        lf = None
        try:
            lf = open(self.lock_file, 'a+')
            if fcntl is not None:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            merged = self._read_disk()
            merged.update(snapshot)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())      # 被 kill -9 / 断电也不留半截文件
            os.replace(tmp, self.path)
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
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

    def start_writer(self, debounce=2.0):
        """后台 debounce 落盘（server 用）。重复调用无副作用。"""
        if self._writer is not None:
            return self._writer

        def loop():
            while True:
                self._dirty.wait()
                time.sleep(debounce)
                self._dirty.clear()
                self.save_now()

        self._writer = threading.Thread(target=loop, daemon=True,
                                        name='key-cache-writer')
        self._writer.start()
        return self._writer

    # ---------------- 读写 ----------------
    @staticmethod
    def h(ticket):
        return hashlib.md5(ticket.encode('utf-8', 'replace')).hexdigest()

    def get(self, ticket, h=None):
        """返回 (key, hit, solve_time)。过期项顺手清掉。"""
        h = h or self.h(ticket)
        with self._lock:
            rec = self.data.get(h)
            if rec:
                if time.time() - rec.get('ts', 0) < self.ttl:
                    return rec.get('key'), True, rec.get('solve_time', 0.0)
                self.data.pop(h, None)
        return None, False, 0.0

    def put(self, ticket, key, solve_time=0.0, h=None):
        if not key:
            return False
        h = h or self.h(ticket)
        with self._lock:
            self.data[h] = {'key': key, 'ts': time.time(),
                            'solve_time': float(solve_time or 0.0)}
        self._dirty.set()
        return True

    def stats(self):
        with self._lock:
            return {'file': self.path, 'source': self.source,
                    'entries': len(self.data), 'ttl': self.ttl,
                    'last_error': self.last_error}


# server.py 与 main.py 各自持有一个实例，共用同一个文件路径（路径探测逻辑一致，
# 所以落到同一个文件），跨进程靠 flock 保证不互相覆盖。
_default = None
_default_guard = threading.Lock()


def default():
    global _default
    if _default is None:
        with _default_guard:
            if _default is None:
                _default = KeyCache()
                _default.load()
    return _default
