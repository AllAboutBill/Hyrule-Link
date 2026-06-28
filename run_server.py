#!/usr/bin/env python3
"""Convenience launcher: python run_server.py [--host H] [--port P] [--open]."""
import argparse
import threading
import time
import webbrowser

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5019)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser on startup")
    a = ap.parse_args()

    if a.open:
        def _open():
            time.sleep(2)
            webbrowser.open(f"http://localhost:{a.port}/")
        threading.Thread(target=_open, daemon=True).start()

    print(f"\n  HyruleLink server -> http://localhost:{a.port}/")
    print("  Share that link (or your machine's IP) with the other players.\n")
    uvicorn.run("server.app:app", host=a.host, port=a.port, reload=a.reload)
