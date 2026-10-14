"""诺米一起听 —— 让 bot 进入网易云音乐「一起听」房间，和对方听同一首歌。

核心能力：
    1. 自动接受「一起听」邀请，进入对方的房间；
    2. 读出房间当前播放的歌曲、播放进度，并定位到当前唱的歌词；
    3. 把这些真实数据通过 LLM 工具交给大模型，让 bot 能"陪聊"；
    4. 换歌时主动发一条消息（可配置关掉）。

依赖：
    需要一个可访问的网易云音乐 API 服务（本项目适配 api-enhanced / NeteaseCloudMusicApi
    兼容实现），并为其配置一个已登录的网易云账号 cookie。详见 README。

作者声明：
    「一起听」相关接口的调用思路参考了 zbqbbm/netease-listen-together-mcp 的
    VPS-HTTP 方案，API 服务使用 neteasecloudmusicapienhanced/api-enhanced。
    感谢这两个项目。
"""

import asyncio
import json
import re
import time
import urllib.parse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

# LRC 歌词行：[mm:ss.xx]歌词内容
LRC_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")

# 邀请私信里 nativeUrl 中出现该关键字即认为是一起听邀请
INVITE_KEYWORD = "listenTogether"


@register(
    "astrbot_plugin_nomi_music",
    "shamate-tv",
    "诺米一起听",
    "1.0.0",
    "https://github.com/shamate-tv/astrbot_plugin_nomi_music",
)
class NomiMusic(Star):
    """一起听插件主体。

    生命周期：
        initialize() 启动后开一个后台协程按 poll_interval 轮询房间状态；
        terminate() 时取消协程并关闭 HTTP 会话。
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}

        # ---- 配置项 ----
        self.api_base = str(cfg.get("api_base") or "http://127.0.0.1:3100").rstrip("/")
        self.cookie_file = str(cfg.get("cookie_file") or "")
        self.poll_interval = max(5, int(cfg.get("poll_interval") or 10))
        self.auto_accept = bool(cfg.get("auto_accept", True))
        self.notify = bool(cfg.get("notify_on_song_change", True))
        self.notify_umo = str(cfg.get("notify_umo") or "")

        # ---- 运行时状态 ----
        self.session = None  # aiohttp 会话
        self._task = None  # 后台轮询任务
        self._cookie_cache = ("", 0.0)
        self._my_uid = ""  # 自身账号 ID，用于过滤发给自己的私信

        self.in_room = False  # 是否已在房间里
        self.room_id = None
        self.room_users = []
        self.song_id = None  # 当前歌曲 ID
        self.song = None  # {name, artists, album, duration}
        self.lyric_lines = []  # [(毫秒时间戳, 歌词)]
        self.progress_ms = 0  # 进度快照
        self.progress_at = 0.0  # 快照时间（用于推算实时进度）
        self.play_status = ""
        self.playlist = []  # 房间歌单里的歌曲 ID
        self.last_umo = ""  # 最近一次交互的会话，用于主动推送
        self._song_cache = {}
        self._lyric_cache = {}
        self._notified_song = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        """插件启用时调用：建会话、起轮询。"""
        self.session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._loop())
        logger.info("[nomi_music] 一起听插件已启动")

    async def terminate(self):
        """插件停用时调用：清理协程与会话，避免任务泄漏。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.session:
            await self.session.close()
        logger.info("[nomi_music] 一起听插件已停止")

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _cookie(self) -> str:
        """读取并缓存网易云账号 cookie（最多缓存 60 秒）。"""
        now = time.time()
        if self._cookie_cache[0] and now - self._cookie_cache[1] < 60:
            return self._cookie_cache[0]
        ck = ""
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                ck = f.read().strip()
        except Exception as e:
            logger.warning(
                f"[nomi_music] 读取 cookie 失败（请在配置里填写正确路径）: {e}"
            )
        self._cookie_cache = (ck, now)
        return ck

    async def _api(self, path: str, **params) -> dict:
        """调用网易云 API 服务。所有失败都返回空 dict，绝不抛出，避免拖垮轮询。"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        params["cookie"] = self._cookie()
        url = f"{self.api_base}/{path.lstrip('/')}"
        try:
            async with self.session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[nomi_music] 请求 {path} 失败: {e}")
            return {}

    async def _get_my_uid(self) -> str:
        """查询自身账号 ID，用于从私信里筛出给自己的消息。只查一次并缓存。"""
        if self._my_uid:
            return self._my_uid
        d = await self._api("user/account")
        uid = str(((d or {}).get("account") or {}).get("id") or "")
        if uid:
            self._my_uid = uid
        return uid

    def _eff_progress_ms(self) -> int:
        """把进度快照按已经过的时间往前推（仅在播放中才有意义）。"""
        p = self.progress_ms
        if self.play_status == "PLAY" and self.progress_at:
            p += int((time.time() - self.progress_at) * 1000)
        return p

    def _cur_line(self):
        """返回（当前歌词, 下一句歌词）。"""
        p = self._eff_progress_ms()
        cur = nxt = None
        for ms, txt in self.lyric_lines:
            if ms <= p:
                cur = txt
            elif nxt is None:
                nxt = txt
                break
        return cur, nxt

    # ------------------------------------------------------------------
    # 后台轮询
    # ------------------------------------------------------------------
    async def _loop(self):
        """轮询主循环：任何一次异常都不终止循环。"""
        await asyncio.sleep(6)  # 等 AstrBot 起来再干活
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[nomi_music] 轮询异常: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        """一轮检查：不在房间就尝试收邀请，在房间里就刷新歌曲。"""
        st = await self._api("listentogether/status")
        data = (st or {}).get("data") or {}

        if not data.get("inRoom"):
            if self.in_room:
                logger.info("[nomi_music] 已离开房间或房间已失效")
            self.in_room = False
            self.room_id = None
            self.room_users = []
            self.song_id = None
            self.song = None
            self.lyric_lines = []
            if self.auto_accept:
                await self._try_accept()
            return

        room = data.get("roomInfo") or {}
        rid = room.get("roomId")
        if not rid:
            return
        if rid != self.room_id:
            self.room_id = rid
            self.in_room = True
            self._notified_song = None
            logger.info(f"[nomi_music] 进入房间 {rid}")
        self.room_users = [
            u.get("nickname") or str(u.get("userId"))
            for u in (room.get("roomUsers") or [])
        ]
        await self._refresh_playlist()

    async def _refresh_playlist(self):
        """拉取房间歌单与播放状态，发现换歌则触发一次处理。"""
        pl = await self._api("listentogether/sync/playlist/get", roomId=self.room_id)
        data = (pl or {}).get("data") or {}
        cmd = data.get("playCommand") or {}

        sid = str(cmd.get("targetSongId") or "")
        self.progress_ms = int(cmd.get("progress") or 0)
        self.progress_at = time.time()
        self.play_status = str(cmd.get("playStatus") or "")

        disp = ((data.get("playlist") or {}).get("displayList") or {}).get(
            "result"
        ) or []
        self.playlist = [str(x) for x in disp]

        if not sid:
            return
        if sid != self.song_id:
            self.song_id = sid
            await self._load_song(sid)
            await self._on_song_change()

    async def _load_song(self, sid: str):
        """拉取歌曲详情与歌词（按歌曲 ID 缓存，避免重复请求）。"""
        info = self._song_cache.get(sid)
        if not info:
            d = await self._api("song/detail", ids=sid)
            songs = (d or {}).get("songs") or []
            s = songs[0] if songs else {}
            info = {
                "name": s.get("name") or "未知歌曲",
                "artists": "/".join(
                    a.get("name", "") for a in (s.get("ar") or s.get("artists") or [])
                ),
                "album": ((s.get("al") or s.get("album")) or {}).get("name") or "",
                "duration": int((s.get("dt") or s.get("duration") or 0) / 1000),
            }
            self._song_cache[sid] = info
        self.song = info

        lines = self._lyric_cache.get(sid)
        if lines is None:
            d2 = await self._api("lyric", id=sid)
            raw = ((d2.get("lrc") or {}).get("lyric")) or ""
            lines = []
            for ln in raw.splitlines():
                m = LRC_RE.match(ln)
                if not m:
                    continue
                # [mm:ss.xx] -> 毫秒
                t = int(m.group(1)) * 60000 + int(float(m.group(2)) * 1000)
                txt = m.group(3).strip()
                if txt:
                    lines.append((t, txt))
            self._lyric_cache[sid] = lines
        self.lyric_lines = lines

    async def _on_song_change(self):
        """换歌回调：第一次进房间只记录，之后换歌才发通知。"""
        if not self.notify or not self.song:
            return
        if self._notified_song is None:
            self._notified_song = self.song_id
            return
        self._notified_song = self.song_id
        s = self.song
        await self._send(f"🎵 换歌了：**{s['name']}** — {s['artists']}")

    # ------------------------------------------------------------------
    # 邀请处理
    # ------------------------------------------------------------------
    async def _find_invite(self):
        """从私信里找一起听邀请。

        邀请不是通过「一起听状态」接口下发的，而是作为一条私信到达收件箱，
        房间号与邀请人 ID 编码在该私信的 generalMsg.nativeUrl 里。

        Returns:
            (room_id, inviter_id, inviter_name)，找不到则返回 None 或三元素 None 组。
        """
        my_id = await self._get_my_uid()
        d = await self._api("msg/private", limit=10)
        for m in (d or {}).get("msgs") or []:
            u = m.get("user") or {}
            if my_id and str(u.get("toUserId")) != my_id:
                continue  # 只看发给自己的私信
            try:
                # lastMsg 是「JSON 里套 JSON」：外层 {msgId, msg}，内层才是真正的消息体
                inner = json.loads(
                    json.loads(u.get("lastMsg") or "{}").get("msg") or "{}"
                )
            except Exception:
                continue
            gm = inner.get("generalMsg") or {}
            nu = urllib.parse.unquote(gm.get("nativeUrl") or "")
            if INVITE_KEYWORD not in nu:
                continue
            # nativeUrl 形如 orpheus://...?roomId=xxx&inviterId=yyy&inviterName=zzz
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse(nu.split("url1=")[-1].split("&url2=")[0]).query
            )
            rid = (q.get("roomId") or [None])[0]
            iid = (q.get("inviterId") or [None])[0]
            iname = (q.get("inviterName") or [""])[0]
            if rid and iid:
                return rid, iid, iname
        return None

    async def _try_accept(self) -> str:
        """找到邀请并接受，返回给用户看的提示语。"""
        inv = await self._find_invite()
        if not inv:
            return ""
        rid, iid, iname = inv
        self.room_id = rid
        r = await self._api("listentogether/accept", roomId=rid, inviterId=iid)
        if (r or {}).get("code") == 200:
            self.in_room = True
            self._notified_song = None
            logger.info(f"[nomi_music] 已接受 {iname}({iid}) 的邀请，房间 {rid}")
            return f"已进入 {iname} 的一起听房间"
        logger.warning(f"[nomi_music] 接受邀请失败: {r}")
        return ""

    # ------------------------------------------------------------------
    # 主动发消息
    # ------------------------------------------------------------------
    async def _send(self, text: str) -> bool:
        """主动发送消息到指定会话（没配置就退化为最近一次说话的会话）。"""
        target = self.notify_umo or self.last_umo
        if not target:
            logger.info(f"[nomi_music] 暂无可发送的会话，跳过: {text}")
            return False
        try:
            return await self.context.send_message(target, MessageChain([Plain(text)]))
        except Exception as e:
            logger.warning(f"[nomi_music] 发送失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 提供给大模型的工具
    # ------------------------------------------------------------------
    @filter.llm_tool("music_now")
    async def music_now(self, event: AstrMessageEvent):
        """查询对方此刻在网易云「一起听」房间里听的是哪首歌、放到第几秒、当前唱到哪一句歌词。
        当对方提到正在听歌、问你在听什么、想让你聊聊这首歌，或你想关心一下他听什么时，调用这个工具。"""
        self.last_umo = event.unified_msg_origin
        if not self.in_room:
            return "对方现在没有在和你一起听歌（一起听房间未连接）。"
        if not self.song:
            return "已连上一起听房间，但还没读到正在播放的歌曲。"
        s = self.song
        cur, nxt = self._cur_line()
        parts = [
            f"歌名：{s['name']}",
            f"歌手：{s['artists']}",
            f"专辑：{s['album']}",
            f"进度：{self._eff_progress_ms() / 1000:.0f} 秒 / 共 {s['duration']} 秒",
            f"播放状态：{'播放中' if self.play_status == 'PLAY' else '暂停'}",
            f"一起听的人：{'、'.join(self.room_users) or '未知'}",
        ]
        if cur:
            parts.append(f"当前歌词：{cur}")
        if nxt:
            parts.append(f"下一句：{nxt}")
        return "\n".join(parts)

    @filter.llm_tool("music_room")
    async def music_room(self, event: AstrMessageEvent):
        """查看一起听房间的整体情况：房间成员、歌单有多少首、接下来几首是什么。"""
        self.last_umo = event.unified_msg_origin
        if not self.in_room:
            return "当前不在任何一起听房间。"
        nxt = []
        if self.song_id and self.song_id in self.playlist:
            i = self.playlist.index(self.song_id)
            nxt = self.playlist[i + 1 : i + 4]
        return (
            f"房间号：{self.room_id}\n"
            f"房间成员：{'、'.join(self.room_users) or '未知'}\n"
            f"歌单共 {len(self.playlist)} 首\n"
            f"接下来：{', '.join(nxt) if nxt else '（到头了）'}"
        )

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("一起听")
    async def cmd_join(self, event: AstrMessageEvent):
        """手动接受最近的一起听邀请。"""
        self.last_umo = event.unified_msg_origin
        msg = await self._try_accept()
        yield event.plain_result(
            msg or "没找到有效的一起听邀请，请先在网易云 App 里邀请你的 bot 账号。"
        )

    @filter.command("听歌状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前一起听状态。"""
        self.last_umo = event.unified_msg_origin
        if not self.in_room:
            yield event.plain_result("现在没在听歌诶，要不要拉我一起听？")
            return
        s = self.song or {}
        cur, _ = self._cur_line()
        txt = f"在听《{s.get('name', '?')}》— {s.get('artists', '?')}"
        if cur:
            txt += f"\n正唱到：{cur}"
        yield event.plain_result(txt)
