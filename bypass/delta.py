#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delta 解卡系统 · 单进程入口。

为什么要有这个文件
------------------
以前是两条命令、两个进程：

    python server.py                    # 对外服务
    python probe_gate.py --watch 300    # 盯上游门禁

服务器上只让跑一个脚本时，第二条就没了 —— 上游改了东西只能等用户来报错。
现在全部收进一个进程，一条命令：

    python delta.py

进程里跑的东西：

    ┌─ HTTP API（/delta /healthz /gate /heal）
    ├─ 求解线程池（--workers）
    ├─ 缓存（落盘 + 退出 flush）
    ├─ 版本监视（上游客户端版本，每小时）
    └─ 自愈线程（探测 -> 失败 -> 学习 -> 修复 -> 复测）

常用姿势
--------
    python delta.py                       # 正式跑
    python delta.py --port 8080 -w 16     # 换端口 / 加大并发
    python delta.py --status              # 看当前状态（不常驻，秒退）
    python delta.py --heal-once           # 手动跑一轮自愈（会真发请求）
    python delta.py --doctor              # 完整上线自检（15 项，含端到端）

环境变量见 README 的「环境变量」一节，最要紧的还是这三个：

    DELTA_NO_PROXY=1        服务器直连（最常见）
    DELTA_HEAL_INTERVAL=180 自愈探测间隔
    DELTA_HEAL=0            只观测不自动修
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _print_status():
    """不启动服务，只把当前状态打出来。排查时最先跑这个。"""
    import link_source as LS
    import selfheal as SH
    try:
        import auth_client as AUTH
        pi = AUTH.proxy_info()
    except Exception:
        pi = {'note': '(auth_client 不可用)'}

    st = LS.load_state()
    print('=' * 68)
    print('Delta 单进程状态')
    print('=' * 68)
    print(f'出网方式      {pi.get("note")}')
    print(f'自愈最优出网  {st.get("egress_best") or "(尚未试出)"}')
    print(f'上游门禁      {st.get("gate") or "未探测"}  '
          f'({LS.GATE_HUMAN.get(st.get("gate") or "", "-")})')
    print(f'已学字段名    {", ".join(st.get("learned_fields") or []) or "(无)"}')
    print(f'已学 sitekey  {LS.known_sitekey() or "(无)"}')
    try:
        import fastlane as FL
        print(f'求解预算      {FL.SOLVE_BUDGET:.0f}s  自适应步间隔 '
              f'{FL.step_gap():.2f}s（范围 {FL.GAP_MIN:.1f}~{FL.GAP_MAX:.1f}s）')
    except Exception:
        pass
    try:
        import retryq as RQ
        q = RQ.default().snapshot(limit=5)
        print(f'重试队列      {q["size"]} 条待解，已出卡 {q["stats"]["solved"]} 条，'
              f'在跑 {q["in_flight"]} 条')
        for it in q['items']:
            print(f'    {it["h"][:8]}  第 {it["tries"]} 次  '
                  f'{it["gate"] or "-":14s}  {it["next_in"]}s 后  '
                  f'{it["err"][:40]}')
    except Exception:
        pass

    try:
        import key_cache as KC
        c = KC.default()
        print(f'缓存          {c.path} ({c.source}, {len(c.data)} 条)')
    except Exception as e:
        print(f'缓存          读取失败: {type(e).__name__}')

    print('\n各层成功率（自学习排序依据）')
    stats = st.get('providers') or {}
    if not stats:
        print('  (还没有记录)')
    for name, p in stats.items():
        streak = p.get('fail_streak') or 0
        flag = f'  连续失败 {streak} 次，已降权' if streak else ''
        print(f'  {name:12s} 成功 {p.get("ok", 0):3d} / 失败 {p.get("fail", 0):3d}'
              f'  最近 {p.get("last_ms") or "-"}ms{flag}')

    print('\n经验表（失败类型 -> 动作 -> 结果）')
    lessons = st.get('lessons') or []
    if not lessons:
        print('  (还没有记录，说明自愈还没遇到过失败)')
    for l in lessons[:8]:
        ago = time.time() - float(l.get('ts') or 0)
        mark = 'OK ' if l.get('ok') else 'NO '
        print(f'  {mark}{l.get("gate"):18s} {l.get("action"):16s} '
              f'{int(ago)}s 前  {str(l.get("note") or "")[:50]}')

    heal = st.get('heal') or {}
    if heal.get('prev_gate'):
        print(f'\n最近一次自愈结论: {heal.get("prev_gate")}'
              f' 动作={heal.get("action") or "-"}'
              f' {str(heal.get("note") or "")[:60]}')
    return 0


