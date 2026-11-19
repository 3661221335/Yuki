# Delta Cardkey Solver

自动完成 Platoboost Delta Key System 关卡递进→获取 key 的全流程求解器

提供 CLI 与 HTTP API 两种用法。

---

## 最近更改

### 一个进程 + 自己学习失败（2026-11）

**服务器只能跑一个脚本，所以服务、监控、自愈全部合并进 `delta.py`，一条命令。**

```bash
python delta.py            # 服务 + 上游监控 + 自愈，全在这一个进程里
```

以前是两条命令两个进程（`server.py` + `probe_gate.py --watch`）。合并之后，
进程里同时跑这些东西：

| 组件 | 作用 |
|---|---|
| HTTP API | `/delta` `/healthz` `/gate` `/heal` |
| 求解线程池 | `--workers`，不是进程数 |
| 缓存 | 落盘 + 退出 flush |
| 版本监视 | 上游客户端版本，每小时 |
| **自愈线程** | 探测 → 失败 → 学习 → 修复 → 复测 |

#### "自己学习失败"到底学了什么

不是把报错翻译成人话，而是把**失败类型 → 试过的动作 → 结果**记下来，下次遇到
同一个失败先试上次有效的那个。三张表存在 state 文件里（不做数据库）：

**① 字段名（从上游的报错里学）**

上游是 NestJS 校验，缺字段会报 `body must have required property 'xxx'`。程序
从这句话里把字段名抠出来存下，下一次请求自动带上。

报错里没写字段名时走第二手：把 7 个候选字段名（`turnstileToken` `cfToken`
`captcha`…）全填成空串发一次，能过就记住"这个组合要带空串"。这个结论存在
`guess_fields`，和"上游明说的字段名"分开记——因为前者必须继续发空串，后者
可以发 `null`，混在一起下一次又会被拒。

**② sitekey（从页面里学）**

上游一旦启用 Turnstile，sitekey 必须交给前端渲染，所以它一定会出现在页面
HTML / JS bundle / 403 响应某一处。每次响应都顺手扫一遍 `0x4A...` 模式，扫到
就记住，captcha 层直接用。

**③ 哪一层还能用（从成功率里学）**

每层都有连续失败计数。某层刚连着失败 3 次，排序时自动往后排（罚分上限 6，
不会永久踢出）；成功一次就复位。这样上游恢复时它自己会回到前排，不需要人管。

#### 失败之后它会做什么

| 上游怎么了 | 程序自己做什么 |
|---|---|
| 缺字段 | 学字段名 → 带空串重发 |
| Cloudflare 挡了 | 刷 cookie → 换出口 IP 方式 → 换备用域名 |
| 要 API key | 检查有没有配 → 配了就自动带上重发 |
| 加了 Turnstile | 挖 sitekey → 有打码 key 就打码出链接 |
| 网络不通 | 直连 / 走环境代理各试一次，哪个通用哪个 |
| 429 限流 | 间隔自动拉长（30s 起，翻倍到 15 分钟上限） |

**修完立刻复测**：动作说成功但复测还不通，就如实报失败，不会在日志里写"已自愈"
而用户那边还在报错。

#### 三个防呆

- **同一类失败 60s 内只修一次**（`DELTA_HEAL_COOLDOWN`），一小时最多 30 次
  （`DELTA_HEAL_HOURLY`）—— 否则检查本身就成了压测。
- **求解失败会立刻叫醒它**，但同类事件自动去重（20 个请求同一个原因 = 一堵墙，
  修一次就够）。
- **只观测不修**：`DELTA_HEAL=0`，`/heal` 仍可手动触发。

#### 用法

```bash
python delta.py                      # 正式跑（默认 2233 端口，10 线程，180s 探测）
python delta.py --status             # 看状态：门禁、学到什么、经验表，秒退
python delta.py --heal-once          # 手动跑一轮（会真发请求）
python delta.py --doctor             # 完整上线自检（15 项，含端到端）
python delta.py --forget-fields      # 清掉学错的字段名（上游改字段后用）
python delta.py --no-heal            # 只跑服务
```

HTTP 侧：

```bash
curl http://127.0.0.1:2233/healthz          # 含 heal 段：running/auto/last/stats/lessons
curl -X POST http://127.0.0.1:2233/heal     # 探测+修复+复测，一次说清
```

`/healthz` 里新增的 `heal` 段长这样：

```json
"heal": {
  "running": true, "auto": true, "interval": 180,
  "last": {"ok": true, "gate": "open", "ms": 487.7, "repaired": false},
  "stats": {"probe": 12, "repair": 1, "fixed": 1, "skipped": 0},
  "lessons": [{"gate": "schema", "action": "guess_fields", "ok": true, "ago": 43.2}],
  "egress_best": "direct"
}
```

#### 验证

```bash
python selftest_heal.py     # 35 项断言，12 个场景，全程不连上游
python selftest_gate.py     # 18 项，六层退路
python preflight.py         # 15 项上线自检
```

`selftest_heal.py` 最关键的一条：伪造"上游要求 `turnstileToken` 字段"，第一次
请求必然失败，然后断言程序自己学会了、下一次请求带上了、复测通过、经验表里记
下了这个结论。**没有任何人改代码。**

### 不需要任何账号：整条链路零凭据（2026-11）

**结论先说：这套系统不需要 Discord 账号，也不需要任何 Platoboost / Delta 账号。**

三条可复核的证据：

**1. 建房接口是公开的**

`link_generator.py` 调的是 `POST https://api.platoboost.net/public/start`，请求体只有  
两个字段：

```json
{"service": 6, "identifier": "<随机 hwid>"}
```

没有 cookie、没有 `Authorization`、没有 API key。实测连发 4 次全部 `HTTP 200` 并  
下发完整 URL（首次 0.8~~6.8s 建 TLS，之后 0.25~~0.26s/条，无速率限制迹象）。

**2. 求解链路里不存在任何账号概念**

```
grep -iE "discord|oauth|authorize|account|login|quota" auth_client.py main.py
→ 零命中
```

`main.py` 的求解只做三件事：`metadata` → `step` → 轮询取 key，全部凭 ticket 完成。

**3. ticket 是自包含的加密信封，没有用户 ID 字段**

ticket 441 字符的结构：

| 偏移        | 长度  | 用途                           |
| --------- | --- | ---------------------------- |
| `[0:16]`  | 16  | meta 的 AES key               |
| `[16:32]` | 16  | meta 的 CTR 计数器初值             |
| `[1:17]`  | 16  | stream 的 AES key（与上一段重叠 1 位） |
| `[17:33]` | 16  | stream 的 CTR 计数器初值           |
| `[33:]`   | 408 | base64url 密文                 |

`base64url(ticket[17:])` 解出 318 字节，前 16 字节正好是那把 key  
（`4c6d01c0...` 对应 `TG0BwLYKXhG3xgBG`），印证了上述布局。**信封里装的是密钥材料  
和密文，没有任何账号引用** —— 谁能拿到 ticket，谁就能以这条会话的身份执行。

所以「每个账号每天只能授权三次」是**页面层**的限制：浏览器打开 auth 页 → 页面要求  
Discord 登录 → 授权后页面替你调 `/public/start`。配额绑在那条路上。  
协议层走公开接口匿名建房，配额成本不落在你这里。

**一条命令自产自解（实测 24.45s → `FREE_be151e...`）：**

```bash
python main.py --generate 1 --no-cache
```

| 你在做什么                  | 需要账号吗         |
| ---------------------- | ------------- |
| 解别人发来的链接               | **不要**        |
| 用 `--generate` 自己产链接再解 | **不要**        |
| 浏览器打开 auth 页手动点授权      | 要（这才消耗 3 次/天） |

只有当上游给 `/public/start` 加上鉴权（Turnstile / token / 设备绑定）时，才必须回到  
浏览器 + 账号池，那时用 `browser_solver.py --pool`。

### 退路：上游哪天加了授权，怎么不被打断（2026-11）

`/public/start` 现在是公开的，但那是**对方的决定，不是我们的保障**。所以把「拿一条  
链接」做成了可插拔的 provider 链（`link_source.py`），上游改一处不会让整条链停摆。

| 上游可能怎么加                     | 谁接管            | 你要做什么                                    |
| --------------------------- | -------------- | ---------------------------------------- |
| 要 API key / Bearer          | `public`（自动带上） | 设 `DELTA_API_KEY`                        |
| 要普通 cookie / `cf_clearance` | `cookie`       | **什么都不用做**，自动尝试                          |
| 主域名被区域策略拦                   | `mirror`       | 设 `DELTA_API_HOSTS=备用域名,...`             |
| 加 Turnstile                 | `captcha`      | 设 `CAPSOLVER_KEY`（sitekey 自动学）           |
| 强制 Discord OAuth            | `browser`      | 装 playwright，`--pool` 走账号池               |
| 整个接口下线                      | `manual`       | 设 `DELTA_LINK_INBOX` / `DELTA_LINK_FEED` |
| step 也要验证码                  | `CAPTCHA_HOOK` | 设 `DELTA_CAPTCHA_CMD="<打码命令>"`           |

#### 三个自适应：不需要等人改代码

**1. 字段名自学习**  
上游是 NestJS 风格校验，缺字段会报 `body must have required property 'xxx'`。  
程序从这句话里**学**出字段名存进 state，下次请求直接带上。它加什么字段，  
自己就学会了。已实测：报 `turnstileToken` → 重试时 body 里就有 `turnstileToken`。

**2. sitekey 自学习**  
上游启用 Turnstile 时，sitekey 必须交给前端渲染，所以一定出现在某个响应里。  
每次响应都扫一遍 `0x4A...`，扫到就记住，`captcha` 层直接用。不用人去挖。

