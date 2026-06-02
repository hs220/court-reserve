"""Create and sync GitHub issues from bug reports via the REST API (stdlib only)."""
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


def _api_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Make a GitHub REST API call. Returns parsed JSON ({} if empty body).

    Raises RuntimeError if not configured or the call fails.
    """
    token = _token()
    if not token:
        raise RuntimeError("GitHub integration not configured (set GITHUB_TOKEN).")

    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=data,
        method=method,
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
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"GitHub API error {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"GitHub request failed: {e}") from e


def create_issue(title: str, body: str, labels: list[str] | None = None) -> dict:
    """Create an issue. Returns {"number": int, "url": str}."""
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    data = _api_request("POST", f"/repos/{_repo()}/issues", payload)
    return {"number": data.get("number"), "url": data.get("html_url", "")}


def set_issue_state(number: int, state: str) -> None:
    """Set an issue's state to 'open' or 'closed'."""
    if state not in ("open", "closed"):
        raise ValueError(f"invalid issue state: {state}")
    _api_request("PATCH", f"/repos/{_repo()}/issues/{number}", {"state": state})


def get_issue_state(number: int) -> str:
    """Return an issue's current GitHub state ('open' or 'closed')."""
    data = _api_request("GET", f"/repos/{_repo()}/issues/{number}")
    return data.get("state", "")
