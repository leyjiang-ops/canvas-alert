import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

# ── 配置 ──────────────────────────────────────────────────────────────────────
CANVAS_URL     = "https://oc.sjtu.edu.cn"
CANVAS_TOKEN   = os.environ["CANVAS_TOKEN"]
SERVERCHAN_KEY = os.environ["SERVERCHAN_KEY"]

REMIND_BEFORE  = timedelta(hours=2)   # 提前多久提醒
DEADLINES_FILE = "deadlines.json"
CST            = timezone(timedelta(hours=8))

# ── Canvas API ────────────────────────────────────────────────────────────────
def auth():
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}

def get_all_pages(url):
    results = []
    while url:
        resp = requests.get(url, headers=auth(), timeout=20)
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

def get_active_courses():
    items = get_all_pages(
        f"{CANVAS_URL}/api/v1/courses?enrollment_state=active&per_page=100"
    )
    return [c for c in items if isinstance(c, dict) and "id" in c]

def get_course_items(course_id):
    """全量拉取一门课的作业和 Quiz（full_sync 专用）。"""
    items = []
    try:
        for a in get_all_pages(
            f"{CANVAS_URL}/api/v1/courses/{course_id}/assignments"
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
        print(f"[跳过] 课程 {course_id} 作业接口错误：{e}")
    try:
        for q in get_all_pages(
            f"{CANVAS_URL}/api/v1/courses/{course_id}/quizzes?per_page=100"
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
        print(f"[跳过] 课程 {course_id} Quiz 接口错误：{e}")
    return items

def check_submission(course_id, raw_id) -> bool:
    """按需查单个作业的提交状态。"""
    try:
        resp = requests.get(
            f"{CANVAS_URL}/api/v1/courses/{course_id}/assignments/{raw_id}"
            f"?include[]=submission",
            headers=auth(), timeout=15,
        )
        resp.raise_for_status()
        sub   = resp.json().get("submission") or {}
        state = sub.get("workflow_state", "unsubmitted")
        return state != "unsubmitted" or bool(sub.get("submitted_at"))
    except Exception as e:
        print(f"[warn] 查询作业提交状态失败：{e}")
        return False

def check_quiz_submission(course_id, raw_id) -> bool:
    """按需查单个 Quiz 的提交状态。"""
    try:
        resp = requests.get(
            f"{CANVAS_URL}/api/v1/courses/{course_id}/quizzes/{raw_id}/submission",
            headers=auth(), timeout=15,
        )
        resp.raise_for_status()
        subs = resp.json().get("quiz_submissions", [])
        if not subs:
            return False
        # workflow_state: "complete" / "pending_review" = 已做；"untaken" = 未做
        return subs[-1].get("workflow_state") in ("complete", "pending_review")
    except Exception as e:
        print(f"[warn] 查询 Quiz 提交状态失败：{e}")
        return False

def is_submitted(item: dict) -> bool:
    """统一入口：根据 id 前缀选择正确的检测函数。"""
    if item["id"].startswith("assignment_"):
        return check_submission(item["course_id"], item["raw_id"])
    else:
        return check_quiz_submission(item["course_id"], item["raw_id"])

def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ── deadlines.json ────────────────────────────────────────────────────────────
# 每个条目结构：
# {
#   "id":        "assignment_123",
#   "raw_id":    123,
#   "course_id": 456,
#   "course":    "高数",
#   "title":     "HW1",
#   "type":      "作业" | "Quiz",
#   "due":       "2024-01-15T10:00:00Z",
#   "remind_at": "2024-01-15T08:00:00Z",   ← 发现时就算好，due - REMIND_BEFORE
#   "reminded":  false                      ← 是否已发过提醒
# }

def load_deadlines() -> dict:
    if os.path.exists(DEADLINES_FILE):
        with open(DEADLINES_FILE) as f:
            return json.load(f)
    return {}

def save_deadlines(data: dict):
    with open(DEADLINES_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def clean_deadlines(data: dict) -> dict:
    """移除已过期的条目。"""
    now  = datetime.now(timezone.utc)
    meta = {k: v for k, v in data.items() if k.startswith("_")}
    live = {k: v for k, v in data.items()
            if not k.startswith("_") and parse_dt(v["due"]) > now}
    return {**meta, **live}

def make_remind_at(due_str: str) -> str:
    """
    计算提醒时刻 = due - REMIND_BEFORE。
    若 due 已不足 REMIND_BEFORE，则设为 5 分钟后（下次 check 必然触发）。
    """
    now    = datetime.now(timezone.utc)
    due_dt = parse_dt(due_str)
    remind = due_dt - REMIND_BEFORE
    if remind <= now:
        remind = now + timedelta(minutes=5)
    return remind.isoformat()

def register_item(deadlines, iid, raw_id, course_id, course,
                  title, item_type, due_str, submitted=False) -> bool:
    """
    写入新条目，返回 True 表示确为新发现。
    submitted=True（发现时已提交）则直接标记 reminded=True，永不触发提醒。
    """
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
        "remind_at": make_remind_at(due_str),   # ← 发现时立即算好
        "reminded":  submitted,                  # ← 已提交视为已提醒
    }
    return True

# ── 微信推送 ──────────────────────────────────────────────────────────────────
def send_wechat(title, body):
    resp = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": body},
        timeout=15,
    )
    print(f"[推送] {title}  →  HTTP {resp.status_code}")

def notify_new_items(new_items: list):
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
    send_wechat(f"📋 新作业发布（{len(new_items)} 项）", "\n".join(lines))

# ── 模式一：stream_check —— 每 5 分钟，检测新作业 ────────────────────────────
def stream_check():
    now       = datetime.now(timezone.utc)
    deadlines = clean_deadlines(load_deadlines())
    meta      = deadlines.get("_meta", {})

    last_check_str = meta.get("last_stream_check")
    last_check     = parse_dt(last_check_str) if last_check_str else now

    try:
        stream_items = get_all_pages(
            f"{CANVAS_URL}/api/v1/users/self/activity_stream?per_page=50"
        )
    except Exception as e:
        print(f"[stream] activity_stream 请求失败：{e}")
        return

    course_name_cache = {}

    def get_course_name(cid):
        if cid not in course_name_cache:
            try:
                r = requests.get(f"{CANVAS_URL}/api/v1/courses/{cid}",
                                 headers=auth(), timeout=10)
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
                            f"{CANVAS_URL}/api/v1/courses/{course_id}/assignments/{aid}"
                            f"?include[]=submission",
                            headers=auth(), timeout=15).json()
                        raw_id  = a.get("id", raw_id)
                        due_str = a.get("due_at", due_str)
                        title   = a.get("name", title)
                    except Exception:
                        pass
            if not raw_id or not due_str or parse_dt(due_str) <= now:
                continue
            iid = f"assignment_{raw_id}"
            if iid not in deadlines:
                # 新发现的作业先查一次提交状态
                submitted = check_submission(course_id, raw_id)
                register_item(deadlines, iid, raw_id, course_id,
                              get_course_name(course_id), title, "作业",
                              due_str, submitted=submitted)
                if submitted:
                    print(f"[stream] 新作业已提交，静默记录：{title}")
                else:
                    new_items.append(deadlines[iid])
                    print(f"[stream] 新作业：{title}")

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
                print(f"[stream] 新 Quiz：{title}")

    deadlines["_meta"] = {**meta, "last_stream_check": now.isoformat()}
    save_deadlines(deadlines)
    notify_new_items(new_items)
    if not new_items:
        print(f"[stream] 无新内容（自 {last_check.astimezone(CST).strftime('%H:%M')} 起）")

