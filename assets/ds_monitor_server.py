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
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "ds-monitor.log"
BJ = timezone(timedelta(hours=8))

# 法定节假日（全天低谷）。数据源：国务院办公厅《关于 2026 年部分节假日安排的通知》
#   放假期间 = 全天按空闲价；makeup（调休补班日）按你的要求也当作全天低谷
#   —— 补班日本来就落在周末、本就是低谷，列出只是把意图写明白（見 SKILL.md“节假日规则”）。
# 每年国务院公布新一年安排后，把下一年加进来即可（旧年份保留，可查历史）。
HOLIDAYS_2026 = [
    {"name": "元旦", "from": "2026-01-01", "to": "2026-01-03", "makeup": ["2026-01-04"]},
    {"name": "春节", "from": "2026-02-15", "to": "2026-02-23", "makeup": ["2026-02-14", "2026-02-28"]},
    {"name": "清明节", "from": "2026-04-04", "to": "2026-04-06", "makeup": []},
    {"name": "劳动节", "from": "2026-05-01", "to": "2026-05-05", "makeup": ["2026-05-09"]},
    {"name": "端午节", "from": "2026-06-19", "to": "2026-06-21", "makeup": []},
    {"name": "中秋节", "from": "2026-09-25", "to": "2026-09-27", "makeup": []},
    {"name": "国庆节", "from": "2026-10-01", "to": "2026-10-07", "makeup": ["2026-09-20", "2026-10-10"]},
]