**3. 状态分类**  
每次失败都归类成 `open / cf-challenge / auth-required / schema /
captcha-required / rate-limited / offline`，并落进 state。全链路失败时抛出的是  
**最有信息量的那个**（而不是笼统的 unknown），`/healthz` 和 `probe_gate.py`  
都读它。

#### 命令速查

> 注意：**服务和上游监控现在是同一个进程**（`delta.py`）。下面的 `probe_gate.py`
> 只在"我就想单独查一眼、不想动服务"时用，不要再挂成第二个常驻服务 ——
> 那等于把探测翻倍。

```bash
python delta.py --status              # 一条命令看全：门禁 / 学到什么 / 经验表
python probe_gate.py                  # 只看门禁：哪层可用、该做什么
python probe_gate.py --json           # 给监控系统吃
python probe_gate.py --find-sitekey   # 上游上 Turnstile 后挖 sitekey
python link_source.py --fetch         # 直接取一条，看哪层接管的
python selftest_gate.py               # 18 项打桩自检，不碰真实上游
python selftest_heal.py               # 35 项自愈自检，不碰真实上游
```

告警不用再单独起 watch，服务进程自己会报：

```bash
# 放在 /etc/delta/delta.env 里，systemctl restart delta 生效
DELTA_ALERT_LOG=/var/log/delta/gate.log
DELTA_ALERT_WEBHOOK=https://your-hook
```

#### 验证：18 项打桩自检全部通过

上游什么时候加门禁没法预知，但代码对不对可以现在就知道。`selftest_gate.py`  
把各种门禁形态伪造出来逐个验证：

```
[1] 正常 200+url                    -> public 层出链接            PASS
[2] 403 Just a moment               -> 判 cf-challenge            PASS
[3] 401 unauthorized                -> 判 auth-required           PASS
[4] required property turnstileToken -> 学字段 -> 重试带上 -> 成功  PASS
[5] invalid turnstile token         -> 判 captcha-required        PASS
[6] 429                             -> 判 rate-limited            PASS
[7] 响应含 0x4A... sitekey          -> 自动记住                    PASS
[8] 外部队列 3 条                    -> 依次消费、空时报错          PASS
[9] public 403 -> cookie 接管        -> 降级成功                    PASS
结果: 18/18
```

其中第 4 项最能说明设计意图：**没人告诉程序要加什么字段，它是从上游报错里学来的。**

### 浏览器通道：什么时候该开浏览器，什么时候别开（2026-11）

**先记结论：能走协议就别开浏览器。**

主链路 `do_step()` 直接 `PUT /api/session/step`，body 里 `"captcha": null`，服务端  
照样接受。因为 reCAPTCHA 是**页面 JS 主动加载并打分**的，而这条链路从不加载页面、  
不执行 JS，服务器连一个可评分的信号都拿不到 —— 没有信号就不评分，不评分就不要  
token。这不是破解验证码，是根本没进入那个流程。

一旦真的用浏览器打开 auth 页，下面这些信号全都有了：

| 信号                    | 协议直连 | 真实浏览器 |
| --------------------- | ---- | ----- |
| `navigator.webdriver` | 不存在  | 可被检测  |
| Canvas / WebGL 指纹     | 无    | 有     |
| 鼠标轨迹、滚动、停留时长          | 无    | 有     |
| reCAPTCHA 脚本          | 从不加载 | 注入并执行 |
| cookie / 历史           | 每次全新 | 可累积   |

机房 IP + 无历史 + 自动化特征 = v3 打分低 → 降级弹 v2 图片挑战。**「打开浏览器  
就有验证」的全部原因就在这里**，跟链接本身无关。

所以 `browser_solver.py` 的定位是**兜底**，不是主路径：  
只有协议被风控拒了、或某些链接强制走页面时才用。

```bash
pip install playwright          # 复用系统 Edge/Chrome，无需 install chromium

# 半自动（推荐）：弹出浏览器，你点掉验证，程序自动抓 key 并写缓存
python browser_solver.py "<url>" --visible

# 全自动：遇到 reCAPTCHA v2 调 CapSolver
python browser_solver.py "<url>" --auto-solve --capsolver <KEY>
```

**profile 是这套方案的核心**。`--profile` 目录默认 `.browser_profile`，持久化  
cookie / localStorage / 指纹种子。同一个 profile 用得越久，reCAPTCHA 信任分越高，  
v2 图片挑战出现得越少。反过来每次开无痕 = 每次都是零信任新访客 = 必然弹挑战。

key 抓到后会写进与 CLI / HTTP **完全相同**的缓存，所以：

```
第一次开浏览器过验证 → 抓 key → 落库
此后同链接复跑        → 0.00s，不用再开浏览器
```

这就是正确的用法：**浏览器只用来过一次验证，之后回到协议链路**。

#### 验证码形态与对策

| 形态                   | 表现           | 对策                                                      |
| -------------------- | ------------ | ------------------------------------------------------- |
| reCAPTCHA v3（无感）     | 看不见，只在后台打分   | 真实浏览器 + 住宅 IP + 养熟的 profile，通常直接过。打码平台对它**无效**（没有可解的挑战） |
| reCAPTCHA v2 复选框     | 一个「我不是机器人」方框 | 分数够时点一下即过；不够才弹图片                                        |
| reCAPTCHA v2 图片      | 九宫格选红绿灯/斑马线  | 只能打码平台（CapSolver/2Captcha）或人工                           |
| Cloudflare Turnstile | 页面内嵌小组件      | 多数无感通过；被拦时换 IP 比打码有效                                    |

**v3 分数低时，换 IP 的收益远大于打码** —— v3 根本没有挑战可解，问题出在 IP 信誉  
和浏览器指纹上。

#### Discord 授权配额（每号每天 3 次）

浏览器路径要过 Discord OAuth，而每个 Discord 账号每天只有 3 次授权额度。  
单账号跑到第 4 条就废，所以 `discord_pool.py` 负责记账和轮换：

```bash
python discord_pool.py add acct-01            # 添加账号（建一个独立 profile）
python discord_pool.py add acct-02
python discord_pool.py list                   # 看今天各号剩几次

python browser_solver.py "<url>" --visible --pool   # 自动挑一个还有额度的号
```

规则是**剩余额度多的优先，其次最久没用的优先**，不会把某个号往死里用。  
拿到 key 才扣额度，失败不记账（`refund` 也留着给上游误判时用）。

日界默认按本机日期，上游若按 UTC 算就设 `DELTA_QUOTA_TZ=utc`，免得  
「我这边过零点了、上游还没重置」。

> **账号池不含密码。** Discord 登录页自己也挂 hCaptcha，做密码自动化极不稳定  
> 且要把凭据落盘。正确做法是每个账号配一个 profile 目录，**首次人工登录三次  
> 点击的事**，之后 cookie 长期有效，程序只需点一下 Authorize —— 那一步已经  
> 自动做了（`try_authorize`）。

同一个 IP 上登录多个 Discord 账号会触发 Discord 自己的风控，账号池要真正跑起来  
得配**一账号一出口 IP**，否则号会连着被封，配额再多也没用。

### 复跑提速：CLI 接入本地缓存（2026-11）

**症状**：同一条链接，第一次解 15.9s，隔几分钟再跑还是 7.1s。理论上第二条应该几  
乎不花时间——上游对同一个 identifier 只会回 `a key for this identifier already
exists.`，本来就不需要重新算。

**根因不在求解逻辑，在"每次进程都要重新握手"**：

| 请求                       | 耗时           |
| ------------------------ | ------------ |
| 第 1 次 `metadata`（进程刚起）   | **3.97s**    |
| 第 2~4 次 `metadata`（连接已热） | 0.38 ~ 0.45s |

多出来的 3.5s 是 DNS + TCP + **TLS 握手**（走代理还要加一次 CONNECT 隧道）。  
单进程内无论怎么优化代码都省不掉——除非根本不发请求。

**做法**：把 server 早就有的缓存层抽成 `key_cache.py`，CLI 也接上。

- 求解前先查 `md5(ticket)`：命中就整条链路跳过，一次网络包都不发
- 求解成功后立刻 `save_now()` 落盘（CLI 马上就退出，不能等 2s 防抖）
- 与 `server.py` 共用同一个文件、同一套 `flock` 读-合并-写，两边互相看得见
- 新增 `--no-cache` 强制重解

**实测**（同一条链接）：

| 场景               | 耗时                             |
| ---------------- | ------------------------------ |
| 首次（真解 + 写缓存）     | 1.9s ~ 7.0s（取决于链路抖动）           |
| **复跑（命中缓存）**     | **0.00s**（整进程墙钟 0.66s，全是解释器启动） |
| HTTP `/delta` 复跑 | **0.05s**                      |

顺带收紧了两处超时：`read 12s -> 8s`、`meta 首轮 12s -> 10s`。上游正常应答是  
0.4s，12s 是 30 倍余量属于过度保守；而一旦链路抽风，12s 会把"一次抖动"直接放大  
成 20s+ 的等待，8s 既不会误杀慢响应，又能让抖动早触发重试。

### 服务器上线的四个坑（2026-11）

本机测试一切正常，搬到 Linux 服务器才暴露的问题。都不涉及求解逻辑，但任何一个  
都能让服务"看着在跑、实际不出 key"。

**1. locale 不是 UTF-8 时，中文日志直接把请求打成异常**

systemd 拉起、Docker 里 `LANG` 没设、`locale` 为 C/POSIX 时，`sys.stdout.encoding`  
会退化成 `ANSI_X3.4-1968`。本项目日志全是中文，于是每次 `print` 都抛  
`UnicodeEncodeError` —— 表现为 `solve exception: UnicodeEncodeError`，看着像算法  
出错，实际只是编码。

```python
# auth_client.py 导入时自执行
stream.reconfigure(encoding='utf-8', errors='replace')
```

`errors='replace'` 是第二道保险：即便 reconfigure 被拒，也只丢字符不抛异常。

