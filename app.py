#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vycecloud — vyce.cloud 免费 VPS 自动续期保活（GitHub Action）
参考项目: https://github.com/zv201413/katabump

两种运行模式（互为备份）:
  1. requests 模式: 纯 HTTP API 查询/续期（vyce 2026-10 新增续期 Bestcap 验证码后，
     纯 API 续期报 missing_captcha → 退出码 2 交给浏览器模式）
  2. selenium 模式: SeleniumBase UC 真实浏览器走 UI：点 Renew → 过 Bestcap
     验证码（pyautogui 物理点击）→ token 回调自动续期。广告域名 DNS 屏蔽 + 弹窗劫持。

用法:
  python app.py --mode requests   # 仅 API（查询/降级检测）
  python app.py --mode selenium   # UI 自动化续期（需 seleniumbase + chrome + Xvfb + pyautogui）
  python app.py --mode auto       # API 优先，失败自动切 UI

环境变量（建议全部通过 GitHub Secrets 注入）:
  VYCE_COOKIE         单账号: __Host-vyce_sid 的值（容忍完整 cookie 字符串格式）
  VYCE_COOKIES_JSON   多账号: ["cookie1","cookie2"] 或 [{"name":"备注","cookie":".."}] 或 {"备注":"cookie"}
  TG_BOT_TOKEN / TG_CHAT_ID      Telegram 通知（可选）
  SERVERCHAN_SENDKEY             Server酱 微信通知（可选）
  PUSHPLUS_TOKEN                 PushPlus 微信通知（可选）
  VYCE_RENEW_THRESHOLD_HOURS     续期阈值，默认 40（剩余不足该小时数才发起续期）

退出码: 0=成功  1=Cookie 失效/硬错误（浏览器也救不了）  2=疑似被拦截（建议 Selenium 兜底）
"""

import os
import re
import sys
import json
import time
import random
import argparse
from datetime import datetime, timezone, timedelta

import requests

BASE_URL = "https://vyce.cloud"
COOKIE_NAME = "__Host-vyce_sid"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BEIJING_TZ = timezone(timedelta(hours=8))
RENEW_THRESHOLD_H = float(os.environ.get("VYCE_RENEW_THRESHOLD_HOURS", "40") or 40)
# vyce 前端 bundle 实测常量: 续期授予 48h(Ry=2*24h), canRenew 阈值 24h(zy=24h)
# 剩余 >24h 时卡片按钮渲染为禁用的 'Cooldown' —— UI 模式只在剩余 ≤24h 时尝试
CAN_RENEW_H = 24


def now_ms():
    return int(time.time() * 1000)


def log(msg=""):
    print(msg, flush=True)


def fmt_bj(ms):
    """毫秒时间戳 → 北京时间字符串"""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ).strftime("%m-%d %H:%M")


def fmt_left(ms):
    """距到期的剩余时长描述"""
    if not ms:
        return "-"
    h = (ms - now_ms()) / 3600000.0
    if h < 0:
        return f"已过期 {-h:.1f}h"
    return f"剩 {h:.1f}h"


# ============ 账号（cookie）加载 ============

def normalize_cookie(raw):
    """容忍多种粘贴格式，统一提取 sid 值"""
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if COOKIE_NAME in s:
        m = re.search(re.escape(COOKIE_NAME) + r"\s*=\s*([^;\s]+)", s)
        if m:
            return m.group(1)
    return s


def load_accounts():
    """多账号优先 VYCE_COOKIES_JSON，否则回退单账号 VYCE_COOKIE"""
    accounts = []
    raw_json = os.environ.get("VYCE_COOKIES_JSON", "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)

            def add(label, cookie):
                v = normalize_cookie(cookie)
                if v:
                    accounts.append({"label": label or f"账号{len(accounts) + 1}", "sid": v})

            if isinstance(data, list):
                for i, item in enumerate(data, 1):
                    if isinstance(item, dict):
                        add(str(item.get("name") or item.get("label") or f"账号{i}"),
                            item.get("cookie") or item.get("sid") or "")
                    else:
                        add(f"账号{i}", item)
            elif isinstance(data, dict):
                for k, v in data.items():
                    add(str(k), v)
            else:
                add("账号1", data)
            log(f"✅ 从 VYCE_COOKIES_JSON 加载 {len(accounts)} 个账号")
        except Exception as e:
            log(f"⚠️ VYCE_COOKIES_JSON 解析失败: {e}")
    if not accounts:
        v = normalize_cookie(os.environ.get("VYCE_COOKIE", ""))
        if v:
            accounts.append({"label": "账号1", "sid": v})
            log("✅ 使用单账号环境变量 VYCE_COOKIE")
    for a in accounts:
        masked = a["sid"][:6] + "****" + a["sid"][-4:] if len(a["sid"]) > 12 else "****"
        log(f"   {a['label']}: {masked}")
    return accounts


# ============ HTTP 传输层 ============

class RequestsTransport:
    """纯 requests 直连 vyce API"""

    def __init__(self, sid):
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": BASE_URL + "/",
            "Origin": BASE_URL,
            "Cookie": f"{COOKIE_NAME}={sid}",
        })

    def _call(self, method, path, payload=None):
        url = BASE_URL + path
        try:
            if payload is None:
                r = self.sess.request(method, url, timeout=25)
            else:
                r = self.sess.request(method, url, json=payload, timeout=25)
            return r.status_code, r.text
        except Exception as e:
            return -1, f"requests exception: {e}"

    def get(self, path):
        return self._call("GET", path)

    def post_json(self, path, payload):
        return self._call("POST", path, payload)


# 页面内 fetch：同源请求自动携带 cookie，TLS/JA3 指纹与真实浏览器一致
_INPAGE_FETCH_JS = """
const [path, method, bodyStr] = arguments;
const done = arguments[arguments.length - 1];
const opts = {method: method, credentials: 'include'};
if (bodyStr !== null) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = bodyStr;
}
fetch(path, opts)
    .then(async r => done({status: r.status, body: await r.text()}))
    .catch(e => done({status: -1, body: String(e)}));
