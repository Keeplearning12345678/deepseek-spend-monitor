# DeepSeek 花费监控浮窗

在 **pi-web 页面左上角**嵌一个迷你浮窗，实时显示 DeepSeek 的**剩余余额**和**花费**：

```
┌───────────────────────────────────┐
│ 剩 ¥27.21   今日 ¥1.45   总 ¥41.07 │
└───────────────────────────────────┘
```

- **剩** — 账户剩余余额（官方接口实时值，人民币）
- **今日** — 今天花的钱（北京时间当天 00:00 起）
- **总** — 累计花费（pi 会话记录合计）
- 悬停看明细：余额更新时间 / 今日调用次数 / 本会话花费 / 会话数 / 当前是高峰还是闲时价
- 拖动移动 · 双击复位 · **配色自动跟随 pi-web 的浅色/深色主题** · 滚动时固定在视口

零第三方依赖（纯 Python 标准库），纯本地运行，只监听 `127.0.0.1`。

---

## 我怎么用？

### 方式 A：你的 AI 装了 pi（推荐）

把这个文件夹放进 `~/.pi/agent/skills/`，然后对 AI 说：

> 帮我装一个 DeepSeek 花费浮窗

AI 会读 `SKILL.md` 自动完成安装。

### 方式 B：手动（三步）

需要 Python 3.8+。

```bash
# 1. 复制文件到一个固定目录
mkdir -p ~/deepseek-spend-monitor
cp -r assets/* ~/deepseek-spend-monitor/
cd ~/deepseek-spend-monitor

# 2. 环境自检（会告诉你缺什么）
python install.py doctor

# 3. 安装（起服务 + 注入 pi-web）
python install.py install
```

**然后重启一次 pi-web**，刷新浏览器页面（F5），浮窗就出现在左上角了。

> 为什么必须重启？pi-web 启动时会把首页 HTML 缓存在内存里，注入的内容要重启后才读到。

### 没有 pi-web 也行

用独立的桌面悬浮窗（同样显示三项，无边框置顶可拖动）：

```bash
python install.py start        # 启动数据服务
python ds_spend_widget.py      # 开悬浮窗
```

---

## 常用命令

```bash
python install.py doctor      # 环境自检（缺 API Key、路径不对等都会提示）
python install.py install     # 安装 / 重新安装（pi-web 升级后重跑这个）
python install.py status      # 看余额、今日、累计，以及注入状态
python install.py start       # 只启动数据服务
python install.py stop        # 停止数据服务
python install.py uninstall   # 移除 pi-web 里的浮窗
```

---

## 数据来源与口径

| 项目 | 来源 |
|------|------|
| 剩余余额 | `GET https://api.deepseek.com/user/balance`（官方接口，每 60 秒刷新） |
| 花费 | pi 会话日志 `~/.pi/agent/sessions/**/*.jsonl` 里的 token 用量 × 官方峰谷单价（按 `config.json` 的轮询间隔增量扫描，默认 10 秒） |

计价（元 / 百万 token，可在 `config.json` 改）：

| 项目 | 闲时 | 高峰 |
|------|------|------|
| 输入·缓存命中 | 0.02 | 0.04 |
| 输入·未命中 | 1.00 | 2.00 |
| 输出 | 4.00 | 8.00 |

高峰 = **北京时间 周一至周五 09:00-12:00、14:00-18:00**，其余为闲时（半价）。程序按每次调用**发生的时间**选价，所以峰谷混用也算得准。

**法定节假日（含放假调休的补班日）整天都算闲时**（v1.4.0 起）：放假期间不再看那两个高峰段，浮窗会显示「谷·节」并在悬停里标出节日名。节假日表在 `config.json` 的 `holidays` 里，每年国务院公布新一年安排后追一条即可（忘了也不影响，只是那一年按平日规则算）。

### 两点要知道

1. **余额是账户级的**（含网页版、其他客户端），而"花费"只统计 pi 的会话 —— 两者一对比就知道其他渠道花了多少。
2. 花费是**估算**（token × 官方单价），和官方账单可能有极小差异；余额是官方真实值。

---

## 定制

