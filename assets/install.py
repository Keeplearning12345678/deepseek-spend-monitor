#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 花费浮窗 —— 一键安装器
================================
在本目录（包含 ds_monitor_server.py / pi-web-widget.js / config.json）直接运行：

  python install.py doctor     环境自检（建议先跑）
  python install.py install    安装：注入 pi-web（内联脚本）+ 启动数据服务 + 生成启动脚本
  python install.py restart    重启 pi-web（让注入生效；会断开当前网页会话）
  python install.py status     查看状态
  python install.py start      只启动数据服务
  python install.py stop       停止数据服务
  python install.py autostart  设置开机自启数据服务（推荐）
  python install.py uninstall  移除 pi-web 里的注入

零第三方依赖，不需要管理员权限。
"""

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "ds_monitor_server.py"
WIDGET = HERE / "pi-web-widget.js"
CONFIG = HERE / "config.json"

MARK_BEGIN = "<!-- ds-spend-widget:begin -->"
MARK_END = "<!-- ds-spend-widget:end -->"


def read_config():
    cfg = {"port": 59992}
    if CONFIG.exists():
        try:
            cfg.update({k: v for k, v in json.loads(CONFIG.read_text("utf-8")).items()
                        if not k.startswith("_")})
        except (OSError, ValueError):
            pass
    return cfg


CFG = read_config()
PORT = int(CFG.get("port", 59992))
URL = "http://127.0.0.1:%d" % PORT


# ---------------- 环境探测 ----------------
def find_pi_web():
    home = Path.home()
    cands = [
        home / "AppData/Roaming/npm/node_modules/@agegr/pi-web",
        home / ".npm-global/lib/node_modules/@agegr/pi-web",
        Path("/usr/local/lib/node_modules/@agegr/pi-web"),
        Path("/usr/lib/node_modules/@agegr/pi-web"),
        Path("/opt/homebrew/lib/node_modules/@agegr/pi-web"),
    ]
    cands += [Path(p) for p in glob.glob(str(home / "AppData/Local/npm-cache/_npx/*/node_modules/@agegr/pi-web"))]
    try:
        r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and r.stdout.strip():
            cands.append(Path(r.stdout.strip()) / "@agegr/pi-web")
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for c in cands:
        try:
            if (c / ".next/server/app/index.html").exists():
                found.append(c)
        except OSError:
            continue
    # 去重
    uniq = []
    for f in found:
        if str(f) not in [str(u) for u in uniq]:
            uniq.append(f)
    return uniq


def service_alive(timeout=2):
    try:
        with urllib.request.urlopen(URL + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_busy():
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def api_key_source():
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "环境变量 DEEPSEEK_API_KEY"
    mp = Path(os.path.expanduser(str(CFG.get("models_json", "~/.pi/agent/models.json"))))
    if mp.exists():
        try:
            cfg = json.loads(mp.read_text("utf-8"))
            for name, p in (cfg.get("providers") or {}).items():
                if p.get("apiKey") and "deepseek" in str(p.get("baseUrl", "")):
                    return "models.json 里的 provider「%s」" % name
        except (OSError, ValueError):
            pass
    return None


def sessions_info():
    pat = os.path.expanduser(str(CFG.get("sessions_glob", "~/.pi/agent/sessions/**/*.jsonl"))).replace("\\", "/")
    root = Path(pat.split("*")[0].rstrip("/")) if "*" in pat else Path(pat)
    if not root.exists():
        return root, 0
    return root, len(list(root.rglob("*.jsonl")))


# ---------------- 注入 ----------------
def _widget_code():
    """读取页面组件源码，把端口占位符换成实际端口"""
    if not WIDGET.exists():
        return None
    code = WIDGET.read_text("utf-8").replace("__PORT__", str(PORT))
    # 防止脚本内容里的 </script> 提前闭合标签
    return code.replace("</script>", "<\\/script>")


# 热加载引导：内联脚本先渲染一份兜底，页面加载完成后若数据服务可用，
# 就取服务端的最新组件源码替换内联版 —— 于是以后改浮窗代码，只需重启数据服务，
# 不必再重启 pi-web（内联那仍保留：服务挂掉时浮窗照样在，只显示 —）。
HOTLOAD = """
(function(){
  if (window.__dsWidgetHotLoad) return;
  window.__dsWidgetHotLoad = true;
  function swap(){
    if (window.__dsWidgetHotLoaded) return;
    try { new Function("return 1")(); } catch (e) { return; }  // CSP 禁 eval 时就安心用内联版
    fetch("http://127.0.0.1:__PORT__/widget.js?t=" + Date.now())
      .then(function(r){ return r.ok ? r.text() : null; })
      .then(function(code){
        if (!code || code.indexOf("ds-spend-widget") < 0) return;
        var old = document.getElementById("ds-spend-widget");
        if (old && old.parentNode) old.parentNode.removeChild(old);
        if (window.__dsSpendWidgetTimer) {
          clearInterval(window.__dsSpendWidgetTimer);
          window.__dsSpendWidgetTimer = null;
        }
        window.__dsSpendWidgetLoaded = false;
        window.__dsSpendWidgetHotLoaded = true;
        new Function(code)();
      })
      .catch(function(){});
  }
  if (document.readyState === "complete") window.setTimeout(swap, 60);
  else window.addEventListener("load", function(){ window.setTimeout(swap, 60); });
})();
"""


def _snippet():
    """内联脚本：不依赖任何外部文件。
    这样即使数据服务没在运行，浮窗本身仍然存在（只显示 — 并每 5 秒自动重试），
    服务恢复后无需刷新页面就会重新显示数字。
    后面再跟一段热加载引导：服务可用时自动换成服务端最新版（省掉重启 pi-web）。"""
    code = _widget_code()
    if code is None:
        return None
    hot = HOTLOAD.replace("__PORT__", str(PORT))
    return (MARK_BEGIN + "<script>" + hot + "</script>"
            + "<script>" + code + "</script>" + MARK_END)


def _strip(html):
    return re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END), "", html, flags=re.S)


def inject(pw):
    html_path = pw / ".next/server/app/index.html"
    html = html_path.read_text("utf-8")
    snip = _snippet()
    if snip is None:
        return False, "找不到 pi-web-widget.js（与 install.py 同目录）"
    bak = html_path.with_suffix(".html.ds-widget.bak")
    if not bak.exists():
        bak.write_text(html, "utf-8")
    clean = _strip(html)
    if "</body>" not in clean:
        return False, "index.html 中未找到 </body>"
    html_path.write_text(clean.replace("</body>", snip + "</body>", 1), "utf-8")
    return True, ("更新（内联 %d 字节）" if MARK_BEGIN in html else "新增（内联 %d 字节）") % len(snip)


def uninject(pw):
    html_path = pw / ".next/server/app/index.html"
    if not html_path.exists():
        return False, "index.html 不存在"
    html = html_path.read_text("utf-8")
    if MARK_BEGIN not in html:
        return False, "未发现注入"
    html_path.write_text(_strip(html), "utf-8")
    return True, "已移除"


def injected_state(pw):
    html_path = pw / ".next/server/app/index.html"
    if not html_path.exists():
        return False
    return MARK_BEGIN in html_path.read_text("utf-8")


# ---------------- 数据服务 ----------------
def start_service():
    if service_alive():
        return True, "已在运行"
    if port_busy():
        return False, "端口 %d 被别的程序占用（改 config.json 的 port）" % PORT
    if not SERVER.exists():
        return False, "找不到 ds_monitor_server.py"
    exe = sys.executable
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")
        cmd = [str(pyw if pyw.exists() else exe), str(SERVER)]
        DETACHED, NEWGRP = 0x00000008, 0x00000200
        subprocess.Popen(cmd, cwd=str(HERE), creationflags=DETACHED | NEWGRP, close_fds=True)
    else:
        subprocess.Popen([exe, str(SERVER)], cwd=str(HERE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        if service_alive():
            return True, "启动成功"
    return False, "启动超时，看日志 ds-monitor.log"


def stop_service():
    pids = []
    if os.name == "nt":
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                if ":%d" % PORT in line and "LISTENING" in line.upper():
                    pid = line.split()[-1]
                    if pid.isdigit() and pid not in pids:
                        pids.append(pid)
        except (OSError, subprocess.SubprocessError):
            pass
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    else:
        try:
            out = subprocess.run(["lsof", "-ti", ":%d" % PORT], capture_output=True, text=True, timeout=20).stdout
            pids = [p for p in out.split() if p.isdigit()]
        except (OSError, subprocess.SubprocessError):
            pass
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
    if not pids:
        return True, "没有在运行的服务"
    return True, "已停止 %d 个进程" % len(pids)


def write_launchers():
    made = []
    if os.name == "nt":
        bat = HERE / "启动数据服务.bat"
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        bat.write_text(
            "@echo off\r\nchcp 65001 >nul\r\n"
            'start "" "%s" "%%~dp0ds_monitor_server.py"\r\n'
            "echo DeepSeek 数据服务已在后台启动 (http://127.0.0.1:%d/state)\r\n"
            "timeout /t 3 >nul\r\n" % (pyw if pyw.exists() else py, PORT),
            encoding="utf-8")
        made.append(bat.name)
    else:
        sh = HERE / "start-service.sh"
        sh.write_text("#!/bin/sh\ncd \"$(dirname \"$0\")\"\nnohup \"%s\" ds_monitor_server.py >/dev/null 2>&1 &\necho started\n"
                      % sys.executable, encoding="utf-8")
        sh.chmod(0o755)
        made.append(sh.name)
    return made


# ---------------- 命令 ----------------
def cmd_doctor():
    ok = True
    print("=" * 62)
    print(" 环境自检")
    print("=" * 62)
    print(" Python           : %s (%s)" % (sys.version.split()[0], sys.executable))
    if sys.version_info < (3, 8):
        print("   ✗ 需要 Python 3.8+")
        ok = False
    print(" 本目录           : %s" % HERE)
    for f in (SERVER, WIDGET, CONFIG):
        print("   %s %s" % ("✓" if f.exists() else "✗", f.name))

    root, n = sessions_info()
    print(" pi 会话目录      : %s" % root)
    if root.exists():
        print("   ✓ 找到 %d 个会话文件（花费统计的数据源）" % n)
    else:
        print("   ✗ 不存在 —— 若不用 pi，花费统计会是 0（余额仍可显示）")
        ok = False

    src = api_key_source()
    print(" 余额接口凭据     : %s" % (src or "✗ 未找到"))
    if not src:
        print("   → 可设环境变量 DEEPSEEK_API_KEY，或确认 pi 的 models.json 里配了 DeepSeek")
        ok = False

    pws = find_pi_web()
    print(" 找到 pi-web      : %s" % (", ".join(str(p) for p in pws) if pws else "✗ 没找到（只能使用桌面浮窗）"))

    print(" 服务端口 %-6d  : %s" % (PORT, "运行中 ✓" if service_alive() else ("被占用 ✗" if port_busy() else "空闲")))
    if not service_alive():
        print("   → 运行：python install.py start")

    if src:
        key, base = None, "https://api.deepseek.com"
        try:
            env = os.environ.get("DEEPSEEK_API_KEY")
            if env:
                key = env
            else:
                mp = Path(os.path.expanduser(str(CFG.get("models_json", "~/.pi/agent/models.json"))))
                for p in (json.loads(mp.read_text("utf-8")).get("providers") or {}).values():
                    if p.get("apiKey") and "deepseek" in str(p.get("baseUrl", "")):
                        key, base = p["apiKey"], p.get("baseUrl")
                        break
            req = urllib.request.Request(base.rstrip("/") + "/user/balance",
                                         headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read().decode("utf-8"))
            bal = (j.get("balance_infos") or [{}])[0]
            print(" 余额接口连通     : ✓ 余额 %s %s" % (bal.get("currency", ""), bal.get("total_balance", "?")))
        except Exception as exc:  # noqa: BLE001
            print(" 余额接口连通     : ✗ %r" % exc)
            ok = False
    print("=" * 62)
    print(" 结论：" + ("环境就绪，可以 install" if ok else "有问题，按上面提示处理"))
    return 0 if ok else 1


def cmd_install():
    print("=" * 62)
    print(" 安装 DeepSeek 花费浮窗")
    print("=" * 62)
    ok, msg = start_service()
    print("[1/3] 数据服务     : %s (%s)" % ("✓" if ok else "✗", msg))

    pws = find_pi_web()
    if pws:
        print("[2/3] 注入 pi-web  :")
        for pw in pws:
            done, info = inject(pw)
            print("      %s %s  %s" % ("✓" if done else "✗", pw, info))
    else:
        print("[2/3] 注入 pi-web  : 跳过（没找到 pi-web 安装目录）")

    made = write_launchers()
    print("[3/3] 生成启动脚本 : %s" % (", ".join(made) or "无"))

    print("-" * 62)
    if pws and service_alive():
        print("完成！接下来重启一次 pi-web，然后刷新浏览器页面（F5）：")
        print("  · 重启原因：pi-web 会把首页 HTML 缓存在内存里，注入要重启才读到")
        print("  · 重启后左上角出现浮窗（可拖动，双击复位，配色自动跟随主题）")
    elif not pws:
        print("完成（数据服务已跑）。想用网页内嵌浮窗请先安装 pi-web：")
        print("  npm i -g @agegr/pi-web")
        print("想用桌面悬浮窗：python ds_spend_widget.py（若已附带）")
    else:
        print("注入完成，但数据服务没起来，请看 ds-monitor.log，或手动 python ds_monitor_server.py")
    print("=" * 62)
    return 0


def cmd_status():
    print("数据服务  : %s (%s/state)" % ("运行中 ✓" if service_alive() else "未运行", URL))
    if service_alive():
        d = None
        for _ in range(4):
            try:
                with urllib.request.urlopen(URL + "/state", timeout=5) as r:
                    d = json.loads(r.read().decode("utf-8"))
                if d.get("balance") is not None:
                    break
            except Exception as exc:  # noqa: BLE001
                print("  读取失败: %r" % exc)
                break
            time.sleep(1.5)
        if d:
            bal = d.get("balance")
            print("  余额    : %s %s%s" % (d.get("currency", ""), bal if bal is not None else "（首次查询中…）",
                                        "" if bal is not None else "  凭据/网络问题？看 ds-monitor.log"))
            print("  今日    : %.4f（%d 次调用，%s价）" % (d.get("today", 0), d.get("todayCalls", 0),
                                                  "高峰" if d.get("peak") else "闲时"))
            print("  本会话  : %.4f" % d.get("session", 0))
            print("  累计    : %.4f（%d 会话 / %d 次调用）" % (d.get("all", 0), d.get("sessions", 0), d.get("allCalls", 0)))
    pws = find_pi_web()
    print("pi-web    : %s" % ("、".join(str(p) for p in pws) if pws else "未找到"))
    for pw in pws:
        print("  %s 注入状态: %s" % ("✓" if injected_state(pw) else "✗", "已注入" if injected_state(pw) else "未注入"))
    return 0


def cmd_uninstall():
    pws = find_pi_web()
    for pw in pws:
        done, info = uninject(pw)
        print("%s %s" % ("✓" if done else "·", info + "  " + str(pw)))
    print("已卸载（数据服务仍在运行，可 install.py stop 停止）")
    return 0


def find_pi_web_processes():
    """找出命令行里含 pi-web 的进程 PID（不会误杀其他 node 程序）"""
    pids = []
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
              "Where-Object { $_.CommandLine -like '*pi-web*' } | "
              "ForEach-Object { $_.ProcessId }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30).stdout
            pids = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            out = subprocess.run(["pgrep", "-f", "pi-web"], capture_output=True, text=True, timeout=20).stdout
            pids = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
        except (OSError, subprocess.SubprocessError):
            pass
    return pids


def cmd_restart(assume_yes=False):
    """重启 pi-web，让注入的浮窗生效（会断开当前网页会话，刷新即可继续）"""
    pids = find_pi_web_processes()
    if not pids:
        print("没找到正在运行的 pi-web 进程（可能没在跑，或命令行里不含 pi-web）")
    else:
        print("找到 %d 个 pi-web 进程，正在停止 …" % len(pids))
        for pid in pids:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            else:
                subprocess.run(["kill", "-9", pid], capture_output=True)
        time.sleep(1.5)
    exe = shutil.which("pi-web") or shutil.which("pi-web.cmd")
    if not exe:
        print("未找到全局 pi-web 命令，请手动启动（例如双击原来的 pi-web 快捷方式）")
        print("如果想让它能自动启动：npm i -g @agegr/pi-web")
        return 0
    print("启动：%s" % exe)
    try:
        if os.name == "nt":
            DETACHED, NEWGRP = 0x00000008, 0x00000200
            subprocess.Popen([exe], cwd=str(Path.home()), creationflags=DETACHED | NEWGRP, close_fds=True)
        else:
            subprocess.Popen([exe], cwd=str(Path.home()), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print("启动失败：%r" % exc)
        return 1
    time.sleep(3)
    print("已重启。请刷新浏览器里的 pi-web 页面（F5），左上角应出现浮窗。")
    return 0


def cmd_autostart():
    """开机自启数据服务（避免重启电脑后浮窗数字消失）"""
    if os.name != "nt":
        print("非 Windows：请把 ds_monitor_server.py 加入登录自启")
        print("  macOS : launchd plist 或 系统设置→登录项")
        print("  Linux : systemd --user 服务 或 crontab 的 @reboot")
        return 0
    startup = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / \
        "Microsoft/Windows/Start Menu/Programs/Startup"
    startup.mkdir(parents=True, exist_ok=True)
    lnk = startup / "DeepSeek 花费数据服务.lnk"
    bat = HERE / "启动数据服务.bat"
    target = bat if bat.exists() else (HERE / "ds_monitor_server.py")
    ps = ("$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%s'); "
          "$s.TargetPath='%s'; $s.WorkingDirectory='%s'; $s.IconLocation='%s,0'; "
          "$s.Description='DeepSeek 花费数据服务（开机自启）'; $s.Save()") % (lnk, target, HERE, sys.executable)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        print("✗ 创建失败：%r" % exc)
        return 1
    if lnk.exists():
        print("✓ 已设置开机自启：%s" % lnk)
        return 0
    print("✗ 创建失败：%s" % ((r.stderr or r.stdout) or "")[:200])
    return 1


def cmd_no_autostart():
    if os.name != "nt":
        return 0
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    lnk = startup / "DeepSeek 花费数据服务.lnk"
    if lnk.exists():
        lnk.unlink()
        print("✓ 已取消开机自启")
    else:
        print("· 未设置开机自启")
    return 0


CMDS = {
    "doctor": cmd_doctor,
    "install": cmd_install,
    "status": cmd_status,
    "uninstall": cmd_uninstall,
    "restart": cmd_restart,
    "autostart": cmd_autostart,
    "no-autostart": cmd_no_autostart,
    "start": lambda: (print(start_service()[1]), 0)[1],
    "stop": lambda: (print(stop_service()[1]), 0)[1],
}


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    fn = CMDS.get(cmd)
    if not fn:
        print("用法：python install.py [doctor|install|restart|status|start|stop|autostart|no-autostart|uninstall]")
        return 2
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