# ── 模式二：full_sync —— 每 6 小时兜底 ───────────────────────────────────────
def full_sync():
    now       = datetime.now(timezone.utc)
    courses   = get_active_courses()
    deadlines = clean_deadlines(load_deadlines())
    new_items = []

    for course in courses:
        cid   = course["id"]
        cname = course.get("name", "未知课程")
        for item in get_course_items(cid):
            if parse_dt(item["due"]) <= now:
                continue
            iid = item["id"]
            # get_course_items 已带 submitted 字段，直接用
            if register_item(deadlines, iid, item["raw_id"], cid,
                             cname, item["title"], item["type"], item["due"],
                             submitted=item["submitted"]):
                if item["submitted"]:
                    print(f"[full_sync] 已提交，静默记录：{item['title']}")
                else:
                    new_items.append(deadlines[iid])
                    print(f"[full_sync] 补漏：{item['title']}")

    save_deadlines(deadlines)
    notify_new_items(new_items)
    total = sum(1 for k in deadlines if not k.startswith("_"))
    print(f"[full_sync] 补漏 {len(new_items)} 项，共记录 {total} 项")

# ── 模式三：check —— 每 5 分钟，纯本地，到点才提醒 ───────────────────────────
def check_reminders():
    """
    读 deadlines.json，找到 remind_at ≤ now 且未提醒的条目。
    整个过程零 Canvas API 调用——除非真的需要发提醒时查一下提交状态。
    """
    now       = datetime.now(timezone.utc)
    deadlines = load_deadlines()
    changed   = False

    due_items = [
        v for k, v in deadlines.items()
        if not k.startswith("_")
        and not v.get("reminded")
        and parse_dt(v["remind_at"]) <= now
    ]

    if not due_items:
        print("[check] 无到期提醒，退出")
        return

    # 只在真正触发时才调用 Canvas API 查提交状态
    to_notify = []
    for item in due_items:
        submitted = is_submitted(item)

        deadlines[item["id"]]["reminded"] = True
        changed = True

        if submitted:
            print(f"[check] 已提交，跳过：{item['title']}")
        else:
            to_notify.append(item)

    if changed:
        save_deadlines(deadlines)

    if not to_notify:
        print("[check] 触发的提醒均已提交，不发送")
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
    send_wechat(f"⚠️ {len(to_notify)} 项作业即将截止！", "\n".join(lines))

# ── 模式四：daily —— 每天 10:00 汇总 ─────────────────────────────────────────
def daily_summary():
    now       = datetime.now(timezone.utc)
    deadlines = clean_deadlines(load_deadlines())
    items_raw = [v for k, v in deadlines.items() if not k.startswith("_")]

    if not items_raw:
        send_wechat("📚 今日课程汇总", "目前没有待完成的作业或 Quiz，尽情摸鱼！")
        return

    all_items = []
    for item in items_raw:
        submitted = is_submitted(item)
        all_items.append({**item, "submitted": submitted,
                          "due_dt": parse_dt(item["due"])})

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
            tag = f"{cst_due.strftime('%m/%d %H:%M')} 截止（还剩 {days} 天），将于 {cst_remind.strftime('%m/%d %H:%M')} 提醒"

        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  {tag}\n"
        )

    send_wechat(f"📚 今日课程汇总（{len(all_items)} 项）", "\n".join(lines))

# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    dispatch = {
        "--stream": stream_check,
        "--sync":   full_sync,
        "--check":  check_reminders,
        "--daily":  daily_summary,
    }
    if mode not in dispatch:
        print(f"未知模式：{mode}，可用：{' / '.join(dispatch)}")
        sys.exit(1)
    dispatch[mode]()