改 `config.json` 后重启数据服务：

```json
{
  "port": 59992,               // 端口（改了要重新 install 注入）
  "prices": { ... },           // 官方调价后改这里
  "peak_hours": [[9,12],[14,18]],
  "sessions_glob": "~/.pi/agent/sessions/**/*.jsonl"
}
```

想改浮窗显示哪几项 → 编辑 `pi-web-widget.js` 顶部的 `FIELDS` 数组：

```js
// 可用 key：balance(余额) today(今日) session(本会话) all(累计)
var FIELDS = [
  { label: "剩",   key: "balance", cls: "ds-balance" },
  { label: "今日", key: "today",   cls: "ds-today" },
  { label: "总",   key: "all",     cls: "ds-all" }
];
```

改这个文件**不用**重启服务或 pi-web，刷新页面即可。

---

## 常见问题

| 现象 | 解决 |
|------|------|
| 刷新后没有浮窗 | 重启 pi-web 了没？`python install.py status` 看注入状态 |
| 显示 `¥ —` | 数据服务没跑：`python install.py start`；浏览器打开 `http://127.0.0.1:59992/state` 验证 |
| 余额空着 | 没配 DeepSeek API Key（`doctor` 会提示），或网络不通 |
| 花费一直是 0 | 会话目录不对（改 `config.json` 的 `sessions_glob`） |
| pi-web 升级后浮窗没了 | 正常，重跑 `python install.py install` |
| 端口被占用 | 改 `config.json` 的 `port`，重新 `install` |

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `SKILL.md` | 给 AI 看的安装指令（放进 skills 目录用） |
| `assets/ds_monitor_server.py` | 数据服务：余额 + 用量扫描，提供 `/state`、`/widget.js` |
| `assets/pi-web-widget.js` | 注入页面内的浮窗组件 |
| `assets/install.py` | 一键安装器（doctor/install/status/start/stop/uninstall） |
| `assets/config.json` | 端口、价格、时段、路径 |
| `assets/ds_spend_widget.py` | 桌面悬浮窗（不用 pi-web 时） |

## 开机自启数据服务（可选）

- Windows：`Win+R` → `shell:startup` → 把 `启动数据服务.bat` 的快捷方式丢进去
- macOS / Linux：把 `start-service.sh` 加到登录项或 crontab `@reboot`

---

## 许可

MIT License — 随意使用、修改、分享。

Copyright (c) 2026 Little Wang

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

数据全部留在本机，除了调用 DeepSeek 官方余额接口外没有任何外发请求。

**浮窗操作**：
- 按住拖动可移动位置（松手会**贴边对齐**）· 双击复位到左上角（位置记忆在浏览器 localStorage）
- **右键浮窗** → 「🔒 锁定位置（融入页面）」/「🔓 解锁位置（可拖动）」/「↖ 复位到左上角」
  - 锁定后：**完全不可拖动**，并去掉阴影/边框/圆角、改用与 pi-web 面板同色的直角背景 → 看起来像页面原生的一行
  - 想换位置：右键解锁 → 拖到新位置 → 再右键锁定（状态自动记住，刷新后依然生效）


### 峰 / 谷标识与悬停明细

浮窗末尾的小字显示当前处于**高峰**（橙）还是**空闲**（绿）时段；法定节假日显示「**谷·国庆**」这种带节名的形式（全天空闲价），调休补班日显示「**谷·调休**」。鼠标悬停会弹出一份明细：

