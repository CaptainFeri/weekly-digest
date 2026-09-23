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


# ---------- اعتبارسنجی ----------
def check_auth():
    """توکن را تست می‌کند و در صورت خطا، پیام مناسب می‌دهد."""
    r = requests.get("https://api.github.com/user", headers=HEADERS, timeout=15)
    if r.status_code == 401:
        sys.exit(
            "❌ 401 Unauthorized — توکن نامعتبر است یا اسکوپ کافی ندارد.\n"
            "راهنما: یک Fine-grained token بسازید با دسترسی‌های:\n"
            "  - Public Repositories (read)\n"
            "  - Metadata (read)\n"
            "  - Issues (read)\n"
            "  - Pull requests (read)\n"
            "  - Contents (read and write برای push)"
        )
    r.raise_for_status()
    data = r.json()
    print(f"✅ Authenticated as: {data['login']}")
    return data["login"]


# ---------- بازه زمانی ----------
def get_week_range():
    """
    بازه شنبه تا چهارشنبه هفته جاری.
    چون cron روز چهارشنبه اجرا می‌شود:
    - end   = امروز (چهارشنبه) ساعت 23:59:59
    - start = 4 روز قبل (شنبه) ساعت 00:00:00
    """
    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


# ---------- جمع‌آوری داده‌ها ----------
def search_events(event_type: str, start: datetime, end: datetime):
    """جست‌وجوی PR یا Issue های ساخته‌شده توسط کاربر در بازه مشخص."""
    query = (
        f"{event_type}:created author:{USERNAME} "
        f"created:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
    )
    url = "https://api.github.com/search/issues"
    params = {"q": query, "per_page": 100}
    print(f"🔎 Query: {query}")

    r = requests.get(url, headers=HEADERS, params=params, timeout=30)

    if r.status_code == 422:
        print(f"⚠️  Query rejected by GitHub: {r.text}")
        return []
    if r.status_code == 403:
        print(f"⚠️  Rate limit or forbidden: {r.text}")
        return []
    if r.status_code == 401:
        sys.exit(
            "❌ 401 on Search API — توکن اجازه جست‌وجو ندارد.\n"
            "اسکوپ‌های Issues و Pull requests را بررسی کنید."
        )
    r.raise_for_status()
    return r.json().get("items", [])


def get_commits(start: datetime, end: datetime):
    """کامیت‌های کاربر در بازه مشخص از طریق Events API."""
    url = f"https://api.github.com/users/{USERNAME}/events"
    params = {"per_page": 100}
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)

    if r.status_code != 200:
        print(f"⚠️  Events API returned {r.status_code}: {r.text[:200]}")
        return []

    events = r.json()
    commits = []
    for ev in events:
        if ev.get("type") != "PushEvent":
            continue
        created = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
        if not (start <= created <= end):
            continue
        for c in ev["payload"].get("commits", []):
            commits.append({
                "repo": ev["repo"]["name"],
                "message": c["message"].split("\n")[0],
                "sha": c["sha"][:7],
                "date": created.isoformat(),
            })
    return commits


# ---------- خلاصه‌سازی با AI (اختیاری) ----------
def summarize_with_ai(prs, issues, commits):
    if not OPENAI_API_KEY:
        return None

    raw = {
        "pull_requests": [{"title": p["title"], "url": p["html_url"]} for p in prs],
        "issues": [{"title": i["title"], "url": i["html_url"]} for i in issues],
        "commits": commits[:50],
    }

    prompt = (
        "تو یک دستیار هستی که خلاصه هفتگی فعالیت‌های گیت‌هاب یک توسعه‌دهنده را "
        "به فارسی و به صورت خوانا و مختصر می‌نویسد. داده‌های JSON زیر را به یک "
        "خلاصه ۳ تا ۵ پاراگرافی با تیترهای مشخص تبدیل کن:\n\n"
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


# ---------- تولید Markdown ----------
def build_markdown(start, end, prs, issues, commits, ai_summary):
    lines = []
    lines.append("# 📊 خلاصه فعالیت هفتگی")
    lines.append("")
    lines.append(
        f"**بازه:** {start.strftime('%Y-%m-%d')} (شنبه) → "
        f"{end.strftime('%Y-%m-%d')} (چهارشنبه)"
    )
    lines.append("")
    lines.append(
        f"**آمار کلی:** {len(commits)} کامیت · "
        f"{len(prs)} PR · {len(issues)} Issue"
    )
    lines.append("")

    if ai_summary:
        lines.append("## 🧠 خلاصه هوشمند")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")

    if prs:
        lines.append("## 🔀 Pull Requests")
        for p in prs:
            state = "✅" if p.get("state") == "closed" else "🟢"
            lines.append(f"- {state} [{p['title']}]({p['html_url']})")
        lines.append("")

    if issues:
        lines.append("## 🐛 Issues")
        for i in issues:
            lines.append(f"- [{i['title']}]({i['html_url']})")
        lines.append("")

    if commits:
        lines.append("## 💻 کامیت‌ها")
        for c in commits[:30]:
            lines.append(f"- `{c['sha']}` {c['message']} — _{c['repo']}_")
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

    prs = search_events("type:pr", start, end)
    issues = search_events("type:issue", start, end)
    commits = get_commits(start, end)

    print(f"📊 PRs: {len(prs)}, Issues: {len(issues)}, Commits: {len(commits)}")

    ai_summary = None
    try:
        ai_summary = summarize_with_ai(prs, issues, commits)
    except Exception as e:
        print(f"⚠️  AI summarization failed: {e}")

    md = build_markdown(start, end, prs, issues, commits, ai_summary)

    out_dir = Path("digests")
    out_dir.mkdir(exist_ok=True)
    filename = out_dir / f"{end.strftime('%Y-%m-%d')}.md"
    filename.write_text(md, encoding="utf-8")
    print(f"✅ نوشته شد: {filename}")


if __name__ == "__main__":
    main()