**2. 只认 `HTTPS_PROXY` 不够，服务器上还要能"不绕代理"**

机房机器常为 apt/yum 配了全局代理，直连本来更快却白绕一圈；反过来某些云主机只给  
`ALL_PROXY`（socks5），而 urllib3 的 `ProxyManager` 根本不支持 socks，硬用等于  
全部失败。现在按 `DELTA_NO_PROXY` > `DELTA_PROXY` > `NO_PROXY` > 标准变量 的  
优先级解析，并把结论打进启动日志和 `/healthz`。

更隐蔽的一处：`requests` 和 `urllib3` 是两套出网逻辑。只关一边会出现  
「metadata（requests）走代理、step（urllib3）走直连」的一半成功怪状，极难查。  
现在 `_resolve_proxy()` 统一给出 `trust_env` 结论，`link_generator` 也跟着走。

**3. 缓存文件在服务器上常常写不了，而且不报错**

原来写死在脚本目录。服务器上 `/opt/delta` 通常归 root、服务用低权限用户跑，  
第一次 `open` 就是 `PermissionError`，被 `except` 吞掉 —— 每次重启从零重解，  
缓存形同不存在，日志里一个字都不说。

改成 `DELTA_CACHE_FILE` > 脚本目录 > `/var/lib/delta` > `~/.cache` > `/tmp`  
依次探测可写性，落点写进启动日志；显式指定却不可写时也会回退并注明原因。

**4. 多进程会互相覆盖缓存，重启还会丢刚出的 key**

`save_cache_now()` 原来是"写自己内存里那份"。一旦用多进程（gunicorn /  
`uvicorn --workers N`），A 进程刚存的 key 会被 B 进程下次落盘直接抹掉。现在  
**读-合并-写 全程在 `flock` 独占锁内**，并用 `fsync` + `os.replace` 原子替换。

另外落盘是 2s 防抖的，`systemctl restart` 不会等——刚出 key 就重启的那几条会丢。  
补了 ASGI **lifespan** 退出钩子（`FastAPI 0.165` 已移除 `app.add_event_handler`，  
老写法包在 `try/except` 里等于静默没注册）+ `atexit` 兜底。

### 修复"卡死"：硬墙钟超时 + 代理失效 + 三处崩溃（2026-11）

症状：一条链接要跑 **88.4s**（meta 阶段独占 70.4s），网络抖一下进程直接挂死到被  
`SIGTERM`，偶尔还抛 `TypeError: cannot unpack non-iterable NoneType object`。

逐个挖出来的真因：

**1. `urllib3.PoolManager` 不读 `HTTP(S)_PROXY` 环境变量**

只有 `requests` / `curl` 会读。所以在必须靠代理出网的机器上，`PoolManager`  
会去**直连**上游，然后一个地址一个地址地超时 —— 实测每次精确卡 **8.00s**  
失败（`getaddrinfo` 返回 IPv4 + IPv6 两条，各 4s connect 超时），  
同一时刻 `curl` 走代理 2/3 次是成功的。

改用 `ProxyManager`（读同一个环境变量）后，同一批请求从 **0/10 成功**  
变成 **6/10 成功、延迟 0.4~2.1s**。

**2. `requests` / `urllib3` 的 timeout 不是墙钟**

`socket` 级超时只管 connect 和 read 各自的上限，read 在连接上有零星数据时  
会被不断重置；`urllib3 2.x` 的 `Timeout(total=)` 只用来算 connect/read 的上限，  
**不是总时长**。实测设 `connect=4 / read=10 / total=12`，单次请求能卡到 **90s+**  
不返回，连 `print` 都出不来。

新增 `call_with_deadline()`：daemon 线程 + `Event.wait` 做真正的墙钟中断，  
所有 HTTP 调用都包在里面。

**3. `with ThreadPoolExecutor` 会付双倍等待**

它退出时 `shutdown(wait=True)` 会阻塞到线程真正结束。于是  
`future.result(timeout=15)` 超时后还要再等那个线程 —— 一次超时付两倍时间，  
meta 阶段 70s 里一半是这么烧掉的。更糟的是 `concurrent.futures` 的 worker  
是 **non-daemon** 线程，且模块在 `atexit` 注册了 `_python_exit()` 去 join 它们，  
所以 `shutdown(wait=False)` 只是让主线程不阻塞，**进程退出照样要等**  
（实测逻辑 7.3s 跑完，进程耗到 120s 被 SIGTERM）。

换成自实现的 `_Task`（daemon 线程），超时后线程自生自灭，不拖进程。

**4. 三处同步调用没有超时保护**

`resolve_meta`（同步重问）、`do_step_with_retry`（整段 step）都是主线程直接调，  
上游把连接挂住就会连着主线程一起卡死。现在都在 `_Task` 里跑，并加总预算  
`SOLVE_BUDGET = 75s` + 动态收紧的 `STEP_TASK_DEADLINE`（剩余预算不足时自动缩）。

**5. `TypeError: cannot unpack non-iterable NoneType object`**

非首轮 metadata 超时且不重问时 `mres` 是 `None`，直接解包就崩。  
现在解包前兜成四元组。


效果对比（同一条链接、同一台机器）：

|       | 修复前           | 修复后                |
| ----- | ------------- | ------------------ |
| 正常求解  | 88.4s         | **7.9 ~ 26s**      |
| 上游卡死  | 无限挂，被 SIGTERM | **72.5s 内返回，立即退出** |
| 网络不可达 | 90s+ 无输出      | **35~41s，中文原因**    |
| 进程退出  | 逻辑跑完后仍挂 100s+ | 与逻辑耗时一致            |

顺带：失败原因统一走 `FAIL_HINTS` 讲人话（"上游网络不可达，请检查本地代理"  
而不是 `round-exhausted`），生成链接也加了退避重试。

### 修复 `outdated client` 导致全线解不出 key（2026-11）

症状：每条链接都卡在第一轮 step，返回  
`{"success": false, "message": "outdated client, please clear your browser cache"}`，  
3 轮跑完 `0/1 成功`，metadata 一切正常（service=3、checkpointCount=1），  
说明链接有效、票没过期，纯粹是版本号被上游拒了。

实测确认上游最低要求已到 **8.2.3**（8.2.2 及以下一律 outdated，8.2.3 放行），  
而代码里兜底值还停在 `5.4.0`，自动跟进又没跟上，于是全挂。

自动跟进失效有两个叠加原因，都已修掉：

1. **固定只扫前 256KB** —— 上游 bundle 已涨到 **1.93MB**，版本字符串被压进字符串表里，  
   位置随机。本次实测 `8.2.3` 在 **字节 1578904（81.8%）** 处，前 256KB 里一个候选都抠不到，  
   `_version_candidates` 直接返回空列表。
2. **全量兜底 GET 拉不完** —— 1.9MB 走 `timeout=8` 的整包下载在慢链路/代理上被中断  
   （实测 10s 掐断、0 字节），`_http_get` 返回 `None`，刷新静默失败，版本号原地不动。

现在的做法（`auth_client.py`）：

- `FALLBACK_VERSION` 提到 `8.2.3`，冷启动即用可用值，不再靠"抓得到才活"。
- **探针优先**：每小时先用假 ticket 打一发 step，确认当前版本还能用（0.5s，不碰真实会话、  
  不占求解名额）。能用就什么都不做 —— 服务器是 `>=` 判定，不确认就往上跳会白白漂移。
- **数值爬升兜底**：明确 outdated 后按 补丁→次版本→主版本 递增试探，第一个通过即用。  
  这条路完全不依赖解析，上游怎么混淆都拦不住。实测从 `5.4.0` 一次爬到 `9.0.0` 后  
  step 立即成功（高版本同样被接受，已用整链出 key 验证）。
- **流式扫 bundle**（第三道兜底，仅在爬升也失败时跑）：边下边找候选、命中即断开，  
  `_rank_candidates` 过滤 `127.0.0` / `01.0.05` 这类假阳性后按数值降序试。
- **三态探针**：`_version_works` 区分 `True`（可用）/ `False`（明确 outdated）/ `None`（网络抖动）。  
  抖动不再被当成"版本不行"，避免超时把版本号无意义地往上跳（改之前实测会跳到 `8.2.5`，  
  而真实值就是 `8.2.3`）。探针改走独立连接池（connect 6s），规避代理首包 4~6s 的握手延迟。
- **step 内就地自愈**：收到 `outdated client` 当场强制刷新一次并重发，把上游升级造成的  
  停摆窗口从"最长 1 小时（下一次定时刷新）"压到"一次请求"。

配套：`_version_probe.py` 可单独探测任意版本号是否被接受：

```bash
python _version_probe.py 8.2.2 8.2.3 9.9.9
```

### 修复"有效期被误判为无效链接"与"重复提交白跑 57s"（2026-11）

**单次 `invalid payload` 不能当死刑。** 实测同一张票：第一发 metadata 回  
`{"success":false,"message":"invalid payload."}`，三分钟后同一张票返回正常  
`Delta Android Keysystem / checkpointCount=1`，并顺利出 key。上游边缘节点与会话复制存在窗口，  
把一次 200 里的 `invalid payload` 当定论，会直接误杀有效链接（而且不重试、1.2s 就返回）——  
这是"有时候明明是新链接却解不了"的来源。

现在 `resolve_meta` 判定无效后会**复议一次**（间隔 0.35s 再查一遍），两次都无效才返回  
`无效链接`。只走失败路径、只多一次 ~0.4s 的 metadata，成功路径零开销。

**`a key for this identifier already exists.` 直接去读 key。** 同一张票二次提交时，  
上游 step 会返回这句话。以前它被当成 step 失败 → 换 service → 3 轮重试，  
实测白烧 **57s**（其中 25s 还是一个连接超时被重试了 4 次）才在最后一刻从 status 里捡到 key。  
现在识别到该消息立即轮询读取，同样的回放 ~5s 拿回原 key。

