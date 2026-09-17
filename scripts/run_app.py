from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import sys

import uvicorn


APP_URL = "http://127.0.0.1:8000"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def open_browser_when_ready() -> None:
    """Open the served application only after Uvicorn is accepting requests."""
    for _ in range(100):
        try:
            with urllib.request.urlopen(APP_URL, timeout=0.5) as response:
                if response.status < 500:
                    webbrowser.open(APP_URL)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)


if __name__ == "__main__":
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
