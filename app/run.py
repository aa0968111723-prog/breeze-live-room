from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from app.settings import Settings, fill_process_environ


def main() -> None:
    fill_process_environ()
    settings = Settings.from_env()
    if os.getenv("BREEZE_OPEN_BROWSER") == "1":
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{settings.port}/")).start()
    uvicorn.run("app.server:app", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
