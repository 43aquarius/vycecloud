#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vycebump — vyce.cloud 免费 VPS 自动续期保活（GitHub Action）
参考项目: https://github.com/zv201413/katabump

两种运行模式（互为备份）:
  1. requests 模式（默认优先）: 纯 HTTP API 调用，秒级完成
  2. selenium 模式（兜底）: SeleniumBase UC 浏览器在页面内 fetch 调用同一 API，
     应对 requests 直连被 Cloudflare 拦截等异常场景

用法:
  python app.py --mode requests   # 仅 API 模式
  python app.py --mode selenium   # 仅浏览器模式（需已安装 seleniumbase + chrome + xvfb）
  python app.py --mode auto       # API 优先，被拦截时自动切浏览器（本地调试用）

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


def run_selenium_mode(accounts):
    """SeleniumBase UC 浏览器兜底模式：逐账号注入 cookie 后在页面内 fetch API"""
    from seleniumbase import SB  # 延迟导入，requests 模式无需安装
    results = []
    with SB(uc=True, headless=False) as sb:
        for acct in accounts:
            log("=" * 46)
            log(f"🌐 [浏览器] 处理 {acct['label']}")
            r = None
            for attempt in (1, 2):
                try:
                    sb.uc_open_with_reconnect(BASE_URL + "/", reconnect_time=4)
                    sb.sleep(2)
                    if not inject_session_cookie(sb, acct["sid"]):
                        raise RuntimeError("cookie 注入失败")
                    sb.open(BASE_URL + "/#/servers")
                    sb.sleep(3)
                    r = process_account(SeleniumTransport(sb), acct)
                except Exception as e:
                    log(f"⚠️ 浏览器模式第 {attempt} 次尝试异常: {e}")
                    r = {"label": acct["label"], "login": "", "outcome": "error",
                         "servers": [], "notes": [f"浏览器模式异常: {e}"]}
                if r and r["outcome"] != "blocked":
                    break
                if attempt == 2 and r:
                    r["notes"].append("浏览器模式重试后仍被拦截，请检查网络/换用代理")
            results.append(r or {"label": acct["label"], "login": "", "outcome": "error",
                                "servers": [], "notes": ["浏览器模式无结果"]})
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
    log("  vycebump — vyce.cloud 自动续期")
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
