import time
from datetime import datetime


def wait_until(target: datetime) -> None:
    now = datetime.now(target.tzinfo) if target.tzinfo is not None else datetime.now()
    delta = (target - now).total_seconds()
    if delta <= 0:
        return
    print(f"Waiting until {target.strftime('%Y-%m-%d %H:%M:%S')} ({int(delta)}s from now)...")
    while True:
        _now = datetime.now(target.tzinfo) if target.tzinfo is not None else datetime.now()
        remaining = (target - _now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 60:
            time.sleep(10)
        elif remaining > 5:
            time.sleep(1)
        else:
            time.sleep(0.1)
    print("Target time reached — executing booking now.")