配套把 `do_step` 里**连接层异常**的重试次数从 3 降到 1：代理抖动时 connect 超时 3s，  
重 4 次能把一次失败放大到 25s，而外层本来就有 3 轮 + 换 service 的重试。

**metadata 等待 6s → 15s，超时不猜 service。** 日志里 `[meta] service=1` 和  
`[step] service=3: incorrect service (advertiser) provided.` 同时出现的根因：  
metadata 慢于 6s 时 `meta_future.result(timeout=6)` 抛超时，`current_service` 变 `None`，  
于是退回 `FALLBACK_SERVICES=[3]` 去打，被上游回 `incorrect service`，白烧一整轮 + 一次 5s 间隔。  
而 `with ThreadPoolExecutor` 退出时 `shutdown(wait=True)` 会等到线程结束——**结果其实已经拿到了，  
只是被 `result()` 的 timeout 丢掉了**。现在改成 15s 等待，仍超时就同步再问一次 metadata，  
绝不带着 `None` 去猜 service。

### 客户端版本号自动跟进上游

提交时携带的 `x-client-version` 必须不低于上游网页客户端要求，旧了会被直接拒绝（`outdated client`）。原先硬编码，上游一升级就要手动改代码重新部署，于是改成自动跟进。

当前实现见上一节：探针确认 → 数值爬升 → 流式扫 bundle 三级递进。其中爬升路径不依赖任何解析，是保证"上游怎么变都能自愈"的那一层；扫 bundle 只用来拿精确值。

> 早前版本是"抓脚本前 256KB 抠版本号"，上游 bundle 涨到 1.9MB 后版本串落到 81.8% 处，该策略已失效并导致全线 `outdated client`，已废弃。

同时 `do_step` 签名变更：`do_step(ticket, token, service)` → `do_step(ticket, service)`，`captcha` 字段按真实浏览器抓包形态发 `null`。

### 移除图形验证码

上游已取消图形验证码（step 的 `captcha` 字段不再校验，旧验证码服务已换 orbit 类型、识别器失效）。本版移除 `captcha_solver.py` 及 `numpy`/`scipy`/`Pillow`/`numba` 依赖，验证码环节直接跳过。`fake-useragent` 改为可选，缺失时退回固定 UA，不影响功能。

### 早前修复

- **HTTP 接口文件读取** — `extract_ticket` 不再读文件（旧版看到含 `/` `\` 或 `.txt` 结尾的输入就当路径 `open()`，`/delta` 可被拿去探测服务器文件）。读文件行为搬到 `extract_ticket_from_arg`，仅命令行参数使用。
- **过短 ticket 白烧 token** — `build_meta_stream` 长度不足返回 `None`，`do_step` 直接返回错误不发请求。新增常量 `MIN_TICKET_LEN = 33`。
- **过期缓存条目只在被查到时删除** — 改为落盘前按 TTL 清理，实测线上 1413 条中 808 条（57%）早已过期。
- **无效/过期链接快速拦截** — 形如 `?d=xxx…` 的链接直接返回 `error: 无效链接: invalid payload.`（约 1.2s）

当前单条求解平均约 6.8s，其中 5.0s 是上游强制的 checkpoint 间隔（不可压缩），非等待部分已接近物理下限。

---

## 快速开始

要求 Python 3.10+。

```bash
# 1. 安装依赖
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# 2. 求解一条链接（CLI）
./venv/bin/python main.py "https://auth.platorelay.com/a?d=<ticket>"

# 3. 部署 API 服务（服务 + 监控 + 自愈，一个进程）
./venv/bin/python delta.py --port 2233 --workers 16
```

---

## 使用方法（CLI）

### 直接求解已有 ticket

```bash
# 从 auth 链接
python main.py "https://auth.platorelay.com/a?d=<ticket>"

# 从原始 ticket 字符串
python main.py "<ticket>"

# 从文件读取（一行一个 ticket，批量）
python main.py tickets.txt
```

> 从文件读取只在命令行参数下生效（走 `extract_ticket_from_arg`）。HTTP 接口用的  
> `extract_ticket` 不读文件，路径原样当作 ticket 处理。

### 生成测试链接并求解

```bash
# 生成 5 条测试链接并求解
python main.py --generate 5

# 生成 3 条，静默模式
python main.py --generate 3 --quiet

# 只生成测试链接，不求解
python main.py --generate 5 --no-auto
```

### 命令行参数

| 参数                      | 说明                           |
| ----------------------- | ---------------------------- |
| `target`                | auth URL / ticket 字符串 / 文件路径 |
| `--generate N` / `-g N` | 通过 Platoboost API 生成 N 条链接   |
| `--quiet` / `-q`        | 静默模式，只输出结果                   |
| `--max-rounds N`        | 最大求解轮数（默认 3）                 |
| `--no-auto`             | 只生成链接，不求解                    |
| `--no-cache`            | 忽略本地 key 缓存，强制重新求解（排障用）      |

> 复跑同一条链接会直接命中本地缓存（`md5(ticket)` 索引），**不发任何网络请求**，  
> 整进程墙钟约 0.6s（基本全是 Python 启动）。要确认链路是否真的活着，加  
> `--no-cache`。

### 作为库使用

```python
from auth_client import extract_ticket, do_step, create_session

# 提取 ticket
ticket = extract_ticket("https://auth.platorelay.com/a?d=...")

# 创建会话
s = create_session()

# 推进 checkpoint（新版协议无需验证码 token）
result = do_step(ticket, service=3)
print(result)
```

---

## HTTP API

服务只有一个接口：`GET /delta`。

### `GET /delta?url=<auth_url>`

求解一条 auth 链接并返回 key。

```bash
curl "http://127.0.0.1:2233/delta?url=https://auth.platorelay.com/a?d=<ticket>"
```

```json
{
  "key": "FREE_xxxxxxxxxx……",
  "cached": false,
  "error": null,
  "made_by": "Hasl_Team",
  "qq_group": "277707901",
  "times": "8.2s"
}
```

| 字段       | 说明                               |
| -------- | -------------------------------- |
| `key`    | 求解成功的 key；失败为 `null`             |
| `cached` | `true` = 命中 24h 缓存，直接返回，未重新求解    |
| `error`  | 失败原因，成功为 `null`                  |
| `times`  | **真实求解耗时**，不是请求时间 缓存命中时返回当初求解的耗时 |

可能的 `error` 值：

| error                       | 含义                                                   |
| --------------------------- | ---------------------------------------------------- |
| `invalid url`               | URL 解析失败                                             |
| `invalid url (no ticket)`   | URL 中没有 `d=` 参数，或 ticket 短于 33 个字符（`MIN_TICKET_LEN`） |
| `无效链接: <上游原因>`              | ticket 无效或已过期，上游 metadata **两次**明确拒绝（已复议）。此类失败不重试    |
| `solve failed`              | 两次求解均未拿到 key（已含一次自动重试）                               |
| `solve exception: XxxError` | 求解过程抛出异常                                             |

---

## 行为说明

**Key 缓存（24 小时）**  
同一 ticket 求解成功后，key 写入 `.key_cache.json`。后续请求同一链接直接命中缓存秒回（`cached: true`），不重复消耗。TTL 设为 24h，与 key 自身的有效期一致；过期条目在读取时跳过、落盘时清除，不会返回已失效的 key，文件也不会持续膨胀。

**同链接并发合并**  
同一链接的并发请求只会触发**一次**真实求解，其余请求等待并共享同一结果。实测 500 并发请求同一未缓存 ticket → 真实求解 1 次，全部返回同一 key。因此不存在"重复请求"报错，失败后也可立即重试。

**限流规避**  
服务端对同一链路的相邻 checkpoint 提交有最小间隔要求（实测 ~5s，低于此值返回 `finishing checkpoints too fast`）。求解器按上次提交的真实时间差主动补齐间隔——间隔已足够则不等待，不足才精确补齐。

**无效链接拦截**  
第一轮 metadata 若明确返回 ticket 无效/已过期（`invalid payload` / `expired` / `not found` 等），**隔 0.35s 复议一次**，两次都无效才返回错误。单次 `invalid payload` 会误杀有效链接（实测同一张票第一次被拒、三分钟后正常出 key），所以不能一发定论。

**已出过 key 的链接**  
step 返回 `a key for this identifier already exists.` 时不再当失败重试，直接轮询读取该标识已有的 key。

**ticket 长度校验**  
ticket 短于 33 个字符时在进入求解流程前就返回错误，不发请求到上游。从 URL 里取出的 `d=` 参数为空或过短都走这条路。

---

## 求解流程

```
ticket → 获取 metadata → 推进 step → 解码回调 → 下一张 ticket → ... → 获取 key
```

关键行为：

- **metadata / status 并行** —— 问服务器情况的那段时间，顺便看看这链接是不是已经完成过了
- **step/poll 重叠** —— 最后一步的step发出后50ms即开始并发轮询key 压掉一个往返
- **轮数动态** —— 由 metadata 的 `checkpointCount` 决定，不硬编码。

---

## 部署

本机（Windows/macOS）只适合调试，正式跑要上 Linux 服务器。下面这套流程按  
「上传 → 自检 → 装服务」的顺序走，每一步都能单独定位问题。

### 服务器要求

| 项      | 要求                                                       |
| ------ | -------------------------------------------------------- |
| 系统     | 任意 Linux（Debian/Ubuntu/CentOS/Alma 均可），Windows 也能跑但只建议调试 |
| Python | **3.9+**（推荐 3.11），需要 `python3-venv`                      |
| 内存     | 512MB 起步；12 个求解线程约吃掉 200MB                               |
| CPU    | 1 核够用，瓶颈在网络往返不在算力                                        |
| 出网     | 必须能访问 `auth.platorelay.com` 与 `api.platoboost.net`（443）  |
| 端口     | 默认 2233，只在内网用的话建议只监听 `127.0.0.1`                         |

> 机房机器一般直连上游最快；如果服务器在受限网络里，才需要配代理。两种情况  
> 都**不要手改代码**，用下面的环境变量切换。

### 第一步：自检（强烈建议）

把整个目录传上去，装完依赖先跑一次：

```bash
cd /opt/delta
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python preflight.py
```

它会按顺序检查 17 项，每项给耗时和结论：

```
[1] 运行环境
  [+] Python 版本  3.11.9 (linux)  (0.00s)
  [+] 必需依赖  requests=2.32.3  urllib3=2.2.3  pycryptodome=3.21.0
  [+] 标准输出编码  stdout.encoding=utf-8