def _bench(count):
    """端到端计时：生成 N 条新链接 -> 逐条真解 -> 输出 p50/p95 与阶段明细。"""
    import auth_client as AUTH
    import link_generator as LG
    import main as SOLVE
    import fastlane as FL

    print(f'[bench] 预热连接...', flush=True)
    FL.prewarm(background=False, timeout=6)
    times, ok = [], 0
    phase_total = {}
    for i in range(max(1, int(count))):
        t0 = time.time()
        try:
            url = LG.get_auth_link(6)
            tk = AUTH.extract_ticket(url)
            key, timer = SOLVE.solve_chain(tk, verbose=False,
                                           budget=FL.SOLVE_BUDGET)
        except Exception as e:
            key, timer = None, None
            print(f'  [{i + 1}] 异常 {type(e).__name__}: {e}', flush=True)
        dt = time.time() - t0
        times.append(dt)
        if key:
            ok += 1
        detail = ''
        if timer is not None:
            parts = sorted(timer.phases.items(), key=lambda x: -x[1])[:3]
            detail = ' '.join(f'{k}={v:.2f}s' for k, v in parts)
            for k, v in timer.phases.items():
                phase_total[k] = phase_total.get(k, 0) + v
        print(f'  [{i + 1}] {"OK  " if key else "FAIL"} {dt:6.2f}s  {detail}',
              flush=True)

    def pct(p):
        s = sorted(times)
        return s[min(len(s) - 1, int(len(s) * p))] if s else 0.0

    print('=' * 64)
    print(f'[bench] {ok}/{len(times)} 成功   平均 {sum(times) / max(1, len(times)):.2f}s'
          f'   p50 {pct(0.5):.2f}s   p95 {pct(0.95):.2f}s')
    for k, v in sorted(phase_total.items(), key=lambda x: -x[1]):
        print(f'    {k:12s} {v / max(1, len(times)):.2f}s/条')
    print('=' * 64)
    return 0 if ok == len(times) else 1


def main():
    ap = argparse.ArgumentParser(
        description='Delta 解卡系统：一个进程跑完服务 + 监控 + 自愈',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s                          正式跑（服务 + 自愈同进程）
  %(prog)s --port 8080 -w 16        换端口 / 并发
  %(prog)s --status                 只打印状态，不常驻
  %(prog)s --heal-once              手动跑一轮自愈（会真发请求）
  %(prog)s --doctor                 完整上线自检（15 项）
  %(prog)s --no-heal                只跑服务，不启动自愈线程
  %(prog)s --bench 5                生成 5 条新链接端到端计时（p50/p95）
  %(prog)s --mode standard          回到保守自愈参数（默认 full=全动作）
''')
    ap.add_argument('--port', '-p', type=int, default=2233)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--workers', '-w', type=int, default=10,
                    help='求解线程数（不是进程数，别和 uvicorn --workers 混）')
    ap.add_argument('--heal-interval', type=int, default=None,
                    help='自愈探测间隔秒数，默认 180；0 = 不启动后台线程')
    ap.add_argument('--no-heal', action='store_true', help='等价 --heal-interval 0')
    ap.add_argument('--status', action='store_true', help='打印状态后退出')
    ap.add_argument('--heal-once', action='store_true', help='跑一轮自愈后退出')
    ap.add_argument('--doctor', action='store_true', help='跑完整上线自检')
    ap.add_argument('--forget-fields', action='store_true',
                    help='清空学到的字段名与猜出来的组合（上游改字段后用）')
    ap.add_argument('--json', action='store_true', help='配合 --status/--heal-once')
    ap.add_argument('--mode', choices=['full', 'standard'],
                    help='自愈模式：full=什么都可以动（默认）；standard=保守冷却')
    ap.add_argument('--bench', type=int, metavar='N',
                    help='生成 N 条新链接端到端计时，不启服务')
    ap.add_argument('--queue', action='store_true', help='只看重试队列，不启服务')
    ap.add_argument('--flush-queue', action='store_true',
                    help='清空失败重试队列（谨慎：未解出的票不会再后台重试）')
    a = ap.parse_args()

    if a.mode:
        os.environ['DELTA_HEAL_MODE'] = a.mode

    if a.flush_queue:
        import retryq as RQ
        q = RQ.default()
        with q._lock:
            n = len(q._items)
            q._deleted = (q._deleted + list(q._items.keys()))[-500:]
            q._items.clear()
            q.save()
        print(f'已清空重试队列 {n} 条')
        return 0

    if a.queue:
        import retryq as RQ
        print(json.dumps(RQ.default().snapshot(limit=50),
                         ensure_ascii=False, indent=2))
        return 0

    if a.bench is not None:
        return _bench(a.bench)

    if a.forget_fields:
        import link_source as LS
        old = LS.forget_fields()
        print(f'已清空 {len(old)} 个字段名' + (f': {", ".join(old)}' if old else ''))
        print('下一次请求会从干净的 body 重新试；若上游真的改字段，自愈会重新学。')
        return 0

    if a.status:
        if a.json:
            import link_source as LS
            import key_cache as KC
            snap = {
                'gate': LS.load_state().get('gate'),
                'learned_fields': LS.load_state().get('learned_fields') or [],
                'sitekey': LS.known_sitekey() or None,
                'providers': LS.load_state().get('providers') or {},
                'lessons': LS.load_state().get('lessons') or [],
                'egress_best': LS.load_state().get('egress_best'),
                'cache': {'path': KC.default().path,
                          'entries': len(KC.default().data)},
            }
            try:
                import fastlane as FL
                snap['fast'] = FL.snapshot()
            except Exception:
                pass
            try:
                import retryq as RQ
                snap['retryq'] = RQ.default().snapshot(limit=20)
            except Exception:
                pass
            print(json.dumps(snap, ensure_ascii=False, indent=2))
            return 0
        return _print_status()

    if a.doctor:
        import preflight
        return preflight.main_entry()

    if a.heal_once:
        import selfheal as SH
        h = SH.Healer(interval=a.heal_interval or 180,
                      auto=not a.no_heal)
        r = h.run_once(source='cli')
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json
              else '\n'.join(f'{k:10s} {v}' for k, v in r.items()))
        return 0 if r.get('ok') else 1

    # 正常启动：服务 + 自愈同一个进程
    import server
    interval = 0 if a.no_heal else a.heal_interval
    try:
        server.run(a.host, a.port, a.workers, interval)
    except KeyboardInterrupt:
        print('\n[delta] 收到中断，正在退出', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
