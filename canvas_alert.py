"""
canvas_alert.py —— 多用户版
用法：
    python canvas_alert.py --stream
    python canvas_alert.py --sync
    python canvas_alert.py --check
    python canvas_alert.py --daily

用户配置通过环境变量 USERS_CONFIG 传入（JSON 数组），示例：
[
  {
    "id": "sjtu",
    "canvas_url": "https://oc.sjtu.edu.cn",
    "canvas_token": "TOKEN_A",
    "serverchan_key": "SCK_A",
    "telegram_bot_token": "...",   // 可选
    "telegram_chat_id": "..."      // 可选
  },
  {
    "id": "fdu",
    "canvas_url": "https://elearning.fudan.edu.cn",
    "canvas_token": "TOKEN_B",
    "serverchan_key": "SCK_B"
  }
]
"""

import os
import re
import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

REMIND_BEFORE = timedelta(hours=2)
CST           = timezone(timedelta(hours=8))


# ══════════════════════════════════════════════════════════════════════════════
# 用户配置加载
# ══════════════════════════════════════════════════════════════════════════════

def load_users() -> list[dict]:
    """
    从 USERS_CONFIG 环境变量读取所有用户配置。
    兼容旧版单用户（CANVAS_TOKEN + SERVERCHAN_KEY 直接传环境变量）。
    """
    raw = os.environ.get("USERS_CONFIG", "")
    if raw:
        return json.loads(raw)

    # 兼容旧版：单用户直接从独立环境变量读
    token = os.environ.get("CANVAS_TOKEN", "")
    sck   = os.environ.get("SERVERCHAN_KEY", "")
    if token and sck:
        return [{
            "id":                 "default",
            "canvas_url":         "https://oc.sjtu.edu.cn",
            "canvas_token":       token,
            "serverchan_key":     sck,
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id":   os.environ.get("TELEGRAM_CHAT_ID", ""),
        }]

    print("错误：未找到用户配置，请设置 USERS_CONFIG 环境变量。")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Canvas API（每个函数接收用户配置 u）
# ══════════════════════════════════════════════════════════════════════════════

def auth(u: dict) -> dict:
    return {"Authorization": f"Bearer {u['canvas_token']}"}