[2] 出网链路
  [+] 出网方式  direct  环境无代理变量，直连
  [+] DNS 解析  auth.platorelay.com->104.21.58.119(2个IP,2ms)
  [+] TCP+TLS 握手  直连  auth: 38/41ms  api: 45/47ms (min/中位)
  [+] 入口页 HTTP  HTTP 200, 845B (第 1 次尝试)
  [+] metadata API  返回正常（invalid payload.）
[3] 本地状态
  [+] 缓存文件  /var/lib/delta/keys.json (auto, 已有 0 条)
  [+] 缓存读写往返  写入 5.3ms, 磁盘读回 0.01ms
  [+] 客户端版本探测  8.2.3  (冷启动兜底 8.2.3)
[4] 端到端
  [+] 生成链接并解出 key  FREE_xxxxxxxx  耗时 6.9s
```

退出码：`0` 全通过、`1` 网络/上游不可达、`2` 依赖或环境不合格。最后那项端到端  
会**真的解一条链接**，所以它绿了基本就可以确认服务能跑。

### 第二步：装成 systemd 服务

```bash
sudo bash deploy/install.sh
```

脚本做的事（幂等，重复执行只更新代码并重启）：建 `delta` 系统用户 →  
同步代码到 `/opt/delta` → 建 venv 装依赖 → 跑一次 preflight → 写  
`/etc/delta/delta.env` → 装 `/etc/systemd/system/delta.service` → 启动。

之后常用命令：

```bash
systemctl status delta          # 状态
journalctl -u delta -f          # 实时日志
systemctl restart delta         # 改完 /etc/delta/delta.env 后重启
curl http://127.0.0.1:2233/healthz
```

`delta.service` 已经带上的几项关键设置：

| 配置                                                    | 作用                                                                                                    |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `StateDirectory=delta`                                | 自动创建 `/var/lib/delta` 并 chown，缓存有地方写                                                                  |
| `Environment=LANG=C.UTF-8` + `PYTHONIOENCODING=utf-8` | 中文日志不乱码、不抛 `UnicodeEncodeError`                                                                       |
| `TimeoutStopSec=60`                                   | 大于 `SOLVE_BUDGET`(26s)。uvicorn 收到 SIGTERM 会等在途求解跑完，设短了会被 SIGKILL —— 而 SIGKILL 不触发 `atexit`，刚出的 key 会丢 |
| `LimitNOFILE=65535`                                   | 高并发下连接数不撞默认 1024                                                                                      |
| `Restart=always` + `RestartSec=3`                     | 崩了自动拉起                                                                                                |
| `ProtectSystem=full` + `NoNewPrivileges`              | 只允许写 `/var/lib/delta`                                                                                 |

### 从 GitHub 一键部署（代码自己上传，服务器只负责拉）

如果代码是你自己推到 GitHub 的，不用再 `scp` 上传。两头各一条命令。

**本机：把当前目录推到你的仓库**（Windows Git Bash / Linux / macOS 通用）

```bash
# GitHub 上先建一个空仓库（不要勾 README / LICENSE，省掉首次合并）
bash deploy/push_github.sh https://github.com/<你的用户名>/<仓库>.git

# 私有仓库用 token，token 只在本次 push 的内存里，不会写进 .git/config
GITHUB_TOKEN=ghp_xxx bash deploy/push_github.sh https://github.com/u/r.git

# 先看会提交哪些文件，不真推
DRY_RUN=1 bash deploy/push_github.sh https://github.com/u/r.git
```

脚本会 `git init`、设 `origin`、提交并推送。key 缓存、自学习状态、重试队列、
账号池、`.env` 都已在 `.gitignore` 里，不会传上去。

**服务器：一条命令拉下来并装好**

```bash
curl -fsSL https://raw.githubusercontent.com/<你的用户名>/<仓库>/main/deploy/oneclick.sh \
  | sudo bash -s -- https://github.com/<你的用户名>/<仓库>.git

# 私有仓库（deploy key 或 token 二选一）
sudo GITHUB_TOKEN=ghp_xxx bash deploy/oneclick.sh https://github.com/u/r.git
# 想连自动更新一起装
sudo DELTA_AUTO_UPDATE=1 bash deploy/oneclick.sh https://github.com/u/r.git
```

它做：拉代码 → 调 `install.sh`（建用户 / venv / systemd / 启动）→ 记部署信息。
重复执行只会更新到远端 HEAD，**不会重置**缓存、学习状态、重试队列。

**以后更新**（三种随你挑）：

```bash
sudo delta-update                      # 装了自动更新才有；HEAD 没变就什么都不做
sudo bash /opt/delta-src/deploy/oneclick.sh   # 重新跑一键脚本
# 或者：在 GitHub Actions 配好 SERVER_HOST / SERVER_SSH_KEY，push 完服务器自己更新
```

自动更新定时器每 15 分钟检查一次，只有 HEAD 变了才重装重启，所以不会影响在途求解。
想立刻打开：

```bash
sudo DELTA_AUTO_UPDATE=1 bash /opt/delta-src/deploy/oneclick.sh
systemctl list-timers delta-update.timer
```

**推送即部署（点按钮也行）**：仓库 Settings → Secrets and variables → Actions 里加
`SERVER_HOST`（服务器地址）、`SERVER_SSH_KEY`（能登录的 SSH 私钥）、
`SERVER_USER`（默认 `root`）、`SERVER_PORT`（默认 22）。`GITHUB_TOKEN` 是 GitHub
自动提供的，不用手动加。

配置好之后有两种触发方式：

1. **点按钮首次部署 / 重新部署**：Actions 标签 → `Deploy to Server` → `Run workflow`。
   服务器若还没装过，工作流会通过 GitHub API 取 `oneclick.sh` 并完成首次安装；
   已装过则只做增量更新。不需要先在服务器上执行任何命令。
2. **push 即更新**：以后把代码 push 到 `main`/`master`，工作流自动 SSH 上去更新。

没配这些 Secret 时流程自动跳过，不会影响原来的打包 / Release。

服务器侧要求：SSH 端口对 GitHub Actions 出口 IP 开放；`SERVER_USER=root` 最省事
（非 root 需要给该用户配 `sudo -n` 免密执行 `env` / `bash`）；服务器能访问
`github.com` 和上游 API；对外端口 2233 放行。

部署完看两处：Actions 日志末尾的健康检查输出，以及
`curl http://<服务器IP>:2233/healthz`。

### 免费服务器：Render（不用甲骨文，不用信用卡）

甲骨文的注册验证太折腾的话，用 Render。它直接读 GitHub 仓库、有免费 Docker
Web Service，注册只要邮箱 + GitHub 账号，不绑卡。

**第 1 步：把代码推到您的 GitHub 仓库**（见上一节）。

**第 2 步：Render 部署**

1. 打开 `render.com`，用邮箱注册，绑定 GitHub。
2. 右上角 **New + → Blueprint** → 选您刚推的仓库。
3. Render 会读到根目录的 `render.yaml`，直接点 **Apply**。
4. 等构建完成，拿到地址 `https://delta-xxxx.onrender.com`。

`render.yaml` 已经配好：Docker 运行 `delta.py`、监听 Render 注入的 `PORT`、
3 个求解线程、健康检查走 `/healthz`、区域选新加坡。

**第 3 步：保活**

Render 免费层 15 分钟没有入站请求就会休眠，下一发请求要等约 1 分钟冷启动。
仓库里带了 `.github/workflows/keepalive.yml`，在 GitHub 仓库加一个 Secret
`RENDER_URL = https://delta-xxxx.onrender.com`，它会每 10 分钟打一次 `/healthz`。

更稳的是再用 `cron-job.org`（免费）建一个 1 分钟间隔的任务打同一个地址——
GitHub 的定时工作流在仓库 60 天没活动后会被自动暂停。

**免费层的边界**：没有持久盘，缓存 / 学习状态 / 重试队列放在 `/tmp`，重启会
重来；服务本身照常可用，自愈会重新学。内存 512MB，所以线程数设 3；量大了
升级到付费层再把 `DELTA_WORKERS` 调高。

**备选：Serv00**（要一个真实 SSH shell 时用）

`serv00.com` 邮箱注册即可，不绑卡，给 SSH + Python + 3 个端口 + 3GB 磁盘，
 FreeBSD 系统，能常驻跑 Python 进程。代价是每 90 天要登录一次面板保号、
 机器在波兰，国内延迟看运气。部署时把仓库 clone 上去，用 `venv` 装依赖，
 用面板的进程守护跑 `python delta.py --host 0.0.0.0 --port <面板给的端口>`。

### Docker（可选）

```bash
docker build -t delta .
docker run -d --name delta -p 2233:2233 \
  -v delta-data:/data \
  -e DELTA_NO_PROXY=1 \
  --restart unless-stopped delta
```

镜像自带 `HEALTHCHECK`（打 `/healthz`，不碰上游）。缓存落在 `/data/keys.json`，  
不挂卷的话容器重建就没了。

### 环境变量

出网方式、缓存位置、鉴权都靠环境变量控制，**不用改代码**：

