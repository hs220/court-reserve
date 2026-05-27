import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SESSION_FILE = Path.home() / ".court-reserve-session.json"


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    cfg["email"] = os.environ.get("CR_EMAIL", "")
    cfg["password"] = os.environ.get("CR_PASSWORD", "")
    if not cfg["email"] or not cfg["password"]:
        raise ValueError("CR_EMAIL and CR_PASSWORD must be set in .env")
    return cfg


def default_org_config():
    """Return an OrgConfig built from config.yaml. Avoids circular import at module level."""
    from booking import OrgConfig
    cfg = load_config()
    return OrgConfig(
        org_id=str(cfg.get("org_id", "13233")),
        scheduler_id=str(cfg.get("scheduler_id", "16983")),
        cost_type_id=str(cfg.get("cost_type_id", "141205")),
        timezone=cfg.get("timezone", "America/Los_Angeles"),
    )
