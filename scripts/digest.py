import os
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

GH_TOKEN = os.environ["GH_TOKEN"]
USERNAME = os.environ["GH_USERNAME"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def get_week_range():
    """
    بازه شنبه تا چهارشنبه هفته جاری را برمی‌گرداند.
    چون cron روز چهارشنبه اجرا می‌شود:
    - end   = امروز (چهارشنبه) ساعت 23:59:59
    - start = 4 روز قبل (شنبه) ساعت 00:00:00
    """
    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def search_events(event_type: str, start: datetime, end: datetime):
    """
    جست‌وجوی PR یا Issue های ساخته‌شده توسط کاربر در بازه مشخص.
    """
    query = (
        f"{event_type}:created author:{USERNAME} "
        f"created:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
    )
    url = "https://api.github.com/search/issues"
    params = {"q": query, "per_page": 100}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json().get("items", [])


def get_commits(start: datetime, end: datetime):
    """
    کامیت‌های کاربر در بازه مشخص را از طریق Events API جمع می‌کند.
    (Events API فقط 90 روز اخیر را می‌دهد که برای ما کافی است.)
    """
    url = f"https://api.github.com/users/{USERNAME}/events"
    params = {"per_page": 100}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    events = r.json()

    commits = []
    for ev in events:
        if ev["type"] != "PushEvent":
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


def summarize_with_ai(prs, issues, commits):
    """
    اگر OPENAI_API_KEY تنظیم شده باشد، یک خلاصه خوانا تولید می‌کند.
    در غیر این صورت None برمی‌گرداند.
    """
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
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def build_markdown(start, end, prs, issues, commits, ai_summary):
    lines = []
    lines.append(f"# 📊 خلاصه فعالیت هفتگی")
    lines.append("")
    lines.append(f"**بازه:** {start.strftime('%Y-%m-%d')} (شنبه) → {end.strftime('%Y-%m-%d')} (چهارشنبه)")
    lines.append("")
    lines.append(f"**آمار کلی:** {len(commits)} کامیت · {len(prs)} PR · {len(issues)} Issue")
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

    if issues:
        lines.append("")
        lines.append("## 🐛 Issues")
        for i in issues:
            lines.append(f"- [{i['title']}]({i['html_url']})")

    if commits:
        lines.append("")
        lines.append("## 💻 کامیت‌ها")
        for c in commits[:30]:
            lines.append(f"- `{c['sha']}` {c['message']} — _{c['repo']}_")

    lines.append("")
    lines.append("---")
    lines.append(f"_تولیدشده به صورت خودکار در {datetime.now(timezone.utc).isoformat()}_")
    return "\n".join(lines)


def main():
    start, end = get_week_range()
    print(f"بازه: {start} → {end}")

    prs = search_events("type:pr", start, end)
    issues = search_events("type:issue", start, end)
    commits = get_commits(start, end)

    print(f"PRs: {len(prs)}, Issues: {len(issues)}, Commits: {len(commits)}")

    ai_summary = None
    try:
        ai_summary = summarize_with_ai(prs, issues, commits)
    except Exception as e:
        print(f"AI summarization failed: {e}")

    md = build_markdown(start, end, prs, issues, commits, ai_summary)

    out_dir = Path("digests")
    out_dir.mkdir(exist_ok=True)
    filename = out_dir / f"{end.strftime('%Y-%m-%d')}.md"
    filename.write_text(md, encoding="utf-8")
    print(f"✅ نوشته شد: {filename}")


if __name__ == "__main__":
    main()