| 变量                           | 默认      | 说明                                            |
| ---------------------------- | ------- | --------------------------------------------- |
| `DELTA_NO_PROXY`             | 未设      | 设为 `1` 强制直连。机房机器建议显式打开，避免被系统全局代理拖慢            |
| `DELTA_PROXY`                | 未设      | 强制走指定代理，如 `http://127.0.0.1:1080`，优先级高于系统变量   |
| `HTTPS_PROXY` / `HTTP_PROXY` | 未设      | 标准变量，urllib3 与 requests 都会用（已做统一对齐）           |
| `NO_PROXY`                   | 未设      | 标准语义，支持 `*`、`.example.com`、`host:port`        |
| `DELTA_CACHE_FILE`           | 自动探测    | 缓存路径。不可写时自动回退并打日志，不会静默丢缓存                     |
| `DELTA_CACHE_TTL`            | `86400` | 缓存有效期（秒）。key 24h 内会被消耗，默认与之对齐                 |
| `DELTA_TOKEN`                | 未设      | 设置后 `/delta` 需带 `X-Token` 头或 `?token=`，不设则不鉴权 |


**链接退路专用**（平时都不用管，上游加门禁时才配）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `DELTA_API_KEY` | 未设 | 上游要凭据时填。会自动同时带 `Authorization: Bearer` 与 `X-API-Key` |
| `DELTA_API_HOSTS` | 未设 | 备用 API 域名，逗号分隔，主域名失败后依次尝试 |
| `CAPSOLVER_KEY` | 未设 | CapSolver 打码密钥（Turnstile） |
| `TWOCAPTCHA_KEY` | 未设 | 2Captcha 打码密钥（Turnstile），与上面二选一 |
| `DELTA_TURNSTILE_SITEKEY` | 自动学 | 一般不用填。程序会从响应里自动学，填了以你为准 |
| `DELTA_LINK_INBOX` | 未设 | 外部链接收件箱文件，每行一条，取走即删（最后的兜底） |
| `DELTA_LINK_FEED` | 未设 | 外部链接 HTTP 源，返回文本行或 `{"urls":[...]}` |
| `DELTA_LINK_ORDER` | 内置顺序 | 自定义尝试顺序，如 `cookie,public` |
| `DELTA_LINK_RETRIES` | `3` | 单层重试次数（仅网络抖动与字段校验失败会重试） |
| `DELTA_LINK_STATE` | `.link_source_state.json` | 门禁状态与学习结果的落盘位置 |
| `DELTA_CAPTCHA_CMD` | 未设 | 求解侧验证码命令，最后一行 stdout 被当作 token |
| `DELTA_HEAL` | `1` | 设 `0` 不启动自愈后台线程（`/heal` 仍可手动触发） |
| `DELTA_HEAL_INTERVAL` | `180` | 自愈探测间隔（秒）。120~300 都合理，太小会增加对上游请求量 |
| `DELTA_HEAL_AUTO` | `1` | 设 `0` 只观测不自动修（比 `DELTA_HEAL=0` 粒度细：线程还在跑） |
| `DELTA_HEAL_COOLDOWN` | `60` | 同一类失败的最小修复间隔（秒），防重复打上游 |
| `DELTA_HEAL_HOURLY` | `30` | 每小时最多执行多少次修复动作 |
| `DELTA_ALERT_LOG` | 未设 | 状态变化日志（追加写，一行一条，制表符分隔） |
| `DELTA_ALERT_WEBHOOK` | 未设 | 状态变化时 POST 的 webhook 地址 |

三个容易踩的点：

1. **`ALL_PROXY=socks5://…` 不起作用**。urllib3 的 `ProxyManager` 不支持 socks，
   代码会识别出来并直接连，同时把这个结论写进启动日志和 `/healthz`。
2. **改了环境变量要重启进程**。`_resolve_proxy()` 只在导入时算一次 —— 故意不
   做热更新，免得运行中途出网方式突变，排查时更难判断。唯一的例外是自愈遇到
   `offline` 时会主动试两种出网方式（这一条是受控的，结果记在 state 的
   `egress_best` 里，`/healthz` 能看到它试出了什么）。
3. **`requests` 和 `urllib3` 是两套出网逻辑**。只改一半会出现「metadata 通、
   step 不通」这种一半成功的怪状，所以代码里统一用一个 `trust_env` 结论。

启动日志会把结论打出来，不用猜：

```
[server] 出网方式: 环境无代理变量，直连
[server] 缓存文件: /var/lib/delta/keys.json (auto, writable=True, 0 条)
[server] 求解线程: 12  监听: 0.0.0.0:2233  鉴权: 关
[server] 客户端版本: 8.2.3
```

### 健康检查

```bash
curl http://127.0.0.1:2233/healthz
```

```json
{"ok": true, "uptime": "3621s", "cache_entries": 128, "solving": 2,
 "client_version": "8.2.3",
 "egress": {"mode": "direct", "note": "环境无代理变量，直连"},
 "cache_file": "/var/lib/delta/keys.json", "cache_writable": true,
 "auth_required": false}
```

只读本地状态、不碰上游，所以永远秒回，可以直接给负载均衡/监控用。

### 鉴权

> 不设 `DELTA_TOKEN` 时 **`/delta` 没有任何认证**，任何能访问该端口的人都能
> 消耗你的求解能力。生产环境至少选一项：

**方式一：内置 token**

```bash
echo 'DELTA_TOKEN=换成你的长随机串' >> /etc/delta/delta.env
systemctl restart delta
# 调用
curl -G "http://127.0.0.1:2233/delta" -H "X-Token: 换成你的长随机串" \
     --data-urlencode "url=<链接>"
```

**方式二：Nginx Basic Auth + 限速**

```nginx
limit_req_zone $binary_remote_addr zone=delta:10m rate=10r/s;

location /delta {
    limit_req zone=delta burst=20 nodelay;
    auth_basic "delta";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:2233;
    proxy_read_timeout 120s;
}

location /healthz {
    allow 127.0.0.1;
    deny all;
}
```

**方式三：只监听本地**

```bash
python delta.py --host 127.0.0.1 --port 2233
```

### 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `0.0.0.0` | 监听地址。仅本机访问用 `127.0.0.1` |
| `--port` | `2233` | 端口 |
| `--workers` | `10` | **求解线程数，不是 uvicorn 进程数**。单次求解约 8s 且多为等待，按预期并发设；上游对同 IP 并发有限制，10~16 实测最稳，再高只会变成排队 + 限流退避 |

启动时自动将进程 fd 上限提到 65535（受 hard limit 约束）。systemd 部署建议同时设 `LimitNOFILE`。

> 单进程多线程即可，**不要**用 `uvicorn --workers 4 server:app` 起多进程：
> 每个进程各有一份内存缓存，虽然落盘已做文件锁合并（不会丢 key），但重复求解
> 是免不了的，等于白烧上游次数。

---

## 性能基线

8 核 Debian 实测：

| 项 | 数值 |
|---|---|
| 单条求解 | **~6.8s**（其中 5.0s 是上游强制的 checkpoint 间隔） |
| └ 非等待部分 | **~1.7s**（物理下限约 1.15s） |
| 首次（本机 Windows + 本地代理） | 1.9 ~ 7.0s（链路抖动决定） |
| **CLI 复跑（缓存命中）** | **0.00s**（整进程 0.66s，其中 0.6s 是解释器启动） |
| 无效链接拦截 | ~2s（含一次复议）|
| 缓存命中响应 | 4.4ms（单请求） |
| 缓存命中吞吐 | 3000 并发全部成功，峰值 ~900 req/s |
| 多票并发 | 3.5× 吞吐（限流 per-ticket，等待可重叠） |
| 稳定性 | 串行 10 次 + 同链接并发 4×6 轮，16/16 成功，无慢例 |

### 首次求解的时间都花在哪

| 阶段 | 冷启动 | 说明 |
|---|---|---|
| DNS + TCP + TLS 握手 | **3.5 ~ 4.0s** | 走代理还要多一次 CONNECT 隧道；单进程无法避免，只能靠缓存绕过 |
| metadata | 0.4s（热连接） | 版本协商 + 取 `service` / `checkpointCount` |
| step | 0.6 ~ 5.0s | 上游强制 checkpoint 间隔，多个 checkpoint 时线性叠加 |
| poll | 0.3s | key 落库后读回 |

所以"为什么第一次要十几秒"的答案基本只有一个：**握手是每次进程启动都要重付的固定
成本**。同一进程内解第 2、3 条链接时连接已热，单条只要 1~7s。复跑有缓存时连这一次
握手也省掉了。

非等待耗时受串行网络往返约束（keep-alive 复用后单次约 0.17s）。到上游的 TCP 握手仅 4ms、TCP+TLS 约 0.09s，网络本身很快，瓶颈在往返次数。

**经代理的机器**（本地代理 + 抖动链路）实测：

| 项 | 数值 |
|---|---|
| 正常求解 | 7.9s / 9.6s / 13.9s / 26.0s（四次独立新链接，全部成功） |
| 缓存/已出 key 复用 | 0.001s（进程内） |
| 上游完全不可达 | 35~41s 内返回明确原因，不挂死 |
| 上游把连接挂住（打桩） | 72.5s 内返回，进程立即退出 |
| 单次 API 调用 | 0.4~2.1s（修复代理前是固定 8.00s 全失败） |

上表的"上游卡死"是打桩强制 `sleep(300)` 的结果，用来验证超时链：三层超时（socket →
`call_with_deadline` → `_Task`）任一层生效即可，且总预算封顶 75s。

---

## 依赖

- Python 3.10+
- pycryptodome — AES-CTR
- requests, urllib3 — 网络请求
- fastapi, uvicorn — HTTP API
- fake-useragent — 可选，随机 UA 池（缺失退回固定 UA）
- uvloop, httptools, orjson — 可选，高并发加速（缺失自动降级）

---

## 文件

