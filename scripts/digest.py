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

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"


# ---------- اعتبارسنجی ----------
def check_auth():
    r = requests.get(f"{REST_URL}/user", headers=HEADERS, timeout=15)
    if r.status_code == 401:
        sys.exit("❌ 401 Unauthorized — توکن نامعتبر است یا اسکوپ کافی ندارد.")
    r.raise_for_status()
    data = r.json()
    print(f"✅ Authenticated as: {data['login']}")
    return data["login"]


# ---------- بازه زمانی ----------
def get_week_range():
    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


# ---------- GraphQL: خلاصه مشارکت‌ها ----------
GRAPHQL_QUERY = """
query($username: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $username) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      restrictedContributionsCount
      commitContributionsByRepository(maxRepositories: 100) {
        repository {
          nameWithOwner
          url
          isPrivate
          defaultBranchRef { name }
        }
        contributions(first: 100) {
          nodes {
            commitCount
            occurredAt
          }
        }
      }
      pullRequestContributions(first: 100) {
        nodes {
          pullRequest {
            title
            url
            state
            createdAt
            repository { nameWithOwner }
          }
        }
      }
      issueContributions(first: 100) {
        nodes {
          issue {
            title
            url
            state
            createdAt
            repository { nameWithOwner }
          }
        }
      }
    }
  }
}
"""


def fetch_contributions(start, end):
    variables = {
        "username": USERNAME,
        "from": start.isoformat(),
        "to": end.isoformat(),
    }
    r = requests.post(
        GRAPHQL_URL,
        headers=HEADERS,
        json={"query": GRAPHQL_QUERY, "variables": variables},
        timeout=30,
    )
    if r.status_code == 401:
        sys.exit("❌ 401 on GraphQL — توکن اجازه دسترسی ندارد.")
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        print(f"⚠️  GraphQL errors: {data['errors']}")
        sys.exit(1)
    return data["data"]["user"]["contributionsCollection"]


# ---------- REST: پیام کامیت‌ها ----------
def fetch_commits_for_repo(repo_full_name: str, start: datetime, end: datetime, author: str):
    """
    کامیت‌های یک مخزن توسط یک author در بازه مشخص.
    repo_full_name مثال: "CaptainFeri/weekly-digest"
    """
    url = f"{REST_URL}/repos/{repo_full_name}/commits"
    params = {
        "author": author,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "per_page": 100,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)

    # مخزن ممکن است خالی باشد یا دسترسی نباشد
    if r.status_code == 409:
        print(f"⚠️  {repo_full_name}: empty repository")
        return []
    if r.status_code == 404:
        print(f"⚠️  {repo_full_name}: not found or no access")
        return []
    if r.status_code == 403:
        print(f"⚠️  {repo_full_name}: rate limit or forbidden")
        return []
    r.raise_for_status()

    commits = []
    for c in r.json():
        message = c["commit"]["message"].split("\n")[0].strip()
        if not message:
            continue
        commits.append({
            "sha": c["sha"][:7],
            "message": message,
            "url": c["html_url"],
            "date": c["commit"]["author"]["date"],
            "repo": repo_full_name,
        })
    return commits


def parse_contributions(cc, start, end):
    # PRها
    prs = []
    for node in cc["pullRequestContributions"]["nodes"]:
        pr = node["pullRequest"]
        prs.append({
            "title": pr["title"],
            "url": pr["url"],
            "state": pr["state"],
            "repo": pr["repository"]["nameWithOwner"],
            "date": pr["createdAt"],
        })

    # Issueها
    issues = []
    for node in cc["issueContributions"]["nodes"]:
        issue = node["issue"]
        issues.append({
            "title": issue["title"],
            "url": issue["url"],
            "state": issue["state"],
            "repo": issue["repository"]["nameWithOwner"],
            "date": issue["createdAt"],
        })

    # کامیت‌ها: اول لیست مخازن را از GraphQL می‌گیریم
    repos_with_commits = []
    for repo_node in cc["commitContributionsByRepository"]:
        repo = repo_node["repository"]
        total = sum(c["commitCount"] for c in repo_node["contributions"]["nodes"])
        if total > 0:
            repos_with_commits.append({
                "repo": repo["nameWithOwner"],
                "url": repo["url"],
                "is_private": repo["isPrivate"],
                "count": total,
            })

    repos_with_commits.sort(key=lambda x: x["count"], reverse=True)

    # سپس برای هر مخزن، پیام کامیت‌ها را از REST می‌گیریم
    all_commits = []
    for r in repos_with_commits:
        print(f"  📥 Fetching commits from {r['repo']} ...")
        commits = fetch_commits_for_repo(r["repo"], start, end, USERNAME)
        for c in commits:
            c["is_private"] = r["is_private"]
        all_commits.extend(commits)

    stats = {
        "total_commits": cc["totalCommitContributions"],
        "total_prs": cc["totalPullRequestContributions"],
        "total_issues": cc["totalIssueContributions"],
        "restricted": cc["restrictedContributionsCount"],
    }

    return prs, issues, repos_with_commits, all_commits, stats


