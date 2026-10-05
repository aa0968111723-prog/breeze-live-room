from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    port: int = 8780
    max_audio_bytes: int = 8 * 1024 * 1024
    max_audio_seconds: float = 30.0
    max_rooms: int = 8
    max_listeners: int = 40
    max_queue: int = 8
    asr_workers: int = 1
    history_limit: int = 200
    allow_testclient: bool = False
    share_host: str = ""
    share_scheme: str = "http"
    translate: bool = True
    data_path: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            port=_int("BREEZE_PORT", 8780),
            max_audio_bytes=_int("BREEZE_MAX_AUDIO_BYTES", 8 * 1024 * 1024),
            max_audio_seconds=float(os.getenv("BREEZE_MAX_AUDIO_SECONDS", "30")),
            max_rooms=_int("BREEZE_MAX_ROOMS", 8),
            max_listeners=_int("BREEZE_MAX_LISTENERS", 40),
            max_queue=_int("BREEZE_MAX_QUEUE", 8),
            asr_workers=_int("BREEZE_ASR_WORKERS", 1),
            history_limit=_int("BREEZE_HISTORY_LIMIT", 200),
            allow_testclient=os.getenv("BREEZE_ALLOW_TESTCLIENT") == "1",
            share_host=os.getenv("BREEZE_SHARE_HOST", ""),
            share_scheme=os.getenv("BREEZE_SHARE_SCHEME", "http"),
            translate=os.getenv("BREEZE_TRANSLATE", "1") != "0",
            data_path=os.getenv("BREEZE_DATA_PATH", ""),
        )