| 文件 | 作用 |
|------|------|
| `delta.py` | **单进程入口**：服务 + 监控 + 自愈一条命令跑完；`--status` `--heal-once` `--doctor` `--forget-fields` |
| `server.py` | HTTP API：缓存（多进程安全落盘）、同链接多请求合并、`/healthz` `/gate` `/heal` |
| `selfheal.py` | **自愈引擎**：后台 daemon 线程，探测 → 失败 → 学习 → 修复 → 复测；经验表 + 失败类型分派 + 出网方式自动切换 |
| `main.py` | 求解链路：step → 回调解码 → 轮询 key；CLI 入口，含缓存前置查询 |
| `key_cache.py` | **key 本地缓存**：CLI 与 server 共用，路径探测 + `flock` 读-合并-写 + TTL |
| `browser_solver.py` | **浏览器通道**（兜底）：真实浏览器过验证后自动抓 key，写同一份缓存 |
| `discord_pool.py` | **Discord 账号池**：每号每天 3 次配额记账 + 轮换 + profile 隔离 |
| `auth_client.py` | auth 服务客户端：AES-CTR、连接池、UA 池、出网方式解析（含运行期切换）、硬墙钟超时 |
| `link_generator.py` | 生成测试链接（主路已委托给 `link_source`） |
| `link_source.py` | **链接获取的多层退路**：public / cookie / mirror / captcha / browser / manual 六层链，字段名 / sitekey / 层级权重自学习 |
| `probe_gate.py` | 门禁诊断 CLI：查当前状态、`--watch` 常驻告警、`--find-sitekey` 挖 Turnstile（**服务里已内建，不必再单独跑**） |
| `selftest_heal.py` | **自愈自检**：12 个场景 35 项断言，伪造「上游加字段 / 加验证码 / 断网 / 限流」，验证学习闭环 |
| `selftest_gate.py` | **退路自检**：伪造 9 种门禁形态，18 项断言，不访问真实上游 |
| `preflight.py` | **服务器上线自检**：依赖 / 编码 / DNS / TLS / API / 缓存 / 版本 / 自愈 / 端到端，15 项逐条给结论 |
| `_version_probe.py` | 单测某个 `x-client-version` 是否被上游接受（排查 `outdated client` 用） |
| `deploy/install.sh` | Linux 一键安装：建用户 → venv → 自检 → systemd。支持 `DELTA_SRC_DIR` / `DELTA_APP_DIR` / `DELTA_PORT` 覆盖 |
| `deploy/oneclick.sh` | **服务器从 GitHub 一键部署**：克隆/拉取 → 调 install.sh → 记部署信息，支持私有仓库 token |
| `deploy/push_github.sh` | **本机一键上传**：git init → commit → push 到自己的 GitHub 仓库 |
| `deploy/update.sh` | 增量更新：HEAD 没变不重装不重启，变了才拉代码并重启 |
| `deploy/delta.service` / `delta-update.service` / `delta-update.timer` | 主服务单元 + 自动更新定时器（每 15 分钟） |
| `.github/workflows/deploy.yml` | push 后 SSH 到服务器自动更新（凭据走 Secrets，没配则跳过） |
| `.github/workflows/keepalive.yml` | 每 10 分钟打 `/healthz`，防止 Render 免费层休眠 |
| `render.yaml` | Render Blueprint：免卡、从 GitHub 一键部署 Docker 服务 |
| `Dockerfile` / `.dockerignore` | 容器镜像，`/data` 卷持久化缓存，自带 `/healthz` 健康检查 |
| `requirements.txt` | Python 依赖 |
| `.gitignore` | 忽略缓存、虚拟环境、`.key_cache.json` 等 |

运行时生成：`.key_cache.json`（key 缓存，可安全删除）、`.key_cache.json.lock`（多进程落盘锁）。

### 可调参数（`main.py` 顶部）

| 常量 | 默认 | 说明 |
|------|------|------|
| `MIN_STEP_GAP` | `5.0` | 相邻 step 最小间隔。**上游硬性要求，不可下调**（低于约 4.5s 必触发限流） |
| `POLL_MAX_ATTEMPTS` | `10` | `about:blank` 后轮询 key 的次数 |
| `POLL_INTERVAL` | `0.1` | 轮询间隔（秒），命中即返回 |
| `POLL_OVERLAP_DELAY` | `0.05` | step发出后多久开始并发轮询（step/poll重叠） |
| `STEP_THROTTLE_RETRIES` | `2` | 遇限流时的重试次数 |
| `MAX_ROUNDS_HARD_CAP` | `12` | 求解轮数上限，防止异常 metadata 导致无限循环 |
| `SOLVE_BUDGET` | `75.0` | **单条链接总墙钟上限（秒）**。到点无条件收手，这是"不会卡死"的最后一道闸 |
| `STEP_TASK_DEADLINE` | `35.0` | 一轮 step（含 gap 等待/多次重试）的墙钟上限，实际取 `min(它, 剩余预算)` |

`auth_client.py` 顶部：

| 常量 | 默认 | 说明 |
|------|------|------|
| `MIN_TICKET_LEN` | `33` | ticket 最短长度。切两组 AES 钥匙要用到 `ticket[17:33]`，**由上游协议决定，不可下调** |
| `FALLBACK_VERSION` | `8.2.3` | 冷启动用的 `x-client-version`。**不可低于上游当前最低要求**（2026-11 实测最低 8.2.3），高了无害——服务器只做 `>=` 判定 |
| `HTTP_DEADLINE` | `15.0` | 单次 API 调用的墙钟上限（外层兜底，内层 socket 超时不可信） |
| `STEP_TIMEOUT` | `connect=6 / read=12 / total=15` | urllib3 socket 级超时。**`total` 不是墙钟上限**，真正的墙钟在 `call_with_deadline` |
| `_LEAK_MAX` | `64` | 允许同时挂着的请求数上限，防极端情况下 daemon 线程无限堆积 |
| `_PROXY` | 读环境变量 | 有 `HTTPS_PROXY`/`HTTP_PROXY` 就用 `ProxyManager`。**`PoolManager` 不读这些变量**，删了这段在需要代理的机器上会全线超时 |

---

## 故障排查

