#!/usr/bin/env python3
"""
Update the Recent Activity section in README.md using the GitHub REST API.

Why this exists:
- Public user events return PushEvent payloads without `size` / `commits`, so
  third-party "recent activity" renderers often show `undefined`.
- We fetch the pushed commit (`/repos/{owner}/{repo}/commits/{head}`) to show a
  stable one-line subject (first line of the commit message).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


LAST_START = "<!--RECENT_ACTIVITY:last_update-->"
LAST_END = "<!--RECENT_ACTIVITY:last_update_end-->"
ACT_START = "<!--RECENT_ACTIVITY:start-->"
ACT_END = "<!--RECENT_ACTIVITY:end-->"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _http_json(url: str, token: str) -> Any:
    # Do not send `X-GitHub-Api-Version` here: pinning an old calendar version has
    # caused `422 Unprocessable Entity` on some endpoints when GitHub deprecates it.
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "hongzhi-gao-profile-recent-activity",
        },
        method="GET",
    )

    # Basic retry for transient failures / secondary rate limits.
    last_err: Optional[BaseException] = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status == 204:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as e:
            last_err = e
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""

            if e.code in (403, 429) or 500 <= e.code <= 599:
                time.sleep(2 + attempt * 2)
                continue

            # Attach response body for easier debugging in Actions logs.
            raise RuntimeError(
                f"GitHub API HTTP {e.code} for {url}: {detail[:800]}"
            ) from e
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(1 + attempt)
            continue

    raise RuntimeError(f"Failed to fetch {url}: {last_err!r}")


def _one_line(text: str, max_len: int = 120) -> str:
    text = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _sanitize_plain(text: str, max_len: int = 120) -> str:
    text = _one_line(text, max_len=max_len)
    text = text.replace('"', "'")
    return text


def _safe_md_link_label(text: str, max_len: int = 100) -> str:
    """Keep Markdown [label](url) from breaking when titles contain brackets."""
    text = _sanitize_plain(text, max_len=max_len)
    text = re.sub(r"[\[\]]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pull_request_web_url(pr: Dict[str, Any], repo_name: str, num: int) -> str:
    """
    Events payloads sometimes omit `pull_request.html_url`; fall back to a
    canonical GitHub PR URL from base repo + number.
    """
    url = str(pr.get("html_url") or "").strip()
    if url:
        return url

    base_full = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if isinstance(base_full, str) and base_full and num:
        return f"https://github.com/{base_full}/pull/{num}"
    if repo_name and num:
        return f"https://github.com/{repo_name}/pull/{num}"
    return ""


def _repo_url(full_name: str) -> str:
    return f"https://github.com/{full_name}"


def _branch_from_ref(ref: str) -> str:
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/tags/"):
        return ref.removeprefix("refs/tags/")
    return ref or "unknown"


def _should_skip_profile_bot_push(*, repo_full_name: str, subject: str) -> bool:
    """
    Avoid flooding the activity list with the README auto-update loop itself.
    """
    if repo_full_name != "hongzhi-gao/hongzhi-gao":
        return False
    s = subject.casefold()
    if s.startswith("chore(readme): update recent activity".casefold()):
        return True
    if "update readme with the recent activity" in s:
        return True
    return False


def _render_push_event(event: Dict[str, Any], token: str) -> Optional[str]:
    repo = event.get("repo") or {}
    repo_name = repo.get("name")
    if not repo_name:
        return None

    payload = event.get("payload") or {}
    head = payload.get("head")
    ref = payload.get("ref") or ""
    branch = _branch_from_ref(str(ref))

    if not head:
        return (
            f"⬆️ Pushed to [{repo_name}]({_repo_url(repo_name)}) on `{branch}` "
            f"(commit SHA unavailable)<br>"
        )

    try:
        commit = _http_json(
            f"https://api.github.com/repos/{repo_name}/commits/{head}", token
        )
    except Exception:
        commit = None

    if not isinstance(commit, dict):
        subject = "(failed to fetch commit)"
    else:
        raw_msg = ((commit.get("commit") or {}).get("message")) or ""
        subject = _sanitize_plain(str(raw_msg))

    if not subject:
        subject = "(empty commit message)"

    if _should_skip_profile_bot_push(repo_full_name=str(repo_name), subject=subject):
        return None

    return (
        f"⬆️ Pushed to [{repo_name}]({_repo_url(repo_name)}) on `{branch}`: "
        f'"{subject}"<br>'
    )


def _render_issue_comment_event(event: Dict[str, Any]) -> Optional[str]:
    repo = event.get("repo") or {}
    repo_name = repo.get("name")
    if not repo_name:
        return None

    payload = event.get("payload") or {}
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}

    num = issue.get("number")
    if not num:
        return None

    num_int = int(num)
    title = _safe_md_link_label(str(issue.get("title") or ""), max_len=100)
    comment_url = str(comment.get("html_url") or "").strip()
    issue_url = str(issue.get("html_url") or "").strip()

    pr_stub = issue.get("pull_request") or {}
    pr_html = str(pr_stub.get("html_url") or "").strip()
    if pr_stub and pr_html:
        # PR thread: link "PR #n" to the pull request; optional link to this comment.
        title_part = f": {title}" if title else ""
        if comment_url and comment_url != pr_html:
            tail = f" ([comment]({comment_url}))"
        else:
            tail = ""
        return (
            f"💬 Commented on [PR #{num_int}{title_part}]({pr_html}){tail} in "
            f"[{repo_name}]({_repo_url(repo_name)})<br>"
        )

    url = comment_url or issue_url
    if not url:
        return None

    title_part = title if title else "(no title)"
    return (
        f"💬 Commented on [#{num_int} {title_part}]({url}) in "
        f"[{repo_name}]({_repo_url(repo_name)})<br>"
    )


def _render_pull_request_event(event: Dict[str, Any]) -> Optional[str]:
    repo = event.get("repo") or {}
    repo_name = repo.get("name")
    if not repo_name:
        return None

    payload = event.get("payload") or {}
    action = str(payload.get("action") or "")
    pr = payload.get("pull_request") or {}

    num = pr.get("number")
    if not num:
        return None

    num_int = int(num)
    title = _safe_md_link_label(str(pr.get("title") or ""), max_len=100)
    url = _pull_request_web_url(pr, str(repo_name), num_int)
    if not url:
        return None

    merged = bool(pr.get("merged"))
    if action == "opened":
        verb = "Opened"
        icon = "💪"
    elif action == "closed" and merged:
        verb = "Merged"
        icon = "🎉"
    elif action == "closed":
        verb = "Closed"
        icon = "❌"
    else:
        verb = action or "updated"
        icon = "🔁"

    title_part = f": {title}" if title else ""
    link_label = f"PR #{num_int}{title_part}"
    return (
        f"{icon} {verb} [{link_label}]({url}) in "
        f"[{repo_name}]({_repo_url(repo_name)})<br>"
    )


def _render_issues_event(event: Dict[str, Any]) -> Optional[str]:
    repo = event.get("repo") or {}
    repo_name = repo.get("name")
    if not repo_name:
        return None

    payload = event.get("payload") or {}
    action = str(payload.get("action") or "")
    if action not in {"opened", "closed", "reopened"}:
        return None

    issue = payload.get("issue") or {}
    num = issue.get("number")
    title = _safe_md_link_label(str(issue.get("title") or ""), max_len=100)
    url = str(issue.get("html_url") or "")
    if not num or not url:
        return None

    num_int = int(num)
    title_part = title if title else "(no title)"
    if action == "closed":
        icon = "✔️"
        verb = "Closed issue"
    elif action == "reopened":
        icon = "🔁"
        verb = "Reopened issue"
    else:
        icon = "❗️"
        verb = "Opened issue"

    return f"{icon} {verb} [#{num_int} {title_part}]({url}) in [{repo_name}]({_repo_url(repo_name)})<br>"


def _render_event(event: Dict[str, Any], token: str) -> Optional[str]:
    etype = event.get("type")
    if etype == "PushEvent":
        return _render_push_event(event, token)
    if etype == "IssueCommentEvent":
        return _render_issue_comment_event(event)
    if etype == "PullRequestEvent":
        return _render_pull_request_event(event)
    if etype == "IssuesEvent":
        return _render_issues_event(event)
    return None


def _fetch_events(username: str, token: str, pages: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for page in range(1, pages + 1):
        url = (
            "https://api.github.com/users/"
            f"{urllib.parse.quote(username)}/events/public?per_page=30&page={page}"
        )
        batch = _http_json(url, token)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
    return out


def _replace_block(text: str, start: str, end: str, inner: str) -> str:
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        raise SystemExit(f"README markers not found or invalid: {start!r} {end!r}")
    si_end = si + len(start)
    return text[:si_end] + "\n" + inner + "\n" + text[ei:]


def _extract_between(text: str, start: str, end: str) -> str:
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1 or ei < si:
        return ""
    si_end = si + len(start)
    return text[si_end:ei].strip("\n")


def _normalize_activity_inner(inner: str) -> str:
    lines: List[str] = []
    for raw in inner.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\d+\.\s*", "", line)
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    username = os.environ.get("ACTIVITY_USERNAME", "").strip() or os.environ.get(
        "GITHUB_REPOSITORY_OWNER", ""
    ).strip()
    if not username:
        print("ACTIVITY_USERNAME or GITHUB_REPOSITORY_OWNER is required", file=sys.stderr)
        return 2

    max_lines = max(1, min(30, _env_int("MAX_ACTIVITY_LINES", 10)))

    readme_path = os.environ.get("README_FILE", "README.md").strip() or "README.md"
    readme = open(readme_path, "r", encoding="utf-8").read()

    # Display "Last updated" in Beijing time (China Standard Time, UTC+8, no DST).
    now = (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%A, %B %d, %Y, %H:%M:%S")
        + " Beijing (UTC+8)"
    )
    last_inner = now

    events = _fetch_events(username, token)
    lines: List[str] = []
    for event in events:
        if len(lines) >= max_lines:
            break
        rendered = _render_event(event, token)
        if rendered:
            lines.append(rendered)

    if not lines:
        lines = ["_No recent public activity matched the supported event types._<br>"]

    numbered = [f"{i + 1}. {line}" for i, line in enumerate(lines)]
    act_inner = "\n".join(numbered)

    old_act_inner = _extract_between(readme, ACT_START, ACT_END)
    if _normalize_activity_inner(old_act_inner) == _normalize_activity_inner(act_inner):
        print("No activity changes detected; leaving README unchanged.")
        return 0

    updated = readme
    updated = _replace_block(updated, LAST_START, LAST_END, last_inner)
    updated = _replace_block(updated, ACT_START, ACT_END, act_inner)

    open(readme_path, "w", encoding="utf-8").write(updated)
    print(f"Updated {readme_path} ({len(lines)} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