# ---------- خلاصه‌سازی با AI ----------
def summarize_with_ai(prs, issues, repos_with_commits, all_commits, stats):
    if not OPENAI_API_KEY:
        return None

    raw = {
        "stats": stats,
        "pull_requests": [{"title": p["title"], "repo": p["repo"]} for p in prs],
        "issues": [{"title": i["title"], "repo": i["repo"]} for i in issues],
        "commits": [
            {"repo": c["repo"], "message": c["message"]}
            for c in all_commits[:80]
        ],
    }

    prompt = (
        "تو یک دستیار هستی که خلاصه هفتگی فعالیت‌های گیت‌هاب یک توسعه‌دهنده را "
        "به فارسی، خوانا و مختصر می‌نویسد.\n\n"
        "بر اساس داده‌های JSON زیر، یک خلاصه ۳ تا ۵ پاراگرافی بنویس که:\n"
        "۱. روی چه موضوعاتی کار شده (بر اساس پیام کامیت‌ها)\n"
        "۲. چه پروژه‌هایی فعال بوده‌اند\n"
        "۳. اگر الگوی مشخصی هست (مثل رفع باگ، افزودن فیچر، رفکتور) اشاره کن\n"
        "اگر عددی صفر بود، به آن اشاره نکن. لحن دوستانه و حرفه‌ای باشد.\n\n"
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


# ---------- Markdown ----------
def build_markdown(start, end, prs, issues, repos_with_commits, all_commits, stats, ai_summary):
    lines = []
    lines.append("# 📊 خلاصه فعالیت هفتگی")
    lines.append("")
    lines.append(
        f"**بازه:** {start.strftime('%Y-%m-%d')} (شنبه) → "
        f"{end.strftime('%Y-%m-%d')} (چهارشنبه)"
    )
    lines.append("")
    lines.append(
        f"**آمار کلی:** {stats['total_commits']} کامیت · "
        f"{stats['total_prs']} PR · {stats['total_issues']} Issue"
    )
    if stats.get("restricted", 0) > 0:
        lines.append(f"_(شامل {stats['restricted']} مشارکت خصوصی)_")
    lines.append("")

    if ai_summary:
        lines.append("## 🧠 خلاصه هوشمند")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")

    # کامیت‌ها به تفکیک مخزن + پیام
    if all_commits:
        lines.append("## 💻 کامیت‌ها")
        lines.append("")

        # گروه‌بندی بر اساس مخزن
        by_repo = {}
        for c in all_commits:
            by_repo.setdefault(c["repo"], []).append(c)

        for repo_name, commits in by_repo.items():
            is_private = commits[0].get("is_private", False)
            lock = "🔒" if is_private else "🌐"
            lines.append(f"### {lock} {repo_name} ({len(commits)} کامیت)")
            lines.append("")
            for c in commits:
                lines.append(f"- [`{c['sha']}`]({c['url']}) {c['message']}")
            lines.append("")

    if prs:
        lines.append("## 🔀 Pull Requests")
        for p in prs:
            icon = "✅" if p["state"] == "MERGED" else ("🟣" if p["state"] == "CLOSED" else "🟢")
            lines.append(f"- {icon} [{p['title']}]({p['url']}) — _{p['repo']}_")
        lines.append("")

    if issues:
        lines.append("## 🐛 Issues")
        for i in issues:
            icon = "✅" if i["state"] == "CLOSED" else "🟢"
            lines.append(f"- {icon} [{i['title']}]({i['url']}) — _{i['repo']}_")
        lines.append("")

    if not (all_commits or prs or issues):
        lines.append("> 🎉 این هفته هیچ فعالیتی ثبت نشده بود.")
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

    cc = fetch_contributions(start, end)
    prs, issues, repos_with_commits, all_commits, stats = parse_contributions(cc, start, end)

    print(f"📊 Stats: {stats}")
    print(f"📦 Repos: {len(repos_with_commits)}, Commits fetched: {len(all_commits)}, "
          f"PRs: {len(prs)}, Issues: {len(issues)}")

    ai_summary = None
    try:
        ai_summary = summarize_with_ai(prs, issues, repos_with_commits, all_commits, stats)
    except Exception as e:
        print(f"⚠️  AI summarization failed: {e}")

    md = build_markdown(
        start, end, prs, issues, repos_with_commits, all_commits, stats, ai_summary
    )

    out_dir = Path("digests")
    out_dir.mkdir(exist_ok=True)
    filename = out_dir / f"{end.strftime('%Y-%m-%d')}.md"
    filename.write_text(md, encoding="utf-8")
    print(f"✅ نوشته شد: {filename}")


if __name__ == "__main__":
    main()