| 现象 | 排查方向 |
|------|----------|
| 突然全部失败，提示 `所有链接通道都失败` | 上游加门禁了。跑 `python probe_gate.py` 看 `状态` 那一行，它直接告诉你是 `cf-challenge` / `auth-required` / `captcha-required` 中的哪一种，并给出该配哪个变量。**不要**先去改代码 |
| `gate` 一直显示 `schema` | 上游加了必填字段。正常情况下自愈会自己学会并重试成功；若持续失败，跑 `python delta.py --status` 看"已学字段名"，再 `python delta.py --forget-fields` 清掉学错的重新学 |
| 提示 `拿不到 Turnstile sitekey` | 上游上了 Turnstile 但 sitekey 还没学到。自愈会自己去挖；手动跑 `python probe_gate.py --find-sitekey`；仍找不到就用浏览器打开页面看 iframe 的 `?k=` 参数，填进 `DELTA_TURNSTILE_SITEKEY` |
| 想在上游改动前就知道，而不是被用户告知 | 什么都不用做。服务进程内建自愈线程，状态一变就写 `DELTA_ALERT_LOG` / 发 `DELTA_ALERT_WEBHOOK`；也可随时 `curl -X POST http://127.0.0.1:2233/heal` 主动查一次 |
| 自愈到底学过什么、有没有生效 | `python delta.py --status`：门禁状态、学到的字段名/sitekey、各层成功率、经验表（失败类型→动作→结果）全在里面 |
| 怀疑退路本身有问题 | `python selftest_gate.py`（18 项）+ `python selftest_heal.py`（35 项）。都是打桩，不碰真实上游 |
| 自愈把出网方式改坏了 | `python delta.py --status` 看"自愈最优出网"，`/healthz` 的 `heal.egress_best` 也有。想锁死就在 `delta.env` 里显式设 `DELTA_NO_PROXY=1` 或 `DELTA_PROXY=…`，重启后以环境变量为准 |
| 同一条链接复跑还是 6~16s，没走缓存 | 先看有没有打印 `[cache] 命中本地缓存`。没有说明缓存没落盘：跑 `python preflight.py` 看"缓存读写往返"这一项；或直接看 `.key_cache.json` 里有没有 `md5(ticket)` 这一条。用 `--no-cache` 会主动跳过缓存，注意别加 |
| 服务器上每条都 `solve exception: UnicodeEncodeError` | locale 不是 UTF-8，中文日志编码失败。先跑 `python -c "import sys;print(sys.stdout.encoding)"`；代码已自动 reconfigure，若仍报错检查是否用了别的输出通道（如自定义 logging handler） |
| 服务器上能上网但程序连不上 | 先看启动日志 `[server] 出网方式`。直连不通就配 `HTTPS_PROXY`；有全局代理但想直连就 `DELTA_NO_PROXY=1`；只给了 `ALL_PROXY`（socks）则 urllib3 不支持，需换成 http 代理 |
| 重启后缓存"消失"，同样的链接又跑一遍 | 看启动日志 `[server] 缓存文件 ... writable=`。`False` 说明运行用户没写权限，用 `DELTA_CACHE_FILE` 指到可写位置（systemd 单元里默认已是 `/var/lib/delta/keys.json`） |
| `systemctl status` 显示 `PermissionError: '/opt/delta/.key_cache.json'` | 同上。或直接 `chown -R delta:delta /opt/delta` |
| 出过 key 的链接，重启后再查又没了 | 若用多进程启动，确认走的是本项目的 `server.py`（单进程多线程）；落盘已做 flock 合并，但 `kill -9` 不触发退出钩子，最多丢 2s 内的写入 |
| `/delta` 返回 `{"error":"unauthorized"}` | 设了 `DELTA_TOKEN`。调用时带 `-H "X-Token: ..."` 或 `?token=...` |
| 装完 systemd 起不来 | `journalctl -u delta -n 50 --no-pager`。多数是 venv 路径不对（`ExecStart` 写死 `/opt/delta/venv/bin/python`），重新跑 `deploy/install.sh` 即可 |
| 服务器 `git clone` 报 403 / 仓库不存在 | 私有仓库要么在服务器配 deploy key（`ssh -T git@github.com` 验证），要么传 `GITHUB_TOKEN`。token 不要写进仓库文件 |
| 更新报 `unrelated histories` | 已修：浅克隆现在直接对齐远端 HEAD。若旧版本脚本还留在服务器上，重新跑一次 `oneclick.sh` 更新脚本自身 |
| push 了但服务器没反应 | 看 Actions 里 Deploy to Server 是否 skipped（ Secrets 没配）；配了就 SSH 上去 `journalctl -u delta-update -n 50` |
| 定时更新把在途请求打断了 | `sudo systemctl disable --now delta-update.timer`，改成手动 `sudo delta-update`（服务本身有 60s 优雅停止，一般不该出现） |
| 大量 `[meta] 网络异常: Connection ... timed out`，最后报"上游网络不可达" | 先 `curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 20 https://auth.platorelay.com/a` 确认链路。若 curl 也挂，是代理/网络问题；若 curl 好而程序不好，检查 `auth_client._PROXY` 是否为空（`PoolManager` 不读 `HTTPS_PROXY`） |
| 每条都跑满 40s 左右才失败 | 属正常：3 轮 metadata 重试 + 退避。链路恢复后会自动变回 8~15s |
| 跑到 26s 被截断，报"超过单条链接时长上限" | 上游响应过慢。现在预算默认 26s（原来 75s），超时的票会自动进后台重试队列继续解，用户下次查询直接命中缓存。要改设 `DELTA_SOLVE_BUDGET` |
| 日志里出现 `[!] step 超过 Ns 未返回` | step 整段超时（`STEP_TASK_DEADLINE`）。先确认 step 接口本身是否正常，别急着加长超时 |
| 进程跑完结果但不退出 | 说明有非 daemon 线程在跑。检查是否又引入了 `ThreadPoolExecutor` —— 它的 worker 是 non-daemon，且 `concurrent.futures` 在 atexit 里 join，`shutdown(wait=False)` 也救不了 |
| 全部链接 `outdated client, please clear your browser cache` | 上游升版本了。正常情况 step 收到该错误会当场自愈并重发；若仍失败，用 `python _version_probe.py 8.2.3 9.0.0` 确认服务器现在的最低版本，再把 `FALLBACK_VERSION` 调高 |
| 同一张票二次提交，`a key for this identifier already exists.` | 正常现象，该标识已出过 key。现在会直接读回原 key（~5s），不再当失败重试 |
| 新链接偶尔被判 `无效链接: invalid payload.` | 已加重试复议；若仍复现，观察 `[meta] 第一次判定无效，复议通过` 日志，说明上游边缘节点存在会话复制窗口 |
| `无效链接: invalid payload.` | ticket 无效或已过期，换一条新链接。这是上游明确拒绝，不是本地 bug |
| `invalid url (no ticket)` | URL 里没有 `d=`，或 ticket 不足 33 个字符 |
| CLI 结尾报 `未获取到 key（...）` | 括号里已是人话原因（`FAIL_HINTS`）：上游网络不可达 / 链接无效 / step 全部失败 / 超时未下发 key 等，按提示处理即可 |
| 偶发耗时 12s 左右 | 若已部署仍出现此情况，检查 `auth_client.py` 的 `do_step` 重试次数；也可能是上游限流退避叠加 |
| 大量 `finishing checkpoints too fast` | 步间隔由 `fastlane.GapGovernor` 自适应（起点 5s，范围 2~6s，连续顺利会缓慢下探）。可用 `DELTA_STEP_GAP` 固定，`DELTA_STEP_GAP_MIN/MAX` 改范围 |
| 高并发下连接失败 | fd 上限不足，确认 systemd `LimitNOFILE=65535` |
| 返回已失效的 key | 确认 `.key_cache.json` 的 TTL 逻辑生效；必要时删除该文件重建 |

CLI 默认 verbose，排查时直接用 `main.py` 跑单条链接观察每轮日志。

---

## 全动作自愈 + 提速 + 失败重试（单进程版）

三条命令解决三件事，仍然是**一个进程**：

```bash
python delta.py                 # 服务 + 监控 + 自愈 + 重试队列，全在里面
python delta.py --bench 5       # 生成 5 条新链接，端到端计时（p50/p95 + 阶段明细）
python delta.py --status        # 看门禁/学到的字段/步间隔/队列，不常驻
python delta.py --doctor        # 17 项上线自检（含真实解卡）
python delta.py --queue         # 只看失败重试队列
python delta.py --flush-queue   # 清空重试队列
```

### 1. 什么都可以动：`DELTA_HEAL_MODE=full`（默认）

原来的自愈每种失败只映射一两个动作，修不动就等着。现在 full 模式把所有牌都摊开：

| 失败类型 | 动作序列 |
|---|---|
| schema（上游要新字段） | 学字段名 → 候选字段空串 → 重建连接池 → 换 UA → 换域名 → 重放 |
| cf-challenge | 刷 cookie → 换出口 → 换域名 → 重建连接池 → 换 UA → 重放 |
| auth-required | 查凭据 → cookie → 外部供应 → 换 UA → 换域名 → 重放 |
| captcha-required | 打码出卡 → 挖 sitekey → 重建连接池 → 换 UA → 重放 |
| offline | 直连/代理互切 → mirror → 重建连接池 → 换 UA → 重放 |
| rate-limited | 拉长轮询 + 3s 短退避复测 → 其余动作兜底 |

冷却从 60s 降到 8s（可用 `DELTA_HEAL_COOLDOWN` 调），每小时上限从 30 提到 240。
要回到保守模式：`DELTA_HEAL_MODE=standard` 或 `python delta.py --mode standard`。

自愈不再只看"能不能拿到链接"。记录过一次成功票据后，它还会探 **metadata 求解链路**
（`probe_solve`）——上游单独动求解接口时，链接通道显示 open 也能被发现并修。

### 2. 缩短等待：fastlane.py

- **步间隔自适应**：不再写死 5s，但也不会盲目压到 1s（实测低于 4.5s 会吃
  `finishing checkpoints too fast`，重试两次总耗时反而更长）。起点 5s，连续顺利
  小步下探到下限，被限流就抬回去，学到的值落盘跨重启保留。
- **幂等请求对冲**：metadata / status 是 GET，迟发 0.9s 的第二发，取先回来的。
  尾延迟从 7s 级压到 1s 级。**step 是推进状态机的，绝不并发重发。**
- **TLS 预热**：启动后台把 auth / api 域名的连接先建好，首解不再现付握手钱。
- **单条预算 26s**（原来 75s）：该失败的早点放弃，剩下的交给后台队列。
  可选 `wait` 参数让愿意等的客户端接住后台结果。
- **竞争等待**：首轮 status 与 metadata 谁先出结论用谁，不再先死等 status 6s。

实测（本机经代理，非服务器直连）：新链接 6.7~8.9s/条，缓存命中 0.000s。

### 3. 百分之百出卡：retryq.py

失败的票不再被丢弃。`/delta` 失败时自动入队，返回：

```json
{
  "key": null, "error": "network-error", "queued": true,
  "job": "cb205fab...", "retry_in": 5.0, "tries": 2
}
```

后台线程按 5/10/20/40/80/160/300s 指数退避重解，成功即写进 key 缓存，
用户下次用同一条链接来问就是毫秒命中。也可以通过 `?wait=30` 让它在这条请求里
多等 30 秒直接接结果（默认 0，不额外占用用户时间）。

| 查询 | 作用 |
|---|---|
| `GET /queue` | 队列全貌：待解条数、在跑、下一次重试时间、失败原因 |
| `GET /job/{id}` | 单条票的重试进度 |
| `GET /stats` | 成功率、平均/p50/p90/p95、提速参数、自愈快照 |
| `GET /healthz` | 新增 `retryq` 和 `fast` 两段 |
| `POST /heal` | 手动自愈（后台线程同时段只会跑一轮，不会探测翻倍） |

队列文件 `.delta_retryq.json` 用 flock + 原子替换落盘，服务重启不丢票；
删除用 tombstone 传播，已解出的票不会在下次重启时"复活"。
永久无效的票（invalid/expired）会立刻丢弃，最多保留 6 小时（`DELTA_RETRY_MAX_AGE`）。

### 新增环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DELTA_HEAL_MODE` | `full` | `full`=全动作；`standard`=保守冷却 |
| `DELTA_SOLVE_BUDGET` | `26` | 单条链接墙钟预算（秒） |
| `DELTA_STEP_GAP` | `5` | 固定步间隔，覆盖自适应 |
| `DELTA_STEP_GAP_MIN/MAX` | `2/6` | 自适应步间隔范围 |
| `DELTA_HEDGE` | 开 | 设 `0` 关闭幂等请求对冲 |
| `DELTA_PREWARM` | 开 | 设 `0` 关闭启动预热 |
| `DELTA_RETRY_BASE/MAX_DELAY` | `5/300` | 重试退避起止（秒） |
| `DELTA_RETRY_MAX_AGE` | `21600` | 票在队列里最多保留多久 |
| `DELTA_RETRY_CONCURRENCY` | `1` | 后台重解并发（默认不抢在线配额） |
| `DELTA_RETRYQ_FILE` | 脚本目录 | 重试队列落盘位置 |
| `DELTA_AUTH_HOSTS` | 无 | 备用求解域名，逗号分隔（自愈可切） |
| `DELTA_HEAL_PREWARM` | 关 | 自愈动作里是否重播连接预热 |

---

## License

MIT