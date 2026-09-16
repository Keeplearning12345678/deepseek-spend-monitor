#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 花费数据服务
======================
为「DeepSeek 余额/花费浮窗」提供数据，零第三方依赖、纯本地。

  GET /state      JSON：余额、今日花费、本会话、累计、token 明细
  GET /widget.js  注入到 pi-web 的浮窗脚本（端口占位符已替换）
  GET /health     存活检查
  GET /refresh    立即刷新余额

数据来源：
  1) 余额    GET https://api.deepseek.com/user/balance   （人民币）
  2) 花费    pi 会话日志 ~/.pi/agent/sessions/**/*.jsonl 的 token 用量
             × 官方峰谷单价（单位与时段见 config.json）

只读、只监听 127.0.0.1，不向外发送任何数据。
"""

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "ds-monitor.log"
BJ = timezone(timedelta(hours=8))

DEFAULTS = {
    "port": 59992,
    "currency": "CNY",
    "poll_seconds": 2,
    "balance_refresh_seconds": 60,
    "peak_weekdays": [0, 1, 2, 3, 4],
    "peak_hours": [[9, 12], [14, 18]],
    "prices": {
        "peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
        "offpeak": {"hit": 0.02, "miss": 1.0, "out": 4.0},
    },
    "sessions_glob": "~/.pi/agent/sessions/**/*.jsonl",
    "models_json": "~/.pi/agent/models.json",
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    f = HERE / "config.json"
    if f.exists():
        try:
            user = json.loads(f.read_text("utf-8"))
            for k, v in user.items():
                if not k.startswith("_"):
                    cfg[k] = v
        except (OSError, ValueError) as exc:
            log("config.json 读取失败，使用默认值: %r" % exc)
    return cfg


CFG = load_config()


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def is_peak(dt):
    """dt 为北京时间；高峰 = 配置的星期 + 时段"""
    if dt.weekday() not in CFG["peak_weekdays"]:
        return False
    h = dt.hour + dt.minute / 60.0
    for a, b in CFG["peak_hours"]:
        if a <= h < b:
            return True
    return False


def next_switch(now):
    """下一个计费时段切换点 → (切换时刻, 切换后是否高峰)；找不到则 None"""
    weekdays = set(CFG["peak_weekdays"])
    cands = []
    for d in range(0, 15):
        day = now + timedelta(days=d)
        if day.weekday() not in weekdays:
            continue
        for a, b in CFG["peak_hours"]:
            for hh in (int(a), int(b)):
                t = day.replace(hour=hh, minute=0, second=0, microsecond=0)
                if t > now:
                    cands.append(t)
    if not cands:
        return None
    nxt = min(cands)
    return nxt, is_peak(nxt + timedelta(seconds=1))


def switch_text(now):
    """距离下次峰谷切换还有多久（供悬停提示用）"""
    r = next_switch(now)
    if not r:
        return {"nextSwitchMin": None, "nextSwitchIsPeak": None, "nextSwitchText": ""}
    nxt, to_peak = r
    mins = (nxt - now).total_seconds() / 60.0
    if mins < 1:
        when = "马上"
    elif mins < 60:
        when = "%d 分钟后" % int(round(mins))
    else:
        h, m = int(mins // 60), int(round(mins % 60))
        when = ("%d 小时 %d 分钟后" % (h, m)) if m else ("%d 小时后" % h)
    txt = ("⏳ %s进入高峰时段（单价翻倍，大任务建议先等等）" % when) if to_peak \
        else ("✅ %s进入空闲时段（单价减半）" % when)
    core = when[:-1] if when.endswith("后") else when      # "11 小时后" → "11 小时"
    short = ("⏳ 即将转高峰" if core == "马上" else "⏳ %s后转高峰" % core) if to_peak \
        else ("✅ 即将转空闲" if core == "马上" else "✅ %s后转空闲" % core)
    return {"nextSwitchMin": round(mins, 2), "nextSwitchIsPeak": bool(to_peak),
            "nextSwitchText": txt, "nextSwitchShort": short}


WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _fmt(v):
    """"0.02" / "1" / "8"：去掉无意义的小数尾零"""
    return "%g" % float(v)


def kfmt(n):
    """token 数缩写成 218K / 1.2M，提示文字用"""
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    return ("%dK" % round(n / 1000.0)) if n >= 1000 else str(n)


def _rates(p):
    """一档价格 → 与 DeepSeek 官网价格表同序的三项文字（缓存命中 / 未命中 / 输出）"""
    return "输入 缓存命中¥%s · 未命中¥%s · 输出¥%s" % (
        _fmt(p["hit"]), _fmt(p["miss"]), _fmt(p["out"]))


def hours_text():
    """把配置里的峰谷规则转成给人看的文字（与官网价格表一一对应）"""
    days = sorted(CFG["peak_weekdays"])
    if days == [0, 1, 2, 3, 4]:
        day_txt = "周一至周五"
    elif len(days) == 7:
        day_txt = "每天"
    else:
        day_txt = "、".join(WD[d] for d in days)
    wins = ["%02d:00-%02d:00" % (int(a), int(b)) for a, b in CFG["peak_hours"]]
    pk, op = CFG["prices"]["peak"], CFG["prices"]["offpeak"]
    return {
        "peakDays": day_txt,
        "peakWindows": "、".join(wins),
        "peakText": "%s %s" % (day_txt, "、".join(wins)),
        "offpeakText": "其余时间（含周末全天）",
        "peakRate": _rates(pk),
        "offpeakRate": _rates(op),
        # 兼容旧版浮窗（只显示未命中一档，避免升级期间出现空白）
        "peakRateShort": "输入¥%s/百万（未命中）· 输出¥%s/百万" % (_fmt(pk["miss"]), _fmt(pk["out"])),
        "offpeakRateShort": "输入¥%s/百万（未命中）· 输出¥%s/百万" % (_fmt(op["miss"]), _fmt(op["out"])),
    }


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ)


def sessions_dir_and_files():
    pat = os.path.expanduser(CFG["sessions_glob"]).replace("\\", "/")
    # 取通配符之前的固定部分作为扫描根目录（去掉末尾斜杠）
    root = Path(pat.split("*")[0].rstrip("/")) if "*" in pat else Path(pat)
    if not root.exists():
        return root, []
    return root, sorted(root.rglob("*.jsonl"))


# ---------------- 会话用量扫描（增量） ----------------
class Scanner:
    def __init__(self):
        self.offsets = {}
        self.recs = []
        self.full_scan()

    def files(self):
        return sessions_dir_and_files()[1]

    def full_scan(self):
        self.offsets.clear()
        self.recs = []
        for f in self.files():
            self._scan(f)

    def delta(self):
        for f in self.files():
            self._scan(f)

    def _scan(self, f):
        key = str(f)
        try:
            size = f.stat().st_size
        except OSError:
            return
        off = self.offsets.get(key, 0)
        if size <= off:
            return
        try:
            with open(f, "rb") as fh:
                fh.seek(off)
                data = fh.read()
        except OSError:
            return
        cut = data.rfind(b"\n")
        if cut < 0:
            return
        self.offsets[key] = off + cut + 1
        pk, op = CFG["prices"]["peak"], CFG["prices"]["offpeak"]
        for line in data[: cut + 1].decode("utf-8", "replace").splitlines():
            if '"usage"' not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            msg = j.get("message") or {}
            u = msg.get("usage") or {}
            if not isinstance(u, dict):
                continue
            bj = parse_ts(j.get("timestamp"))
            if bj is None:
                continue
            rec = {
                "bj": bj,
                "input": int(u.get("input") or 0),
                "cacheRead": int(u.get("cacheRead") or 0),
                "output": int(u.get("output") or 0),
                "file": key,
            }
            rec["peak"] = is_peak(bj)          # 这条调用落在高峰还是空闲时段
            p = pk if rec["peak"] else op
            # 成本拆成三块（悬停提示的成本构成条用）：上下文重发 / 模型输出 / 新增输入
            rec["ctx_cost"] = rec["cacheRead"] / 1e6 * float(p["hit"])
            rec["new_cost"] = rec["input"] / 1e6 * float(p["miss"])
            rec["out_cost"] = rec["output"] / 1e6 * float(p["out"])
            rec["cost"] = rec["ctx_cost"] + rec["new_cost"] + rec["out_cost"]
            self.recs.append(rec)

    def summary(self):
        now = datetime.now(BJ)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week0 = day0 - timedelta(days=now.weekday())   # 本周一 00:00（北京时间）
        month0 = day0.replace(day=1)                   # 本月 1 日 00:00
        files = self.files()
        cur, best = None, -1.0
        for f in files:
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best:
                best, cur = m, str(f)
        s = {
            "today": 0.0, "today_calls": 0, "today_in": 0, "today_hit": 0, "today_out": 0,
            "today_peak": 0.0, "today_peak_calls": 0,
            "today_offpeak": 0.0, "today_off_calls": 0,
            "week_peak": 0.0, "week_offpeak": 0.0,
            "month_peak": 0.0, "month_offpeak": 0.0,
            "today_ctx": 0.0, "today_new": 0.0, "today_out_cost": 0.0,
            "session_ctx": 0.0, "ctx_now": 0, "ctx_hint": "", "ctx_pct": 0,
            "session": 0.0, "session_calls": 0, "all": 0.0, "all_calls": 0,
            "sessions": len(files), "peak": is_peak(now),
        }
        for r in self.recs:
            s["all"] += r["cost"]
            s["all_calls"] += 1
            if r["bj"] >= day0:
                s["today"] += r["cost"]
                s["today_calls"] += 1
                s["today_in"] += r["input"]
                s["today_hit"] += r["cacheRead"]
                s["today_out"] += r["output"]
                # 今日花费再按计费时段拆开（悬停提示显示“高峰 / 空闲各花了多少”）
                s["today_ctx"] += r["ctx_cost"]
                s["today_new"] += r["new_cost"]
                s["today_out_cost"] += r["out_cost"]
                if r.get("peak"):
                    s["today_peak"] += r["cost"]
                    s["today_peak_calls"] += 1
                else:
                    s["today_offpeak"] += r["cost"]
                    s["today_off_calls"] += 1
            # 本周 / 本月的峰谷对比（悬停提示里三行并列显示）
            if r["bj"] >= month0:
                if r.get("peak"):
                    s["month_peak"] += r["cost"]
                else:
                    s["month_offpeak"] += r["cost"]
                if r["bj"] >= week0:
                    if r.get("peak"):
                        s["week_peak"] += r["cost"]
                    else:
                        s["week_offpeak"] += r["cost"]
            if cur and r["file"] == cur:
                s["session"] += r["cost"]
                s["session_calls"] += 1
                s["session_ctx"] += r["ctx_cost"]
        # 上下文健康：当前会话最后一次调用的上下文规模 + 压缩建议
        if cur:
            for r in reversed(self.recs):
                if r["file"] == cur:
                    s["ctx_now"] = r["cacheRead"] + r["input"]
                    break
        if s["session"] > 0:
            s["ctx_pct"] = int(round(s["session_ctx"] / s["session"] * 100))
        hint_tokens = int(CFG.get("compact_hint_tokens", 150000))
        if s["ctx_now"] >= hint_tokens:
            s["ctx_hint"] = ("⚡ 当前上下文 %s · 本会话 %d%% 的成本花在重发上下文"
                             " → 建议 /compact（之后每次调用约省一半）"
                             % (kfmt(s["ctx_now"]), s["ctx_pct"]))
        elif s["ctx_now"]:
            s["ctx_hint"] = "⚡ 当前上下文 %s · 健康" % kfmt(s["ctx_now"])
        # 今日最贵的几次调用（定位浪费点）
        today_recs = [r for r in self.recs if r["bj"] >= day0]
        top = sorted(today_recs, key=lambda x: -x["cost"])[:5]
        s["top_calls"] = [{
            "cost": round(r["cost"], 4),
            "time": r["bj"].strftime("%H:%M"),
            "ctx": r["cacheRead"],
            "out": r["output"],
        } for r in top if r["cost"] > 0]
        return s


# ---------------- API Key / 余额 ----------------
def load_api_key():
    """优先环境变量，其次 pi 的 models.json 里 baseUrl 含 deepseek 的 provider"""
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env.strip(), "https://api.deepseek.com"
    try:
        cfg = json.loads(Path(os.path.expanduser(CFG["models_json"])).read_text("utf-8"))
    except (OSError, ValueError):
        return None, "https://api.deepseek.com"
    provs = cfg.get("providers") or {}
    preferred = ("new-provider", "deepseek")
    for name in preferred:
        p = provs.get(name) or {}
        if p.get("apiKey") and "deepseek" in str(p.get("baseUrl", "")):
            return p["apiKey"], p.get("baseUrl") or "https://api.deepseek.com"
    for p in provs.values():
        if p.get("apiKey") and "deepseek" in str(p.get("baseUrl", "")):
            return p["apiKey"], p["baseUrl"]
    return None, "https://api.deepseek.com"


def fetch_balance(key, base, timeout=10):
    req = urllib.request.Request(
        base.rstrip("/") + "/user/balance",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8"))
    want = CFG["currency"]
    infos = j.get("balance_infos") or []
    for i in infos:
        if i.get("currency") == want:
            return float(i.get("total_balance") or 0), want
    if infos:
        return float(infos[0].get("total_balance") or 0), str(infos[0].get("currency") or want)
    return 0.0, want


# ---------------- 状态 ----------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.scanner = Scanner()
        self.sum = self.scanner.summary()
        self.balance = None
        self.balance_at = None
        self.balance_err = ""
        self.key, self.base = load_api_key()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = 0.0
        while True:
            try:
                self.scanner.delta()
                s = self.scanner.summary()
                with self.lock:
                    self.sum = s
            except Exception as exc:  # noqa: BLE001
                log("scan: %r" % exc)
            if time.time() - last > CFG["balance_refresh_seconds"]:
                last = time.time()
                self.refresh_balance()
            time.sleep(CFG["poll_seconds"])

    def refresh_balance(self):
        if not self.key:
            with self.lock:
                self.balance_err = "no-api-key"
            return

        def work():
            try:
                v, _ = fetch_balance(self.key, self.base)
                with self.lock:
                    self.balance, self.balance_at, self.balance_err = v, datetime.now(), ""
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.balance_err = type(exc).__name__
                log("balance: %r" % exc)

        threading.Thread(target=work, daemon=True).start()

    def snapshot(self):
        with self.lock:
            s = dict(self.sum)
            return {
                "ok": True,
                "balance": self.balance,
                "balanceErr": self.balance_err,
                "balanceAt": self.balance_at.strftime("%H:%M") if self.balance_at else None,
                "today": round(s["today"], 6),
                "todayCalls": s["today_calls"],
                "todayIn": s["today_in"],
                "todayHit": s["today_hit"],
                "todayOut": s["today_out"],
                "todayPeak": round(s["today_peak"], 6),
                "todayPeakCalls": s["today_peak_calls"],
                "todayOffpeak": round(s["today_offpeak"], 6),
                "todayOffCalls": s["today_off_calls"],
                "weekPeak": round(s["week_peak"], 6),
                "weekOffpeak": round(s["week_offpeak"], 6),
                "monthPeak": round(s["month_peak"], 6),
                "monthOffpeak": round(s["month_offpeak"], 6),
                "todayCtxCost": round(s["today_ctx"], 6),
                "todayNewCost": round(s["today_new"], 6),
                "todayOutCost": round(s["today_out_cost"], 6),
                "ctxNow": s["ctx_now"],
                "ctxNowText": kfmt(s["ctx_now"]),
                "ctxPct": s["ctx_pct"],
                "ctxHint": s["ctx_hint"],
                "topCalls": s.get("top_calls") or [],
                **switch_text(datetime.now(BJ)),
                "session": round(s["session"], 6),
                "sessionCalls": s["session_calls"],
                "all": round(s["all"], 6),
                "allCalls": s["all_calls"],
                "sessions": s["sessions"],
                "peak": s["peak"],
                "hours": hours_text(),
                "currency": CFG["currency"],
                "ts": int(time.time()),
            }


STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _raw(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/widget.js":
            js = HERE / "pi-web-widget.js"
            text = js.read_text("utf-8") if js.exists() else "/* pi-web-widget.js missing */"
            # 把端口占位符替换成实际端口，前端无需知道配置
            text = text.replace("__PORT__", str(CFG["port"]))
            self._raw(text.encode("utf-8"), "application/javascript; charset=utf-8")
        elif path in ("/state", "/"):
            self._json(STATE.snapshot())
        elif path == "/health":
            self._json({"ok": True, "ts": int(time.time())})
        elif path == "/refresh":
            STATE.refresh_balance()
            self._json({"ok": True, "msg": "refreshing"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_a):
        pass


def main():
    global STATE
    STATE = State()
    port = int(CFG["port"])
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log("端口 %d 无法监听: %r" % (port, exc))
        raise SystemExit("端口 %d 被占用，请改 config.json 里的 port" % port)
    srv.daemon_threads = True
    log("service listening on 127.0.0.1:%d" % port)
    print("DeepSeek 花费数据服务已启动: http://127.0.0.1:%d/state" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
