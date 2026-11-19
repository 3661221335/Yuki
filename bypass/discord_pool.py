#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discord 账号池：配额记账 + 轮换 + 登录态隔离。

【为什么需要这个】
走浏览器路径时，auth 页会要求 Discord OAuth 授权，而每个 Discord 账号每天只有
3 次授权额度。单账号跑到第 4 条就废，所以必须：

  1. 记账：谁今天用了几次，清清楚楚
  2. 轮换：优先用"今天还没用满、且最久没用"的账号
  3. 隔离：一个账号一个浏览器 profile，登录态互不污染

【为什么不存密码】
Discord 的登录页自己也挂 hCaptcha，用账号密码做自动化登录极不稳定，而且密码
落盘是纯粹的风险。正确做法是：**每个账号配一个 profile 目录，首次人工登录一次**，
之后 cookie 一直有效（Discord 的登录态能维持很久），程序只需点一下 Authorize。

所以这里的账号 = {标签, profile 目录, 配额记录}，不含任何凭据。

【日界】
默认按本机日期重置。如果上游按 UTC 算，设 DELTA_QUOTA_TZ=utc 对齐，
避免"我这边刚过零点、上游还没重置"的错位。
"""

import json
import os
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, '.discord_accounts.json')
DEFAULT_LIMIT = 3          # 每账号每天授权次数


def _today():
    """返回用于判断"新的一天"的日期串。"""
    tz = (os.environ.get('DELTA_QUOTA_TZ') or 'local').strip().lower()
    if tz == 'utc':
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return time.strftime('%Y-%m-%d')


class AccountPool:
    """账号池。数据落在 JSON，多进程用简单重读规避（写很少，不需要 flock）。

    结构：
    {
      "limit": 3,
      "accounts": {
        "discord-a": {"profile": ".profiles/discord-a", "used": 1,
                       "date": "2026-11-20", "last_used": 1795..., "note": ""}
      }
    }
    """

    def __init__(self, path=None, limit=None):
        self.path = path or DEFAULT_PATH
        self.limit = limit or DEFAULT_LIMIT
        self.data = {'limit': self.limit, 'accounts': {}}
        self.load()

    # ---------------- 磁盘 ----------------
    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get('accounts'), dict):
                self.data = d
                self.data.setdefault('limit', self.limit)
                self.limit = int(self.data.get('limit') or self.limit)
        except Exception:
            pass
        self._roll_day()
        return self.data

    def save(self):
        tmp = f'{self.path}.tmp.{os.getpid()}'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
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

    def _roll_day(self):
        """把过期的 used 清零。只在内存里做，调用方负责 save。"""
        today = _today()
        changed = False
        for rec in self.data['accounts'].values():
            if rec.get('date') != today:
                rec['date'] = today
                rec['used'] = 0
                changed = True
        return changed

    # ---------------- 账号管理 ----------------
    def add(self, label, profile=None, note=''):
        profile = profile or os.path.join('.profiles', label)
        self.data['accounts'][label] = {
            'profile': profile, 'used': 0, 'date': _today(),
            'last_used': 0, 'note': note,
        }
        self.save()
        return self.data['accounts'][label]

    def remove(self, label):
        if label in self.data['accounts']:
            self.data['accounts'].pop(label)
            self.save()
            return True
        return False

    def remaining(self, label):
        rec = self.data['accounts'].get(label)
        if not rec:
            return 0
        if rec.get('date') != _today():
            return self.limit
        return max(0, self.limit - int(rec.get('used') or 0))

    def pick(self, exclude=()):
        """挑一个还能用、且最久没用过的账号（轮换，避免单号集中消耗）。

        返回 (label, rec) 或 (None, None)。
        """
        self._roll_day()
        cands = [(lb, r) for lb, r in self.data['accounts'].items()
                 if lb not in exclude and self.remaining(lb) > 0]
        if not cands:
            return None, None
        # 剩余多的优先，其次最久没用的优先
        cands.sort(key=lambda x: (-self.remaining(x[0]), x[1].get('last_used') or 0))
        return cands[0]

    def consume(self, label, n=1):
        rec = self.data['accounts'].get(label)
        if not rec:
            return False
        if rec.get('date') != _today():
            rec['date'] = _today()
            rec['used'] = 0
        rec['used'] = int(rec.get('used') or 0) + n
        rec['last_used'] = time.time()
        self.save()
        return True

    def refund(self, label):
        """授权失败时把额度退回去 —— 上游没扣就不该算我们的账。"""
        rec = self.data['accounts'].get(label)
        if rec and int(rec.get('used') or 0) > 0:
            rec['used'] = int(rec['used']) - 1
            self.save()
            return True
        return False

    # ---------------- 展示 ----------------
    def summary(self):
        self._roll_day()
        rows = []
        for lb, r in sorted(self.data['accounts'].items()):
            rows.append({
                'label': lb,
                'remaining': self.remaining(lb),
                'used': int(r.get('used') or 0),
                'limit': self.limit,
                'profile': r.get('profile'),
                'exists': os.path.isdir(r.get('profile') or ''),
                'note': r.get('note', ''),
            })
        total = sum(x['remaining'] for x in rows)
        return {'accounts': rows, 'total_remaining': total,
                'limit_per_account': self.limit, 'date': _today()}

    def reset_all(self):
        today = _today()
        for r in self.data['accounts'].values():
            r['date'] = today
            r['used'] = 0
        self.save()


def main():
    """命令行查配额：python discord_pool.py / add / reset"""
    import argparse
    ap = argparse.ArgumentParser(description='Discord 账号池配额管理')
    sub = ap.add_subparsers(dest='cmd')

    a = sub.add_parser('add', help='添加账号')
    a.add_argument('label')
    a.add_argument('--profile', default=None)
    a.add_argument('--note', default='')

    r = sub.add_parser('remove', help='删除账号')
    r.add_argument('label')

    sub.add_parser('list', help='查看配额（默认）')
    sub.add_parser('reset', help='今天全部清零（谨慎）')

    args = ap.parse_args()
    pool = AccountPool()

    if args.cmd == 'add':
        rec = pool.add(args.label, args.profile, args.note)
        print(f'已添加 {args.label} -> {rec["profile"]}')
        print('下一步：用该 profile 启动浏览器，手动登录一次 Discord')
        print(f'  python browser_solver.py "<url>" --visible --profile {rec["profile"]}')
    elif args.cmd == 'remove':
        print('已删除' if pool.remove(args.label) else '没有这个账号')
    elif args.cmd == 'reset':
        pool.reset_all()
        print('已全部清零')
    else:
        s = pool.summary()
        print(f'日期: {s["date"]}   单号上限: {s["limit_per_account"]}')
        if not s['accounts']:
            print('（还没有账号，用 add 添加）')
        for x in s['accounts']:
            flag = 'OK ' if x['exists'] else 'NEW'
            print(f'  [{flag}] {x["label"]:<16} 剩余 {x["remaining"]}/{x["limit"]}'
                  f'   profile={x["profile"]}  {x["note"]}')
        print(f'合计今日剩余: {s["total_remaining"]} 次')


if __name__ == '__main__':
    main()
