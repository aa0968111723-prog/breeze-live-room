from __future__ import annotations

import json
from pathlib import Path


def annotate_question(zh: str) -> str:
    """Add a fullwidth question mark only when the line already asks one. Keep the raw ASR text elsewhere."""
    text = (zh or "").strip()
    if not text or text.endswith(("?", "？", "。", "！")):
        return text
    if text.endswith(("嗎", "呢")):
        return text + "？"
    return text


def should_join(prev: str, nxt: str, gap_ms: int) -> bool:
    """Conservative join. Live segments stay separate so retries keep the same segment id."""
    if gap_ms > 400 or not prev or not nxt:
        return False
    if prev[-1] in "。！？?!.":
        return False
    if len(nxt) > 8:
        return False
    return True


def parse_glossary(raw: str, limit: int = 40) -> list[dict]:
    rows = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        zh, en = text.split("=", 1)
        zh = zh.strip()[:40]
        en = en.strip()[:80]
        if zh and en:
            rows.append({"zh": zh, "en": en})
        if len(rows) >= limit:
            break
    return rows


def export_text(events: list[dict], kind: str) -> str:
    rows = [item for item in events if item.get("status") not in {"missing", "error", "timeout", "cancelled"} or item.get("zh") or item.get("en")]
    if kind == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if kind == "txt":
        lines = []
        for item in rows:
            stamp = _range(item)
            body = " ".join(part for part in (item.get("zh") or "", item.get("en") or "") if part)
            lines.append(f"{stamp} {body}".strip())
        return "\n".join(lines) + ("\n" if lines else "")
    if kind in {"srt", "vtt"}:
        return _cues(rows, vtt=kind == "vtt")
    raise ValueError("不支援的匯出格式")


def _range(item: dict) -> str:
    if item.get("t0_ms") is None:
        return ""
    start = int(item["t0_ms"])
    end = int(item["t1_ms"]) if item.get("t1_ms") is not None else start + 1000
    return f"{_ts(start)} --> {_ts(end)}"


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _cues(rows: list[dict], vtt: bool) -> str:
    blocks = ["WEBVTT\n"] if vtt else []
    index = 1
    for item in rows:
        if item.get("t0_ms") is None:
            continue
        start = int(item["t0_ms"])
        end = int(item["t1_ms"]) if item.get("t1_ms") is not None else start + 1000
        if end <= start:
            end = start + 400
        text = item.get("zh") or ""
        if item.get("en"):
            text = (text + "\n" + item["en"]).strip()
        if not text:
            continue
        stamp = f"{_ts(start)} --> {_ts(end)}"
        if vtt:
            stamp = stamp.replace(",", ".")
            blocks.append(f"{stamp}\n{text}\n")
        else:
            blocks.append(f"{index}\n{stamp}\n{text}\n")
        index += 1
    return "\n".join(blocks)


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