| 行 | 内容 |
|---|---|
| （已移出） | 三档单价与时段表不再占用悬停提示 —— 改放**右键菜单 → 📋 计费规则与单价** |
| 峰谷对比 | 今日 / 本周 / 本月各自的花费在高峰、空闲两段各是多少 |
| **今日成本构成** | 上下文重发 / 模型输出 / 新增输入 三块及占比 —— 看出钱到底花在哪 |
| **上下文健康** | 当前会话上下文规模 + 有多少成本花在重发上下文，超阈值建议 `/compact` |
| **峰谷倒计时** | 距进入下一个计费时段还有多久（好决定批量任务现在跑还是等等） |
| **缓存命中率**（v1.5.0） | 今日的 高峰 / 空闲 / 全天 三个命中率；未命中单价比命中**贵 50×** |
| **本周 / 本月平均命中率**（v1.11.0） | 悬停的「本周」「本月」两行末尾各带一个平均命中率 |
| **省钱建议**（v1.6.0） | 未命中重发花了多少、若都命中能省多少、主因是什么 —— 直接告诉你钱漏在哪 |
| **悬停里的三档单价**（v1.10.0） | `单价（元/百万，命中/未命中/输出）：空闲 0.02/1/4 · 高峰 0.04/2/8` |
| **今日成本构成饼图**（v1.10.0） | 右键 → 🧩 环形饼图：三块占比 + 中心合计 + 图例带金额与百分比 |
| **压缩提醒图标**（v1.9.0） | 命中率低到值得压缩时，浮窗上直接亮 `✂️`（不用悬停就能看见），点它看判据与金额；右键菜单可开关（默认值 `config.json` 的 `alert_icon`）|
| **该不该压缩上下文**（v1.8.0） | 按 `上下文 ×(1−命中率)` 算每次因未命中多花的钱，跟压缩一次性成本做盈亏平衡 → 直接给「建议压缩 / 不必压缩 + 几次回本」|
| **一次真实的白花钱**（v1.11.0） | 把「每轮变化的时间」写进系统提示 → 前缀失效 → 一天多花 ¥15.6；SKILL.md 里给了自查方法 |
| **假期提醒**（v1.5.0） | 放假前一个工作日提醒「明天起全天闲时，大任务留到明早跑」；假期最后一天提醒「明天恢复高峰价」 |

时段与价格全部读自 `config.json`（`peak_weekdays` / `peak_hours` / `prices` / `holidays`），改配置重启数据服务即可生效。

> 想省钱的话：先看「今日成本构成」—— 多数情况下最大项是每次调用重发的**上下文**，
> 而不只是模型输出。压上下文（早点 compact、精简工具定义、别让单会话拖太长）往往比挑时段更有效。



改浮窗代码后**只需重启数据服务 + F5**，不用再重启 pi-web：
页面里内联一份兜底脚本，加载完成后再向数据服务取最新版并原地替换（服务挂了就用兜底那份）。

### 桌面悬浮窗也有完整的峰 / 谷明细

不想嵌进 pi-web 也能用：`python ds_spend_widget.py` 起一个置顶小窗（或双击 `启动花费浮窗.bat`），
显示「剩 / 今日 / 总」+「峰 / 谷」标识，鼠标移上去弹出与网页版**完全一致**的明细
（三档单价、今日/本周/本月峰谷对比与平均命中率、今日成本构成、峰谷倒计时）。

右键可锁定位置（锁定后不可拖动），位置与模式记在 `widget-state.json`；
底部一行显示本会话花费（右侧紧跟**距下次峰谷切换的倒计时**）、今日调用次数、当前按高峰价还是空闲价计费、余额刷新时间。

调试用：`python shot-widget.py`（重启浮窗并自动截一张悬停提示的图），加 `--keep` 则不重启。

## 依赖与致谢

**这是第三方扩展，不是 pi-web 的官方功能** —— 注入依赖 pi-web 的页面结构（只有 `/` 一个路由、
首页 HTML 在 `.next/server/app/index.html`）；pi-web 大改后需重跑一次 `install` 并重启。

| 组件 | 作者 | 许可 |
|---|---|---|
| [pi-web](https://github.com/agegr/pi-web)（`@agegr/pi-web`）—— 注入并适配的目标 UI | [@agegr](https://github.com/agegr) | MIT |
| [pi](https://github.com/earendil-works/pi)（`@earendil-works/pi-coding-agent`）—— 会话日志来源 | Mario Zechner / @earendil-works | MIT |
| 本 skill（浮窗组件、数据服务、安装器）| Little Wang | MIT |

计价规则以 [DeepSeek 官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing) 为准，
本项目只是把它的峰谷规则实现了一遍（含法定节假日全天闲时）；单价与时段可在 `config.json` 里改，不用改代码。
