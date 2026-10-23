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
import argparse
from datetime import datetime, timezone, timedelta

import requests

BASE_URL = "https://vyce.cloud"
COOKIE_NAME = "__Host-vyce_sid"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BEIJING_TZ = timezone(timedelta(hours=8))
RENEW_THRESHOLD_H = float(os.environ.get("VYCE_RENEW_THRESHOLD_HOURS", "40") or 40)


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
    """从 chrome 进程 environ 读 DISPLAY（SB 可能自建 display，如 :1003）"""
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "chrome"],
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
        pass
    return None


class UIBot:
    """单账号浏览器 UI 续期机器人"""

    def __init__(self, sb, acct):
        self.sb = sb
        self.d = sb.driver
        self.acct = acct

    # ---- 页面工具 ----

    def fetch_json(self, path):
        try:
            return self.d.execute_script(
                "return fetch(arguments[0]).then(r => r.ok ? r.json()"
                " : null).catch(() => null);", path)
        except Exception:
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

    def measure_viewport(self):
        g = json.loads(self.d.execute_script(
            "return JSON.stringify({sx:window.screenX,sy:window.screenY,"
            "ow:window.outerWidth,oh:window.outerHeight,"
            "iw:window.innerWidth,ih:window.innerHeight});"))
        return (g["sx"] + (g["ow"] - g["iw"]) / 2,
                g["sy"] + (g["oh"] - g["ih"]))

    # ---- 主流程 ----

    def process(self):
        acct = self.acct
        result = {"label": acct["label"], "login": "",
                  "outcome": "ok", "servers": [], "notes": []}
        log("=" * 46)
        log(f"🌐 [浏览器UI] 处理账号: {acct['label']}")

        # 会话校验（页面内 fetch，GET 不需要验证码）
        me = None
        for _ in range(15):
            me = self.fetch_json("/api/me")
            if me and me.get("user"):
                break
            self.sb.sleep(1)
        if not (me and me.get("user")):
            result["outcome"] = "auth"
            result["notes"].append("Cookie 已失效，请重新登录 vyce.cloud 更新 Secrets")
            return result
        result["login"] = me["user"].get("login", "")
        log(f"👤 会话有效: {result['login']}")

        # 等面板渲染完成（React 慢渲染/load 超时场景下 Renew 按钮出现才算就绪）
        for _ in range(25):
            if self.d.execute_script(
                    "return [...document.querySelectorAll('button')]"
                    ".some(b => (b.innerText||'').trim()==='Renew');"):
                break
            self.sb.sleep(1)
        else:
            result["notes"].append("面板未渲染出 Renew 按钮")
            log("⚠️ 面板未就绪（未找到 Renew 按钮）")
            return result

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

        # 刷新最终状态
        if renew_happened:
            data2 = self.fetch_json("/api/servers")
            if data2:
                try:
                    final = {s.get("id"): s for s in data2.get("servers", []) or []}
                    for item in result["servers"]:
                        f = final.get(item.get("id"))
                        if f:
                            item["status"] = f.get("status", item["status"])
                            item["expires"] = f.get("expiresAt", item["expires"])
                except Exception:
                    pass
        return result

    def ui_renew_one(self, name):
        """对单台服务器执行 UI 续期：点 Renew → 过验证码 → 返回 (ok, action)"""
        d, sb = self.d, self.sb
        before = self.fetch_json("/api/servers") or {}
        b_exp = None
        for s in before.get("servers", []) or []:
            if s.get("name") == name:
                b_exp = s.get("expiresAt")
                break

        self.inject_all()
        # pyautogui 绑定 Chrome 实际 display
        disp = get_chrome_display() or os.environ.get("DISPLAY")
        if disp:
            os.environ["DISPLAY"] = disp
        import pyautogui
        pyautogui.PAUSE = 0.15

        for rnd in range(1, 6):
            # 打开续期对话框（JS 点击，原生点击会被遮挡）
            self.inject_all()
            self.clear_overlays()
            d.execute_script(
                "const b = [...document.querySelectorAll('button')]"
                ".find(b => (b.innerText||'').trim() === 'Renew');"
                "if (b) { b.click(); return true; } return false;")
            sb.sleep(2)
            self.inject_all()
            self.clear_overlays()

            # 等 bestcap iframe
            frame = None
            for _ in range(15):
                fs = d.find_elements("css selector", "iframe[src*='vyceai']")
                if fs:
                    frame = fs[0]
                    break
                sb.sleep(1)
            if not frame:
                log(f"   [{name}] 第{rnd}轮: 验证码未出现，重试")
                continue

            # 坐标计算
            try:
                ifr = json.loads(d.execute_script(
                    "const f=document.querySelector(\"iframe[src*='vyceai']\");"
                    "const r=f.getBoundingClientRect();"
                    "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height});"))
            except Exception:
                continue
            if ifr.get("w", 0) < 50:
                sb.sleep(2)
                continue
            d.switch_to.frame(frame)
            chk = None
            try:
                for _ in range(10):
                    r = d.execute_script(
                        "const c=document.getElementById('check');"
                        "if(!c)return null;const b=c.getBoundingClientRect();"
                        "return JSON.stringify({x:b.x,y:b.y,w:b.width,"
                        "h:b.height});")
                    if r:
                        chk = json.loads(r)
                        break
                    time.sleep(1)
                if not chk:
                    d.switch_to.default_content()
                    continue
            finally:
                d.switch_to.default_content()

            vx = ifr["x"] + chk["x"] + chk["w"] / 2
            vy = ifr["y"] + chk["y"] + chk["h"] / 2

            # 落点验证
            at_pt = d.execute_script(
                "const el = document.elementFromPoint(arguments[0],"
                " arguments[1]);"
                "return el ? (el.tagName + '|' + (el.src || el.id || ''))"
                " : 'null';", vx, vy) or ""
            if "vyceai" not in at_pt:
                self.clear_overlays()
                time.sleep(0.3)
            log(f"   [{name}] 第{rnd}轮: 点击复选框 (视口 {vx:.0f},{vy:.0f})")

            # 物理点击（真实输入事件，过行为验证）
            vp_x, vp_y = self.measure_viewport()
            sx, sy = vp_x + vx, vp_y + vy
            try:
                pyautogui.moveTo(sx - 50, sy - 30, duration=0.25)
                time.sleep(0.1)
                pyautogui.moveTo(sx, sy, duration=0.2)
                time.sleep(0.4)
                pyautogui.mouseDown()
                time.sleep(0.08)
                pyautogui.mouseUp()
            except Exception as e:
                log(f"   [{name}] 物理点击异常: {e}")
                continue

            # 等 PoW + 自动续期（state: idle→working→success/verified）
            deadline = time.time() + 50
            while time.time() < deadline:
                sb.sleep(3)
                # 1) fetch 挂钩捕获 renew 响应（最可靠）
                resp = None
                try:
                    resp = d.execute_script("return window.__renewResp || null;")
                except Exception:
                    pass
                if resp:
                    st = resp.get("status")
                    err = (resp.get("body") or {}).get("error", "")
                    if st == 200:
                        log(f"   [{name}] ✅ 续期请求成功(HTTP 200)")
                        return True, "✅ 续期成功（浏览器过验证码）"
                    if st == 429 and err == "renewal_cooldown":
                        log(f"   [{name}] 🧊 续期冷却中(此前已续期)")
                        return True, "🧊 24h 续期冷却中（此前已续期，正常）"
                    if st in (400,):
                        log(f"   [{name}] 续期被拒: {err}")
                        break
                # 2) 兜底: expiresAt 变化
                now_data = self.fetch_json("/api/servers") or {}
                for s in now_data.get("servers", []) or []:
                    if s.get("name") == name and s.get("expiresAt") and \
                            b_exp and s["expiresAt"] > b_exp:
                        log(f"   [{name}] ✅ expiresAt 已延长")
                        return True, "✅ 续期成功（浏览器过验证码）"
            # 本轮失败，重试下一轮（widget 会重渲染）
        return False, "❌ 验证码多轮尝试未通过（可稍后重试）"


