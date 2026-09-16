# -*- coding: utf-8 -*-
"""
桌面浮窗调试工具：重启浮窗 → 悬停其「峰 / 谷」标识 → 截图提示框
用法：python shot-widget.py            （重启浮窗并截图）
      python shot-widget.py --keep     （不重启，直接对当前浮窗截图）
截图落在 C:/Users/JIAN QING/.betterwright/artifacts/<会话>/desktop-widget-tip.png
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

u = ctypes.windll.user32
u.SetProcessDPIAware()
CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
HERE = Path(__file__).resolve().parent
SHOT = Path(r"C:/Users/JIAN QING/.betterwright/artifacts/85b42e1702877c85/desktop-widget-tip.png")


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def widget_pids():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                        "Where-Object { $_.CommandLine -like '*ds_spend_widget*' } | "
                        "ForEach-Object { $_.ProcessId }"], capture_output=True)
    return [int(x) for x in r.stdout.decode("utf-8", errors="replace").split() if x.strip().isdigit()]


def restart():
    for p in widget_pids():
        subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True)
    time.sleep(1.5)
    pr = subprocess.Popen(["python", "ds_spend_widget.py"], cwd=str(HERE),
                          creationflags=0x00000008 | 0x00000200, close_fds=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("浮窗已重启 PID %d，等它拉数据…" % pr.pid)
    time.sleep(7)
    return pr.pid


def windows(pids):
    out = []

    def cb(hwnd, _l):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 20 and h > 15:
                out.append((rect.left, rect.top, w, h))
        return True

    u.EnumWindows(CB(cb), 0)
    return out


def main():
    pids = set(widget_pids())
    if "--keep" not in sys.argv:
        pids = {restart()}
    base = windows(pids)
    if not base:
        print("✗ 没找到浮窗窗口")
        return 1
    print("浮窗窗口: %s" % base)
    x, y, w, h = base[0]

    old = POINT()
    u.GetCursorPos(ctypes.byref(old))
    hit = None
    for fy in (0.30, 0.45, 0.62):
        yy = int(y + h * fy)
        for fx in range(78, 101, 2):
            xx = int(x + w * fx / 100.0)
            u.SetCursorPos(xx, yy)
            time.sleep(0.5)
            ws = windows(pids)
            if len(ws) > len(base):
                hit = (xx, yy, ws)
                break
        if hit:
            break

    if not hit:
        print("✗ 没弹出提示框")
        u.SetCursorPos(old.x, old.y)
        return 2

    xx, yy, ws = hit
    print("★ 提示框弹出！鼠标 (%d,%d)  窗口数 %d→%d" % (xx, yy, len(base), len(ws)))
    time.sleep(0.6)
    bbox = (max(0, min(b[0] for b in ws) - 30), max(0, min(b[1] for b in ws) - 30),
            max(b[0] + b[2] for b in ws) + 30, max(b[1] + b[3] for b in ws) + 30)
    img = ImageGrab.grab(bbox=bbox)
    SHOT.parent.mkdir(parents=True, exist_ok=True)
    img.save(SHOT)
    print("截图: %s (%dx%d)" % (SHOT, img.width, img.height))
    u.SetCursorPos(old.x, old.y)
    print("鼠标已移回原位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
