"""Started in the background by run.bat: opens the web player once the server is ready."""
import json
import os
import sys
import time
import urllib.request
import webbrowser

if os.environ.get("KARAOKE_NO_BROWSER"):
    sys.exit(0)

for _ in range(900):
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=1))
        if d.get("model_ready"):
            webbrowser.open("http://127.0.0.1:8765")
            break
    except Exception:
        pass
    time.sleep(1)
