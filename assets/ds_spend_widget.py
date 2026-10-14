#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 花费 · 桌面悬浮窗（数据服务的客户端）
=============================================
不用 pi-web 也能用：一个无边框、置顶、可拖动的小窗口，
显示「剩 / 今日 / 总」，数据来自本地数据服务 127.0.0.1:<port>/state。

用法：
    python ds_spend_widget.py            # 需要数据服务在运行
    python ds_spend_widget.py --port 59992

操作：拖动移动 · 双击切换紧凑/详细 · 右键菜单（刷新/置顶/退出）
末尾还有一个「峰 / 谷」时段标识：鼠标移上去显示空闲/高峰两段的完整三档单价
（输入缓存命中 / 未命中 / 输出，与 DeepSeek 官网价格表同序）
位置会记住在同目录 widget-state.json
"""

import argparse
import json
import sys
import threading
import tkinter as tk
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "widget-state.json"

BG = "#10151b"
BORDER = "#2b3644"
FG = "#e9eef5"
DIM = "#8a97a6"
GREEN = "#48d597"
ACCENT = "#5aa9ff"
FONT = "Microsoft YaHei UI"


def money(v):
    if v is None:
        return None
    v = float(v)
    if v >= 1000:
        return "%.0f" % v
    if v >= 100:
        return "%.1f" % v
    if v >= 1:
        return "%.2f" % v
    if v >= 0.01:
        return "%.3f" % v
    return "%.4f" % v


class Widget:
    def __init__(self, port):
        self.url = "http://127.0.0.1:%d/state" % port
        self.state = {}
        try:
            self.state = json.loads(STATE_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            pass
        self.drag = None
        self.compact = bool(self.state.get("compact", False))
        self.locked = bool(self.state.get("locked", False))
        self._tipw = None            # 「峰 / 谷」悬停提示的窗口
        self.peak_tip_text = ""
        self.last_data = None        # 最近一次 /state 数据（右键菜单查计费规则用）
        self._build()

    def _build(self):
        r = tk.Tk()
        self.root = r
        r.overrideredirect(True)
        r.configure(bg=BORDER)
        try:
            r.attributes("-topmost", bool(self.state.get("topmost", True)))
            r.attributes("-alpha", 0.96)
        except tk.TclError:
            pass

        box = tk.Frame(r, bg=BG, padx=10, pady=6)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        self.box = box

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")
        self.cells = {}
        for i, (label, key, color) in enumerate(
            [("剩", "balance", GREEN), ("今日", "today", ACCENT), ("总", "all", FG)]
        ):
            if i:
                tk.Frame(row, bg=BORDER, width=1).pack(side="left", fill="y", padx=9)
            cell = tk.Frame(row, bg=BG)
            cell.pack(side="left")
            tk.Label(cell, text=label, bg=BG, fg=DIM, font=(FONT, 8)).pack(side="left", padx=(0, 3))
            v = tk.Label(cell, text="—", bg=BG, fg=color, font=(FONT, 14, "bold"))
            v.pack(side="left")
            self.cells[key] = v

        # 末尾的「峰 / 谷」时段标识（鼠标移上去看三档单价）
        tk.Frame(row, bg=BORDER, width=1).pack(side="left", fill="y", padx=9)
        self.peak = tk.Label(row, text="—", bg=BG, fg=DIM,
                             font=(FONT, 9, "bold"), cursor="hand2")
        self.peak.pack(side="left")
        self.peak.bind("<Enter>", self._show_peak_tip)
        self.peak.bind("<Leave>", self._hide_peak_tip)

        # 压缩提醒图标（v1.9.0）：命中率低到值得压缩时才出现；点一下看判据与金额
        self.alert = tk.Label(row, text="", bg=BG, fg="#e0a34a",
                              font=(FONT, 10, "bold"), cursor="hand2")
        self.alert.pack(side="left", padx=(6, 0))
        self.alert.bind("<Button-1>", lambda _e: self._show_alert())

        self.tip = tk.Label(box, text="", bg=BG, fg=DIM, font=(FONT, 7))
        self.tip.pack(anchor="w", pady=(3, 0))

        for w in (box, row, self.tip, self.peak):
            w.bind("<Button-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", self.drag_end)
        r.bind("<Double-Button-1>", lambda e: self.toggle_compact())
        r.bind("<Button-3>", self.menu)

        self.lock_var = tk.BooleanVar(value=self.locked)
        self.icon_var = tk.BooleanVar(value=bool(self.state.get("alert_icon", True)))
        self.menu_w = tk.Menu(r, tearoff=0)
        self.menu_w.add_command(label="立即刷新", command=self.fetch)
        self.menu_w.add_command(label="切换紧凑/详细", command=self.toggle_compact)
        self.menu_w.add_command(label="置顶开关", command=self.toggle_topmost)
        self.menu_w.add_command(label="📋 计费规则与单价", command=self.show_rules)
        self.menu_w.add_command(label="🧩 今日成本构成", command=self.show_cost)
        self.menu_w.add_checkbutton(label="锁定位置（不可拖动）", variable=self.lock_var,
                                    command=self.toggle_lock)
        self.menu_w.add_checkbutton(label="显示压缩提醒图标", variable=self.icon_var,
                                    command=self.toggle_icon)
        self.menu_w.add_separator()
        self.menu_w.add_command(label="退出", command=self.quit)

        self.apply_compact()
        r.geometry("+%d+%d" % (int(self.state.get("x", 40)), int(self.state.get("y", 150))))
        self.fetch()
        self.tick()

    def apply_compact(self):
        if self.compact:
            self.tip.pack_forget()
        else:
            self.tip.pack(anchor="w", pady=(3, 0))

    def toggle_compact(self):
        self.compact = not self.compact
        if self.compact:
            self.tip.pack_forget()
        else:
            self.tip.pack(anchor="w", pady=(3, 0))
        self.save()

    def toggle_topmost(self):
        cur = bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", not cur)
        self.save()

    def toggle_lock(self):
        self.locked = bool(self.lock_var.get())
        self.tip.configure(text=("位置已锁定 · 右键解锁" if self.locked else ""))
        self.save()

    def drag_start(self, e):
        if self.locked:
            return
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.drag:
            self.root.geometry("+%d+%d" % (e.x_root - self.drag[0], e.y_root - self.drag[1]))

    def drag_end(self, _e):
        self.drag = None
        self.save()

    # ---------- 「峰 / 谷」悬停提示 ----------
    def _show_peak_tip(self, _e=None):
        if self._tipw is not None or not self.peak_tip_text:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.configure(bg=BORDER)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(w, text=self.peak_tip_text, bg=BG, fg=FG, font=(FONT, 9),
                 justify="left", wraplength=620).pack(padx=11, pady=9)
        # 提示框放到浮窗下方；贴屏幕底边时改放到上方
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        x = min(self.root.winfo_rootx() + self.peak.winfo_x(), max(0, sw - 680))
        y = self.root.winfo_rooty() + self.root.winfo_height() + 6
        if y + w.winfo_reqheight() > sh - 40:
            y = max(0, self.root.winfo_rooty() - w.winfo_reqheight() - 6)
        w.geometry("+%d+%d" % (x, y))
        self._tipw = w

    def _hide_peak_tip(self, _e=None):
        if self._tipw is not None:
            try:
                self._tipw.destroy()
            except tk.TclError:
                pass
            self._tipw = None

    def _popup(self, text):
        """点一下就关的信息框（右键菜单里的「计费规则」用）"""
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.configure(bg=BORDER)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        lb = tk.Label(w, text=text, bg=BG, fg=FG, font=(FONT, 9),
                      justify="left", wraplength=560)
        lb.pack(padx=12, pady=10)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        x = min(self.root.winfo_rootx() + 20, max(0, sw - 620))
        y = self.root.winfo_rooty() + self.root.winfo_height() + 6
        if y + w.winfo_reqheight() > sh - 40:
            y = max(0, self.root.winfo_rooty() - w.winfo_reqheight() - 6)
        w.geometry("+%d+%d" % (x, y))
        w.bind("<Button-1>", lambda _e: w.destroy())
        lb.bind("<Button-1>", lambda _e: w.destroy())
        self.root.after(20000, lambda: w.winfo_exists() and w.destroy())

    def _show_alert(self):
        """点图标：为什么要压缩、能省多少（判据自己跑出来的结论）"""
        ca = (self.last_data or {}).get("compactAdvice") or {}
        if not ca.get("text"):
            self._popup("暂无判据数据（等数据服务返回）")
            return
        lines = [ca["text"], ""]
        lines.append("上下文 %s · 命中率 %s%%" % (self._ctx_text((self.last_data or {}).get("ctxNowText")),
                                                 ("%.0f" % ca["hitPct"]) if ca.get("hitPct") is not None else "—"))
        if ca.get("waste") is not None:
            lines.append("每次因未命中多花 ¥%.3f · 重发一次 ¥%.3f" % (ca["waste"], ca.get("resend") or 0))
        if ca.get("oneTime") is not None:
            lines.append("压缩一次性成本 ¥%.3f（重建缓存 + 摘要）" % ca["oneTime"])
        lines += ["", "判据：C×(1−h)×价差  vs  压缩一次性成本", "阈值可在 config.json 的 compact_rule 里改",
                  "", "（点一下关闭）"]
        self._popup("\n".join(lines))

    @staticmethod
    def _ctx_text(s):
        return s or "—"

    def toggle_icon(self):
        self.state["alert_icon"] = bool(self.icon_var.get())
        self.save()
        if self.last_data:
            self._render_peak(self.last_data)

    def show_cost(self):
        """右键菜单：今日成本构成 —— 环形饼图 + 图例（三块金额/占比/单价说明）"""
        d = self.last_data or {}
        c = float(d.get("todayCtxCost") or 0)
        o = float(d.get("todayOutCost") or 0)
        n = float(d.get("todayNewCost") or 0)
        tot = c + o + n
        if not tot:
            self._popup("今日暂无花费记录")
            return
        parts = [("重发上下文（命中）", c, GREEN), ("模型输出", o, "#d9a441"),
                 ("新增输入（未命中）", n, "#e06c6c")]
        W, H = 446, 214
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.configure(bg=BORDER)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        cv = tk.Canvas(w, width=W, height=H, bg=BG, highlightthickness=0)
        cv.pack(padx=1, pady=1)
        SZ = 132
        x0, y0, x1, y1 = 14, 42, 14 + SZ, 42 + SZ
        cv.create_text(14, 12, anchor="nw", fill=FG, font=(FONT, 9, "bold"),
                       text="今日成本构成（点一下关闭）")
        start = 90.0
        for _name, v, col in parts:
            ext = -360.0 * v / tot
            cv.create_arc(x0, y0, x1, y1, start=start, extent=ext,
                          fill=col, outline=BG, width=1)
            start += ext
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        cv.create_oval(cx - 34, cy - 34, cx + 34, cy + 34, fill=BG, outline=BG)
        cv.create_text(cx, cy - 8, fill=FG, font=(FONT, 12, "bold"), text="¥%s" % money(tot))
        cv.create_text(cx, cy + 10, fill=DIM, font=(FONT, 7), text="今日合计")
        lx, ly = 176, 44
        for name, v, col in parts:
            cv.create_rectangle(lx, ly + 2, lx + 11, ly + 13, fill=col, outline="")
            cv.create_text(lx + 17, ly, anchor="nw", fill=FG, font=(FONT, 8),
                           text="%s" % name)
            cv.create_text(lx + 17 + 132, ly, anchor="nw", fill=FG, font=(FONT, 8),
                           text="¥%-8s %2.0f%%" % (money(v), 100.0 * v / tot))
            ly += 24
        cv.create_text(lx, ly + 6, anchor="nw", fill=DIM, font=(FONT, 8),
                       text="“新增输入”= 没吃到缓存、按未命中单价计费的部分")
        cv.create_text(lx, ly + 22, anchor="nw", fill=DIM, font=(FONT, 8),
                       text="其单价是命中的 50×（空闲 1 vs 0.02 元/百万）")
        cv.create_text(lx, ly + 38, anchor="nw", fill=DIM, font=(FONT, 8),
                       text="占比高时说明上下文一直在按未命中价重发")
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        px = min(self.root.winfo_rootx() + 20, max(0, sw - W - 20))
        py = self.root.winfo_rooty() + self.root.winfo_height() + 6
        if py + H > sh - 40:
            py = max(0, self.root.winfo_rooty() - H - 6)
        w.geometry("+%d+%d" % (px, py))
        w.bind("<Button-1>", lambda _e: w.destroy())
        cv.bind("<Button-1>", lambda _e: w.destroy())
        self.root.after(30000, lambda: w.winfo_exists() and w.destroy())


    def show_rules(self):
        """右键菜单：查看计费规则与单价（这些从悬停提示里移出来了，日常不干扰）"""
        d = self.last_data or {}
        h = d.get("hours") or {}
        self._popup("\n".join([
            "计费规则（改 config.json 可换模型价格）",
            "",
            "空闲时段：" + (h.get("offpeakRate") or h.get("offpeakRateShort") or "-"),
            "高峰时段：" + (h.get("peakRate") or h.get("peakRateShort") or "-"),
            "单价单位：元 / 百万 tokens",
            "",
            "高峰：" + (h.get("peakText") or "-") + "（北京时间）",
            "空闲：" + (h.get("offpeakText") or "-"),
            "",
            "（点一下关闭）",
        ]))

    def menu(self, e):
        try:
            self.menu_w.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu_w.grab_release()

    def save(self):
        st = {
            "x": self.root.winfo_x(), "y": self.root.winfo_y(),
            "compact": self.compact, "topmost": bool(self.root.attributes("-topmost")),
            "locked": self.locked,
        }
        try:
            STATE_FILE.write_text(json.dumps(st, indent=2), "utf-8")
        except OSError:
            pass

    def quit(self):
        self.save()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def fetch(self):
        def work():
            try:
                with urllib.request.urlopen(self.url, timeout=5) as r:
                    d = json.loads(r.read().decode("utf-8"))
                self.root.after(0, lambda: self.render(d, None))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: self.render(None, type(exc).__name__))

        threading.Thread(target=work, daemon=True).start()

    def render(self, d, err):
        if d is None:
            for v in self.cells.values():
                v.configure(text="—")
            self.peak.configure(text="—", fg=DIM)
            self.peak_tip_text = ""
            self._hide_peak_tip()
            self.tip.configure(text="数据服务未运行 · 请先 python install.py start")
            return
        full = lambda v: ("¥" + money(v)) if v is not None else "—"  # noqa: E731
        self.last_data = d
        self.cells["balance"].configure(text=full(d.get("balance")))
        self.cells["today"].configure(text=full(d.get("today")))
        self.cells["all"].configure(text=full(d.get("all")))
        self._render_peak(d)
        sw = d.get("nextSwitchShort") or ""
        self.tip.configure(
            text="本会话 ¥%s%s · 今日 %d 次 · %s · %s"
                 % (money(d.get("session")) or "-",
                    ("  ·  " + sw) if sw else "",
                    d.get("todayCalls") or 0,
                    "高峰价" if d.get("peak") else "空闲价", d.get("balanceAt") or "-"))

    def _render_peak(self, d):
        """「峰 / 谷」标识 + 悬停提示（时段、三档单价、峰谷对比、成本构成、倒计时、压缩建议）"""
        peak = bool(d.get("peak"))
        h = d.get("hours") or {}
        hn = d.get("holidayToday") or ""      # 节假日名（v1.4.0）

        def kfmt(n):
            n = int(n or 0)
            return ("%dK" % round(n / 1000.0)) if n >= 1000 else str(n)

        def costbar():
            c = float(d.get("todayCtxCost") or 0)
            o = float(d.get("todayOutCost") or 0)
            n = float(d.get("todayNewCost") or 0)
            t = c + o + n
            if not t:
                return "今日成本构成：—"
            pct = lambda v: int(round(v / t * 100))  # noqa: E731
            return ("今日成本构成：上下文 ¥%s（%d%%）· 输出 ¥%s（%d%%）· 新增 ¥%s（%d%%）"
                    % (money(c), pct(c), money(o), pct(o), money(n), pct(n)))

        lines = [
            "当前处于【高峰时段】—— 单价较高（空闲时段只要一半）" if peak
            else "当前处于【空闲时段】—— 单价为高峰时段的一半",
        ]
        # 三档单价（v1.10.0：加回悬停，之前只在右键菜单里）
        if h.get("priceLine"):
            lines.append(h["priceLine"])
        # 节假日全天低谷（v1.4.0）：说明为什么现在是谷（文案由服务端给，调休日说法不同）
        if hn and not peak:
            lines.append(d.get("holidayTodayText") or
                         ("📅 今天是【%s】—— 法定节假日全天按空闲价" % hn))
        # 假期提醒（v1.4.1）：明天放假 / 今天是假期最后一天
        for k in ("holidayEveText", "holidayLastText"):
            if d.get(k):
                lines.append(d[k])
        def pct(v):
            return "—" if v is None else ("%g%%" % v)

        lines += [
            "",
            "今日：高峰 ¥%s（命中 %s）· 空闲 ¥%s（命中 %s）· 全天 %s" % (
                money(d.get("todayPeak") or 0), pct(d.get("todayPeakHitRate")),
                money(d.get("todayOffpeak") or 0), pct(d.get("todayOffpeakHitRate")),
                pct(d.get("todayHitRate"))),
        ]
        lines += [
            "本周：高峰 ¥%s · 空闲 ¥%s · 平均命中 %s" % (money(d.get("weekPeak") or 0),
                                            money(d.get("weekOffpeak") or 0), pct(d.get("weekHitRate"))),
            "本月：高峰 ¥%s · 空闲 ¥%s · 平均命中 %s" % (money(d.get("monthPeak") or 0),
                                            money(d.get("monthOffpeak") or 0), pct(d.get("monthHitRate"))),
        ]
        if d.get("savingTip"):
            lines.append(d["savingTip"])
        if d.get("adviceTip"):
            lines.append(d["adviceTip"])
        if (d.get("compactAdvice") or {}).get("text"):
            lines.append(d["compactAdvice"]["text"])
        if d.get("ctxHint"):
            lines.append(d["ctxHint"])
        if d.get("nextSwitchText"):
            lines += ["", d["nextSwitchText"]]
        lines += ["", "右键 → 规则 · 成本构成 · 锁定/复位"]
        # 压缩提醒图标（v1.9.0）：判据自己跑，值得压缩时才亮；开关在右键菜单
        if "alert_icon" not in self.state and d.get("showAlertIcon") is not None:
            self.icon_var.set(bool(d["showAlertIcon"]))     # 未手动设过 → 跟配置默认
        ca = d.get("compactAdvice") or {}
        on = bool(ca.get("action") == "compact") and bool(self.icon_var.get())
        self.alert.configure(text="✂️" if on else "", fg="#e0a34a")
        _short = d.get("holidayShort") or ""
        self.peak.configure(text=("谷·%s" % (_short or "节")) if (hn and not peak) else ("峰" if peak else "谷"),
                            fg=("#ffb454" if peak else GREEN))
        self.peak_tip_text = "\n".join(lines)
        if self._tipw is not None:      # 提示正开着就同步刷新内容
            self._hide_peak_tip()
            self._show_peak_tip()

    def tick(self):
        self.fetch()
        self.root.after(5000, self.tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args()
    port = a.port
    if port is None:
        try:
            port = int(json.loads((HERE / "config.json").read_text("utf-8")).get("port", 59992))
        except (OSError, ValueError):
            port = 59992
    try:
        Widget(port).run()
    except Exception as exc:  # noqa: BLE001
        print("启动失败：%r（需要图形界面 + tkinter）" % exc)
        sys.exit(1)
