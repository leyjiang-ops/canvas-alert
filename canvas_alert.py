import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

# ── 配置 ──────────────────────────────────────────────────────────────────────
CANVAS_URL   = "https://oc.sjtu.edu.cn"
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
SERVERCHAN_KEY = os.environ["SERVERCHAN_KEY"]

WARN_HOURS = 2          # approaching due 阈值（小时）
STATE_FILE = "notified.json"
CST = timezone(timedelta(hours=8))

# ── Canvas API 工具函数 ────────────────────────────────────────────────────────
def headers():
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}

def get_all_pages(url):
    """自动翻页，返回所有结果。"""
    results = []
    while url:
        resp = requests.get(url, headers=headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        # 解析 Link header 获取下一页
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
    """返回一门课的所有作业和 Quiz（有 due date 的）。"""
    items = []

    try:
        for a in get_all_pages(f"{CANVAS_URL}/api/v1/courses/{course_id}/assignments?per_page=100"):
            if not isinstance(a, dict) or not a.get("due_at"):
                continue
            items.append({
                "id":    f"assignment_{a['id']}",
                "title": a.get("name", "未知作业"),
                "type":  "作业",
                "due":   a["due_at"],
            })
    except requests.exceptions.HTTPError as e:
        print(f"[跳过] 课程 {course_id} 作业接口错误：{e}")

    try:
        for q in get_all_pages(f"{CANVAS_URL}/api/v1/courses/{course_id}/quizzes?per_page=100"):
            if not isinstance(q, dict):
                continue
            due = q.get("due_at") or q.get("lock_at")
            if not due:
                continue
            items.append({
                "id":    f"quiz_{q['id']}",
                "title": q.get("title", "未知 Quiz"),
                "type":  "Quiz",
                "due":   due,
            })
    except requests.exceptions.HTTPError as e:
        print(f"[跳过] 课程 {course_id} Quiz 接口错误：{e}")

    return items

def parse_due(due_str):
    return datetime.fromisoformat(due_str.replace("Z", "+00:00"))

# ── 状态持久化（notified.json）─────────────────────────────────────────────────
def load_notified():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)   # {item_id: due_iso_str}
    return {}

def save_notified(data: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def clean_notified(data: dict):
    """删除已过期的记录，防止文件无限增长。"""
    now = datetime.now(timezone.utc)
    return {k: v for k, v in data.items() if parse_due(v) > now}

# ── 微信推送 ──────────────────────────────────────────────────────────────────
def send_wechat(title, body):
    resp = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": body},
        timeout=15,
    )
    print(f"[推送] {title}  →  HTTP {resp.status_code}")

# ── 模式一：每天 10:00 CST 汇总 ───────────────────────────────────────────────
def daily_summary():
    now     = datetime.now(timezone.utc)
    courses = get_active_courses()

    all_items = []
    for course in courses:
        for item in get_course_items(course["id"]):
            due_dt = parse_due(item["due"])
            if due_dt > now:
                item["course"] = course.get("name", "未知课程")
                item["due_dt"] = due_dt
                all_items.append(item)

    all_items.sort(key=lambda x: x["due_dt"])

    if not all_items:
        send_wechat(
            "📚 今日课程汇总",
            "目前没有待完成的作业或 Quiz，尽情摸鱼！"
        )
        return

    lines = [f"共 **{len(all_items)}** 项待完成：\n"]
    for item in all_items:
        cst_due = item["due_dt"].astimezone(CST)
        delta   = item["due_dt"] - now
        days    = delta.days
        hours   = delta.seconds // 3600

        if days == 0 and hours < 24:
            tag = f"⚠️ 今天 {cst_due.strftime('%H:%M')} 截止（还剩约 {hours} 小时）"
        elif days == 1:
            tag = f"明天 {cst_due.strftime('%H:%M')} 截止"
        else:
            tag = f"{cst_due.strftime('%m/%d %H:%M')} 截止（还剩 {days} 天）"

        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  {tag}\n"
        )

    send_wechat(f"📚 今日课程汇总（{len(all_items)} 项）", "\n".join(lines))


# ── 模式二：每 15 分钟检查 approaching due ─────────────────────────────────────
def check_approaching():
    now      = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=WARN_HOURS)
    courses  = get_active_courses()

    notified = clean_notified(load_notified())
    urgent   = []

    for course in courses:
        for item in get_course_items(course["id"]):
            due_dt = parse_due(item["due"])
            if now < due_dt <= deadline and item["id"] not in notified:
                item["course"] = course.get("name", "未知课程")
                item["due_dt"] = due_dt
                urgent.append(item)
                notified[item["id"]] = item["due"]   # 标记为已通知

    save_notified(notified)

    if not urgent:
        print("[检查完成] 无 approaching due，不推送")
        return

    urgent.sort(key=lambda x: x["due_dt"])
    lines = []
    for item in urgent:
        cst_due      = item["due_dt"].astimezone(CST)
        minutes_left = int((item["due_dt"] - now).total_seconds() / 60)
        lines.append(
            f"- **[{item['type']}]** {item['course']}\n"
            f"  {item['title']}\n"
            f"  截止：{cst_due.strftime('%H:%M')}（还剩 **{minutes_left} 分钟**）\n"
        )

    send_wechat(
        f"⚠️ {len(urgent)} 项作业即将截止！",
        "\n".join(lines),
    )


# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode == "--daily":
        daily_summary()
    elif mode == "--check":
        check_approaching()
    else:
        print(f"未知模式：{mode}，用 --daily 或 --check")
        sys.exit(1)
