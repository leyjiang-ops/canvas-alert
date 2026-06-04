# Canvas Alert

Canvas 作业 / Quiz 自动监控：新作业发布立即推送，截止前 2 小时提醒，每天 10:00 CST 汇总，
并且把所有未提交 DDL 同步到苹果日历 / Google 日历。

跑在 GitHub Actions 上，无需自己的服务器。支持多个 Canvas 账户（如 SJTU + FDU）共用一个仓库。

## 快速开始

1. Fork 本仓库
2. 打开配置页（GitHub Pages）：`https://<你的用户名>.github.io/canvas-alert/`
3. 在页面里填 Canvas Token + 通知渠道（Server酱 / Telegram），点「生成 USERS_CONFIG JSON」
4. 把 JSON 复制到仓库的 `Settings → Secrets → Actions → New secret`，命名为 `USERS_CONFIG`
5. 在 `Actions` 标签页启用 workflows

更详细的步骤见配置页底部的「部署步骤」。

## 苹果日历 / Google 日历订阅

`--ics` 模式会把每个用户的未提交 DDL 写到 `docs/calendar_<id>.ics`，GitHub Pages 直接托管。

**订阅 URL 模板：**

```
webcal://<你的用户名>.github.io/<仓库名>/calendar_<calendar_id>.ics
```

- 在 iPhone Safari 打开 `webcal://` 链接，系统会弹"添加订阅日历"
- macOS 日历 App：菜单栏 `文件 → 新建日历订阅`，粘贴 URL
- Google Calendar：用 `https://` 版本，从「通过网址添加日历」加进去

> ⚠️ `<calendar_id>` 字段在 `USERS_CONFIG` 里。配置生成器默认会塞一个 16 字符随机串，
> 外人猜不到链接。如果你没设这个字段，URL 里就是 `id` 字段本身（如 `sjtu`），是可猜的。

打开 `https://<你的用户名>.github.io/canvas-alert/#subscribe`，输入你的 `calendar_id`，
页面会生成一键添加按钮 + 二维码 + 各平台订阅步骤。

已提交的作业会在下一次 `--sync`（最长 6 小时）后从日历里自动消失。

## 运行模式

| 命令 | 用途 | 触发频率 |
|---|---|---|
| `python canvas_alert.py --stream` | 检测新发布的作业（基于 activity_stream） | 每 5 分钟 |
| `python canvas_alert.py --check`  | 到点的提醒（截止前 2 小时） | 每 5 分钟 |
| `python canvas_alert.py --sync`   | 全量兜底，刷新提交状态 | 每 6 小时 |
| `python canvas_alert.py --daily`  | 每日汇总 | 每天 10:00 CST |
| `python canvas_alert.py --ics`    | 生成 `docs/calendar_<id>.ics` | 跟 stream / sync 一起跑 |

工作流定义在 `.github/workflows/`。

## USERS_CONFIG 字段

```json
[
  {
    "id":                 "sjtu",
    "canvas_url":         "https://oc.sjtu.edu.cn",
    "canvas_token":       "...",
    "calendar_id":        "a7f3k9zq2pmw8x4n",
    "serverchan_key":     "SCT...",
    "telegram_bot_token": "123456:ABC...",
    "telegram_chat_id":   "-100123456789"
  }
]
```

- `id`：用户标识，决定 `deadlines_<id>.json` 的文件名，**必填**
- `calendar_id`：日历订阅文件名，建议填随机串，缺省时退回到 `id`
- `serverchan_key` / `telegram_*`：通知渠道，至少配一个

## 仓库结构

```
.github/workflows/   # check / sync / daily 三个定时任务
canvas_alert.py      # 主程序，所有模式都在这一个文件里
docs/                # GitHub Pages 站点 + 生成的 .ics 文件
deadlines_<id>.json  # 每个用户的状态文件，由 workflow 自动维护
```
