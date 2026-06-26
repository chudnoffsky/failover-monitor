"""
Healthcheck скрипт для контейнера монитора
"""

import os
import sys
import urllib.request

PORT = os.getenv("STATUS_PORT", "8080")
URL = f"http://127.0.0.1:{PORT}/healthz"

try:
    with urllib.request.urlopen(URL, timeout=3) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
