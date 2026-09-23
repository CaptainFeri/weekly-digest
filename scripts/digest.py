import os
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- تنظیمات ----------
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
USERNAME = os.environ.get("GH_USERNAME", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not GH_TOKEN:
    sys.exit("❌ GH_TOKEN is not set. Add it to repository secrets as GH_PAT.")
if not USERNAME:
    sys.exit("❌ GH_USERNAME is not set.")

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "weekly-digest-script",
}

REST_URL = "https://api.github.com"


# ---------- اعتبارسنجی ----------
def check_auth():
    r = requests.get(f"{REST_URL}/user", headers=HEADERS, timeout=15)
    if r.status_code == 401:
        sys.exit("❌ 401 Unauthorized — توکن نامعتبر است یا اسکوپ کافی ندارد.")
    r.raise_for_status()
    print(f"✅ Authenticated as: {r.json()['login']}")


# ---------- بازه زمانی ----------
def get_week_range():
    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


# ---------- لیست مخازن شما (owner) ----------
def list_owned_repos():
    """
    فقط مخازنی که شما owner آن‌ها هستید (نه fork ها).
    شامل مخازن شخصی و سازمانی که owner هستید.
    """
    repos = []
    page = 1
    while True:
        url = f"{REST_URL}/user/repos"
        params = {
            "per_page": 100,
            "page": page,
            "sort": "pushed",
            "affiliation": "owner",   # ← فقط owner (نه collaborator)
        }
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1

    print(f"📦 Found {len(repos)} owned repositories")
    return repos


# ---------- همه کامیت‌های یک مخزن (بدون فیلتر author) ----------
def fetch_all_commits_for_repo(repo_full_name, start, end):
    """
    همه کامیت‌های مخزن در بازه مشخص، از هر author ی.
    """
    url = f"{REST_URL}/repos/{repo_full_name}/commits"
    params = {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "per_page": 100,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)

    if r.status_code == 409:
        return []
    if r.status_code == 404:
        return []
    if r.status_code == 403:
        print(f"⚠️  {repo_full_name}: rate limit or forbidden")
        return []
    if r.status_code == 422:
        print(f"⚠️  {repo_full_name}: empty repository")
        return []
    r.raise_for_status()

    commits = []
    for c in r.json():
        message = c["commit"]["message"].split("\n")[0].strip()
        if not message:
            continue

        # author می‌تواند None باشد (کامیت با ایمیل نامعتبر)
        author_login = c.get("author", {}).get("login") if c.get("author") else None
        author_name = c["commit"]["author"]["name"]
        author_email = c["commit"]["author"]["email"]

        commits.append({
            "sha": c["sha"][:7],
            "message": message,
            "url": c["html_url"],
            "date": c["commit"]["author"]["date"],
            "repo": repo_full_name,
            "author_login": author_login,      # ممکن است None باشد
            "author_name": author_name,
            "author_email": author_email,
            "is_self": (
                author_login == USERNAME
                or author_email == f"{USERNAME}@users.noreply.github.com"
            ),
        })
    return commits


def collect_all_commits(repos, start, end):
    """از همه مخازن شما، همه کامیت‌های بازه را جمع می‌کند."""
    all_commits = []
    for i, repo in enumerate(repos, 1):
        full_name = repo["full_name"]

        # بهینه‌سازی: اگر مخزن در بازه push نشده، رد کن
        pushed_at = repo.get("pushed_at")
        if pushed_at:
            try:
                pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                if pushed_dt < start:
                    continue
            except Exception:
                pass

        print(f"  [{i}/{len(repos)}] 📥 {full_name}")
        commits = fetch_all_commits_for_repo(full_name, start, end)
        for c in commits:
            c["is_private"] = repo["private"]
        all_commits.extend(commits)

    return all_commits