"""


class SeleniumTransport:
    """通过已登录浏览器执行同源 fetch"""

    def __init__(self, sb):
        self.sb = sb
        try:
            sb.driver.set_script_timeout(90)
        except Exception:
            pass

    def _call(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload)
        try:
            res = self.sb.driver.execute_async_script(_INPAGE_FETCH_JS, path, method, body)
        except Exception as e:
            return -1, f"execute_async_script failed: {e}"
        if not isinstance(res, dict):
            return -1, "no-response"
        try:
            return int(res.get("status", -1)), str(res.get("body", ""))
        except Exception:
            return -1, "bad-response"

    def get(self, path):
        return self._call("GET", path)

    def post_json(self, path, payload):
        return self._call("POST", path, payload)


def classify(status, body_text):
    """
    响应分类:
      ok       200 成功
      cooldown 429 renewal_cooldown（24 小时冷却，属正常）
      captcha  400 missing_captcha / invalid_captcha（续期接口已启用 Bestcap
               验证码，纯 API 无法通过 → 需浏览器 UI 模式）
      auth     401/419 unauthenticated（Cookie 失效）
      blocked  网络异常/超时/403/5xx（疑似被拦截 → 值得浏览器兜底）
      error    其它硬错误
    """
    try:
        err = (json.loads(body_text) or {}).get("error", "") if body_text else ""
    except Exception:
        err = ""
    if status == 200:
        return "ok"
    if status == 429 and err == "renewal_cooldown":
        return "cooldown"
    if status == 400 and err in ("missing_captcha", "invalid_captcha"):
        return "captcha"
    if status in (401, 419) or err in ("unauthenticated", "unauthorized"):
        return "auth"
    if status in (-1, 0, 408, 429, 502, 503, 504) or 500 <= status <= 599 or status == 403:
        return "blocked"
    return "error"


# ============ 核心续期逻辑（两种传输层共用） ============

def process_account(transport, acct):
    """处理单个账号：校验会话 → 拉取服务器列表 → 逐台决策续期 → 汇报最终状态"""
    result = {"label": acct["label"], "login": "", "outcome": "ok", "servers": [], "notes": []}
    log("=" * 46)
    log(f"🧾 处理账号: {acct['label']}")

    # ---- 1. 会话校验 ----
    st, txt = transport.get("/api/me")
    cat = classify(st, txt)
    if cat == "blocked":
        result["outcome"] = "blocked"
        result["notes"].append(f"会话校验疑似被拦截 (HTTP {st})")
        log(f"⚠️ 会话校验疑似被拦截 (HTTP {st}): {txt[:120]}")
        return result
    if cat != "ok":
        result["outcome"] = "auth" if cat == "auth" else "error"
        result["notes"].append("Cookie 已失效，请重新登录 vyce.cloud 复制 __Host-vyce_sid 并更新 Secrets" if cat == "auth" else f"会话校验异常 (HTTP {st}): {txt[:80]}")
        return result
    try:
        result["login"] = (json.loads(txt) or {}).get("user", {}).get("login", "")
    except Exception:
        pass
    log(f"👤 会话有效: {result['login'] or acct['label']}")

    # ---- 2. 服务器列表 ----
    st, txt = transport.get("/api/servers")
    cat = classify(st, txt)
    if cat != "ok":
        result["outcome"] = "blocked" if cat == "blocked" else ("auth" if cat == "auth" else "error")
        result["notes"].append(f"获取服务器列表失败 (HTTP {st}): {txt[:80]}")
        log(f"⚠️ 获取服务器列表失败 (HTTP {st}, {cat}): {txt[:120]}")
        return result
    try:
        servers = (json.loads(txt) or {}).get("servers", []) or []
    except Exception as e:
        result["outcome"] = "error"
        result["notes"].append(f"服务器列表解析失败: {e}")
        return result
    log(f"📦 共 {len(servers)} 台服务器")
    if not servers:
        result["notes"].append("账号下没有服务器，跳过")
        return result

    renew_happened = False
    for sv in servers:
        sid_, name = sv.get("id", ""), sv.get("name", "?")
        status = sv.get("status", "?")
        expires = sv.get("expiresAt")
        paid_until = sv.get("paidUntil")
        action = ""

        if sv.get("abuseFrozen"):
            action = "⚠️ 服务器被冻结(abuse)，请人工检查"
        elif sv.get("suspended"):
            action = "⚠️ 服务器被暂停: " + str(sv.get("suspendReason") or "原因未知")
        elif paid_until and paid_until > now_ms():
            action = f"💎 付费有效期内，跳过（至 {fmt_bj(paid_until)}）"
        elif not expires:
            action = "ℹ️ 无到期时间字段，跳过"
        elif status == "stopped" and expires > now_ms():
            action = f"⏸️ 已停止但未到期（剩 {fmt_left(expires)}），视为手动停止，跳过"
        elif expires - now_ms() > RENEW_THRESHOLD_H * 3600000:
            action = f"⏳ 有效期充足（{fmt_left(expires)}），无需续期"
        else:
            # ---- 3. 发起续期 ----
            log(f"🔄 [{name}] 发起续期 ...")
            st2, txt2 = transport.post_json(f"/api/servers/{sid_}/renew", {})
            cat2 = classify(st2, txt2)
            if st2 == 200:
                action = "✅ 续期成功"
                log(f"✅ [{name}] 续期成功")
                renew_happened = True
            elif cat2 == "cooldown":
                action = "🧊 24h 续期冷却中（此前已续期，正常）"
                log(f"🧊 [{name}] {action}")
            elif cat2 == "captcha":
                action = "🔐 续期需过 Bestcap 验证码（纯 API 无法续期，需浏览器模式）"
                log(f"🔐 [{name}] {action}")
                result["outcome"] = "blocked"
            elif cat2 == "blocked":
                action = f"⚠️ 续期请求疑似被拦截 (HTTP {st2})"
                log(f"⚠️ [{name}] {action}")
                result["outcome"] = "blocked"
            elif cat2 == "auth":
                action = "❌ Cookie 已失效"
                result["outcome"] = "auth"
            else:
                action = f"❌ 续期失败 (HTTP {st2}): {txt2[:60]}"
                log(action)
                if result["outcome"] == "ok":
                    result["outcome"] = "error"

        item = {"id": sid_, "name": name, "os": sv.get("os", ""), "region": sv.get("region", ""),
                "plan": sv.get("planId", ""), "action": action,
                "status": status, "expires": expires}
        result["servers"].append(item)

    # ---- 4. 续期后重新拉取，汇报最终状态 ----
    if not renew_happened:
        return result
    st3, txt3 = transport.get("/api/servers")
    if classify(st3, txt3) == "ok":
        try:
            final = {s.get("id"): s for s in (json.loads(txt3) or {}).get("servers", []) or []}
            for item in result["servers"]:
                f = final.get(item.get("id"))
                if f:
                    item["status"] = f.get("status", item["status"])
                    item["expires"] = f.get("expiresAt", item["expires"])
                    if f.get("hostIssue"):
                        item["action"] += f" | 主机异常: {f['hostIssue']}"
        except Exception as e:
            log(f"⚠️ 刷新最终状态失败(不影响结果): {e}")

    return result


def inject_session_cookie(sb, sid):
    """向当前浏览器注入/替换 vyce 会话 cookie（保留 cf_clearance 以免重新过盾）"""
    try:
        sb.driver.execute_cdp_cmd("Network.deleteCookies", {"name": COOKIE_NAME})
    except Exception:
        pass
    ok = False
    try:
        sb.driver.execute_cdp_cmd("Network.setCookie", {
            "name": COOKIE_NAME, "value": sid, "path": "/",
            "secure": True, "url": BASE_URL + "/",
        })
        ok = True
    except Exception as e:
        log(f"⚠️ CDP setCookie 失败: {e}")
    if not ok:
        try:
            sb.driver.add_cookie({"name": COOKIE_NAME, "value": sid,
                                  "path": "/", "secure": True})
            ok = True
        except Exception as e:
            log(f"⚠️ add_cookie 失败: {e}")
    return ok


# ============ 浏览器 UI 自动化（Bestcap 验证码方案） ============
#
# vyce 2026-10 起对续期接口启用自研 Bestcap 验证码（captcha.vyceai.com）：
#   POST /api/servers/{id}/renew 需要 captchaResponse=bestcap token，
#   token 只能在真实浏览器里点击复选框（PoW+行为验证）获得。
# 本方案：
#   1. UC 反检测浏览器 + DNS 屏蔽第三方广告域（广告脚本会崩 driver / 弹窗遮挡）
#   2. CDP 注入 cookie → 页面内 fetch 查询 API（GET 接口不需要验证码）
#   3. 需续期时走 UI：JS 点 Renew → !important CSS 强制 bestcap 隐藏 iframe 可见
#      → pyautogui 物理点击复选框（trusted 事件）→ state: idle→working→success
#      → token 回调自动 POST 续期 → 挂钩的 fetch 捕获响应确认结果
#   4. window.open 劫持防止广告弹窗盖住浏览器窗口

AD_BLOCK_RULES = (
    "--host-resolver-rules="
    "MAP highrevenueformat.com 127.0.0.1,"
    "MAP nap5k.com 127.0.0.1,"
    "MAP n6wxm.com 127.0.0.1,"
    "MAP jhnwr.com 127.0.0.1,"
    "MAP ldrws.com 127.0.0.1,"
    "MAP uplcm.com 127.0.0.1,"
    "MAP t.me 127.0.0.1"
)

FORCE_CSS = (
    "<style id='bc-force'>"
    "iframe[src*='vyceai']{"
    " width:400px !important;height:120px !important;"
    " visibility:visible !important;opacity:1 !important;"
    " position:relative !important;display:block !important;"
    " min-width:400px !important;min-height:120px !important;"
    " z-index:99999 !important;}"
    "iframe[style*='2147483647']{display:none !important;}"
    "</style>"
)

OBSERVER_JS = (
    "if (!window.__bcObs) { window.__bcObs = new MutationObserver("
    "mutations => { for (const m of mutations)"
    " for (const n of m.addedNodes) {"
    "  if (n.tagName === 'IFRAME' && (!n.src ||"
    "      (!n.src.includes('vyceai') && !n.src.includes('ad.html')))) {"
    "   const s = getComputedStyle(n);"
    "   if (parseInt(s.zIndex) > 1000) n.remove();"
    "  } } });"
    " window.__bcObs.observe(document.documentElement,"
    " {childList: true, subtree: true}); }"
)

BLOCK_POPUP_JS = (
    "if (!window.__openBlocked) { window.__openBlocked = true;"
    " window.__origOpen = window.open;"
    " window.open = function() { return null; }; }"
)

HOOK_FETCH_JS = (
    "if (!window.__fetchHooked) { window.__fetchHooked = true;"
    " window.__renewResp = null;"
    " const of = window.fetch;"
    " window.fetch = function(...a) {"
    "  const url = String(a[0]);"
    "  if (url.includes('/renew')) {"
    "   window.__renewResp = null;"
    "   return of.apply(this, a).then(r => {"
    "    try { r.clone().json().then(j => {"
    "     window.__renewResp = {status: r.status, body: j};"
    "    }).catch(()=>{}); } catch (e) {}"
    "    return r;"
    "   });"
    "  }"
    "  return of.apply(this, a);"
    " }; }"
)


def get_chrome_display():
    """从 chrome 渲染进程 environ 读 DISPLAY（SB 可能自建 display，如 :1003）
    注意必须精确匹配浏览器二进制路径——宽匹配 'chrome' 会先命中
    chromedriver（其 DISPLAY 继承自 shell），导致物理点击发错显示器"""
    import subprocess
    for pat in ("chrome-linux64/chrome",          # seleniumbase 安装的 CFT
                "/opt/google/chrome/chrome",       # runner 自带 stable
                "/usr/local/bin/google-chrome",    # workflow 软链的 CFT
                "/usr/bin/google-chrome"):
        try:
            out = subprocess.run(["pgrep", "-f", pat],
                                 capture_output=True, text=True).stdout or ""
            for pid in out.split():
                try:
                    with open(f"/proc/{pid}/environ", "rb") as f:
                        for e in f.read().decode("utf-8", "replace").split("\0"):
                            if e.startswith("DISPLAY="):
                                return e.split("=", 1)[1]
                except Exception:
                    continue
        except Exception:
            continue
    return None


class UIBot:
    """单账号浏览器 UI 续期机器人"""

    def __init__(self, sb, acct, rt=None):
        self.sb = sb
        self.d = sb.driver
        self.acct = acct
        self.rt = rt or RequestsTransport(acct["sid"])

    # ---- 页面工具 ----

    def fetch_json(self, path):
        """页面内 fetch JSON（8s JS 侧竞速超时，防网络悬挂吃掉 script timeout）"""
        try:
            return self.d.execute_script(
                "return Promise.race(["
                " fetch(arguments[0]).then(r => r.ok ? r.json()"
                "  : null).catch(() => null),"
                " new Promise(res => setTimeout(() => res(null), 8000))"
                "]);", path)
        except Exception:
            return None

    def servers_via_requests(self):
        """requests 侧查询服务器列表（绕过页面内 fetch mock，数据永远新鲜）"""
        try:
            st, tx = self.rt.get("/api/servers")
            if st == 200:
                return (json.loads(tx) or {}).get("servers", []) or []
        except Exception:
            pass
        return None

    def inject_all(self):
        try:
            self.d.execute_script(
                "if (!document.getElementById('bc-force'))"
                " document.head.insertAdjacentHTML('beforeend',"
                " arguments[0]);", FORCE_CSS)
            self.d.execute_script(OBSERVER_JS)
            self.d.execute_script(BLOCK_POPUP_JS)
            self.d.execute_script(HOOK_FETCH_JS)
        except Exception:
            pass

    def clear_overlays(self):
        try:
            self.d.execute_script(
                "document.querySelectorAll('iframe').forEach(f => {"
                " const s = getComputedStyle(f);"
                " if ((f.src && !f.src.includes('vyceai') &&"
                "     !f.src.includes('ad.html') &&"
                "     parseInt(s.zIndex) > 1000) ||"
                "     (!f.src && s.position === 'fixed')) f.remove(); });")
        except Exception:
            pass

    # ---- 主流程 ----

    def process(self):
        acct = self.acct
        result = {"label": acct["label"], "login": "",
                  "outcome": "ok", "servers": [], "notes": []}
        log("=" * 46)
        log(f"🌐 [浏览器UI] 处理账号: {acct['label']}")

        # 会话校验（页面内 fetch，GET 不需要验证码）
        me = None
        for _ in range(6):
            me = self.fetch_json("/api/me")
            if me and me.get("user"):
                break
            self.sb.sleep(1)
        if not (me and me.get("user")):
            # 页面内拿不到可能是网络悬挂 → requests 侧复核再定性
            st, tx = self.rt.get("/api/me")
            if st == 200:
                try:
                    me = json.loads(tx)
                except Exception:
                    me = None
        if not (me and me.get("user")):
            result["outcome"] = "auth"
            result["notes"].append("Cookie 已失效，请重新登录 vyce.cloud 更新 Secrets")
            return result
        result["login"] = me["user"].get("login", "")
        log(f"👤 会话有效: {result['login']}")

        # 等 bestcap.js 全局库就绪（软检查：仅续期路径真正需要它，
        # 网络抖动加载失败时不阻断查询/跳过类路径，续期路径内会重载重试）
        bc_ready = False
        for _ in range(30):
            try:
                if self.d.execute_script(
                        "return typeof window.bestcap === 'object' &&"
                        " typeof window.bestcap.render === 'function';"):
                    bc_ready = True
                    break
            except Exception:
                pass
            self.sb.sleep(1)
        if not bc_ready:
            log("⚠️ bestcap.js 未就绪（非续期场景不影响，续期时会重载重试）")
        # sitekey 从 /api/config 动态获取（bundle 默认值兜底）
        cfg = self.fetch_json("/api/config") or {}
        self.sitekey = (cfg.get("bestcapSitekey")
                        or "bc_site_b05a3b42a31232929d6194f3")
        # React 面板是否渲染出服务器卡片
        # （剩余>24h 时卡片按钮是 'Cooldown' 而非 'Renew'，都算渲染成功）
        self.panel_ready = False
        for _ in range(20):
            if self.d.execute_script(
                    "const t = [...document.querySelectorAll('button')]"
                    ".map(b => (b.innerText||'').trim());"
                    "return t.some(x => ['Renew','Cooldown',"
                    "'Renew and start','Renew now'].includes(x));"):
                self.panel_ready = True
                break
            self.sb.sleep(1)
        log(f"🧩 bestcap={'✅' if bc_ready else '⚠️'}, "
            f"sitekey={self.sitekey[:20]}..., "
            f"React面板={'✅' if self.panel_ready else '❌'}")

        data = self.fetch_json("/api/servers")
        servers = (data or {}).get("servers", []) or []
        log(f"📦 共 {len(servers)} 台服务器")
        if not servers:
            result["notes"].append("账号下没有服务器，跳过")
            return result

        renew_happened = False
        for sv in servers:
            sid_, name = sv.get("id", ""), sv.get("name", "?")
            status = sv.get("status", "?")
            expires = sv.get("expiresAt")
            paid_until = sv.get("paidUntil")
            action = ""

            if sv.get("abuseFrozen"):
                action = "⚠️ 服务器被冻结(abuse)，请人工检查"
            elif sv.get("suspended"):
                action = "⚠️ 服务器被暂停: " + str(sv.get("suspendReason") or "原因未知")
            elif paid_until and paid_until > now_ms():
                action = f"💎 付费有效期内，跳过（至 {fmt_bj(paid_until)}）"
            elif not expires:
                action = "ℹ️ 无到期时间字段，跳过"
            elif status == "stopped" and expires > now_ms():
                action = f"⏸️ 已停止但未到期（剩 {fmt_left(expires)}），视为手动停止，跳过"
            elif expires - now_ms() > RENEW_THRESHOLD_H * 3600000:
                action = f"⏳ 有效期充足（{fmt_left(expires)}），无需续期"
            elif expires - now_ms() > CAN_RENEW_H * 3600000:
                # vyce canRenew 规则: 剩余>24h 卡片按钮是禁用的 'Cooldown'
                action = (f"🧊 续期冷却中（剩 {fmt_left(expires)}，"
                          f"≤{CAN_RENEW_H}h 后可续），跳过")
            else:
                log(f"🔄 [{name}] UI 续期 ...")
                ok, action = self.ui_renew_one(name)
                if ok:
                    renew_happened = True
                elif action.startswith("❌"):
                    result["outcome"] = "error"

            result["servers"].append({
                "id": sid_, "name": name, "os": sv.get("os", ""),
                "region": sv.get("region", ""), "plan": sv.get("planId", ""),
                "action": action, "status": status, "expires": expires})

        # 刷新最终状态（requests 侧取，绕过页面内 mock 的陈旧数据）
        if renew_happened:
            data2 = {"servers": self.servers_via_requests() or []}
            if data2["servers"]:
                try:
                    final = {s.get("id"): s for s in data2["servers"]}
                    for item in result["servers"]:
                        f = final.get(item.get("id"))
                        if f:
                            item["status"] = f.get("status", item["status"])
                            item["expires"] = f.get("expiresAt", item["expires"])
                except Exception:
                    pass
        return result

    def ui_renew_one(self, name):
        """对单台服务器执行续期：React 面板 Renew 对话框路径
        （exp24 配方, 2026-10-26 本地实测点击被接受且续期成功）"""
        return self._renew_via_panel(name)

    def _renew_via_panel(self, name):
        """React 面板续期（exp24 实测成功配方, 2026-10-26）
        关键要素（24 轮实验结论, 缺一不可）:
          1. 运行时点击校准: pyautogui 投递坐标 vs 渲染坐标有漂移(公式差40px+)
             → 点击视口中心, document 捕获阶段记录实际落点计算偏移
          2. 页面预热 50s 拟人漫游: 会话页龄越长点击越易被 Bestcap 接受
          3. 对话框 iframe 区域内漫游 60s 再点击: #glyphs 行为采集需轨迹数据
          4. 多段逼近轨迹 + 90ms 按压的物理点击（isTrusted）
          5. idle→working→idle 复位后: iframe 内小漫游 20s + 自动重点(≤5次/轮)
          6. 结果验证走 requests 侧: 页面内 fetch 被 mock 时返回陈旧数据
        """
        d, sb = self.d, self.sb
        # bestcap.js 就绪检查（脚本加载失败时重载页面最多 2 次）
        # —— 续期对话框的验证码 iframe 由 bestcap.render 创建，缺它必失败
        bc_ok = False
        try:
            bc_ok = d.execute_script(
                "return typeof window.bestcap === 'object';")
        except Exception:
            pass
        for reload_n in range(2):
            if bc_ok:
                break
            log(f"   [{name}] bestcap 未就绪, 重载页面 ({reload_n + 1}/2) ...")
            try:
                d.get(BASE_URL + "/")
            except Exception:
                try:
                    d.execute_script("window.stop()")
                except Exception:
                    pass
            for _ in range(20):
                try:
                    if d.execute_script(
                            "return typeof window.bestcap === 'object';"):
                        bc_ok = True
                        break
                except Exception:
                    pass
                time.sleep(1)
        if not bc_ok:
            log(f"   [{name}] ⚠️ bestcap 仍未就绪（继续尝试，可能 iframe 不出现）")

        # 基准 expiresAt 从 requests 侧取（不受页面内 mock 影响）
        b_exp = None
        for s in self.servers_via_requests() or []:
            if s.get("name") == name:
                b_exp = s.get("expiresAt")
                break
        if not b_exp:
            return False, "❌ 未找到服务器或无到期时间"

        self.inject_all()
        disp = get_chrome_display() or os.environ.get("DISPLAY")
        if disp:
            os.environ["DISPLAY"] = disp
        import pyautogui
        pyautogui.PAUSE = 0.03

        # ---- 1. 运行时点击校准（document 捕获实际落点）----
        off = None
        for _ in range(6):
            try:
                d.execute_script(
                    "window.__land = null;"
                    "window.__calH = function(e) {"
                    " window.__land = {x: e.clientX, y: e.clientY}; };"
                    "document.addEventListener('click', window.__calH, true);"
                    "window.scrollTo(0,0);")
                g = json.loads(d.execute_script(
                    "return JSON.stringify({sx:window.screenX,"
                    "sy:window.screenY,ow:window.outerWidth,"
                    "oh:window.outerHeight,iw:window.innerWidth,"
                    "ih:window.innerHeight});"))
            except Exception:
                break
            est_x = g["sx"] + (g["ow"] - g["iw"]) / 2 + g["iw"] / 2
            est_y = g["sy"] + (g["oh"] - g["ih"]) + g["ih"] / 2
            pyautogui.moveTo(int(est_x), int(est_y), duration=0.25)
            time.sleep(0.1)
            pyautogui.mouseDown()
            time.sleep(0.06)
            pyautogui.mouseUp()
            time.sleep(0.7)
            try:
                land = d.execute_script("return window.__land")
                d.execute_script(
                    "document.removeEventListener('click',"
                    " window.__calH, true);")
            except Exception:
                land = None
            if land:
                off = (est_x - land["x"], est_y - land["y"])
                break
            time.sleep(1.5)
        if not off:
            return False, "❌ 点击校准失败（物理坐标映射不可用）"
        off_x, off_y = off
        log(f"   [{name}] ✅ 校准偏移 ({off_x:+.0f},{off_y:+.0f})")

        # ---- 2. 页面预热 50s（拟人漫游, 喂行为采集 + 提高页龄）----
        log(f"   [{name}] 🖱 页面预热 50s ...")
        wt0 = time.time()
        while time.time() - wt0 < 50:
            tx = random.randint(100, 1100) + off_x
            ty = random.randint(120, 620) + off_y
            pyautogui.moveTo(tx, ty, duration=random.uniform(0.2, 0.5))
            time.sleep(random.uniform(0.15, 0.5))

        # ---- 3. 主循环: 最多 4 轮 ----
        for rnd in range(1, 5):
            log(f"   [{name}] ===== 第 {rnd} 轮 =====")
            self.inject_all()
            self.clear_overlays()
            # 按服务器名定位卡片按钮（多账号多服务器时不能点错卡）
            has_renew = d.execute_script(
                "const nm = arguments[0];"
                "const btns = [...document.querySelectorAll('button')]"
                " .filter(b => (b.innerText||'').trim() === 'Renew');"
                "for (const b of btns) {"
                " let el = b;"
                " for (let i = 0; i < 15 && el; i++) {"
                "  el = el.parentElement;"
                "  if (el && (el.textContent||'').includes(nm))"
                "   return true; } }"
                "return false;", name)
            if not has_renew:
                # 剩余>24h 时按钮是禁用的 'Cooldown' —— 正常冷却而非错误
                cooldown = d.execute_script(
                    "const nm = arguments[0];"
                    "const btns = [...document.querySelectorAll('button')]"
                    " .filter(b => (b.innerText||'').trim()"
                    "  === 'Cooldown');"
                    "for (const b of btns) {"
                    " let el = b;"
                    " for (let i = 0; i < 10 && el; i++) {"
                    "  el = el.parentElement;"
                    "  if (el && (el.textContent||'').includes(nm))"
                    "   return true; } }"
                    "return false;", name)
                if cooldown:
                    return True, ("🧊 续期冷却中（卡片按钮显示 Cooldown），"
                                  "本次跳过")
                log(f"   [{name}] Renew 按钮未出现，重试")
                time.sleep(2)
                continue
            d.execute_script(
                "const nm = arguments[0];"
                "const btns = [...document.querySelectorAll('button')]"
                " .filter(b => (b.innerText||'').trim() === 'Renew');"
                "for (const b of btns) {"
                " let el = b;"
                " for (let i = 0; i < 15 && el; i++) {"
                "  el = el.parentElement;"
                "  if (el && (el.textContent||'').includes(nm))"
                "   { b.click(); return true; } } }"
                "return false;", name)
            frame = None
            for _ in range(20):
                fs = d.find_elements("css selector",
                                     "iframe[src*='vyceai']")
                if fs:
                    frame = fs[0]
                    break
                time.sleep(1)
            if not frame:
                log(f"   [{name}] 验证码 iframe 未出现")
                continue
            # 强制 iframe 可见 + 取自身矩形
            d.execute_script(
                "const s = document.createElement('style');"
                "s.textContent = \"iframe[src*='vyceai']{"
                " width:400px !important;height:120px !important;"
                " visibility:visible !important;opacity:1 !important;"
                " position:relative !important;display:block !important;"
                " z-index:99999 !important;}\";"
                "document.head.appendChild(s);")
            time.sleep(2)
            try:
                ifr = json.loads(d.execute_script(
                    "const f=[...document.querySelectorAll("
                    "\"iframe[src*='vyceai']\")][0];"
                    "const r=f.getBoundingClientRect();"
                    "return JSON.stringify({x:r.x,y:r.y,w:r.width,"
                    "h:r.height});"))
            except Exception as e:
                log(f"   [{name}] iframe rect 异常: {str(e)[:60]}")
                continue
            # 等复选框渲染（28x28 真复选框优先, 记录尺寸变化历史）
            chk = None
            sizes = []
            try:
                d.switch_to.frame(frame)
                d.execute_script(
                    "window.__rec = [];"
                    "const c = document.getElementById('check');"
                    "if (c) ['click'].forEach(t =>"
                    " c.addEventListener(t, e => {"
                    "  window.__rec.push({t: t, trusted: e.isTrusted});"
                    " }, true));")
                for i in range(25):
                    r = d.execute_script(
                        "const c=document.getElementById('check');"
                        "if(!c)return null;"
                        "const b=c.getBoundingClientRect();"
                        "return JSON.stringify({x:b.x,y:b.y,w:b.width,"
                        "h:b.height});")
                    if r:
                        c2 = json.loads(r)
                        if not sizes or sizes[-1] != (c2["w"], c2["h"]):
                            sizes.append((c2["w"], c2["h"]))
                        if c2["w"] >= 20:
                            chk = c2
                            break
                        if i == 24:
                            chk = c2
                    time.sleep(1)
                d.switch_to.default_content()
            except Exception:
                try:
                    d.switch_to.default_content()
                except Exception:
                    pass
            if not chk:
                log(f"   [{name}] 复选框未渲染 (尺寸历史 {sizes})")
                continue
            log(f"   [{name}] check={chk} 尺寸历史={sizes}")

            # ---- 4. 关键: iframe 区域内拟人漫游 60s（行为采集窗口）----
            fx0 = ifr["x"] + off_x
            fy0 = ifr["y"] + off_y
            ccx_ = fx0 + chk["x"] + chk["w"] / 2
            ccy_ = fy0 + chk["y"] + chk["h"] / 2
            log(f"   [{name}] 🖱 iframe 区域漫游 60s ...")
            wt0 = time.time()
            while time.time() - wt0 < 60:
                if random.random() < 0.5:
                    tx = ccx_ + random.uniform(-50, 200)
                    ty = ccy_ + random.uniform(-35, 45)
                else:
                    tx = fx0 + random.uniform(
                        5, max(10, ifr["w"] - 5))
                    ty = fy0 + random.uniform(
                        5, max(10, ifr["h"] - 5))
                pyautogui.moveTo(tx, ty,
                                 duration=random.uniform(0.15, 0.45))
                time.sleep(random.uniform(0.1, 0.4))

            # ---- 5. 多段逼近轨迹 + 物理点击 ----
            cx = ifr["x"] + chk["x"] + chk["w"] / 2
            cy = ifr["y"] + chk["y"] + chk["h"] / 2
            sx, sy = cx + off_x, cy + off_y
            try:
                x0 = sx - random.randint(150, 250)
                y0 = sy - random.randint(90, 140)
                pyautogui.moveTo(x0, y0, duration=0.3)
                time.sleep(random.uniform(0.2, 0.4))
                for i in range(4):
                    fx = (x0 + (sx - x0) * (i + 1) / 4
                          + random.uniform(-12, 12))
                    fy = (y0 + (sy - y0) * (i + 1) / 4
                          + random.uniform(-8, 8))
                    pyautogui.moveTo(fx, fy, duration=0.1)
                    time.sleep(random.uniform(0.05, 0.12))
                time.sleep(random.uniform(0.2, 0.4))
                pyautogui.moveTo(int(sx), int(sy), duration=0.18)
                time.sleep(random.uniform(0.15, 0.3))
                pyautogui.mouseDown()
                time.sleep(0.09)
                pyautogui.mouseUp()
                log(f"   [{name}] ✅ 物理点击 ({sx:.0f},{sy:.0f})")
            except Exception as e:
                log(f"   [{name}] 点击异常: {e}")
                continue

            # ---- 6. 等结果（200s, 复位自动重点）----
            t0 = time.time()
            last_st = "idle"
            n_clicks = 1
            while time.time() - t0 < 200:
                time.sleep(7)
                # iframe 组件状态 + 点击可信记录
                try:
                    d.switch_to.frame(frame)
                    snap = d.execute_script(
                        "const c=document.getElementById('card');"
                        "return JSON.stringify({"
                        " st: c ? c.getAttribute('data-state') : null,"
                        " rec: window.__rec || []});")
                    d.switch_to.default_content()
                    j = json.loads(snap or "{}")
                    if j.get("st") and j["st"] != last_st:
                        log(f"   [{name}] 状态: {last_st} → {j['st']}"
                            f" (点击#{n_clicks}) | {j.get('rec')}")
                        last_st = j["st"]
                        if j["st"] == "idle" and n_clicks < 5:
                            # 点击被吃掉后回 idle（验证被拒）→ 小漫游后重点
                            time.sleep(10)
                            wt1 = time.time()
                            while time.time() - wt1 < 20:
                                tx = fx0 + random.uniform(
                                    5, max(10, ifr["w"] - 5))
                                ty = fy0 + random.uniform(
                                    5, max(10, ifr["h"] - 5))
                                pyautogui.moveTo(
                                    tx, ty,
                                    duration=random.uniform(0.15, 0.4))
                                time.sleep(random.uniform(0.1, 0.35))
                            pyautogui.moveTo(int(sx), int(sy),
                                             duration=0.2)
                            time.sleep(0.25)
                            pyautogui.mouseDown()
                            time.sleep(0.09)
                            pyautogui.mouseUp()
                            n_clicks += 1
                            log(f"   [{name}] 🔁 复位重点 #{n_clicks}")
                except Exception:
                    try:
                        d.switch_to.default_content()
                    except Exception:
                        pass
                # 续期响应 hook（429 冷却判定）
                try:
                    resp = d.execute_script(
                        "return window.__renewResp || null;")
                except Exception:
                    resp = None
                if resp and resp.get("status") == 429:
                    err = ((resp.get("body") or {})
                           .get("error") or "")
                    if err == "renewal_cooldown":
                        return True, ("🧊 24h 续期冷却中"
                                      "（此前已续期，正常）")
                # requests 侧验证 expiresAt（绕过页面内 mock）
                for s in self.servers_via_requests() or []:
                    if s.get("name") == name and s.get("expiresAt") \
                            and s["expiresAt"] > b_exp:
                        log(f"   [{name}] ✅ expiresAt → "
                            f"{s['expiresAt']}")
                        return True, "✅ 续期成功（浏览器过验证码）"
            # 本轮失败: Esc 关对话框再战
            try:
                d.execute_script(
                    "document.dispatchEvent(new KeyboardEvent("
                    "'keydown', {key: 'Escape', keyCode: 27}));")
            except Exception:
                pass
            time.sleep(2)
        return False, "❌ 验证码多轮尝试未通过"


def run_selenium_mode(accounts):
    """浏览器 UI 自动化模式：逐账号启动 UC 浏览器走完整续期流程

    页面加载两阶段策略（vyce 前端 8 个初始化接口任一失败/悬挂都会卡死
    React —— 2026-10-26 runner 失败根因: 旧版网络屏蔽 stock/keys/credits/
    billing 导致 fetch reject, Promise 链断裂, 服务器卡片永不渲染）:
      阶段1 自然加载 ×3 —— 真实数据最干净
      阶段2 注入全量 fetch mock 再加载 ×3 —— 预取数据 + 空 shape 兜底,
            所有 mock 响应都是成功(200), React 初始化链不会断
    mock 仅拦截 GET —— 续期 POST /api/servers/{id}/renew 绝不能被吞
    （旧版 includes('/api/servers') 会连 renew POST 一起匹配, 是潜伏 bug）"""
    from seleniumbase import SB  # 延迟导入，requests 模式无需安装
    results = []
    for acct in accounts:
        try:
            rt = RequestsTransport(acct["sid"])
            # ---- requests 预取全部 8 个初始化接口（mock 数据源）----
            pre = {}
            for path in ("/api/me", "/api/servers", "/api/config",
                         "/api/stock", "/api/keys", "/api/billing",
                         "/api/credits", "/api/waitlist"):
                for _ in range(3):
                    try:
                        st, tx = rt.get(path)
                        if st == 200:
                            pre[path] = tx
                            break
                    except Exception:
                        pass
                    time.sleep(2)
            # /api/me 是 React 渲染前置条件, 多给几次机会
            if "/api/me" not in pre:
                for _ in range(4):
                    try:
                        st, tx = rt.get("/api/me")
                        if st == 200:
                            pre["/api/me"] = tx
                            break
                    except Exception:
                        pass
                    time.sleep(3)
            # 空数据兜底 shape: 宁可渲染出空面板也不能卡 Loading
            pre.setdefault("/api/config", json.dumps({
                "github": True, "devLogin": False,
                "minAccountAgeDays": 30, "signupsEnabled": True,
                "freeServersEnabled": True, "maintenanceBanner": "",
                "enabledPlans": ["mini", "plus", "pro"],
                "bestcapSitekey": "bc_site_b05a3b42a31232929d6194f3"}))
            pre.setdefault("/api/stock", "[]")
            pre.setdefault("/api/keys", "[]")
            pre.setdefault("/api/billing", "{}")
            pre.setdefault("/api/credits", "{}")
            pre.setdefault("/api/waitlist", "[]")
            log(f"[{acct['label']}] 预取接口 {len(pre)}/8: "
                f"{sorted(pre.keys())}")

            with SB(uc=True, headless=False, locale_code="en-US",
                    chromium_arg=AD_BLOCK_RULES) as sb:
                d = sb.driver
                try:
                    d.set_script_timeout(25)
                except Exception:
                    pass
                # CDP 注入 cookie（不依赖页面加载状态）
                d.execute_cdp_cmd("Network.setCookie", {
                    "name": COOKIE_NAME,
                    "value": acct["sid"],
                    "url": BASE_URL + "/",
                    "secure": True,
                    "httpOnly": True,
                })
                # 网络级广告屏蔽: 第三方广告会崩 driver/弹窗遮挡/偷走点击
                d.execute_cdp_cmd("Network.enable", {})
                d.execute_cdp_cmd("Network.setBlockedURLs", {"urls": [
                    "*highrevenueformat.com*", "*nap5k.com*",
                    "*n6wxm.com*", "*jhnwr.com*", "*ldrws.com*",
                    "*uplcm.com*", "*t.me*", "*tag.min.js*",
                    "*invoke.js*", "*ad.html*",
                ]})
                # 预注入: window.open 劫持（续期广告链接 uplcm.com）
                # + ad.html iframe 清除观察者
                d.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument", {
                        "source":
                            "window.open = function() { return null; };"
                            "function __killAds() {"
                            " document.querySelectorAll('iframe')"
                            "  .forEach(f => {"
                            "   if (f.src && f.src.includes('ad.html'))"
                            "    f.remove(); }); }"
                            "__killAds();"
                            "if (!window.__adObs) {"
                            " window.__adObs = new MutationObserver("
                            "  muts => {"
                            "   for (const m of muts)"
                            "    for (const n of m.addedNodes) {"
                            "     if (n.tagName === 'IFRAME' && n.src &&"
                            "         n.src.includes('ad.html'))"
                            "      n.remove(); } });"
                            " window.__adObs.observe("
                            "  document.documentElement,"
                            "  {childList: true, subtree: true}); }"})
                d.set_page_load_timeout(45)

                def panel_rendered(sec=35):
                    """React 真正渲染完服务器卡片 = 出现卡片按钮
                    （剩余>24h 显示 Cooldown, ≤24h 显示 Renew）"""
                    for _ in range(sec // 2):
                        time.sleep(2)
                        try:
                            if d.execute_script(
                                    "const t=[...document.querySelectorAll"
                                    "('button')].map(b =>"
                                    " (b.innerText||'').trim());"
                                    "return t.some(x => ['Renew','Cooldown',"
                                    "'Renew and start','Renew now']"
                                    ".includes(x));"):
                                return True
                        except Exception:
                            pass
                    return False

                def nav_once():
                    try:
                        d.get(BASE_URL + "/")
                    except Exception:
                        try:
                            d.execute_script("window.stop()")
                        except Exception:
                            pass

                panel = False
                # 阶段1: 自然加载 ×3（真实数据最干净）
                for load_n in range(1, 4):
                    log(f"ℹ️ [{acct['label']}] 自然加载 {load_n}/3 ...")
                    nav_once()
                    if panel_rendered(40):
                        panel = True
                        break
                    try:
                        log("   " + str(d.execute_script(
                            "return JSON.stringify({signedIn: !document.body"
                            ".innerText.includes('Sign in'), bestcap: typeof"
                            " window.bestcap});")))
                    except Exception:
                        pass
                # 阶段2: 注入全量 fetch mock（GET-only）再试 ×3
                if not panel:
                    log("ℹ️ 自然加载未渲染 → 注入全量 fetch mock 加速")
                    mock_js = (
                        "const __mock = " + json.dumps(pre) + ";"
                        "const __of = window.fetch;"
                        "window.fetch = function(url) {"
                        " const u = String(url);"
                        " const mth = (arguments.length > 1 &&"
                        "  arguments[1] && arguments[1].method) ?"
                        "  String(arguments[1].method).toUpperCase()"
                        "  : 'GET';"
                        " if (mth === 'GET') {"
                        "  for (const k in __mock) {"
                        "   if (u.includes(k)) {"
                        "    const body = __mock[k];"
                        "    return Promise.resolve({ok: true,"
                        "     status: 200,"
                        "     json: () => Promise.resolve("
                        "      JSON.parse(body)),"
                        "     text: () => Promise.resolve(body)});"
                        "   }"
                        "  }"
                        " }"
                        " return __of.apply(this, arguments);"
                        "};")
                    try:
                        d.execute_cdp_cmd(
                            "Page.addScriptToEvaluateOnNewDocument",
                            {"source": mock_js})
                    except Exception as e:
                        log(f"⚠️ fetch mock 注入失败: {e}")
                    for load_n in range(1, 4):
                        log(f"ℹ️ [{acct['label']}] mock 加载 "
                            f"{load_n}/3 ...")
                        nav_once()
                        if panel_rendered(40):
                            panel = True
                            break
                if not panel:
                    try:
                        dg = d.execute_script(
                            "return JSON.stringify({bodyTxt: document.body"
                            " ? document.body.innerText.slice(0, 100)"
                            " : null, bestcap: typeof window.bestcap});")
                    except Exception:
                        dg = "page-dead"
                    log(f"❌ [{acct['label']}] 面板未渲染: {dg}")
                    results.append({"label": acct["label"], "login": "",
                                    "outcome": "error", "servers": [],
                                    "notes": [f"React 面板未渲染: {dg}"]})
                    continue
                log(f"✅ [{acct['label']}] 面板渲染完成")
                results.append(UIBot(sb, acct, rt).process())
        except Exception as e:
            log(f"⚠️ [浏览器UI] {acct['label']} 异常: {e}")
            results.append({"label": acct["label"], "login": "",
                            "outcome": "error", "servers": [],
                            "notes": [f"浏览器模式异常: {e}"]})
    return results


# ============ 通知模块（Telegram + 微信双通道） ============

def build_report(results, mode_label):
    lines = [f"🖥 vyce.cloud 自动续期报告（{mode_label}模式）", ""]
    all_ok = True
    for acct in results:
        icon = {"ok": "✅", "auth": "❌", "blocked": "🚫", "error": "❌"}.get(acct["outcome"], "ℹ️")
        if acct["outcome"] != "ok":
            all_ok = False
        head = f"{icon} {acct['label']}"
        if acct.get("login"):
            head += f"（{acct['login']}）"
        lines.append(head)
        for note in acct.get("notes", []):
            lines.append(f"   • {note}")
        for sv in acct.get("servers", []):
            line = (f"   • {sv['name']}［{sv.get('status', '?')}］"
                    f"{sv.get('os', '')} · {sv.get('region', '')}")
            lines.append(line)
            lines.append(f"      到期 {fmt_bj(sv.get('expires'))}（{fmt_left(sv.get('expires'))}）— {sv.get('action', '')}")
        lines.append("")
    lines.append(f"🕘 {datetime.now(tz=BEIJING_TZ).strftime('%Y-%m-%d %H:%M')} 北京时间")
    text = "\n".join(lines)
    if not all_ok:
        text += "\n\n💡 Cookie 失效请重新登录 https://vyce.cloud 获取 __Host-vyce_sid 后更新仓库 Secrets"
    return text, all_ok


def send_telegram(text):
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("ℹ️ 未配置 TG_BOT_TOKEN/TG_CHAT_ID，跳过 Telegram 推送")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=15)
        log("📩 Telegram: " + ("发送成功" if r.ok else f"发送失败 {r.status_code}: {r.text[:100]}"))
    except Exception as e:
        log(f"⚠️ Telegram 发送异常: {e}")


def send_serverchan(title, text):
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not key:
        log("ℹ️ 未配置 SERVERCHAN_SENDKEY，跳过 Server酱 推送")
        return
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": text}, timeout=15)
        log("📩 Server酱: " + ("发送成功" if r.ok else f"发送失败 {r.status_code}: {r.text[:100]}"))
    except Exception as e:
        log(f"⚠️ Server酱 发送异常: {e}")


def send_pushplus(title, text):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        log("ℹ️ 未配置 PUSHPLUS_TOKEN，跳过 PushPlus 推送")
        return
    try:
        r = requests.post("https://www.pushplus.plus/send",
                          json={"token": token, "title": title,
                                "content": text, "template": "txt"}, timeout=15)
        log("📩 PushPlus: " + ("发送成功" if r.ok else f"发送失败 {r.status_code}: {r.text[:100]}"))
    except Exception as e:
        log(f"⚠️ PushPlus 发送异常: {e}")


def notify(results, mode_label):
    text, all_ok = build_report(results, mode_label)
    title = ("✅ vyce 续期成功" if all_ok else "❌ vyce 续期异常，请检查")
    print("\n" + text + "\n")
    send_telegram(text)
    send_serverchan(title, text)
    send_pushplus(title, text)


# ============ 主流程 ============

def aggregate_exit(results):
    outs = [r.get("outcome") for r in results]
    if "blocked" in outs:
        return 2
    if "auth" in outs or "error" in outs:
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="vyce.cloud 自动续期保活")
    parser.add_argument("--mode", choices=["requests", "selenium", "auto"],
                        default="auto", help="requests=纯API / selenium=浏览器 / auto=API优先+自动兜底")
    args = parser.parse_args()
    mode = args.mode

    log("#" * 46)
    log("  vycecloud — vyce.cloud 自动续期")
    log(f"  模式: {mode} | 续期阈值: {RENEW_THRESHOLD_H}h")
    log("#" * 46)

    accounts = load_accounts()
    if not accounts:
        log("❌ 未配置任何账号: 请设置 VYCE_COOKIE 或 VYCE_COOKIES_JSON")
        sys.exit(1)

    exit_code = 1
    if mode in ("requests", "auto"):
        results = [process_account(RequestsTransport(a["sid"]), a) for a in accounts]
        exit_code = aggregate_exit(results)
        if exit_code == 0:
            notify(results, "API")
        elif exit_code == 2:
            log("⚠️ API 模式疑似被拦截（退出码 2）")
            if mode == "requests":
                log("   → 按 workflow 设计交由浏览器模式兜底，本进程不重复推送通知")
                sys.exit(2)
            # auto 模式：进程内自动兜底
            log("   → auto 模式: 自动切换浏览器模式重试 ...")
            try:
                se_results = run_selenium_mode(accounts)
                se_exit = aggregate_exit(se_results)
                notify(se_results, "浏览器")
                sys.exit(0 if se_exit == 0 else 1)
            except ImportError:
                log("   ⚠️ 本地未安装 seleniumbase，无法兜底")
                sys.exit(2)
            except Exception as e:
                log(f"   ⚠️ 浏览器模式异常: {e}")
                sys.exit(2)
        else:
            notify(results, "API")
            sys.exit(exit_code)

    # selenium 模式（显式指定或被 workflow 兜底调用）
    if mode == "selenium":
        try:
            results = run_selenium_mode(accounts)
        except Exception as e:
            log(f"❌ 浏览器模式启动失败: {e}")
            results = [{"label": a["label"], "login": "", "outcome": "error",
                        "servers": [],
                        "notes": [f"浏览器模式启动失败: {e}"]} for a in accounts]
        exit_code = aggregate_exit(results)
        exit_code = 1 if exit_code != 0 else 0
        notify(results, "浏览器")
        sys.exit(exit_code)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
