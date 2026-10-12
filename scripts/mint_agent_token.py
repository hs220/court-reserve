"""Mint a token for the home agent's API (web/routes/agent_api.py).

    python scripts/mint_agent_token.py

Prints the token once, for the person to paste into the home agent's phone app
(Settings › Connections › CourtReserve), and the line for this app's `.env`, which
holds only its SHA-256. Nothing is written anywhere: running it again makes a new
token, and replacing the `.env` line (then restarting `web`) revokes the old one.
"""

import hashlib
import secrets


def mint() -> tuple[str, str]:
    token = "cr_agent_" + secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def main() -> None:
    token, digest = mint()
    print("Token (shown once -- paste it into the home agent, keep no copy):")
    print(f"  {token}")
    print()
    print("Line for court-reserve's .env on the NAS (then restart the web service):")
    print(f"  AGENT_API_TOKEN_SHA256={digest}")


if __name__ == "__main__":
    main()