def run_selenium_mode(accounts):
    """浏览器 UI 自动化模式：逐账号启动 UC 浏览器走完整续期流程"""
    from seleniumbase import SB  # 延迟导入，requests 模式无需安装
    results = []
    for acct in accounts:
        try:
            with SB(uc=True, headless=False, locale_code="en-US",
                    chromium_arg=AD_BLOCK_RULES) as sb:
                d = sb.driver
                # CDP 注入 cookie（不依赖页面加载状态）
                d.execute_cdp_cmd("Network.setCookie", {
                    "name": COOKIE_NAME,
                    "value": acct["sid"],
                    "url": BASE_URL + "/",
                    "secure": True,
                    "httpOnly": True,
                })
                # 打开面板（容忍 load 事件超时，DOM 可用即可）
                d.set_page_load_timeout(40)
                for nav in range(2):
                    try:
                        d.get(BASE_URL + "/")
                    except Exception:
                        log("ℹ️ 页面 load 事件超时，继续（DOM 已可用）")
                    # 等 React 面板渲染（最长 60s）
                    ready = False
                    for _ in range(60):
                        try:
                            rs = d.execute_script(
                                "return document.readyState + '|' +"
                                " (document.querySelectorAll('button').length"
                                " > 3 ? 'Y' : 'N');")
                            if rs and rs.endswith("|Y"):
                                ready = True
                                break
                        except Exception:
                            pass
                        sb.sleep(1)
                    if ready:
                        break
                    log(f"ℹ️ 第{nav+1}次导航后面板未渲染，重试")
                results.append(UIBot(sb, acct).process())
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
