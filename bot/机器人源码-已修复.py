import os
os.environ['TZ'] = 'Asia/Shanghai'
import time
try:
    time.tzset()
except Exception:
    pass

import botpy, re, asyncio, requests, random, io, json, base64, subprocess, tempfile, uuid
from botpy.message import GroupMessage
from botpy.http import Route as _QQRoute
from PIL import Image
import urllib3

try:
    from botpy.types.message import MarkdownPayload, Media
except Exception:
    try:
        from botpy.types import MarkdownPayload, Media
    except Exception:
        from botpy.types.message import MarkdownPayload
        Media = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENGINE_URL = "https://yuki-delta.antideploy.app"
IMG_URL = "https://uapis.cn/api/v1/random/image?category=acg&type=pc"
NCM_API_URL = "https://zm.wwoyun.cn"
MAX_AUDIO_BYTES = 8 * 1024 * 1024

HELP_MENU = """**Yuki 机器人使用菜单**

**一、解卡功能**
@发送解卡链接即可自动解卡
支持链接：`https://auth.platorelay.com/a?d=xxxxx`
**二、生成未解链接**
发送指令 `/生成安卓链接`
发送指令 `/生成苹果链接`
**三、点歌功能**
发送以下任意指令即可点歌
`点歌 歌名`
`听歌 歌名`
`wyy 歌名`

更多功能持续更新中"""

def _extract_file_info(upload_res):
    if isinstance(upload_res, dict):
        fi = upload_res.get("file_info")
        if fi: return fi
        fi = (upload_res.get("data") or {}).get("file_info")
        if fi: return fi
    if isinstance(upload_res, str):
        m = re.search(r'file_info=([^&]+)', upload_res)
        if m: return m.group(1)
    return None

async def _patched_post_group_file(
    self, group_openid, file_type, url=None,
    srv_send_msg=False, file_data=None, file_name=None
):
    payload = {"file_type": file_type, "srv_send_msg": srv_send_msg}
    if url: payload["url"] = url
    if file_data: payload["file_data"] = file_data
    if file_name: payload["file_name"] = file_name
    route = _QQRoute("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
    res = await self._http.request(route, json=payload)
    if isinstance(res, str):
        try: res = json.loads(res)
        except Exception: pass
    return res

try:
    botpy.api.BotAPI.post_group_file = _patched_post_group_file
except Exception as e:
    print(f"[patch] failed: {e}")

def get_scaled_size(url):
    return 400, 225, url

def check_link_format(link):
    m = re.search(r'[?&]d=([A-Za-z0-9_\-]+)', link)
    if not m:
        return False, "链接格式错误，缺少 d 参数，请检查链接是否完整。"
    ticket = m.group(1)
    if len(ticket) < 33:
        return False, "链接已损坏或过期，ticket 长度不足，请重新获取链接。"
    return True, ticket

def search_music(keyword):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com/"
    }
    try:
        search_res = requests.get(
            f"{NCM_API_URL}/search",
            params={"keywords": keyword, "limit": 1},
            headers=headers,
            timeout=10,
            verify=False,
        )
        if search_res.status_code != 200:
            return None
        search_data = search_res.json() or {}
        songs = (search_data.get("result") or {}).get("songs") or []
        if not songs:
            return None
        song = songs[0]
        song_id = song["id"]
        song_name = song.get("name", keyword)
        artists = song.get("artists") or song.get("ar") or []
        artist = artists[0].get("name", "未知歌手") if artists else "未知歌手"

        url_res = requests.get(
            f"{NCM_API_URL}/song/url/v1",
            params={"id": song_id, "level": "standard"},
            headers=headers,
            timeout=10,
            verify=False,
        )
        url_data = url_res.json() or {}
        audio_url = ""
        data_list = url_data.get("data") or []
        if data_list and data_list[0].get("url"):
            audio_url = data_list[0]["url"]

        return {
            "id": song_id,
            "name": song_name,
            "artist": artist,
            "audio_url": audio_url,
        }
    except Exception:
        return None

async def safe_reply(message, msg_seq, content="", msg_type=0, markdown=None, media=None):
    last_err = None
    for attempt in range(3):
        try:
            kwargs = {"content": content, "msg_seq": msg_seq * 10 + attempt}
            if msg_type:
                kwargs["msg_type"] = msg_type
            if markdown is not None:
                kwargs["markdown"] = markdown
            if media is not None:
                kwargs["media"] = media
            return await message.reply(**kwargs)
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.3)
    print(f"[safe_reply] failed: {last_err}")
    return None