def get_all_pages(u: dict, url: str) -> list:
    results = []
    while url:
        resp = requests.get(url, headers=auth(u), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        next_url = None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break
        url = next_url
    return results


def get_active_courses(u: dict) -> list:
    items = get_all_pages(u,
        f"{u['canvas_url']}/api/v1/courses?enrollment_state=active&per_page=100"
    )
    return [c for c in items if isinstance(c, dict) and "id" in c]


def get_course_items(u: dict, course_id: int) -> list:
    """拉取一门课的作业 + Quiz，含提交状态。"""
    items = []
    try:
        for a in get_all_pages(u,
            f"{u['canvas_url']}/api/v1/courses/{course_id}/assignments"
            f"?per_page=100&include[]=submission"
        ):
            if not isinstance(a, dict) or not a.get("due_at"):
                continue
            sub   = a.get("submission") or {}
            state = sub.get("workflow_state", "unsubmitted")
            items.append({
                "id":        f"assignment_{a['id']}",
                "raw_id":    a["id"],
                "title":     a.get("name", "未知作业"),
                "type":      "作业",
                "due":       a["due_at"],
                "submitted": state != "unsubmitted" or bool(sub.get("submitted_at")),
            })
    except requests.exceptions.HTTPError as e:
        print(f"  [跳过] 课程 {course_id} 作业接口错误：{e}")

    try:
        for q in get_all_pages(u,
            f"{u['canvas_url']}/api/v1/courses/{course_id}/quizzes?per_page=100"
        ):
            if not isinstance(q, dict):
                continue
            due = q.get("due_at") or q.get("lock_at")
            if not due:
                continue
            items.append({
                "id":        f"quiz_{q['id']}",
                "raw_id":    q["id"],
                "title":     q.get("title", "未知 Quiz"),
                "type":      "Quiz",
                "due":       due,
                "submitted": False,
            })
    except requests.exceptions.HTTPError as e:
        print(f"  [跳过] 课程 {course_id} Quiz 接口错误：{e}")

    return items


def check_submission(u: dict, course_id: int, raw_id: int) -> bool:
    try:
        resp = requests.get(
            f"{u['canvas_url']}/api/v1/courses/{course_id}/assignments/{raw_id}"
            f"?include[]=submission",
            headers=auth(u), timeout=15,
        )
        resp.raise_for_status()
        sub   = resp.json().get("submission") or {}
        state = sub.get("workflow_state", "unsubmitted")
        return state != "unsubmitted" or bool(sub.get("submitted_at"))
    except Exception as e:
        print(f"  [warn] 查询作业提交状态失败：{e}")
        return False


def check_quiz_submission(u: dict, course_id: int, raw_id: int) -> bool:
    try:
        resp = requests.get(
            f"{u['canvas_url']}/api/v1/courses/{course_id}/quizzes/{raw_id}/submission",
            headers=auth(u), timeout=15,
        )
        resp.raise_for_status()
        subs = resp.json().get("quiz_submissions", [])
        return bool(subs) and subs[-1].get("workflow_state") in ("complete", "pending_review")
    except Exception as e:
        print(f"  [warn] 查询 Quiz 提交状态失败：{e}")
        return False


def is_submitted(u: dict, item: dict) -> bool:
    if item["id"].startswith("assignment_"):
        return check_submission(u, item["course_id"], item["raw_id"])
    return check_quiz_submission(u, item["course_id"], item["raw_id"])


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ══════════════════════════════════════════════════════════════════════════════
# 本地状态（每个用户独立的 deadlines_{id}.json）
# ══════════════════════════════════════════════════════════════════════════════

def deadlines_path(u: dict) -> Path:
    return Path(f"deadlines_{u['id']}.json")


def load_deadlines(u: dict) -> dict:
    p = deadlines_path(u)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_deadlines(u: dict, data: dict):
    deadlines_path(u).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def clean_deadlines(data: dict) -> dict:
    now  = datetime.now(timezone.utc)
    meta = {k: v for k, v in data.items() if k.startswith("_")}
    live = {k: v for k, v in data.items()
            if not k.startswith("_") and parse_dt(v["due"]) > now}
    return {**meta, **live}


def make_remind_at(due_str: str) -> str:
    now    = datetime.now(timezone.utc)
    remind = parse_dt(due_str) - REMIND_BEFORE
    if remind <= now:
        remind = now + timedelta(minutes=5)
    return remind.isoformat()


def register_item(deadlines, iid, raw_id, course_id, course,
                  title, item_type, due_str, submitted=False) -> bool:
    if iid in deadlines:
        return False
    deadlines[iid] = {
        "id":        iid,
        "raw_id":    raw_id,
        "course_id": course_id,
        "course":    course,
        "title":     title,
        "type":      item_type,
        "due":       due_str,
        "remind_at": make_remind_at(due_str),
        "reminded":  submitted,
        "submitted": submitted,
    }
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 推送（每个用户发给自己的渠道）
# ══════════════════════════════════════════════════════════════════════════════

def _md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~",     r"<s>\1</s>", text)
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)
    return text


def send_wechat(u: dict, title: str, body: str):
    sck = u.get("serverchan_key", "")
    if not sck:
        return
    resp = requests.post(
        f"https://sctapi.ftqq.com/{sck}.send",
        data={"title": title, "desp": body},
        timeout=15,
    )
    print(f"  [微信][{u['id']}] {title}  →  HTTP {resp.status_code}")


def send_telegram(u: dict, title: str, body: str):
    tg_token = u.get("telegram_bot_token", "")
    tg_chat  = u.get("telegram_chat_id", "")
    if not tg_token or not tg_chat:
        return
    html = f"<b>{title}</b>\n\n{_md_to_html(body)}"
    resp = requests.post(
        f"https://api.telegram.org/bot{tg_token}/sendMessage",
        json={"chat_id": tg_chat, "text": html, "parse_mode": "HTML"},
        timeout=15,
    )
    print(f"  [Telegram][{u['id']}] {title}  →  HTTP {resp.status_code}")


def notify(u: dict, title: str, body: str):
    send_wechat(u, title, body)
    send_telegram(u, title, body)