DEFAULTS = {
    "port": 59992,
    "currency": "CNY",
    "poll_seconds": 2,
    "balance_refresh_seconds": 60,
    "peak_weekdays": [0, 1, 2, 3, 4],
    "peak_hours": [[9, 12], [14, 18]],
    "holidays": HOLIDAYS_2026,
    "offpeak_days": [],
    "alert_icon": True,        # 命中率低到值得压缩时，浮窗上显示提醒图标（客户端菜单可各自关掉）
    "compact_rule": {
        "_comment": "什么时候建议 /compact：命中率低于 hit_below 且每次调用因未命中的额外支出 ≥ min_waste（元）",
        "hit_below": 0.95,
        "min_waste": 0.02,
        "keep_ratio": 0.25,
        "summary_tokens": 400,
    },
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


def _parse_day(s):
    """'2026-10-01' / '2026-10-01T00:00' → date"""
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _holiday_index():
    """config 的 holidays / offpeak_days → ({日期: 节日名}, 全天低谷日期集合)"""
    names, days = {}, set()
    for h in CFG.get("holidays") or []:
        if not isinstance(h, dict):
            continue
        d0 = _parse_day(h.get("from"))
        d1 = _parse_day(h.get("to")) or d0
        if not d0:
            continue
        nm = str(h.get("name") or "节假日")
        cur = d0
        while cur <= d1:
            names[cur] = nm
            days.add(cur)
            cur += timedelta(days=1)
        for mk in h.get("makeup") or []:      # 调休补班日：同样按全天低谷
            d = _parse_day(mk)
            if d:
                names.setdefault(d, nm + "调休")
                days.add(d)
    for s in CFG.get("offpeak_days") or []:    # 额外手动指定的全天低谷日
        d = _parse_day(s)
        if d:
            names.setdefault(d, "指定低谷日")
            days.add(d)
    return names, days


HOLIDAY_NAME, OFFPEAK_DAYS = _holiday_index()


def _day(d):
    return d.date() if isinstance(d, datetime) else d


def offpeak_allday(d):
    """该日是否全天低谷（法定节假日放假期间 / 调休补班日 / 手动指定日）"""
    return _day(d) in OFFPEAK_DAYS


def holiday_name(d):
    """该日的节日名（非节假日返回空串）"""
    return HOLIDAY_NAME.get(_day(d), "")


def holiday_max_year():
    """已收录的节假日年份（用于提醒该更新了）"""
    yrs = [_day(d).year for d in OFFPEAK_DAYS]
    return max(yrs) if yrs else 0


# 只收“放假期间”的日期（不含 makeup 补班日）—— 用于“假期最后一天”这类播报
HOLIDAY_ONLY = set()
for _h in CFG.get("holidays") or []:
    if isinstance(_h, dict):
        _d0, _d1 = _parse_day(_h.get("from")), _parse_day(_h.get("to")) or _parse_day(_h.get("from"))
        _c = _d0
        while _d0 and _c <= _d1:
            HOLIDAY_ONLY.add(_c)
            _c += timedelta(days=1)


def holiday_short(d):
    """小窗标识用的短名：国庆节→国庆（亚 2 字的“春节”“元旦”不动）；调休日直接显示“调休”"""
    raw = holiday_name(d)
    if raw.endswith("调休"):
        return "调休"
    return raw[:-1] if raw.endswith("节") and len(raw) > 2 else raw


def holiday_alerts(now):
    """假期提醒（悬停提示用）：明天放假 / 今天是假期最后一天

    只在“真的会变便宜/变贵”时才报：
      · 假期前：明天是放假首日**且明天本来是工作日**（周一到周五）→ 否则周末本就闲时，提了是噪声
      · 假期末：今天是放假期间，明天不再全天闲时 → 提醒高峰要回来了
    """
    today, tm = _day(now), _day(now) + timedelta(days=1)
    out = {"holidayEve": "", "holidayEveText": "", "holidayLastText": "", "holidayShort": holiday_short(now),
           "holidayTodayText": ""}
    _nm = holiday_name(now)
    if _nm:
        _base = _nm[:-2] if _nm.endswith("调休") else _nm.replace("调休", "")
        out["holidayTodayText"] = (("📅 今天为【%s调休日】（补班）—— 这天也按全天空闲价" % _base) if _nm.endswith("调休")
                                   else ("📅 今天是【%s】—— 法定节假日全天按空闲价" % _base))
    if tm in OFFPEAK_DAYS and today not in OFFPEAK_DAYS and tm.weekday() < 5:
        out["holidayEve"] = holiday_name(tm)
        out["holidayEveText"] = ("🎉 明天起【%s】放假，全天闲时 —— 大任务建议留到明早跑"
                                 % holiday_name(tm).replace("调休", ""))
    if today in HOLIDAY_ONLY and tm not in OFFPEAK_DAYS:
        out["holidayLastText"] = ("⏰ 假期最后一天：明天（%s）起恢复高峰价（09:00-12:00、14:00-18:00）"
                                  % tm.strftime("%m-%d"))
    return out


def hit_rate(hit, miss):
    """缓存命中率（%）：cacheRead / (cacheRead + 未命中 input)；无输入时返回 None"""
    hit, miss = int(hit or 0), int(miss or 0)
    if hit + miss <= 0:
        return None
    return round(100.0 * hit / (hit + miss), 1)


def compact_advice(ctx_now, hit_pct, peak_now):
    """该不该 /compact？—— 用“每次重发省下的钱”跟“压缩的一次性成本”做盈亏平衡

    为什么不能只看上下文大小，也不能只看命中率：
      重发成本 = C × [h×p_hit + (1−h)×p_miss]，两条腿各算一半 —— 所以要看 C×(1−h) 这个乘积。
      而压缩的一次性成本 ≈ 压缩后上下文重建缓存（keep_ratio×C×p_miss）+ 摘要输出 —— 实测摘要只有几百 token。
    默认阈值（写到 config.json 的 compact_rule）：命中率 < 95% 且每次因未命中多花 ≥¥0.02。
    空闲价下，C=400K & h=95% 回本需 4.4 次；C=150K & h=90% 需 2.9 次；C=400K & h=99.9% 需 17.5 次（不值得）。
    """
    r = CFG.get("compact_rule") or {}
    keep = float(r.get("keep_ratio", 0.25))
    S = float(r.get("summary_tokens", 400))
    hit_below = float(r.get("hit_below", 0.95))
    min_waste = float(r.get("min_waste", 0.02))
    p = CFG["prices"]["peak"] if peak_now else CFG["prices"]["offpeak"]
    ph, pm, po = float(p["hit"]), float(p["miss"]), float(p["out"])
    C = int(ctx_now or 0)
    h = None if hit_pct is None else max(0.0, min(1.0, hit_pct / 100.0))
    out = {"action": "unknown", "text": "", "breakEven": None,
           "waste": None, "oneTime": None, "hitPct": hit_pct, "ctx": C}
    if C <= 0 or h is None:
        return out
    resend = C / 1e6 * (h * ph + (1 - h) * pm)          # 下一次重发这个上下文的成本
    waste = C / 1e6 * (1 - h) * (pm - ph)               # 其中因未命中多花的部分
    one = keep * C / 1e6 * pm + S / 1e6 * po            # 压缩的一次性成本
    save = max(1e-9, resend - keep * C / 1e6 * ph)      # 压缩后每次省多少
    n = one / save
    out.update({"waste": round(waste, 4), "oneTime": round(one, 4),
                "breakEven": round(n, 1), "resend": round(resend, 4)})
    if h < hit_below and waste >= min_waste:
        out["action"] = "compact"
        ret = "1 次就回本" if n < 1 else ("%d 次内回本" % round(n))
        out["text"] = ("✂️ 建议压缩：命中率 %.0f%%（偏低）→ 每次因未命中多花 ¥%.3f；压缩一次 ¥%.3f，%s"
                       % (h * 100, waste, one, ret))
    else:
        out["action"] = "keep"
        if h >= hit_below:
            out["text"] = ("✅ 不必压缩：命中率 %.0f%% 已经很高 —— 重发每次 ¥%.3f；压缩 ¥%.3f，要 %d 次才回本"
                           % (h * 100, resend, one, int(n) + 1))
        else:
            out["text"] = ("✅ 不必压缩：上下文只有 %s，即使命中率 %.0f%% 每次也只多花 ¥%.3f，不值得折腾"
                           % (kfmt(C), h * 100, waste))
    return out


def miss_reason(r):
    """未命中归因 → (key, 可读文案)。按可信度从高到低；判定只看“同一会话上一条调用”

    key 用于统计与对策映射，文案用于显示。
    """
    if r.get("first"):
        return "first", "会话首次调用（缓存未建立）"
    if r.get("model_changed"):
        return "model", "换了模型（%s → %s，缓存不跨模型）" % (r.get("prev_model") or "?", r.get("model") or "?")
    cb, cn = r.get("ctx_before"), int(r.get("ctx_now") or 0)
    if cb and cn < cb * 0.7:
        return "compact", "上下文刚被压缩（compact）"
    g = r.get("gap_min")
    if g is None or g >= 15:
        if g is None:
            when = "较久"
        elif g >= 120:
            when = "%.1f 小时" % (g / 60.0)
        else:
            when = "%g 分钟" % g
        return "gap", "停顿 %s后缓存过期" % when
    if int(r.get("cacheRead") or 0) < 1000 and int(r.get("input") or 0) >= 20000:
        return "prefix", "前缀整体变化（系统提示/工具定义可能改过）"
    return "rewrite", "上下文被改写，前缀失效"


# 按主因给的“对策”——只说能执行的动作，不说空话
REASON_ADVICE = {
    "first": "长任务尽量在同一会话里跑完；频繁开新会话 = 每次都从零建缓存（首次全按未命中计费）",
    "model": "同一段时间别来回换模型（缓存不跨模型），换一次就得重建整个前缀",
    "compact": "少触发 /compact：压缩只能省之后的输出，而触发它的那一次会把整段上下文按未命中重发；宁可分段、及时收尾",
    "gap": "隔得久（≥15 分钟）再接着旧会话 = 旧上下文全重发；这种情况开新会话反而更划算",
    "prefix": "别频繁改系统提示 / 工具定义（MCP、模型配置、技能），一改当轮对话的缓存就整体失效",
    "rewrite": "避免在会话中间插入大段文件内容或工具输出（会把前缀顶掉），先看清楚再动手",
}


TREND_FILE = HERE / "ds-hit-trend.jsonl"
_TREND_LAST_WRITE = [0.0]


def _load_trend():
    """每日命中率历史：{"2026-10-01": {"hit":…, "miss":…, "rate":…}}"""
    out = {}
    try:
        if TREND_FILE.exists():
            for line in TREND_FILE.read_text("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                j = json.loads(line)
                out[str(j.get("d"))] = {"hit": int(j.get("hit") or 0),
                                        "miss": int(j.get("miss") or 0),
                                        "rate": j.get("rate"),
                                        "cost": j.get("cost")}
    except (OSError, ValueError):
        pass
    return out


TREND = _load_trend()


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def is_peak(dt):
    """dt 为北京时间；高峰 = 配置的星期 + 时段；法定节假日（含调休）全天低谷"""
    if offpeak_allday(dt):
        return False
    if dt.weekday() not in CFG["peak_weekdays"]:
        return False
    h = dt.hour + dt.minute / 60.0
    for a, b in CFG["peak_hours"]:
        if a <= h < b:
            return True
    return False


def next_switch(now):
    """下一个计费时段切换点 → (切换时刻, 切换后是否高峰)；找不到则 None

    节假日整日低谷：落在节假日内的“高峰起点/终点”不再算切换点，
    所以节假日里给出的是节后第一个真正的高峰起点。
    """
    weekdays = set(CFG["peak_weekdays"])
    cands = []
    for d in range(0, 40):                      # 40 天窗口，足够跨过最长假期（春节 9 天）
        day = now + timedelta(days=d)
        if day.weekday() not in weekdays or offpeak_allday(day):
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
    year = datetime.now(BJ).year
    yrs = sorted({_day(d).year for d in OFFPEAK_DAYS}) or [year]
    stale = holiday_max_year() < year          # 收录的节假日表比当前年份旧 → 提醒更新
    return {
        "peakDays": day_txt,
        "peakWindows": "、".join(wins),
        "peakText": "%s %s" % (day_txt, "、".join(wins)),
        "offpeakText": "其余时间（含周末、法定节假日全天）",
        # 悬停里显示三档单价（v1.10.0：之前只在右键菜单里，与文档描述不一致）
        "priceLine": "单价（元/百万，命中/未命中/输出）：空闲 %s/%s/%s · 高峰 %s/%s/%s"
                     % (_fmt(op["hit"]), _fmt(op["miss"]), _fmt(op["out"]),
                        _fmt(pk["hit"]), _fmt(pk["miss"]), _fmt(pk["out"])),
        "holidayToday": holiday_name(datetime.now(BJ)),
        "holidayNote": "法定节假日放假期间（含调休日）全天按空闲价",
        "holidayYears": yrs,
        "holidayStale": bool(stale),
        "holidayStaleHint": ("⚠️ 节假日表只到 %d 年，请更新 config.json 的 holidays" % max(yrs)) if stale else "",
        **holiday_alerts(datetime.now(BJ)),
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
        self.prev = {}          # key → 同会话上一条调用（归因用）
        self.recs = []
        self.full_scan()

    def files(self):
        return sessions_dir_and_files()[1]

    def full_scan(self):
        self.offsets.clear()
        self.prev.clear()
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
                "model": msg.get("model") or "",
                "provider": msg.get("provider") or "",
            }
            # 与同一会话上一条调用对比（未命中归因的原材料）
            pr = self.prev.get(key)
            rec["ctx_now"] = rec["cacheRead"] + rec["input"]
            rec["first"] = pr is None
            if pr is not None:
                rec["gap_min"] = round((bj - pr["bj"]).total_seconds() / 60.0, 1)
                rec["ctx_before"] = pr["cacheRead"] + pr["input"]
                rec["prev_model"] = pr.get("model") or ""
                rec["model_changed"] = bool(rec["prev_model"] and rec["model"]
                                            and rec["prev_model"] != rec["model"])
            else:
                rec["gap_min"] = None
                rec["ctx_before"] = None
                rec["prev_model"] = ""
                rec["model_changed"] = False
            self.prev[key] = rec
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
        pk, op = CFG["prices"]["peak"], CFG["prices"]["offpeak"]   # 未命中归因与“潜在节省”要用单价
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
            "today_peak_hit": 0, "today_peak_in": 0, "today_off_hit": 0, "today_off_in": 0,
            "week_peak": 0.0, "week_offpeak": 0.0,
            "week_hit": 0, "week_in": 0, "month_hit": 0, "month_in": 0,
            "month_peak": 0.0, "month_offpeak": 0.0,
            "today_ctx": 0.0, "today_new": 0.0, "today_out_cost": 0.0,
            "today_save": 0.0,                      # “若未命中都吃到缓存”能省的钱
            "today_miss_reasons": {},               # 归因计数
            "session_ctx": 0.0, "ctx_now": 0, "ctx_hint": "", "ctx_pct": 0,
            "session": 0.0, "session_calls": 0, "all": 0.0, "all_calls": 0,
            "last_hit": 0, "last_in": 0,        # 当前会话最近几次调用的命中/未命中（算压缩建议用）
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
                # 未命中归因 + 潜在节省（同一 token 若从缓存读只需 hit 价）
                _p = pk if r.get("peak") else op
                s["today_save"] += r["input"] / 1e6 * (float(_p["miss"]) - float(_p["hit"]))
                _hr = hit_rate(r["cacheRead"], r["input"])
                if _hr is not None and _hr < 50 and r["cacheRead"] + r["input"] > 10000:
                    _key, _why = miss_reason(r)
                    s["today_miss_reasons"][_key] = s["today_miss_reasons"].get(_key, 0) + 1
                if r.get("peak"):
                    s["today_peak"] += r["cost"]
                    s["today_peak_calls"] += 1
                    s["today_peak_hit"] += r["cacheRead"]
                    s["today_peak_in"] += r["input"]
                else:
                    s["today_offpeak"] += r["cost"]
                    s["today_off_calls"] += 1
                    s["today_off_hit"] += r["cacheRead"]
                    s["today_off_in"] += r["input"]
            # 本周 / 本月的峰谷对比（悬停提示里三行并列显示）
            if r["bj"] >= month0:
                if r.get("peak"):
                    s["month_peak"] += r["cost"]
                else:
                    s["month_offpeak"] += r["cost"]
                s["month_hit"] += r["cacheRead"]
                s["month_in"] += r["input"]
                if r["bj"] >= week0:
                    if r.get("peak"):
                        s["week_peak"] += r["cost"]
                    else:
                        s["week_offpeak"] += r["cost"]
                    s["week_hit"] += r["cacheRead"]
                    s["week_in"] += r["input"]
            if cur and r["file"] == cur:
                s["session"] += r["cost"]
                s["session_calls"] += 1
                s["session_ctx"] += r["ctx_cost"]
        # 上下文健康：当前会话最后一次调用的上下文规模 + 压缩建议
        if cur:
            # 当前会话的命中率取“最近几次调用的聚合”——比只看最后一次稳：
            # 会话第一次调用总是 0% 命中，但它不代表之后的状态（之后前缀就缓存住了）。
            mine = [r for r in self.recs if r["file"] == cur]
            if mine:
                s["ctx_now"] = mine[-1]["cacheRead"] + mine[-1]["input"]
                tail = mine[-5:] if len(mine) >= 3 else mine
                s["last_hit"] = sum(x["cacheRead"] for x in tail)
                s["last_in"] = sum(x["input"] for x in tail)
        if s["session"] > 0:
            s["ctx_pct"] = int(round(s["session_ctx"] / s["session"] * 100))
        hint_tokens = int(CFG.get("compact_hint_tokens", 150000))
        if s["ctx_now"] >= hint_tokens:
            s["ctx_hint"] = ("⚡ 当前上下文 %s · 本会话 %d%% 的成本花在重发上下文"
                             % (kfmt(s["ctx_now"]), s["ctx_pct"]))
        elif s["ctx_now"]:
            s["ctx_hint"] = "⚡ 当前上下文 %s · 健康" % kfmt(s["ctx_now"])

        # 省钱建议：把“未命中重发”的钱和“如果都命中缓存”的差额算出来，并按主因给对策
        s["saving_tip"] = ""
        _miss_cost = s["today_new"]
        if s["today_save"] >= 0.05:
            _tip = ("💡 今天未命中重发 ¥%.2f，若都吃到缓存约 ¥%.2f（可省 ¥%.2f）"
                    % (_miss_cost, _miss_cost - s["today_save"], s["today_save"]))
            if s["today_miss_reasons"]:
                _key, _n = max(s["today_miss_reasons"].items(), key=lambda kv: kv[1])
                _names = {"first": "会话首次调用", "model": "换模型", "compact": "刚被压缩",
                          "gap": "停顿过久缓存过期", "prefix": "前缀整体变化", "rewrite": "上下文被改写"}
                _tip += "　主因：%s（%d 次）" % (_names.get(_key, _key), _n)
            s["saving_tip"] = _tip

        # 每日归档（TREND -> ds-hit-trend.jsonl）：只做历史留档，不再对外输出（趋势视图已删）
        by_day = {}
        d14 = day0 - timedelta(days=13)
        for r in self.recs:
            if r["bj"] >= d14:
                k = r["bj"].date()
                a = by_day.setdefault(k, [0, 0, 0.0])
                a[0] += r["cacheRead"]
                a[1] += r["input"]
                a[2] += r["cost"]
        for d, (h, m, c) in by_day.items():
            TREND[d.isoformat()] = {"hit": h, "miss": m, "rate": hit_rate(h, m), "cost": round(c, 4)}
        _nowts = time.time()
        if _nowts - _TREND_LAST_WRITE[0] > 60:
            _TREND_LAST_WRITE[0] = _nowts
            try:
                TREND_FILE.write_text("\n".join(
                    json.dumps({"d": k, "hit": v["hit"], "miss": v["miss"], "rate": v.get("rate"),
                                "cost": v.get("cost")}, ensure_ascii=False)
                    for k, v in sorted(TREND.items())), "utf-8")
            except OSError:
                pass
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


# 注意：DeepSeek 在国内可直连，而国内常见的代理工具（Clash 等）常设系统代理为 127.0.0.1:7890。
# Python 的 urllib 会自动读系统代理，代理未启动时就全变成 ConnectionRefused（10061），
# 导致浮窗余额永远为空。这里显式忽略系统代理，直连官方接口。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_balance(key, base, timeout=10):
    req = urllib.request.Request(
        base.rstrip("/") + "/user/balance",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
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
        now = datetime.now(BJ)
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
                "todayPeakHitRate": hit_rate(s["today_peak_hit"], s["today_peak_in"]),
                "todayOffpeak": round(s["today_offpeak"], 6),
                "todayOffCalls": s["today_off_calls"],
                "todayOffpeakHitRate": hit_rate(s["today_off_hit"], s["today_off_in"]),
                "todayHitRate": hit_rate(s["today_hit"], s["today_in"]),
                "todayMissCost": round(s["today_new"], 4),
                "todayMissSaving": round(s["today_save"], 4),
                "savingTip": s.get("saving_tip") or "",
                "compactAdvice": (compact_advice(s.get("ctx_now"), hit_rate(s.get("last_hit"), s.get("last_in")),
                                                 s.get("peak"))),
                "showAlertIcon": bool(CFG.get("alert_icon", True)),
                "weekPeak": round(s["week_peak"], 6),
                "weekOffpeak": round(s["week_offpeak"], 6),
                "weekHitRate": hit_rate(s["week_hit"], s["week_in"]),
                "monthPeak": round(s["month_peak"], 6),
                "monthOffpeak": round(s["month_offpeak"], 6),
                "monthHitRate": hit_rate(s["month_hit"], s["month_in"]),
                "todayCtxCost": round(s["today_ctx"], 6),
                "todayNewCost": round(s["today_new"], 6),
                "todayOutCost": round(s["today_out_cost"], 6),
                "ctxNow": s["ctx_now"],
                "ctxNowText": kfmt(s["ctx_now"]),
                "ctxPct": s["ctx_pct"],
                "ctxHint": s["ctx_hint"],
                **switch_text(now),
                "session": round(s["session"], 6),
                "sessionCalls": s["session_calls"],
                "all": round(s["all"], 6),
                "allCalls": s["all_calls"],
                "sessions": s["sessions"],
                "peak": s["peak"],
                "holidayToday": holiday_name(now),
                "holidayOff": offpeak_allday(now),
                **holiday_alerts(now),          # holidayShort / holidayEveText / holidayLastText
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
            # 出异常也要回 JSON：否则客户端只看到连接被掐断（RemoteDisconnected），无从排查
            try:
                self._json(STATE.snapshot())
            except Exception as exc:  # noqa: BLE001
                log("snapshot failed: %r" % exc)
                self._json({"ok": False, "error": type(exc).__name__, "message": str(exc)[:200]}, 500)
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
