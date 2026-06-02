"""Create GitHub issues from bug reports via the REST API (stdlib only)."""
import json
import os
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"


def is_configured() -> bool:
    return bool(_token())


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _repo() -> str:
    return os.environ.get("GITHUB_REPO", "hs220/court-reserve")


def create_issue(title: str, body: str, labels: list[str] | None = None) -> dict:
    """Create an issue. Returns {"number": int, "url": str}.

    Raises RuntimeError if not configured or the API call fails.
    """
    token = _token()
    if not token:
        raise RuntimeError("GitHub integration not configured (set GITHUB_TOKEN).")

    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{_repo()}/issues",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "court-reserve-bug-reporter",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"GitHub API error {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"GitHub request failed: {e}") from e

    return {"number": data.get("number"), "url": data.get("html_url", "")}
