# 一起听 · AstrBot 插件

让 AstrBot 里的 bot **真正进入网易云音乐的「一起听」房间** —— 你和它听的是同一首歌，它知道你听到哪一句，可以陪你聊歌词、聊歌手、聊当下。

> ⚠️ **重要声明**：本插件调用的是网易云音乐的**非官方接口**，仅供个人学习与自用。
> 请**务必使用小号**，避免主号被风控。不保证长期可用，接口随时可能失效。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 🎧 真正进入房间 | 你在网易云 App 里拉它「一起听」，它会真的加入同一个房间 |
| 📖 知道你在听什么 | 歌名、歌手、专辑、播放进度、**当前唱到哪一句歌词** |
| 🤖 交给大模型 | 提供 `music_now` / `music_room` 两个 LLM 工具，让 bot 自己决定何时查询 |
| 🔔 换歌提醒 | 切歌时主动发一条消息（可关闭） |
| ⌨️ 指令 | `/一起听`、`/听歌状态` |

效果上，你可以直接问 bot：**「我现在在听什么？」**，它会用真实数据回答，而不是编造。

---

## 前置条件（这一步绕不开）

本插件**不自带**网易云接口实现，需要你自备两样东西：

1. **一个网易云 API 服务**（本项目适配 [`api-enhanced`](https://github.com/neteasecloudmusicapienhanced/api-enhanced)）
2. **一个网易云账号的 cookie**（建议专门注册一个小号）

### ① 部署网易云 API 服务

**方式 A：Docker（推荐）**

```bash
docker run -d --name netease-api --restart always \
  -p 3100:3000 -e PORT=3000 \
  neteasecloudmusicapienhanced/api-enhanced:latest
```

> 如果拉不到镜像，可以下载源码后用任意 Node 20 环境运行：
> ```bash
> curl -L -o api.tar.gz https://github.com/neteasecloudmusicapienhanced/api-enhanced/archive/refs/heads/main.tar.gz
> tar xzf api.tar.gz && mv api-enhanced-main /opt/netease-api
> docker run -d --name netease-api --restart always -p 3100:3000 \
>   -v /opt/netease-api:/app -w /app node:20-alpine \
>   sh -c "npm install --omit=dev --ignore-scripts && node app.js"
> ```

验证：浏览器打开 `http://<你的地址>:3100/search?keywords=test`，能返回 JSON 就成功了。

### ② 获取账号 cookie

**推荐：扫码登录**（不需要手抄 cookie）

```bash
# 1. 取 unikey
curl "http://127.0.0.1:3100/login/qr/key?timestamp=$(date +%s%3N)"
# 2. 用返回的 unikey 生成二维码
curl "http://127.0.0.1:3100/login/qr/create?key=<unikey>&qrimg=true"
#    qrimg 是 base64 图片，用手机网易云 App（登录目标小号）扫码确认
# 3. 轮询结果，code=803 时返回登录成功（其中 cookie 字段就是你要的）
curl "http://127.0.0.1:3100/login/qr/check?key=<unikey>"
```

**备用：手动复制**

浏览器登录 `https://music.163.com` → `F12` → **Application → Cookies → music.163.com**，
找到 `MUSIC_U` 和 `__csrf` 两项，拼成：

```
MUSIC_U=xxxxxx; __csrf=yyyyyy
```

> ⚠️ `MUSIC_U` 是 HttpOnly cookie，**用 `document.cookie` 是取不到的**，必须从 DevTools 的 Cookies 表格里复制。

把 cookie 存成文件，例如 `data/plugin_data/nomi_music/cookie.txt`。

### ③ 互相关注

「一起听」只能邀请好友：**你的账号和 bot 的网易云账号要互相关注**。

---

## 安装

1. AstrBot WebUI → 插件市场 → 搜索 `nomi_music`
2. 或者在 WebUI「插件管理 → 从链接安装」里填本仓库地址

---

## 配置

在 WebUI 的插件配置页填写：

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `api_base` | 网易云 API 服务地址 | `http://127.0.0.1:3100` |
| `cookie_file` | cookie 文件路径（填 `data/plugin_data/...` 下的路径） | 空 |
| `poll_interval` | 轮询间隔（秒），越小越实时、请求越多 | `10` |
| `auto_accept` | 收到邀请自动接受 | `true` |
| `notify_on_song_change` | 换歌时主动发消息 | `true` |
| `notify_umo` | 换歌提醒发到哪个会话（留空 = 最近一次跟 bot 说话的会话） | 空 |

---

## 使用

### 指令

```
/一起听      手动接受最近的一起听邀请
/听歌状态    查看当前在听什么
```

### 自然语言

直接问 bot 就行，它会自己决定要不要调工具：

```
我现在在听什么？
帮我看看这歌到哪句了
房间里有几个人？
```

> 需要所用模型支持**函数调用（Function Calling）**。

### 让 bot「知道」自己在陪听

插件提供了数据，但**怎么说话取决于你的人格提示词**。建议在人格里加一段，例如：

```
【一起听】
当对方提到在听歌、问你在听什么、让你聊聊这首歌时，先用 music_now 查一下，
再照着真实内容聊——可以说说这句歌词、这个歌手、或者你自己当下的感受，
像戴着半边耳机的人那样随口聊。
重要：只有工具真的返回了内容，才能说"我在听/我看到"；没连上就直说，不许编造歌名和歌词。
```

---

## 常见问题

**Q：bot 说"房间未连接"？**
房间有效期约 30 分钟，过期后需要重新邀请。开启 `auto_accept` 后，重新邀请它会自动进入。

**Q：换歌提醒没发出来？**
需要**先在 QQ 里跟 bot 说过一次话**（插件要记住会话地址），或者手动配置 `notify_umo`。

**Q：cookie 失效了？**
登录态通常能维持较长时间，失效后重新走一遍扫码流程，替换 cookie 文件即可。

**Q：会不会封号？**
非官方接口存在风控风险，**请用小号**。轮询间隔不建议低于 10 秒。

---

## 灵感来源与致谢

- 一起听接口的调用链路参考了 [zbqbbm/netease-listen-together-mcp](https://github.com/zbqbbm/netease-listen-together-mcp) 的 VPS-HTTP 方案，感谢原作者。
- API 服务使用 [neteasecloudmusicapienhanced/api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)。
- 插件基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发。

---

## License

MIT
