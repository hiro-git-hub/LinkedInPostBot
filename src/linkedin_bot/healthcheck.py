"""Docker-Healthcheck: `python -m linkedin_bot.healthcheck`.

Der Bot schreibt jede Minute einen Heartbeat, wenn Telegram und Postgres erreichbar sind.
Ist er zu alt, hängt der Bot oder hat keine Verbindung. Bewusst ohne schwere Imports – läuft jede Minute.
"""

import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path("/tmp/linkedin-bot-heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 60
MAX_AGE_SECONDS = 180  # drei verpasste Heartbeats


def write_heartbeat(path: Path = HEARTBEAT_FILE) -> None:
    path.write_text(str(time.time()))


def is_healthy(path: Path = HEARTBEAT_FILE, max_age: float = MAX_AGE_SECONDS) -> tuple[bool, str]:
    try:
        age = time.time() - float(path.read_text())
    except (FileNotFoundError, ValueError):
        return False, "kein Heartbeat"
    if age > max_age:
        return False, f"letzter Heartbeat vor {age:.0f}s"
    return True, f"Heartbeat vor {age:.0f}s"


def main() -> None:
    healthy, reason = is_healthy()
    print(reason)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