async def send_music_card(message, song_info, seq):
    try:
        cover_url = f"{IMG_URL}&t={int(time.time() * 1000) + random.randint(1, 99999)}"
        md = (
            f"**{song_info['name']}**\n\n"
            f"歌手：{song_info['artist']}\n\n"
            f"[网易云音乐](https://music.163.com/song?id={song_info['id']})\n\n"
            f"![img #400px #225px]({cover_url})"
        )
        markdown = MarkdownPayload(content=md)
        await safe_reply(message, seq, content=" ", msg_type=2, markdown=markdown)
        return True
    except Exception as e:
        print(f"[send_music_card] {e}")
        return False

def _prepare_audio_data(audio_url):
    try:
        dl = requests.get(
            audio_url, timeout=60, verify=False,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if dl.status_code != 200 or not dl.content:
            return None, "download_failed"

        audio_data = dl.content
        if len(audio_data) > MAX_AUDIO_BYTES:
            return None, "too_large"

        return base64.b64encode(audio_data).decode(), None
    except Exception:
        return None, "error"

async def send_audio_file(message, audio_url, seq, file_name="music.mp3"):
    file_b64, err = await asyncio.to_thread(_prepare_audio_data, audio_url)

    if err == "too_large":
        return "too_large"
    if not file_b64:
        return False

    try:
        upload_res = await message._api.post_group_file(
            group_openid=message.group_openid,
            file_type=4,
            file_data=file_b64,
            file_name=file_name,
            srv_send_msg=False,
        )

        file_info = _extract_file_info(upload_res)
        if not file_info:
            return False

        media = Media(file_info=file_info, file_type=4)
        r = await safe_reply(message, seq, content=" ", msg_type=7, media=media)
        return r is not None
    except Exception:
        return False

def request_engine_once(link):
    """
    调用解卡引擎（单次请求，机器人端零重试）。端点必须是 /delta。

    重试全部交给服务端：run_solves 首发+次发、solve_chain 3 轮 checkpoint、
    后台 retryq 指数退避，wait=110 让服务端在这一个请求里长等队列把 key 接走
    （服务端上限 120s）。机器人只发一次，不再套重试。
    """
    try:
        res = requests.get(
            f"{ENGINE_URL}/delta",
            params={"url": link, "wait": 110},
            timeout=(15, 180),
            verify=False,
        )
        # /delta 无论成功失败都返回 JSON；非 2xx 也尽量解析出 error
        try:
            data = res.json()
        except Exception:
            return {"key": None, "error": f"状态码{res.status_code}: {res.text[:50]}"}
        # 服务端字段名是 times（如 "0.204s"），兼容成秒数浮点，供界面显示耗时
        raw_times = data.get("times") or data.get("elapsed") or 0
        try:
            if isinstance(raw_times, str):
                elapsed = float(raw_times.rstrip("s").strip() or 0)
            else:
                elapsed = float(raw_times)
        except Exception:
            elapsed = 0.0
        data["elapsed"] = elapsed
        return data
    except requests.exceptions.Timeout:
        return {"key": None, "error": "连接引擎超时", "elapsed": 0.0}
    except requests.exceptions.ConnectionError:
        return {"key": None, "error": "无法连接引擎，请检查域名是否正确", "elapsed": 0.0}
    except Exception as e:
        return {"key": None, "error": f"请求异常: {str(e)[:50]}", "elapsed": 0.0}

def generate_android_link():
    hwid = os.urandom(16).hex()
    api_url = "https://api.platorelay.com/public/start"
    payload = {"service": 6, "identifier": hwid}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Content-Type": "application/json",
        "Origin": "https://auth.platorelay.com",
        "Referer": "https://auth.platorelay.com/"
    }
    try:
        res = requests.post(api_url, json=payload, headers=headers, timeout=10, verify=False)
        data = res.json()
        if isinstance(data, dict):
            if "url" in data: return data["url"]
            if "link" in data: return data["link"]
            if "redirect" in data: return data["redirect"]
            if "token" in data: return f"https://auth.platorelay.com/a?d={data['token']}"
            if "data" in data and isinstance(data["data"], dict):
                if "url" in data["data"]: return data["data"]["url"]
                if "link" in data["data"]: return data["data"]["link"]
                if "token" in data["data"]: return f"https://auth.platorelay.com/a?d={data['data']['token']}"
        return f"生成异常，返回: {json.dumps(data)[:100]}"
    except Exception as e:
        return f"请求失败: {str(e)}"