def notify_new_items(u: dict, new_items: list):
    if not new_items:
        return
    now = datetime.now(timezone.utc)
    new_items.sort(key=lambda x: parse_dt(x["due"]))
    lines = [f"发现 **{len(new_items)}** 个新作业/Quiz：\n"]
    for item in new_items:
        cst_due    = parse_dt(item["due"]).astimezone(CST)
        cst_remind = parse_dt(item["remind_at"]).astimezone(CST)
        days       = (parse_dt(item["due"]) - now).days
        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  截止：{cst_due.strftime('%m/%d %H:%M')}（还剩 {days} 天）\n"
            f"  将于 {cst_remind.strftime('%m/%d %H:%M')} 提醒你\n"
        )
    notify(u, f"📋 新作业发布（{len(new_items)} 项）", "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# 模式一：stream_check —— 检测新作业（每 5 分钟）
# ══════════════════════════════════════════════════════════════════════════════

def stream_check(u: dict):
    now       = datetime.now(timezone.utc)
    deadlines = clean_deadlines(load_deadlines(u))
    meta      = deadlines.get("_meta", {})

    last_check_str = meta.get("last_stream_check")
    last_check     = parse_dt(last_check_str) if last_check_str else now

    try:
        stream_items = get_all_pages(u,
            f"{u['canvas_url']}/api/v1/users/self/activity_stream?per_page=50"
        )
    except Exception as e:
        print(f"  [stream][{u['id']}] activity_stream 请求失败：{e}")
        return

    course_name_cache = {}

    def get_course_name(cid):
        if cid not in course_name_cache:
            try:
                r = requests.get(f"{u['canvas_url']}/api/v1/courses/{cid}",
                                 headers=auth(u), timeout=10)
                course_name_cache[cid] = r.json().get("name", f"课程{cid}")
            except Exception:
                course_name_cache[cid] = f"课程{cid}"
        return course_name_cache[cid]

    new_items = []

    for event in stream_items:
        if not isinstance(event, dict):
            continue
        ts = event.get("created_at") or event.get("updated_at")
        if not ts or parse_dt(ts) <= last_check:
            continue
        if event.get("context_type") != "Course":
            continue

        course_id  = event.get("course_id")
        event_type = event.get("type", "")

        if event_type == "Assignment":
            assignment = event.get("assignment") or {}
            raw_id     = assignment.get("id") or event.get("assignment_id")
            due_str    = assignment.get("due_at")
            title      = assignment.get("title") or event.get("title", "未知作业")
            if not raw_id or not due_str:
                aid = event.get("assignment_id")
                if aid:
                    try:
                        a       = requests.get(
                            f"{u['canvas_url']}/api/v1/courses/{course_id}/assignments/{aid}"
                            f"?include[]=submission",
                            headers=auth(u), timeout=15).json()
                        raw_id  = a.get("id", raw_id)
                        due_str = a.get("due_at", due_str)
                        title   = a.get("name", title)
                    except Exception:
                        pass
            if not raw_id or not due_str or parse_dt(due_str) <= now:
                continue
            iid = f"assignment_{raw_id}"
            if iid not in deadlines:
                submitted = check_submission(u, course_id, raw_id)
                register_item(deadlines, iid, raw_id, course_id,
                              get_course_name(course_id), title, "作业",
                              due_str, submitted=submitted)
                if submitted:
                    print(f"  [stream][{u['id']}] 新作业已提交，静默：{title}")
                else:
                    new_items.append(deadlines[iid])
                    print(f"  [stream][{u['id']}] 新作业：{title}")

        elif event_type in ("Quiz", "Quizzes::Quiz"):
            quiz    = event.get("quiz") or {}
            raw_id  = quiz.get("id") or event.get("quiz_id")
            due_str = quiz.get("due_at") or quiz.get("lock_at")
            title   = quiz.get("title") or event.get("title", "未知 Quiz")
            if not raw_id or not due_str or parse_dt(due_str) <= now:
                continue
            iid = f"quiz_{raw_id}"
            if register_item(deadlines, iid, raw_id, course_id,
                             get_course_name(course_id), title, "Quiz", due_str):
                new_items.append(deadlines[iid])
                print(f"  [stream][{u['id']}] 新 Quiz：{title}")

    deadlines["_meta"] = {**meta, "last_stream_check": now.isoformat()}
    save_deadlines(u, deadlines)
    notify_new_items(u, new_items)
    if not new_items:
        print(f"  [stream][{u['id']}] 无新内容（自 {last_check.astimezone(CST).strftime('%H:%M')} 起）")


# ══════════════════════════════════════════════════════════════════════════════
# 模式二：full_sync —— 全量兜底（每 6 小时）
# ══════════════════════════════════════════════════════════════════════════════

def full_sync(u: dict):
    now       = datetime.now(timezone.utc)
    courses   = get_active_courses(u)
    deadlines = clean_deadlines(load_deadlines(u))
    new_items = []

    def fetch(course):
        return course, get_course_items(u, course["id"])

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch, c): c for c in courses}
        for future in as_completed(futures):
            try:
                course, items = future.result()
            except Exception as e:
                print(f"  [sync][{u['id']}] 拉取失败：{e}")
                continue
            cid   = course["id"]
            cname = course.get("name", "未知课程")
            for item in items:
                if parse_dt(item["due"]) <= now:
                    continue
                iid = item["id"]
                if iid in deadlines:
                    deadlines[iid]["submitted"] = item["submitted"]
                    continue
                if register_item(deadlines, iid, item["raw_id"], cid,
                                 cname, item["title"], item["type"], item["due"],
                                 submitted=item["submitted"]):
                    if item["submitted"]:
                        print(f"  [sync][{u['id']}] 已提交，静默：{item['title']}")
                    else:
                        new_items.append(deadlines[iid])
                        print(f"  [sync][{u['id']}] 补漏：{item['title']}")

    save_deadlines(u, deadlines)
    notify_new_items(u, new_items)
    total = sum(1 for k in deadlines if not k.startswith("_"))
    print(f"  [sync][{u['id']}] 补漏 {len(new_items)} 项，共记录 {total} 项")


