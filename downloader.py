"""
downloader.py —— 本地运行
检查 deadlines.json 中临近 due 且未提交的「作业」，
自动把作业附件 PDF 下载到本地文件夹。

用法：
    python downloader.py                  # 下载未来 24h 内到期的作业 PDF
    python downloader.py --hours 48       # 自定义提前时间
    python downloader.py --dir D:/作业    # 自定义下载目录
"""

import os
import re
import sys
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── 配置（优先从环境变量读，没有再用命令行参数）────────────────────────────────
CANVAS_URL     = "https://oc.sjtu.edu.cn"
CANVAS_TOKEN   = os.environ.get("CANVAS_TOKEN", "")
DEADLINES_FILE = Path(__file__).parent / "deadlines.json"
CST            = timezone(timedelta(hours=8))


# ── Canvas API 基础 ───────────────────────────────────────────────────────────
def auth():
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── 提交状态检查 ───────────────────────────────────────────────────────────────
def is_submitted(course_id, raw_id) -> bool:
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
        print(f"    [warn] 查询提交状态失败：{e}")
        return False          # 查不到就保守下载


# ── 提取作业里的文件 ID ────────────────────────────────────────────────────────
def extract_file_ids(course_id: int, assignment_id: int) -> list[str]:
    """
    从作业详情（description HTML）里找所有 Canvas 文件 ID。
    Canvas 在 HTML 中嵌入文件链接的几种写法都覆盖到：
      /api/v1/files/12345
      /courses/xxx/files/12345
      data-api-endpoint="…/files/12345"
    """
    try:
        resp = requests.get(
            f"{CANVAS_URL}/api/v1/courses/{course_id}/assignments/{assignment_id}",
            headers=auth(), timeout=20,
        )
        resp.raise_for_status()
        desc = resp.json().get("description") or ""
    except Exception as e:
        print(f"    [warn] 拉取作业详情失败：{e}")
        return []

    ids: set[str] = set()
    ids.update(re.findall(r'/api/v1/files/(\d+)', desc))
    ids.update(re.findall(r'/files/(\d+)', desc))
    ids.update(re.findall(r'data-api-endpoint="[^"]*?/files/(\d+)', desc))
    return list(ids)


# ── 下载单个文件（仅 PDF）────────────────────────────────────────────────────
def download_file(file_id: str, dest_folder: Path) -> str | None:
    """
    通过 Canvas 文件 API 获取真实下载链接，只保存 PDF。
    返回保存路径，或 None（非 PDF / 已存在 / 失败）。
    """
    try:
        info = requests.get(
            f"{CANVAS_URL}/api/v1/files/{file_id}",
            headers=auth(), timeout=15,
        ).json()
    except Exception as e:
        print(f"    [warn] 获取文件 {file_id} 信息失败：{e}")
        return None

    filename     = info.get("filename", f"file_{file_id}")
    content_type = info.get("content-type", "")
    url          = info.get("url", "")

    # 只处理 PDF
    if not (filename.lower().endswith(".pdf") or "pdf" in content_type):
        return None

    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / filename

    if dest.exists():
        size_kb = dest.stat().st_size // 1024
        print(f"    [已存在] {filename} ({size_kb} KB)，跳过")
        return str(dest)

    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"    [✓ 下载] {filename} ({len(r.content)//1024} KB)  →  {dest}")
        return str(dest)
    except Exception as e:
        print(f"    [warn] 下载失败：{e}")
        return None


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(warn_hours: int, download_dir: Path):
    if not CANVAS_TOKEN:
        print("错误：请先设置环境变量 CANVAS_TOKEN")
        print("  Windows: set CANVAS_TOKEN=你的token")
        print("  Mac/Linux: export CANVAS_TOKEN=你的token")
        sys.exit(1)

    if not DEADLINES_FILE.exists():
        print(f"错误：找不到 {DEADLINES_FILE}")
        print("  请先运行一次 GitHub Actions 的 --sync，")
        print("  或把仓库里最新的 deadlines.json 放到同目录下。")
        sys.exit(1)

    with open(DEADLINES_FILE, encoding="utf-8") as f:
        deadlines = json.load(f)

    now      = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=warn_hours)

    # 只取「作业」类型、未过期、在时间窗口内的条目
    targets = [
        v for k, v in deadlines.items()
        if not k.startswith("_")
        and v.get("type") == "作业"
        and now < parse_dt(v["due"]) <= deadline
    ]

    if not targets:
        print(f"[下载器] 未来 {warn_hours} 小时内没有作业 due，无需下载。")
        return

    targets.sort(key=lambda x: parse_dt(x["due"]))
    print(f"[下载器] 找到 {len(targets)} 个临近 due 的作业，开始处理...\n")

    downloaded_total = 0

    for item in targets:
        cst_due      = parse_dt(item["due"]).astimezone(CST)
        hours_left   = (parse_dt(item["due"]) - now).total_seconds() / 3600
        course_id    = item["course_id"]
        raw_id       = item["raw_id"]

        print(f"▶ [{item['course']}]  {item['title']}")
        print(f"  due: {cst_due.strftime('%m/%d %H:%M')}（还剩 {hours_left:.1f} 小时）")

        # 确认未提交
        if is_submitted(course_id, raw_id):
            print("  [跳过] 已提交\n")
            continue

        # 提取附件文件 ID
        file_ids = extract_file_ids(course_id, raw_id)
        if not file_ids:
            print("  [!] 作业描述中未找到 PDF 附件\n")
            continue

        # 下载目录：download_dir/课程名/作业名/
        safe = lambda s: re.sub(r'[\\/:*?"<>|]', '_', s)
        dest_folder = download_dir / safe(item["course"]) / safe(item["title"])

        count = 0
        for fid in file_ids:
            path = download_file(fid, dest_folder)
            if path:
                count += 1
        downloaded_total += count
        if count == 0:
            print("  [!] 没有找到可下载的 PDF（可能是非 PDF 附件或链接失效）")
        print()

    print(f"[下载器] 完成，共下载 {downloaded_total} 个文件。")
    print(f"         保存位置：{download_dir.resolve()}")


# ── 命令行入口 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Canvas 作业 PDF 下载器")
    parser.add_argument(
        "--hours", type=int, default=24,
        help="提前多少小时下载（默认 24）"
    )
    parser.add_argument(
        "--dir", type=str, default=str(Path.home() / "Downloads" / "Canvas作业"),
        help="本地保存目录（默认 ~/Downloads/Canvas作业）"
    )
    args = parser.parse_args()
    main(warn_hours=args.hours, download_dir=Path(args.dir))