def generate_ios_link():
    hwid = str(uuid.uuid4())
    api_url = "https://api.platorelay.com/public/start"
    payload = {"service": 5, "identifier": hwid}
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Content-Type": "application/json",
        "Origin": "https://auth.platorelay.com",
        "Referer": "https://auth.platorelay.com/"
    }
    try:
        res = requests.post(api_url, json=payload, headers=headers, timeout=10, verify=False)
        data = res.json()
        if isinstance(data, dict):
            if "url" in data: return data["url"]
            if "link" in data: return data["link"]
            if "redirect" in data: return data["redirect"]
            if "token" in data: return f"https://auth.platorelay.com/a?d={data['token']}"
            if "data" in data and isinstance(data["data"], dict):
                if "url" in data["data"]: return data["data"]["url"]
                if "link" in data["data"]: return data["data"]["link"]
                if "token" in data["data"]: return f"https://auth.platorelay.com/a?d={data['data']['token']}"
        return f"生成异常，返回: {json.dumps(data)[:100]}"
    except Exception as e:
        return f"请求失败: {str(e)}"

class MyClient(botpy.Client):

    async def _send_md(self, message, seq, md_text):
        markdown = MarkdownPayload(content=md_text)
        return await safe_reply(message, seq, content=" ", msg_type=2, markdown=markdown)

    async def on_group_at_message_create(self, message: GroupMessage):
        raw_text = (message.content or "").strip()

        if re.search(r'(?:^|\s|/)(?:help|菜单|命令)(?:\s|$)', raw_text):
            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = HELP_MENU + f"\n\n![img #{target_w}px #{target_h}px]({img_url})"
            await self._send_md(message, 1, md_content)
            return

        song_match = re.search(r'(?:^|\s|/)(?:点歌|听歌|wyy)\s+(.+)', raw_text)
        if song_match:
            keyword = song_match.group(1).strip()
            if not keyword:
                return
            music_info = await asyncio.to_thread(search_music, keyword)
            if not music_info:
                await safe_reply(message, 1, content=f"没搜到《{keyword}》，可能是接口抽风或版权受限，换个歌名试试。")
                return
            if not music_info.get("audio_url"):
                await send_music_card(message, music_info, seq=1)
                await safe_reply(message, 4, content="该歌曲无版权或无法获取音频，请点击上方链接去网易云收听。")
                return

            await send_music_card(message, music_info, seq=1)

            safe_title = re.sub(r'[\\/:*?"<>|]', '_', music_info['name'])
            safe_artist = re.sub(r'[\\/:*?"<>|]', '_', music_info['artist'])
            file_name = f"{safe_title} - {safe_artist}.mp3"

            send_result = await send_audio_file(message, music_info["audio_url"], seq=3, file_name=file_name)
            if send_result == "too_large":
                await safe_reply(message, 4, content="这首歌文件过大，无法直接发送，请点击上方链接去网易云收听。")
            elif send_result is False:
                await safe_reply(message, 4, content="音频发送失败，请点击上方链接去网易云收听。")
            return

        if "/生成安卓链接" in raw_text:
            await safe_reply(message, 1, content="正在生成链接，请等待")
            generated_link = await asyncio.to_thread(generate_android_link)

            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = (
                f"**安卓链接生成成功**\n\n"
                f"```text\n{generated_link}\n```\n"
                f"![img #{target_w}px #{target_h}px]({img_url})\n\n"
                "by YUKI"
            )
            await self._send_md(message, 2, md_content)
            return

        if "/生成苹果链接" in raw_text:
            await safe_reply(message, 1, content="正在生成链接，请等待")
            generated_link = await asyncio.to_thread(generate_ios_link)

            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = (
                f"**苹果链接生成成功**\n\n"
                f"```text\n{generated_link}\n```\n"
                f"![img #{target_w}px #{target_h}px]({img_url})\n\n"
                "by YUKI"
            )
            await self._send_md(message, 2, md_content)
            return

        if "lootlabs" in raw_text or "loot-link" in raw_text:
            await safe_reply(message, 1, content="抱歉，目前暂不支持 Lootlabs 链接。")
            return

        url_match = re.search(r'https?://auth\.platorelay\.com/a\?d=[A-Za-z0-9_\-&%=\.]+', raw_text)
        if not url_match:
            return
        link = url_match.group(0)

        ok_format, format_result = check_link_format(link)
        if not ok_format:
            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = (
                f"**链接无效**\n{format_result}\n\n"
                f"![img #{target_w}px #{target_h}px]({img_url})\n\nbye YUKI"
            )
            await self._send_md(message, 1, md_content)
            return

        sender_openid = ""
        try:
            sender_openid = message.author.member_openid
        except Exception:
            pass
        mention = f"<@{sender_openid}>" if sender_openid else ""

        welcome_msg = (
            f"{mention} 欢迎使用Yuki的解卡机器人\n"
            "正在为您解卡.....\n"
            "限时优惠:一天0.99\\一个周3.99\\一个月6.99\n"
            "可绕过DC验证的解卡机器人\n"
            "获取:3661221335\n"
            "获取API:3562501187.3661221335\n"
            "可发送help查看菜单"
        )
        await self._send_md(message, 1, welcome_msg)

        try:
            data = {}
            elapsed = 0.0
            error = None
            key = None

            # 【无机器人端重试】只请求一次，wait=110 让服务端内部把
            # 首发/次发/后台队列重试全部跑完再返回。
            data = await asyncio.to_thread(request_engine_once, link)
            key = data.get("key")
            error = str(data.get("error") or "")
            elapsed = data.get("elapsed", 0.0)

            if key:
                img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
                target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
                md_content = (
                    f"{mention}\n"
                    f"**解卡成功**\n"
                    f"```key\n{key.strip()}\n```\n"
                    f"解卡耗时: {elapsed:.2f}s\n\n"
                    f"![img #{target_w}px #{target_h}px]({img_url})\n\n"
                    "限时优惠:一天0.99\\一个周3.99\\一个月6.99\n"
                    "可绕过DC验证的解卡机器人\n"
                    "获取:3661221335\n"
                    "获取API:3562501187.3661221335\n"
                    "by YUKI"
                )
                await self._send_md(message, 2, md_content)
                return

            lower_err = error.lower()
            if "invalid url" in lower_err or "expired" in lower_err:
                friendly_error = (
                    "**链接已过期或无效**\n"
                    "请重新进入roblox获取新链接后再发送。\n"
                    "注意：获取链接后不要用浏览器打开，否则大概率无法解卡"
                )
            elif "timeout" in lower_err or "network" in lower_err or "超时" in lower_err or "连接引擎" in lower_err:
                friendly_error = "**服务器繁忙**\n引擎响应超时，请稍后再试。"
            elif error == "THROTTLE":
                friendly_error = "**请求过于频繁**\n上游服务器正在排队，请稍等几秒后再发送。"
            else:
                friendly_error = (
                    f"**解卡失败**\n"
                    f"真实原因：`{error}`\n\n"
                )

            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = (
                f"{mention}\n"
                f"{friendly_error}\n"
                "限时优惠:一天0.99\\一个周3.99\\一个月6.99\n"
                "可绕过DC验证的解卡机器人\n"
                "获取:3661221335\n"
                "获取API:3562501187.3661221335\n"
                f"解卡耗时: {elapsed:.2f}s\n\n"
                f"![img #{target_w}px #{target_h}px]({img_url})\n\n"
                "by YUKI"
            )
            await self._send_md(message, 2, md_content)
            return

        except Exception as e:
            friendly_error = f"解卡异常：{str(e)[:200]}"
            img_url = f"{IMG_URL}&t={int(time.time() * 1000)}"
            target_w, target_h, img_url = await asyncio.to_thread(get_scaled_size, img_url)
            md_content = (
                f"{mention}\n"
                f"{friendly_error}\n"
                f"![img #{target_w}px #{target_h}px]({img_url})\n\n"
                "by YUKI"
            )
            await self._send_md(message, 2, md_content)

if __name__ == "__main__":
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    intents = botpy.Intents.all()
    client = MyClient(intents=intents)
    client.run(appid="1905559407", secret="0Mj6UsHg6XyQsLpJoJpLsQyX6gGrT5iL")
