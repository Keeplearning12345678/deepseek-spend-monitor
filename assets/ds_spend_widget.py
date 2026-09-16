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

        self.tip = tk.Label(box, text="", bg=BG, fg=DIM, font=(FONT, 7))
        self.tip.pack(anchor="w", pady=(3, 0))

        for w in (box, row, self.tip, self.peak):
            w.bind("<Button-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", self.drag_end)
        r.bind("<Double-Button-1>", lambda e: self.toggle_compact())
        r.bind("<Button-3>", self.menu)

        self.lock_var = tk.BooleanVar(value=self.locked)
        self.menu_w = tk.Menu(r, tearoff=0)
        self.menu_w.add_command(label="立即刷新", command=self.fetch)
        self.menu_w.add_command(label="切换紧凑/详细", command=self.toggle_compact)
        self.menu_w.add_command(label="置顶开关", command=self.toggle_topmost)
        self.menu_w.add_command(label="📋 计费规则与单价", command=self.show_rules)
        self.menu_w.add_command(label="💸 今日最贵调用", command=self.show_top)
        self.menu_w.add_checkbutton(label="锁定位置（不可拖动）", variable=self.lock_var,
                                    command=self.toggle_lock)
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

    def show_top(self):
        """右键菜单：今日最贵的几次调用（从悬停提示里移出来了）"""
        d = self.last_data or {}
        arr = d.get("topCalls") or []
        if not arr:
            self._popup("今日暂无调用记录")
            return

        def k(n):
            n = int(n or 0)
            return ("%dK" % round(n / 1000.0)) if n >= 1000 else str(n)

        lines = ["今日最贵 %d 次（按金额排序）" % len(arr), ""]
        for i, c in enumerate(arr, 1):
            lines.append("%d. ¥%-9s %s　上下文 %-7s 输出 %s"
                         % (i, money(c.get("cost")), c.get("time"),
                            k(c.get("ctx")), k(c.get("out"))))
        lines += ["", "（点一下关闭）"]
        self._popup("\n".join(lines))

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
        """「峰 / 谷」标识 + 悬停提示（时段、三档单价、峰谷对比、成本构成、倒计时、最贵调用）"""
        peak = bool(d.get("peak"))
        h = d.get("hours") or {}
        self.peak.configure(text="峰" if peak else "谷",
                            fg=("#ffb454" if peak else GREEN))

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
            "",
            "今日：高峰 ¥%s · 空闲 ¥%s" % (money(d.get("todayPeak") or 0),
                                            money(d.get("todayOffpeak") or 0)),
            "本周：高峰 ¥%s · 空闲 ¥%s" % (money(d.get("weekPeak") or 0),
                                            money(d.get("weekOffpeak") or 0)),
            "本月：高峰 ¥%s · 空闲 ¥%s" % (money(d.get("monthPeak") or 0),
                                            money(d.get("monthOffpeak") or 0)),
            costbar(),
        ]
        if d.get("ctxHint"):
            lines.append(d["ctxHint"])
        if d.get("nextSwitchText"):
            lines += ["", d["nextSwitchText"]]
        lines += ["", "右键 → 计费规则 · 最贵调用"]
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
