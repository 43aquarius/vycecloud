# vycecloud 🔁

**vyce.cloud 免费 VPS 自动续期保活** —— 通过 GitHub Actions 每天自动调用 vyce.cloud 的续期接口，让免费服务器永不过期。

> 灵感来自 [katabump](https://github.com/zv201413/katabump)（Katabump 续期项目），本项目针对 [vyce.cloud](https://vyce.cloud) 从零逆向了其面板与前端。vyce 于 2026-10 为续期接口启用了自研 Bestcap 验证码（PoW + 行为采集），纯 API 无法续期，因此采用 **纯 API 检查（秒级）+ SeleniumBase UC 浏览器续期（UI 过验证码）** 的分工架构。

[English](#english) | 中文

---

## 📌 背景

vyce.cloud 提供免费 Linux VPS（2GB 内存 / 1 vCore / 6GB NVMe / 独立 IPv6），但免费服务器**每次续期只能延长 2 天**，且**每 24 小时只能续期一次**，忘记续期服务器就会被停止。

本项目利用 GitHub Actions 定时任务每天自动续期：

| 接口 | 说明 |
|------|------|
| `GET /api/me` | 校验会话是否有效 |
| `GET /api/servers` | 拉取服务器列表（名称/状态/到期时间） |
| `POST /api/servers/{id}/renew` | 续期 **+2 天**（须携带 Bestcap 验证码 token，仅浏览器 UI 模式发起）；若服务器因未续期停止，会**自动重新启动** |
| 认证方式 | Cookie：`__Host-vyce_sid=...`（GitHub OAuth 登录后的会话） |

**续期策略**：API 检查阶段只在服务器剩余有效期不足 40 小时（或已因过期停止）时才触发浏览器续期流程，避免缩短手动续期获得的更长有效期；撞上 24h 冷却（429 或禁用的 Cooldown 按钮）视为正常，不影响任务结果。

**分工架构**（续期接口已启用 Bestcap 验证码，纯 API 无法续期）：
1. **requests 检查模式**（第一步）：纯 HTTP 查询会话与剩余时长，秒级完成；无需续期时直接结束（省去浏览器开销），Cookie 失效时明确报错
2. **selenium 续期模式**（第二步）：检查发现有服务器需续期时，workflow 自动安装浏览器环境，SeleniumBase UC 模式打开真实面板：点 Renew → 物理点击过 Bestcap 验证码 → token 回调自动发起续期，最后推送最终通知

## 🚀 快速开始

### 1. Fork 或导入本仓库

### 2. 获取 vyce.cloud 的 Cookie

1. 浏览器登录 https://vyce.cloud （GitHub OAuth）
2. `F12` 打开开发者工具 → **Application（应用）** → **Cookies** → `https://vyce.cloud`
3. 找到 `__Host-vyce_sid`，复制其 **Value**（一长串字符）

> 也可以在 **Network（网络）** 面板任意 `api` 请求的 **Request Headers → Cookie** 里复制整串，脚本会自动提取。

### 3. 配置 Secrets

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 必填 | 说明 |
|-------------|------|------|
| `VYCE_COOKIE` | ✅（单账号） | `__Host-vyce_sid` 的值 |
| `VYCE_COOKIES_JSON` | 多账号时使用 | 见下方格式 |
| `TG_BOT_TOKEN` | 可选 | Telegram Bot Token |
| `TG_CHAT_ID` | 可选 | Telegram Chat ID |
| `SERVERCHAN_SENDKEY` | 可选 | [Server酱](https://sct.ftqq.com) SendKey（微信通知） |
| `PUSHPLUS_TOKEN` | 可选 | [PushPlus](https://www.pushplus.plus) Token（微信通知） |

**多账号格式**（与 `VYCE_COOKIE` 二选一，JSON 优先）：

```json
["第一个cookie的值", "第二个cookie的值"]
```

或带备注：

```json
[
  {"name": "主号", "cookie": "第一个cookie的值"},
  {"name": "小号", "cookie": "第二个cookie的值"}
]
```

### 4. 启用 Actions

- 仓库顶部 **Actions** 标签页 → 若提示 "Workflows aren't being run"，点击 **Enable** 开启
- 手动测试：选择 **Vyce Auto Renew** → **Run workflow** → 运行模式选 `auto` → **Run**
- 查看运行日志确认 ✅，之后每天 UTC 02:00（北京时间 10:00）自动执行

## 📡 通知效果

成功与失败都会推送（同时配置多个渠道则全部推送）：

```
🖥 vyce.cloud 自动续期报告（浏览器模式）

✅ 主号（43aquarius）
   • mini-01［running］Ubuntu 24.04 · Asia
      到期 10-26 10:35（剩 47.9h）— ✅ 续期成功

🕘 2026-10-24 10:00 北京时间
```

Cookie 失效时会收到：

```
❌ 主号
   • Cookie 已失效，请重新登录 vyce.cloud 复制 __Host-vyce_sid 并更新 Secrets
```

## 🔧 进阶

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `VYCE_RENEW_THRESHOLD_HOURS` | `40` | 剩余有效期低于该小时数时触发浏览器续期流程 |
| 手动触发 `force_mode` | `auto` | `requests`=仅API检查 / `selenium`=仅浏览器续期（测试链路用） |

**本地运行**：

```bash
pip install requests
export VYCE_COOKIE="你的sid"
python app.py --mode requests
```

## ⚠️ 注意事项

1. **Cookie 有效期有限**：失效会收到通知，按上面步骤重新获取并更新 Secret 即可
2. 续期冷却 24h + 每次延长 2 天：每天跑一次是稳定节奏；即便某天 GitHub cron 延误，仍有约 24 小时缓冲
3. 脚本对账号下**所有服务器**生效（含以后新建的），付费中的服务器（`paidUntil` 在未来）会自动跳过
4. 被人工停止（未到期）的服务器不会被动启动，尊重手动操作
5. 本项目仅供个人学习研究，请遵守 vyce.cloud 服务条款；滥用导致的封号与本项目无关
6. 建议仓库保持 **Private**，公开时也**绝不要**把 Cookie 写进任何代码文件

---

# English

**Auto-renewal / keep-alive for [vyce.cloud](https://vyce.cloud) free VPS** via GitHub Actions, inspired by [katabump](https://github.com/zv201413/katabump).

Free vyce.cloud servers expire after **2 days per renewal** with a **24-hour renewal cooldown**. This project calls the panel's internal API once daily to keep your servers alive:

- `GET /api/me` — validate session, `GET /api/servers` — list servers, `POST /api/servers/{id}/renew` — renew (+2 days, auto-restarts stopped servers; **requires a Bestcap captcha token, so renewal is only issued through the browser UI**)
- Auth = session cookie `__Host-vyce_sid` (get it from DevTools → Application → Cookies after GitHub OAuth login)
- **Two-stage pipeline**: a fast pure-HTTP check stage (session + remaining validity, exits early when no renewal is needed), then a SeleniumBase UC-browser stage that clicks through the Bestcap captcha and lets the panel issue the renewal
- Renews only when remaining validity < 40h (or server already stopped by expiry); a 24h cooldown (429 or a disabled Cooldown button) is treated as success
- Notifications via **Telegram** (`TG_BOT_TOKEN` + `TG_CHAT_ID`) and/or **WeChat** (`SERVERCHAN_SENDKEY` / `PUSHPLUS_TOKEN`) — all optional Secrets
- Multi-account supported via `VYCE_COOKIES_JSON`: `["sid1","sid2"]` or `[{"name":"label","cookie":"sid"}]`

**Setup**: fork this repo → add your cookie as Secret `VYCE_COOKIE` (plus optional notification Secrets) → enable Actions → optionally trigger **Vyce Auto Renew** manually to verify. It then runs daily at 02:00 UTC (10:00 Beijing).

Use responsibly and follow vyce.cloud's terms of service.
