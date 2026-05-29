from pathlib import Path
from datetime import datetime
import pytz
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_PT = pytz.timezone("America/Los_Angeles")

def _localdt(dt, fmt: str = "%m/%d %H:%M") -> str:
    """Convert a UTC datetime (naive or aware) to Pacific local time string."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        return dt  # already a formatted string
    try:
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(_PT).strftime(fmt)
    except Exception:
        return str(dt)

templates.env.filters["localdt"] = _localdt