# ---------- خلاصه‌سازی با AI ----------
def summarize_with_ai(all_commits, stats_by_author):
    if not OPENAI_API_KEY:
        return None

    raw = {
        "total_commits": len(all_commits),
        "by_author": stats_by_author,
        "commits": [
            {
                "repo": c["repo"],
                "author": c["author_login"] or c["author_name"],
                "message": c["message"],
                "is_self": c["is_self"],
            }
            for c in all_commits[:120]
        ],
    }

    prompt = (
        "تو یک دستیار هستی که خلاصه هفتگی فعالیت‌های گیت‌هاب را به فارسی، "
        "خوانا و مختصر می‌نویسد.\n\n"
        "این خلاصه دربارهٔ **همه کامیت‌های روی مخازن یک کاربر** است، "
        "شامل کامیت‌های خودش و همکارانش.\n\n"
        "بر اساس داده‌های JSON زیر، یک خلاصه ۳ تا ۵ پاراگرافی بنویس که:\n"
        "۱. روی چه موضوعاتی کار شده (بر اساس پیام کامیت‌ها)\n"
        "۲. چه پروژه‌هایی فعال بوده‌اند\n"
        "۳. چه کسانی مشارکت داشته‌اند (خود کاربر vs دیگران)\n"
        "۴. اگر الگوی مشخصی هست (رفع باگ، فیچر، رفکتور) اشاره کن\n"
        "اگر عددی صفر بود، به آن اشاره نکن.\n\n"
        f"{json.dumps(raw, ensure_ascii=False, indent=2)}"
    )

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------- آمار ----------
def compute_stats(all_commits):
    """آمار کلی بر اساس author و repo."""
    by_author = {}
    by_repo = {}
    for c in all_commits:
        author = c["author_login"] or c["author_name"]
        by_author[author] = by_author.get(author, 0) + 1

        repo = c["repo"]
        if repo not in by_repo:
            by_repo[repo] = {"total": 0, "by_author": {}}
        by_repo[repo]["total"] += 1
        by_repo[repo]["by_author"][author] = \
            by_repo[repo]["by_author"].get(author, 0) + 1

    return by_author, by_repo


# ---------- Markdown ----------
def build_markdown(start, end, all_commits, by_author, by_repo, ai_summary):
    lines = []
    lines.append("# 📊 خلاصه فعالیت هفتگی مخازن")
    lines.append("")
    lines.append(
        f"**بازه:** {start.strftime('%Y-%m-%d')} (شنبه) → "
        f"{end.strftime('%Y-%m-%d')} (چهارشنبه)"
    )
    lines.append("")
    lines.append(f"**مجموع کامیت‌ها:** {len(all_commits)}")
    lines.append("")

    if ai_summary:
        lines.append("## 🧠 خلاصه هوشمند")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")

    # آمار مشارکت‌کنندگان
    if by_author:
        lines.append("## 👥 مشارکت‌کنندگان")
        lines.append("")
        for author, count in sorted(by_author.items(), key=lambda x: x[1], reverse=True):
            marker = "👤 (شما)" if author == USERNAME else ""
            lines.append(f"- **{author}** {marker} — {count} کامیت")
        lines.append("")

    # کامیت‌ها به تفکیک مخزن
    if all_commits:
        lines.append("## 💻 کامیت‌ها به تفکیک مخزن")
        lines.append("")

        for repo_name in sorted(by_repo, key=lambda r: by_repo[r]["total"], reverse=True):
            repo_data = by_repo[repo_name]
            commits = [c for c in all_commits if c["repo"] == repo_name]
            is_private = commits[0].get("is_private", False)
            lock = "🔒" if is_private else "🌐"

            lines.append(f"### {lock} {repo_name} ({repo_data['total']} کامیت)")
            lines.append("")

            # گروه‌بندی بر اساس author
            for c in commits:
                author = c["author_login"] or c["author_name"]
                marker = "" if c["is_self"] else " 👥"
                lines.append(
                    f"- [`{c['sha']}`]({c['url']}) {c['message']} "
                    f"— _{author}_{marker}"
                )
            lines.append("")

    if not all_commits:
        lines.append("> 🎉 این هفته هیچ کامیتی روی مخازن شما ثبت نشده بود.")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_تولیدشده به صورت خودکار در {datetime.now(timezone.utc).isoformat()}_"
    )
    return "\n".join(lines)


# ---------- main ----------
def main():
    check_auth()
    start, end = get_week_range()
    print(f"📅 بازه: {start} → {end}")

    repos = list_owned_repos()
    all_commits = collect_all_commits(repos, start, end)
    print(f"💻 Total commits found: {len(all_commits)}")

    by_author, by_repo = compute_stats(all_commits)
    print(f"👥 Authors: {len(by_author)}, 📦 Active repos: {len(by_repo)}")

    ai_summary = None
    try:
        ai_summary = summarize_with_ai(all_commits, by_author)
    except Exception as e:
        print(f"⚠️  AI summarization failed: {e}")

    md = build_markdown(start, end, all_commits, by_author, by_repo, ai_summary)

    out_dir = Path("digests")
    out_dir.mkdir(exist_ok=True)
    filename = out_dir / f"{end.strftime('%Y-%m-%d')}.md"
    filename.write_text(md, encoding="utf-8")
    print(f"✅ نوشته شد: {filename}")


if __name__ == "__main__":
    main()