# ══════════════════════════════════════════════════════════════════════════════
# 模式三：check —— 纯本地，到点提醒（每 5 分钟）
# ══════════════════════════════════════════════════════════════════════════════

def check_reminders(u: dict):
    now       = datetime.now(timezone.utc)
    deadlines = load_deadlines(u)
    changed   = False

    due_items = [
        v for k, v in deadlines.items()
        if not k.startswith("_")
        and not v.get("reminded")
        and parse_dt(v["remind_at"]) <= now
    ]

    if not due_items:
        print(f"  [check][{u['id']}] 无到期提醒")
        return

    to_notify = []
    for item in due_items:
        submitted = is_submitted(u, item)
        deadlines[item["id"]]["reminded"] = True
        deadlines[item["id"]]["submitted"] = submitted
        changed = True
        if submitted:
            print(f"  [check][{u['id']}] 已提交，跳过：{item['title']}")
        else:
            to_notify.append(item)

    if changed:
        save_deadlines(u, deadlines)

    if not to_notify:
        print(f"  [check][{u['id']}] 触发的提醒均已提交，不发送")
        return

    to_notify.sort(key=lambda x: parse_dt(x["due"]))
    lines = []
    for item in to_notify:
        cst_due      = parse_dt(item["due"]).astimezone(CST)
        minutes_left = int((parse_dt(item["due"]) - now).total_seconds() / 60)
        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  截止：{cst_due.strftime('%H:%M')}（还剩 **{minutes_left} 分钟**）\n"
        )
    notify(u, f"⚠️ {len(to_notify)} 项作业即将截止！", "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# 模式四：daily —— 每天 10:00 汇总
# ══════════════════════════════════════════════════════════════════════════════

def daily_summary(u: dict):
    now = datetime.now(timezone.utc)

    # ── 先做一次同步，确保 deadlines 是最新的（token 已在入口验证过）──
    print(f"  [daily][{u['id']}] 先同步最新数据…")
    full_sync(u)

    deadlines = clean_deadlines(load_deadlines(u))
    items_raw = [v for k, v in deadlines.items() if not k.startswith("_")]

    if not items_raw:
        notify(u, "📚 今日课程汇总", "目前没有待完成的作业或 Quiz，尽情摸鱼！")
        return

    all_items = []
    for item in items_raw:
        submitted = is_submitted(u, item)
        deadlines[item["id"]]["submitted"] = submitted
        all_items.append({**item, "submitted": submitted,
                          "due_dt": parse_dt(item["due"])})
    save_deadlines(u, deadlines)

    all_items.sort(key=lambda x: (x["submitted"], x["due_dt"]))
    pending = sum(1 for i in all_items if not i["submitted"])
    lines   = [f"共 **{len(all_items)}** 项（未提交 **{pending}** 项）：\n"]

    for item in all_items:
        cst_due    = item["due_dt"].astimezone(CST)
        delta      = item["due_dt"] - now
        days       = delta.days
        hours      = delta.seconds // 3600
        cst_remind = parse_dt(item["remind_at"]).astimezone(CST)

        if item["submitted"]:
            tag = f"~~{cst_due.strftime('%m/%d %H:%M')} 截止~~ **（已提交 ✓）**"
        elif days == 0:
            tag = (f"⚠️ 今天 {cst_due.strftime('%H:%M')} 截止（还剩约 {hours} 小时）"
                   + (f"，将于 {cst_remind.strftime('%H:%M')} 提醒"
                      if not item.get("reminded") else "，提醒已发送"))
        elif days == 1:
            tag = f"明天 {cst_due.strftime('%H:%M')} 截止，将于 {cst_remind.strftime('%m/%d %H:%M')} 提醒"
        else:
            tag = (f"{cst_due.strftime('%m/%d %H:%M')} 截止（还剩 {days} 天）"
                   f"，将于 {cst_remind.strftime('%m/%d %H:%M')} 提醒")

        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  {tag}\n"
        )

    notify(u, f"📚 今日课程汇总（{len(all_items)} 项）", "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# 入口：遍历所有用户
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 模式五：ics —— 生成日历订阅文件 docs/calendar_{id}.ics
# ══════════════════════════════════════════════════════════════════════════════

def _ics_escape(text: str) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """RFC5545 按 75 字节折行（中文按字节计），续行以空格开头。"""
    if len(line.encode("utf-8")) <= 73:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            out.append(cur)
            cur = b" " + b
        else:
            cur += b
    out.append(cur)
    return "\r\n".join(seg.decode("utf-8") for seg in out)


def _ics_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_ics(u: dict):
    deadlines = clean_deadlines(load_deadlines(u))
    items     = [v for k, v in deadlines.items()
                 if not k.startswith("_") and not v.get("submitted")]
    cal_id    = u.get("calendar_id", u["id"])   # 可选随机 id 提升隐私

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//canvas-alert//DDL//CN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:Canvas DDL ({u['id']})", "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for it in sorted(items, key=lambda x: x["due"]):
        due = parse_dt(it["due"])
        end = due + timedelta(minutes=30)
        summary = f"[{it.get('type', '作业')}] {it.get('course', '')} - {it.get('title', '')}"
        desc    = f"截止：{due.astimezone(CST).strftime('%Y-%m-%d %H:%M')} (CST)"
        lines += [
            "BEGIN:VEVENT",
            _ics_fold(f"UID:{it['id']}@canvas-alert-{cal_id}"),
            f"DTSTAMP:{_ics_dt(due)}",          # 确定性时间戳：deadline 不变则文件不变
            f"DTSTART:{_ics_dt(due)}",
            f"DTEND:{_ics_dt(end)}",
            _ics_fold("SUMMARY:" + _ics_escape(summary)),
            _ics_fold("DESCRIPTION:" + _ics_escape(desc)),
            "BEGIN:VALARM", "ACTION:DISPLAY",
            _ics_fold("DESCRIPTION:" + _ics_escape(summary)),
            "TRIGGER:-PT2H", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")

    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    out = docs / f"calendar_{cal_id}.ics"
    out.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    print(f"  [ics][{u['id']}] 写入 {out}（{len(items)} 个事件）")


MODES = {
    "--stream": stream_check,
    "--sync":   full_sync,
    "--check":  check_reminders,
    "--daily":  daily_summary,
    "--ics":    generate_ics,
}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in MODES:
        print(f"未知模式：{mode}，可用：{' / '.join(MODES)}")
        sys.exit(1)

    fn    = MODES[mode]
    users = load_users()
    print(f"[{mode}] 共 {len(users)} 个用户\n")

    # 需要 Canvas API 的模式（--check 和 --ics 只读本地 JSON，不需要验证）
    needs_api = mode in ("--stream", "--sync", "--daily")

    for u in users:
        print(f"── 用户 {u['id']} ({u.get('canvas_url', '')}) ──")

        # 对需要 API 的模式，先验证 token
        if needs_api:
            try:
                r = requests.get(
                    f"{u['canvas_url']}/api/v1/users/self",
                    headers=auth(u), timeout=15,
                )
                if r.status_code == 401:
                    print(f"  [跳过] Canvas token 无效（401），请让该用户重新生成 token\n")
                    continue
            except Exception as e:
                print(f"  [跳过] Canvas 连接失败：{e}\n")
                continue

        try:
            fn(u)
        except Exception as e:
            print(f"  [错误] {e}\n")
            continue
        print()
