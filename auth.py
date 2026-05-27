import json
from pathlib import Path
from playwright.sync_api import Page, BrowserContext

LOGIN_URL = "https://app.courtreserve.com/Account/Login"

BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def login(page: Page, email: str, password: str) -> None:
    print(f"Logging in as {email}...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)
    if "login" in page.url.lower():
        raise RuntimeError("Login failed — check CR_EMAIL and CR_PASSWORD in .env")
    print("Login successful.")


def save_session(context: BrowserContext, session_file: Path) -> None:
    state = context.storage_state()
    session_file.write_text(json.dumps(state))
    print(f"Session saved to {session_file}")


def load_session(context: BrowserContext, session_file: Path) -> bool:
    if not session_file.exists():
        return False
    state = json.loads(session_file.read_text())
    context.add_cookies(state.get("cookies", []))
    return True


def is_logged_in(page: Page, org_url: str) -> bool:
    page.goto(org_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1500)
    return "login" not in page.url.lower()


def ensure_logged_in(context: BrowserContext, page: Page, email: str, password: str, org_url: str, session_file: Path) -> None:
    loaded = load_session(context, session_file)
    if loaded and is_logged_in(page, org_url):
        print("Using existing session.")
        return
    login(page, email, password)
    save_session(context, session_file)
