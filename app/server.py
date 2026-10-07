from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from email.utils import formatdate
from mimetypes import guess_type
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from app.aio import cancellation_pending, wait_bounded
from app.asr import CliAsr, ResidentAsr
from app.audio import AudioError, convert_to_wav, ffmpeg_bin, wav_duration_seconds
from app.auth import audience_origin_allowed, new_host_token, require_host, require_local_host, same_secret
from app.dispatch import ListenerSlot, RoomBus, for_listener
from app.pipeline import Pipeline, PipelineError, Segment
from app.rooms import RoomBook, RoomIdError, validate_room_id, validate_session_id
from app.settings import Settings, fill_process_environ
from app.share import list_share_hosts, listen_url
from app.store import CaptionStore
from app.textutil import export_text, parse_glossary
from app.translate import Translator

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
DEFAULT_WHISPER = ROOT / "tools" / "whisper-cli.exe"
DEFAULT_SERVER = ROOT / "tools" / "whisper-server.exe"
TMP = ROOT / "tmp"
PROMPT = "以下是台灣國語的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"

_TRACKED: list[FastAPI] = []
# First 8 hex digits of the file's sha256. Long enough to bust a cache, short enough for a URL.
_ASSET_HASH_LEN = 8
_STAMP_SUFFIXES = {".html", ".htm", ".js", ".mjs"}
# from "...js", import "...js", and import("...js"). Query strings are replaced, not stacked.
_MODULE_IMPORT_RE = re.compile(
    r"""(?P<lead>\bfrom\s+|\bimport(?:\s*\(\s*|\s+))(?P<quote>["'])"""
    r"""(?P<url>/static/(?P<name>[^"'\\?#]+?\.js))(?:\?[^"'\\]*)?(?P=quote)"""
)
# Embedded in a <script type="module"> URL. Anything else is not a version.
_VERSION_RE = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")


def app_version() -> str:
    try:
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"
    token = raw.split()[0] if raw else ""
    if _VERSION_RE.fullmatch(token) is None:
        return "0"
    return token


def static_asset_token(path: Path) -> str:
    """Token embedded in import URLs. VERSION alone does not move when a script changes."""

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:_ASSET_HASH_LEN]
    return f"{app_version()}-{digest}"


def _static_import_target(name: str, static_dir: Path) -> Path | None:
    if not name or name.startswith(("/", "\\")) or "\\" in name or ".." in Path(name).parts:
        return None
    # NUL raises ValueError from resolve(); a very long name raises OSError from is_file().
    try:
        root = static_dir.resolve()
        target = (root / name).resolve()
        target.relative_to(root)
        if not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    return target


def _rewrite_static_imports(text: str, static_dir: Path) -> tuple[str, list[Path]]:
    targets: list[Path] = []

    def repl(match: re.Match[str]) -> str:
        target = _static_import_target(match.group("name"), static_dir)
        if target is None:
            return match.group(0)
        try:
            token = static_asset_token(target)
        except OSError:
            return match.group(0)
        targets.append(target)
        lead = match.group("lead")
        quote = match.group("quote")
        url = match.group("url")
        return f"{lead}{quote}{url}?v={token}{quote}"

    return _MODULE_IMPORT_RE.sub(repl, text), targets


def stamp_static_imports(text: str, static_dir: Path) -> str:
    """Add ?v=<version>-<content hash> to /static/*.js module imports."""

    return _rewrite_static_imports(text, static_dir)[0]


def _body_etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _static_media_type(path: str | os.PathLike[str]) -> str:
    """Serve scripts as text/javascript. Windows may map .js to text/plain."""

    if Path(path).suffix.lower() in {".js", ".mjs"}:
        return "text/javascript"
    return guess_type(str(path))[0] or "application/octet-stream"


def _stamped_static(path: Path, static_dir: Path, stat_result: os.stat_result) -> tuple[bytes, str] | None:
    if path.suffix.lower() not in _STAMP_SUFFIXES:
        return None
    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    stamped, targets = _rewrite_static_imports(text, static_dir)
    rewritten = stamped.encode("utf-8")
    if rewritten == original:
        return None
    mtimes = [stat_result.st_mtime]
    for target in targets:
        try:
            mtimes.append(target.stat().st_mtime)
        except OSError:
            continue
    return rewritten, formatdate(max(mtimes), usegmt=True)


class _RewrittenStaticResponse(Response):
    """Stamped HTML or JS. HEAD stays header-only, same as FileResponse."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and str(scope.get("method", "GET")).upper() == "HEAD":
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            if self.background is not None:
                await self.background()
            return
        await super().__call__(scope, receive, send)


class RevalidatingStaticFiles(StaticFiles):
    """Send Cache-Control: no-cache and keep ETag so browsers revalidate.

    A cached room_client.js paired with a newer host.html or room.html throws
    on import and the page script never starts. Copies stored before this
    header existed will not revalidate during their heuristic lifetime, so
    served HTML and JS get a content-hash query on each /static/*.js import.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: dict,
        status_code: int = 200,
    ) -> Response:
        directory = Path(self.directory) if self.directory is not None else Path(full_path).parent
        stamped = _stamped_static(Path(full_path), directory, stat_result)
        media_type = _static_media_type(full_path)
        if stamped is None:
            response: Response = FileResponse(
                full_path,
                status_code=status_code,
                stat_result=stat_result,
                media_type=media_type,
                headers={"Cache-Control": "no-cache"},
            )
        else:
            body, last_modified = stamped
            # ETag is the stamped bytes, not mtime-size: a script edit changes
            # the injected ?v= without touching the HTML file's stat.
            response = _RewrittenStaticResponse(
                content=body,
                status_code=status_code,
                media_type=media_type,
                headers={
                    "Cache-Control": "no-cache",
                    "etag": _body_etag(body),
                    "last-modified": last_modified,
                },
            )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident = int(handle.read().split()[1])
        return resident * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


class Conn:
    def __init__(self, ws: WebSocket, maxsize: int):
        self.ws = ws
        self.slot = ListenerSlot(ws.send_json, maxsize=maxsize)
        self.slot.last_pong = time.monotonic()


# Audience replay is the newest screenful, never the whole class. Export stays
# complete. The whole replay hello, not only the backfill field, stays within
# 100 KiB so 180 replays a minute stay near 18 MB. A fatter body drops the oldest rows.
AUDIENCE_BACKFILL_ROWS = 200
AUDIENCE_BACKFILL_BYTES = 100 * 1024
_REPLAY_CONTROL = frozenset({"caption_deleted", "captions_cleared", "captions_expired"})


def _json_bytes(obj) -> int:
    """Starlette send_json: compact separators, UTF-8, non-ASCII left as-is."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _trim_audience_rows(rows: list[dict], budget: int = AUDIENCE_BACKFILL_BYTES) -> list[dict]:
    """Newest captions whose JSON array fits in `budget`. Each row is encoded once.

    Re-encoding the whole tail after every dropped row was quadratic. A long
    class then stalled the event loop for the whole replay burst.
    """
    if budget < 2 or not rows:
        return []
    tail = [for_listener(item) for item in rows[-AUDIENCE_BACKFILL_ROWS:]]
    sizes = [_json_bytes(item) for item in tail]
    used = 2
    count = 0
    for size in reversed(sizes):
        cost = size if count == 0 else size + 1
        if used + cost > budget:
            break
        used += cost
        count += 1
    if count:
        return tail[-count:]
    item = dict(tail[-1])
    for key in ("zh", "en", "error"):
        text = item.get(key)
        if isinstance(text, str) and len(text) > 80:
            item[key] = text[:80]
    return [item] if _json_bytes([item]) <= budget else []


def _cap_replay_hello(hello: dict, budget: int = AUDIENCE_BACKFILL_BYTES) -> dict:
    """Keep one replay hello within `budget` wire bytes.

    Newest backfill rows win. Captions already in that backfill are dropped
    from history and events first, then older captions, and only then the
    oldest backfill rows. A hello that already fits is left unchanged, so a
    short class still receives its live window and its backfill.
    """
    list_keys = [key for key in ("history", "events", "backfill") if isinstance(hello.get(key), list)]
    lists = {key: list(hello[key]) for key in list_keys}
    sizes = {key: [_json_bytes(row) for row in lists[key]] for key in list_keys}
    covered: dict[str, int] = {}
    for row in lists.get("backfill", ()):
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        if ident:
            covered[str(ident)] = int(row.get("version") or 1)

    def is_control(row: dict) -> bool:
        return str(row.get("type") or "") in _REPLAY_CONTROL

    def is_redundant(row: dict) -> bool:
        if not isinstance(row, dict) or is_control(row):
            return False
        ident = row.get("id")
        if not ident or str(ident) not in covered:
            return False
        return int(row.get("version") or 1) <= covered[str(ident)]

    def is_caption(row: dict) -> bool:
        return isinstance(row, dict) and not is_control(row) and bool(row.get("id"))

    drop_order: list[tuple[str, int]] = []

    def add_class(key: str, predicate) -> None:
        for index, row in enumerate(lists.get(key) or []):
            if predicate(row):
                drop_order.append((key, index))

    for key in ("history", "events"):
        add_class(key, is_redundant)
    for key in ("history", "events"):
        add_class(key, lambda row: is_caption(row) and not is_redundant(row))
    add_class("backfill", lambda row: True)
    for key in ("history", "events"):
        add_class(key, lambda row: isinstance(row, dict) and is_control(row))

    probe = dict(hello)
    for key in list_keys:
        probe[key] = []
    total = _json_bytes(probe)
    for key in list_keys:
        row_sizes = sizes[key]
        if row_sizes:
            total += sum(row_sizes) + len(row_sizes) - 1
    kept = {key: [True] * len(lists[key]) for key in list_keys}
    remaining = {key: len(lists[key]) for key in list_keys}
    last_backfill = len(lists["backfill"]) - 1 if lists.get("backfill") else -1

    def saving(key: str, index: int) -> int:
        size = sizes[key][index]
        if remaining[key] <= 1:
            return size
        return size + 1

    if total > budget:
        for key, index in drop_order:
            if total <= budget:
                break
            if not kept[key][index]:
                continue
            if key == "backfill" and index == last_backfill:
                continue
            total -= saving(key, index)
            kept[key][index] = False
            remaining[key] -= 1
    for key in list_keys:
        hello[key] = [row for row, flag in zip(lists[key], kept[key]) if flag]
    if _json_bytes(hello) <= budget:
        return hello
    # The newest row alone can still be fatter than the hello once the envelope
    # is counted. Shorten its text once; if that is not enough, send no row.
    backfill = [row for row in hello.get("backfill") or [] if isinstance(row, dict)]
    if backfill:
        item = dict(backfill[-1])
        for key in ("zh", "en", "error"):
            text = item.get(key)
            if isinstance(text, str) and len(text) > 80:
                item[key] = text[:80]
        hello["backfill"] = [item]
        if _json_bytes(hello) <= budget:
            return hello
        hello["backfill"] = []
    if _json_bytes(hello) <= budget:
        return hello
    hello["history"] = []
    hello["events"] = []
    return hello


# Replay buckets are keyed by the TCP peer, not by whatever id the client sends.
# A full table drops expired keys, then the quietest one, instead of growing forever.
_REPLAY_KEY_CAP = 4096


class _ReplayGate:
    """Caps replay/backfill hellos. Live captions are not counted.

    One bucket is the TCP peer address (a classroom behind NAT shares it).
    The other is that address plus the audience client id, and it can only
    tighten the address cap. A missing or forged client id shares one bucket
    per address. A refused hello is explicit: the caller sends
    backfill_deferred instead of an empty screen.
    """

    def __init__(
        self,
        ip_limit: int,
        client_limit: int | None = None,
        window_s: float = 60.0,
        *,
        clock=None,
        max_keys: int = _REPLAY_KEY_CAP,
    ):
        self.ip_limit = max(1, int(ip_limit))
        self.limit = self.ip_limit
        requested = self.ip_limit if client_limit is None else int(client_limit)
        self.client_limit = max(1, min(self.ip_limit, requested))
        self.window_s = float(window_s)
        self.max_keys = max(1, int(max_keys))
        self._clock = clock or time.monotonic
        self._ip_hits: dict[str, list[float]] = {}
        self._client_hits: dict[tuple[str, str], list[float]] = {}

    def _prune(self, bucket: list[float], now: float) -> None:
        cutoff = now - self.window_s
        if bucket and bucket[0] <= cutoff:
            bucket[:] = [item for item in bucket if item > cutoff]

    def _drop_expired(self, store: dict, now: float) -> None:
        cutoff = now - self.window_s
        dead = [key for key, bucket in store.items() if not bucket or bucket[-1] <= cutoff]
        for key in dead:
            del store[key]

    def _forget_expired(self, now: float) -> None:
        self._drop_expired(self._ip_hits, now)
        self._drop_expired(self._client_hits, now)

    def _make_room(self, store: dict, now: float) -> None:
        if len(store) < self.max_keys:
            return
        # This table only. The other one may hold an empty bucket not stamped yet.
        self._drop_expired(store, now)
        while len(store) >= self.max_keys:
            oldest = min(store, key=lambda key: (store[key][-1] if store[key] else 0.0))
            del store[oldest]

    def _take(self, store: dict, key, now: float) -> list[float]:
        bucket = store.get(key)
        if bucket is not None:
            self._prune(bucket, now)
            if bucket:
                return bucket
            del store[key]
        self._make_room(store, now)
        fresh: list[float] = []
        store[key] = fresh
        return fresh

    def _retry_ms(self, bucket: list[float], now: float) -> int:
        if not bucket:
            return 1000
        wait = self.window_s - (now - bucket[0])
        if wait < 0.25:
            wait = 0.25
        return int(wait * 1000) + 1

    def allow(self, ip: str, client_id: str = "") -> tuple[bool, int]:
        now = float(self._clock())
        self._forget_expired(now)
        ip_key = ip or ""
        who = client_id or ""
        ip_bucket = self._take(self._ip_hits, ip_key, now)
        if len(ip_bucket) >= self.ip_limit:
            # A refused address must not allocate a client bucket.
            return False, self._retry_ms(ip_bucket, now)
        client_bucket = self._take(self._client_hits, (ip_key, who), now)
        if len(client_bucket) >= self.client_limit:
            return False, self._retry_ms(client_bucket, now)
        ip_bucket.append(now)
        client_bucket.append(now)
        return True, 0


# Room for multipart boundaries and the small text fields around one audio part.
_BODY_SLOP = 65536
_FIELD_MAX = 64 * 1024


def _content_too_large(request: Request, settings: Settings) -> bool:
    raw = request.headers.get("content-length")
    if not raw:
        return False
    try:
        size = int(raw)
    except ValueError:
        return False
    return size > settings.max_audio_bytes + _BODY_SLOP


async def _wait_until_join_ready(pipeline: Pipeline, key: tuple[str, str, int], timeout: float) -> None:
    """Owner has reserved this segment but not registered a flight. Do not take another slot."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while key in pipeline._reserved or key in pipeline._active:
        flight = pipeline._flight.get(key)
        if flight is not None and not flight.done():
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.01)


async def _read_upload(upload, limit: int) -> bytes:
    if upload is None or isinstance(upload, str):
        raise AudioError(400, "沒有收到音訊")
    chunks = []
    total = 0
    while True:
        block = await upload.read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise AudioError(413, f"音訊超過 {limit} bytes，已拒絕")
        chunks.append(block)
    if total == 0:
        raise AudioError(400, "沒有收到音訊")
    return b"".join(chunks)


async def _read_body_capped(request: Request, limit: int, settings: Settings) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        chunks.append(chunk)
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


async def _push_form(request: Request, settings: Settings):
    """Read a capped multipart body, then parse it with Starlette.

    Non-multipart is 415. A body over the cap is 413. A read that outlives
    upload_read_timeout_s is 408. A malformed part is 400, never 500.
    """
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "multipart/form-data":
        raise HTTPException(status_code=415, detail="上傳格式不正確")
    try:
        async with asyncio.timeout(settings.upload_read_timeout_s):
            body = await _read_body_capped(request, settings.max_audio_bytes + _BODY_SLOP, settings)
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail="上傳逾時") from exc
    sent = False

    async def replay():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    capped = Request(request.scope, replay)
    try:
        return await capped.form(max_files=1, max_fields=16, max_part_size=_FIELD_MAX)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="上傳格式不正確") from exc


def _field(form, request: Request, name: str) -> str:
    value = form.get(name)
    if value is None or isinstance(value, str) and value == "":
        value = request.query_params.get(name, "")
    return str(value or "")


_MAX_SEGMENT_MS = 48 * 60 * 60 * 1000


def _optional_ms(form, request: Request, name: str) -> int | None:
    raw = _field(form, request, name)
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return 0
    if value > _MAX_SEGMENT_MS:
        return _MAX_SEGMENT_MS
    return value


def _public_result(done: Segment) -> JSONResponse:
    if done.status == "timeout":
        code = 408
    elif done.status == "cancelled":
        code = 409
    elif done.status == "error":
        code = 422
    else:
        code = 200
    body = done.public()
    body["ok"] = code == 200
    body["detail"] = done.error
    return JSONResponse(status_code=code, content=body)


class _ReferrerPolicy:
    """Set Referrer-Policy without buffering the body.

    Starlette's http decorator middleware reads each response into memory.
    The 1000-segment class is measured in RSS, so this stays a header stamp.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_policy(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"referrer-policy", b"no-referrer"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_policy)


def create_app(settings: Settings | None = None, asr=None, translator: Translator | None = None, decoder=None) -> FastAPI:
    if settings is None:
        fill_process_environ()
        settings = Settings.from_env()
    token = new_host_token()
    translator = translator or Translator(
        enabled=settings.translate,
        key=os.getenv("OPENAI_API_KEY", ""),
        token_budget=settings.token_budget,
    )
    model = Path(settings.model_path) if settings.model_path else DEFAULT_MODEL
    whisper = Path(settings.whisper_path) if settings.whisper_path else DEFAULT_WHISPER
    resident_error = ""
    book = RoomBook(settings.max_rooms, settings.room_idle_s)
    bus = RoomBus(settings.history_limit, caption_cap=getattr(settings, "room_caption_cap", 5000))
    store = CaptionStore(settings.data_path or None)
    if asr is None:
        if settings.asr_mode == "resident":
            resident = ResidentAsr(
                settings.resident_url,
                server_bin=Path(settings.server_path) if settings.server_path else DEFAULT_SERVER,
                model=model,
                threads=settings.asr_threads,
                startup_timeout_s=settings.resident_startup_s,
                inference_timeout_s=settings.asr_timeout_s,
                audio_context=settings.asr_audio_context,
                beam_size=settings.asr_beam_size,
                best_of=settings.asr_best_of,
            )
            started = resident.start()
            if started.ok:
                asr = resident
            else:
                resident_error = started.error
                asr = resident
        else:
            asr = CliAsr(whisper, model, threads=settings.asr_threads, timeout_s=settings.asr_timeout_s, audio_context=settings.asr_audio_context, beam_size=settings.asr_beam_size, best_of=settings.asr_best_of)
    share_override = {"host": settings.share_host}
    tasks: list[asyncio.Task] = []
    replay_floors: dict[str, float] = {}
    replay_gate = _ReplayGate(settings.replay_per_minute, settings.replay_client_per_minute)
    share_cache: dict[str, object] = {"at": 0.0, "hosts": []}

    def current_host() -> str | None:
        host = (share_override["host"] or "").strip()
        return host or None

    def _audience_extra_hosts() -> tuple[str, ...]:
        now = time.monotonic()
        cached_at = float(share_cache.get("at") or 0.0)
        cached = share_cache.get("hosts")
        if not isinstance(cached, list) or now - cached_at >= 5:
            try:
                cached = list_share_hosts()
            except Exception:
                logging.getLogger("breeze.server").exception("share host list failed")
                cached = []
            share_cache["hosts"] = cached
            share_cache["at"] = now
        hosts = [str(item) for item in cached if item]
        chosen = current_host()
        if chosen:
            hosts.append(chosen)
        if settings.share_host:
            hosts.append(settings.share_host)
        return tuple(hosts)

    def _load_replay_floor(room_id: str) -> float | None:
        if room_id in replay_floors:
            return replay_floors[room_id]
        if not store.enabled:
            return None
        try:
            value = store.get_replay_floor(room_id)
        except Exception:
            logging.getLogger("breeze.server").exception("replay floor lookup failed")
            return None
        if value is None:
            return None
        replay_floors[room_id] = value
        return value

    def _seal_replay(room_id: str) -> None:
        # Flush first so saved updated_at values are already behind this floor.
        if store.enabled:
            try:
                store.flush()
            except Exception:
                logging.getLogger("breeze.server").exception("caption store flush before replay seal failed")
        floor = time.time()
        replay_floors[room_id] = floor
        if store.enabled:
            try:
                store.set_replay_floor(room_id, floor)
            except Exception:
                logging.getLogger("breeze.server").exception("replay floor save failed")

    def ensure_room(room_id: str) -> dict:
        fresh = book.get(room_id) is None
        room = book.open(room_id)
        if fresh:
            room["replay_not_before"] = _load_replay_floor(room_id)
        return room

    def _host_authorized(request: Request) -> bool:
        try:
            require_host(request, token, settings)
        except HTTPException:
            return False
        return True

    def _listen_key_of(room_id: str) -> str:
        room = book.get(room_id)
        if not room:
            return ""
        return str(room.get("listen_key") or "")

    def _listener_authorized(ws: WebSocket, room: dict, listen_key: str) -> bool:
        if same_secret(listen_key, str(room.get("listen_key") or "")):
            return True
        header = ws.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return same_secret(supplied, token)

    def _captions_since_open(room_id: str, rows: list[dict]) -> list[dict]:
        room = book.get(room_id)
        floor = None if room is None else room.get("replay_not_before")
        if floor is None:
            return list(rows)
        stamps = bus._caption_at.get(room_id, {})
        kept: list[dict] = []
        for row in rows:
            kind = str(row.get("type") or "")
            # Expiry has no caption timestamp. Dropping it here hid the notice
            # from a listener who reconnected after the line had already aged out.
            if kind in {"captions_cleared", "caption_deleted", "captions_expired", "ping", "pong"}:
                kept.append(row)
                continue
            stamp = stamps.get(str(row.get("id") or ""))
            if stamp is not None and float(stamp) > float(floor):
                kept.append(row)
        return kept

    def _audience_captions(rows: list[dict]) -> list[dict]:
        return [for_listener(item) for item in rows]

    def _audience_backfill(rows: list[dict]) -> list[dict]:
        """Last rows a listener may see, and never a larger JSON body than the budget.

        The wire encoding matches Starlette's send_json (compact separators).
        """
        return _trim_audience_rows(rows, AUDIENCE_BACKFILL_BYTES)

    def _fanout(snap: dict) -> None:
        room = book.get(str(snap.get("room_id") or ""))
        if room is None:
            return
        room["history"] = bus.history(str(snap.get("room_id") or ""))
        room["last_active"] = time.monotonic()
        outgoing = for_listener(snap)
        dead = []
        for conn in list(room["listeners"]):
            if not conn.slot.offer(outgoing):
                dead.append(conn)
        for conn in dead:
            room["listeners"].discard(conn)

    def _announce_live(room_id: str) -> None:
        """Tell this room's listeners whether the host mic is on. No secrets."""
        room = book.get(room_id)
        if room is None:
            return
        note = {"type": "room", "room_id": room_id, "live": bool(room.get("session_active"))}
        dead = []
        for conn in list(room["listeners"]):
            if not conn.slot.offer(note):
                dead.append(conn)
        for conn in dead:
            room["listeners"].discard(conn)

    def on_event(event: dict):
        try:
            snap = bus.publish(event)
            if snap is None:
                return None
            _fanout(snap)
            if store.enabled and snap.get("id") and snap.get("type") not in {"captions_cleared", "caption_deleted", "captions_expired"}:
                store.submit_save(snap)
            return snap
        except Exception:
            logging.getLogger("breeze.server").exception("caption publish failed")
            return None

    def _release_room(room_id: str) -> None:
        """Idle or close drops runtime, not captions that are still inside the ttl."""
        keep = bus.has_captions(room_id)
        if not keep and store.enabled:
            try:
                keep = store.has_room(room_id)
            except Exception:
                logging.getLogger("breeze.server").exception("caption store lookup failed")
                keep = True
        retained: list[dict] = []
        if keep:
            bus.retire(room_id)
            retained = bus.caption_state(room_id)
            if not retained and store.enabled:
                try:
                    retained = store.room_rows(room_id)
                except Exception:
                    logging.getLogger("breeze.server").exception("caption order lookup failed")
                    retained = []
        else:
            bus.drop(room_id)
        pipeline.drop_room(room_id)
        if retained:
            pipeline.note_retained_order(room_id, retained)
        _seal_replay(room_id)

    def _split_kept_id(room_id: str, seg_id: str) -> tuple[str, int]:
        prefix = room_id + ":"
        if not str(seg_id).startswith(prefix):
            return "", 0
        session, sep, seq_text = str(seg_id)[len(prefix):].rpartition(":")
        if not sep:
            return "", 0
        try:
            seq = int(seq_text)
        except ValueError:
            return "", 0
        if seq < 1 or not session:
            return "", 0
        return session, seq

    def _expire_captions() -> None:
        # Each caption expires on its own updated time, including in an open room.
        # SQLite purge uses the same rule. An empty idle room then drops its runtime.
        removed = bus.prune_expired(settings.caption_ttl_s)
        expired: dict[str, list[str]] = {}
        for room_id, seg_id in removed:
            session_id, seq = _split_kept_id(room_id, seg_id)
            if session_id and seq:
                pipeline.forget_expired(room_id, session_id, seq)
            expired.setdefault(room_id, []).append(seg_id)
        for room_id, ids in expired.items():
            # Listeners must hear this. Dropping the line with no event looks like a glitch.
            on_event({"type": "captions_expired", "room_id": room_id, "ids": ids})
        for room_id in list(bus._state):
            if bus.has_captions(room_id) or book.get(room_id) is not None:
                continue
            bus.drop(room_id)
            pipeline.drop_room(room_id)

    pipeline = Pipeline(asr, translator, PROMPT, TMP, settings, on_event=on_event)
    hydrated: set[str] = set()
    hydrate_jobs: dict[str, asyncio.Task] = {}

    async def _hydrate_room(room_id: str) -> None:
        if room_id in hydrated:
            return
        if not store.enabled:
            hydrated.add(room_id)
            return
        try:
            rows = await asyncio.to_thread(store.room_rows, room_id)
        except Exception:
            logging.getLogger("breeze.server").exception("caption hydrate failed")
            hydrate_jobs.pop(room_id, None)
            return
        cutoff = time.time() - float(settings.caption_ttl_s)
        fresh = []
        for row in rows or []:
            try:
                updated = float(row.get("updated_at") or 0)
            except (TypeError, ValueError):
                updated = 0.0
            if updated and updated < cutoff:
                continue
            fresh.append(row)
        hydrated.add(room_id)
        bus.hydrate(room_id, fresh)
        pipeline.seed_from_store(room_id, fresh)

    async def ensure_hydrated(room_id: str) -> None:
        """Load this room from SQLite the first time it is opened, joined, or pushed."""
        if room_id in hydrated:
            return
        job = hydrate_jobs.get(room_id)
        if job is None:
            job = asyncio.get_running_loop().create_task(_hydrate_room(room_id))
            hydrate_jobs[room_id] = job
        await job

    def decode(src: Path, work: Path) -> Path:
        if decoder:
            return decoder(src, work)
        ffmpeg = ffmpeg_bin(ROOT)
        if not ffmpeg:
            raise AudioError(422, "找不到 ffmpeg。請安裝 ffmpeg，或把 ffmpeg.exe 放進 tools\\")
        wav = convert_to_wav(src, work, ffmpeg, timeout=int(settings.decode_timeout_s))
        seconds = wav_duration_seconds(wav)
        if seconds is not None and seconds > settings.max_audio_seconds:
            raise AudioError(413, f"音訊長於 {settings.max_audio_seconds} 秒，已拒絕")
        return wav

    async def sweep_once() -> None:
        try:
            for room_id in book.sweep():
                _release_room(room_id)
            _expire_captions()
            if store.enabled:
                await asyncio.to_thread(store.purge_expired, settings.caption_ttl_s)
        except Exception:
            logging.getLogger("breeze.server").exception("sweep failed")

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(1)
            await sweep_once()

    async def shutdown() -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        await pipeline.aclose()
        if hasattr(asr, "close"):
            asr.close()
        store.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.ensure_workers()
        tasks.append(asyncio.create_task(sweep_loop()))
        # After create_app returns the bus is still empty (tests depend on that).
        # Replaying starts here, before the first request is served.
        if store.enabled:
            try:
                room_ids = await asyncio.to_thread(store.room_ids)
            except Exception:
                logging.getLogger("breeze.server").exception("caption room list failed")
                room_ids = []
            for room_id in room_ids:
                await ensure_hydrated(room_id)
        try:
            yield
        finally:
            await shutdown()

    app = FastAPI(title="breeze-live-room", lifespan=lifespan)
    # Outer header only. The listen key is in the page query; do not send that URL onward.
    app.add_middleware(_ReferrerPolicy)

    static_files = RevalidatingStaticFiles(directory=STATIC)
    app.mount("/static", static_files, name="static")
    app.state.settings = settings
    app.state.token = token
    app.state.pipeline = pipeline
    app.state.rooms = book.rooms
    app.state.room_book = book
    app.state.bus = bus
    app.state.translator = translator
    app.state.asr = asr
    app.state.store = store
    app.state.share_override = share_override
    app.state.shutdown = shutdown
    app.state.sweep_once = sweep_once
    app.state.resident_error = resident_error

    def share_for(room_id: str, *, include_key: bool = False) -> str | None:
        key = _listen_key_of(room_id) if include_key else ""
        return listen_url(room_id, settings.port, settings.share_scheme, current_host(), key or None)

    async def _page(path: Path, request: Request) -> Response:
        stat_result = await asyncio.to_thread(path.stat)
        return static_files.file_response(
            path,
            stat_result,
            {"type": "http", "headers": request.scope["headers"]},
        )

    @app.get("/")
    async def host_page(request: Request) -> Response:
        return await _page(STATIC / "host.html", request)

    @app.get("/r/{room_id}")
    async def room_page(request: Request, room_id: str) -> Response:
        try:
            validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _page(STATIC / "room.html", request)

    @app.get("/api/host-token")
    async def host_token(request: Request) -> Response:
        require_local_host(request, settings)
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/api/setup")
    async def setup(request: Request, room_id: str = "class") -> dict:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        host_view = _host_authorized(request)
        url = share_for(room_id, include_key=host_view)
        resident_ready = isinstance(asr, ResidentAsr) and await asyncio.to_thread(asr.health)
        asr_ready = resident_ready if isinstance(asr, ResidentAsr) else (whisper.is_file() and model.is_file() if isinstance(asr, CliAsr) else True)
        payload = {
            "room": room_id,
            "listen_url": url,
            "share_ready": url is not None,
            "share_message": None if url else "尚無可供其他裝置使用的連結",
            "share_hosts": list_share_hosts(),
            "share_host": current_host() or "",
            "port": settings.port,
            "whisper": whisper.exists(),
            "model": model.exists(),
            "ffmpeg": ffmpeg_bin(ROOT) is not None,
            "translate_configured": bool(translator.key) and settings.translate,
            "translate_verified": False,
            "translate_label": translator.status_label(),
            "asr_mode": "resident" if isinstance(asr, ResidentAsr) else "cli",
            "asr_ready": asr_ready,
            "model_reloads_each_segment": isinstance(asr, CliAsr),
            "resident_error": getattr(asr, "last_error", "") or resident_error,
            "host_token": None,
            "queue": pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "storage": store.enabled,
            "storage_recovered": bool(getattr(store, "recovered", False)),
        }
        if host_view:
            key = _listen_key_of(room_id)
            if key:
                payload["listen_key"] = key
        return payload

    @app.get("/api/health")
    async def health() -> Response:
        asr_ready = await asyncio.to_thread(asr.health) if isinstance(asr, ResidentAsr) else (whisper.is_file() and model.is_file() if isinstance(asr, CliAsr) else True)
        ready = asr_ready and (decoder is not None or ffmpeg_bin(ROOT) is not None)
        return JSONResponse({"service": "breeze-live-room", "ready": ready, "asr_ready": asr_ready}, status_code=200 if ready else 503, headers={"Cache-Control": "no-store"})

    @app.get("/api/qr")
    async def qr(request: Request, room_id: str = "class") -> Response:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = share_for(room_id, include_key=_host_authorized(request))
        if not url:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "尚無可供其他裝置使用的連結"})
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png")

    @app.post("/api/rooms/open")
    async def open_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or "class"))
        room = ensure_room(room_id)
        await ensure_hydrated(room_id)
        return {
            "ok": True,
            "room": room_id,
            "listen_url": share_for(room_id, include_key=True),
            "listen_key": room.get("listen_key") or "",
        }

    @app.post("/api/rooms/touch")
    async def touch_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            raise HTTPException(status_code=404, detail="房間不存在或已結束")
        book.touch(room_id)
        return {"ok": True}

    @app.post("/api/rooms/close")
    async def close_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        room = book.close(room_id)
        closing = []
        if room:
            closing = list(room["listeners"])
            room["listeners"].clear()
        removed = book.sweep()
        for gone in removed:
            _release_room(gone)
        for conn in closing:
            try:
                await conn.ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "ended"})
            except Exception:
                pass
            conn.slot.alive = False
        for conn in closing:
            try:
                await conn.ws.close(code=4404)
            except Exception:
                pass
        return {"ok": True}

    @app.post("/api/session/active")
    async def session_active(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            ensure_room(room_id)
        book.set_session_active(room_id, bool(body.get("active")))
        _announce_live(room_id)
        return {"ok": True}

    @app.post("/api/session/end")
    async def session_end(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        await ensure_hydrated(room_id)
        flush_s = None
        if "flush_s" in body:
            try:
                flush_s = max(0.0, float(body.get("flush_s")))
            except (TypeError, ValueError):
                flush_s = None
        if str(body.get("flush", "1")).strip().lower() in {"0", "false", "no"}:
            flush_s = 0.0
        last_seq = None
        if "last_seq" in body and body.get("last_seq") is not None:
            try:
                last_seq = int(body.get("last_seq"))
            except (TypeError, ValueError):
                last_seq = None
        await pipeline.end_session(room_id, session_id, flush_s=flush_s, last_seq=last_seq)
        await asyncio.to_thread(store.flush)
        book.set_session_active(room_id, False)
        _announce_live(room_id)
        return {"ok": True}

    @app.post("/api/segment/missing")
    async def segment_missing(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        if book.get(room_id) is None:
            ensure_room(room_id)
        segment = pipeline.mark_missing(room_id, session_id, seq, str(body.get("reason") or "主持端放棄這段"))
        return {"ok": True, **segment.public()}

    @app.post("/api/segment/cancel")
    async def segment_cancel(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        try:
            segment = pipeline.request_cancel(room_id, session_id, seq)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status == "cancelled", **segment.public()}

    @app.post("/api/segment/retranslate")
    async def retranslate(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        zh = body.get("zh")
        try:
            segment = await pipeline.retranslate(room_id, session_id, seq, zh if isinstance(zh, str) else None)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status != "error", **segment.public()}

    @app.post("/api/glossary")
    async def set_glossary(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or "default"))
        rows = parse_glossary(str(body.get("text") or ""))
        pipeline.glossary[(room_id, session_id)] = rows
        return {"ok": True, "count": len(rows)}

    @app.post("/api/share-host")
    async def set_share_host(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        share_override["host"] = str(body.get("host") or "").strip()
        room_id = str(body.get("room_id") or "class")
        return {
            "ok": True,
            "listen_url": share_for(room_id, include_key=True),
            "listen_key": _listen_key_of(room_id),
        }

    @app.get("/api/metrics")
    async def metrics(request: Request) -> dict:
        require_host(request, token, settings)
        return {
            **pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "rooms": book._active_count(),
            "rss_bytes": rss_bytes(),
            "tokens_used": translator.tokens_used,
            "price": translator.price_note(),
            "store_errors": store.errors,
            "storage_recovered": bool(getattr(store, "recovered", False)),
        }

    @app.get("/api/export")
    async def export(request: Request, room_id: str, kind: str = "txt") -> Response:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        if kind not in {"txt", "json", "srt", "vtt"}:
            raise HTTPException(status_code=400, detail="不支援的匯出格式")
        try:
            if store.enabled:
                events = await asyncio.to_thread(store.room_rows, room_id)
            else:
                events = bus.caption_state(room_id)
            payload = export_text(events, kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media = "application/json" if kind == "json" else "text/plain; charset=utf-8"
        return Response(payload, media_type=media, headers={"Cache-Control": "no-store"})

    @app.delete("/api/captions")
    async def delete_captions(request: Request, room_id: str, id: str = "", session_id: str = "", seq: int = 0) -> dict:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        if id or session_id or seq:
            segment_id, parsed_session, parsed_seq = _caption_target(room_id, id, session_id, seq)
            await ensure_hydrated(room_id)
            known = pipeline.caption_known(room_id, parsed_session, parsed_seq) or bus.has_caption(room_id, segment_id)
            if not known and store.enabled:
                try:
                    known = await asyncio.to_thread(store.has_id, room_id, segment_id)
                except Exception as exc:
                    logging.getLogger("breeze.server").exception("caption lookup failed")
                    raise HTTPException(status_code=503, detail="字幕儲存暫時無法讀取，沒有刪除") from exc
            if not known:
                raise HTTPException(status_code=404, detail="找不到這段字幕")
            # Seal before the store delete so a late emit cannot save the row again.
            # Memory and the bus stay until the store accepts the delete. A raised
            # store error must not report success or hide a row that will replay.
            pipeline.brace_delete(room_id, parsed_session, parsed_seq)
            pending = store.enqueue_delete_id(room_id, segment_id)
            try:
                removed = await asyncio.wrap_future(pending)
            except Exception:
                pipeline.abort_delete(room_id, parsed_session, parsed_seq)
                logging.getLogger("breeze.server").exception("caption store delete failed")
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "detail": "字幕儲存暫時無法刪除，畫面上的字幕還留著"},
                )
            pipeline.delete_segment(room_id, parsed_session, parsed_seq)
            event = bus.delete_caption(room_id, segment_id, parsed_session, parsed_seq)
            _fanout(event)
            return {"ok": True, "deleted": int(removed or 0), "id": segment_id}
        pipeline.mute_room(room_id)
        pending = store.enqueue_delete_room(room_id)
        try:
            removed = await asyncio.wrap_future(pending)
        except Exception:
            pipeline.unmute_room(room_id, abort=True)
            logging.getLogger("breeze.server").exception("caption store delete failed")
            return JSONResponse(
                status_code=503,
                content={"ok": False, "detail": "字幕儲存暫時無法刪除，畫面上的字幕還留著"},
            )
        pipeline.unmute_room(room_id)
        pipeline.invalidate_room(room_id)
        event = bus.clear_room(room_id)
        _fanout(event)
        room = book.get(room_id)
        if room is not None:
            room["history"] = []
        return {"ok": True, "deleted": int(removed or 0)}

    @app.post("/api/push")
    async def push(request: Request) -> dict:
        require_host(request, token, settings)
        if _content_too_large(request, settings):
            raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        form = None
        reserved = False
        reserved_key = None
        try:
            # Auth and Origin already ran. Read the body before taking an ASR slot
            # so a slow upload cannot sit on the recognition queue.
            form = await _push_form(request, settings)
            try:
                room_id = validate_room_id(_field(form, request, "room_id"))
                session_id = validate_session_id(_field(form, request, "session_id"))
            except RoomIdError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                seq = int(_field(form, request, "seq"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="段落序號不正確") from exc
            if seq < 1 or seq > 100000:
                raise HTTPException(status_code=400, detail="段落序號不正確")
            key = (room_id, session_id, seq)
            if not pipeline.joinable_without_slot(key):
                if not pipeline.try_admit_count():
                    raise HTTPException(status_code=429, detail="辨識佇列已滿，請稍後再送")
                reserved = True
                pipeline.note_reserved(key)
                reserved_key = key
            ensure_room(room_id)
            await ensure_hydrated(room_id)
            retry = _field(form, request, "retry") == "1" or request.headers.get("x-breeze-retry") == "1"
            t0_ms = _optional_ms(form, request, "t0_ms")
            t1_ms = _optional_ms(form, request, "t1_ms")
            # Both ends are present: keep a positive duration. Equal ends (and a
            # negative span that clamped to the same instant) would export a cue
            # with end == start, which is not a valid subtitle interval.
            if t0_ms is not None and t1_ms is not None and t1_ms <= t0_ms:
                if t0_ms >= _MAX_SEGMENT_MS:
                    t0_ms = _MAX_SEGMENT_MS - 1
                t1_ms = t0_ms + 1
            segment = Segment(
                room_id=room_id,
                session_id=session_id,
                seq=seq,
                t0_ms=t0_ms,
                t1_ms=t1_ms,
            )
            held = reserved
            reserved = False
            try:
                raw = await _read_upload(form.get("audio"), settings.max_audio_bytes)
            except AudioError as exc:
                pipeline.fail_received(segment, exc.detail)
                if held:
                    pipeline.release_slot()
                    held = False
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            if not held and (key in pipeline._reserved or key in pipeline._active):
                await _wait_until_join_ready(pipeline, key, pipeline._result_wait_s())
            # Default still waits for English. Opt in to return at Chinese: form
            # wait_translation=0 and/or header x-breeze-async-translation: 1.
            opt_out = _field(form, request, "wait_translation").strip().lower() in {"0", "false"}
            if request.headers.get("x-breeze-async-translation") == "1":
                opt_out = True
            try:
                done = await pipeline.submit(
                    segment, raw, decode, slot_held=held, retry=retry, owner=held, wait_translation=not opt_out,
                )
            except AudioError as exc:
                recorded = pipeline.get(segment.room_id, segment.session_id, segment.seq)
                if recorded is not None and recorded.status in {"error", "timeout", "cancelled"}:
                    body = recorded.public()
                    body["ok"] = False
                    body["detail"] = exc.detail or recorded.error
                    # AudioError keeps its own status. _public_result would turn every error into 422.
                    return JSONResponse(status_code=exc.status, content=body)
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            except PipelineError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            return _public_result(done)
        finally:
            if reserved:
                pipeline.release_slot()
            if reserved_key:
                pipeline.clear_reserved(reserved_key)
            if form is not None:
                await form.close()

    def _audience_client_id(cid: str) -> str:
        text = (cid or "").strip()
        if text and len(text) <= 64 and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in text):
            return text
        # Omitted and forged ids used to get a fresh bucket per socket, which
        # multiplied the per-client quota up to the per-address ceiling.
        return "anon"

    async def _refuse_listen(ws: WebSocket, code: int, reason: str, room_id: str = "") -> None:
        """Accept, name the refusal, then close.

        A close before accept is an HTTP 403. Browsers report that as 1006, so
        the page cannot show 「連結已失效」 or 「無法開啟」. The message carries
        no hello and no caption.
        """
        await ws.accept()
        note: dict = {"type": "room_unavailable", "reason": reason}
        if room_id:
            note["room_id"] = room_id
        try:
            await ws.send_json(note)
        except Exception:
            pass
        await ws.close(code=code)

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket, room_id: str = "class", cursor: int = 0, replay: int = 0, k: str = "", cid: str = "") -> None:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError:
            await _refuse_listen(ws, 1008, "rejected")
            return
        # Origin and the listen key are checked before any caption is read.
        # Accept still happens so the browser can see the refusal instead of 1006.
        if not audience_origin_allowed(
            ws.headers.get("origin"),
            ws.headers.get("host", ""),
            settings,
            _audience_extra_hosts(),
        ):
            await _refuse_listen(ws, 1008, "rejected", room_id)
            return
        room = book.get(room_id)
        if room is not None and not _listener_authorized(ws, room, k):
            await _refuse_listen(ws, 4401, "link_invalid", room_id)
            return
        await ws.accept()
        if room is None:
            # Accept is required to name the reason. The page must not treat this
            # accept as "live": the room is still closed or not open yet.
            stored = book.rooms.get(room_id)
            reason = "ended" if stored is not None and stored.get("ended") else "unknown_or_ended"
            note = {"type": "room_unavailable", "room_id": room_id, "reason": reason}
            if reason == "unknown_or_ended":
                note["retry_after_ms"] = 5000
            await ws.send_json(note)
            await ws.close(code=4404)
            return
        if len(room["listeners"]) >= settings.max_listeners:
            await ws.send_json({
                "type": "room_unavailable",
                "room_id": room_id,
                "reason": "full",
                "retry_after_ms": 20000,
            })
            await ws.close(code=1013)
            return
        conn = Conn(ws, settings.listener_queue)
        room["listeners"].add(conn)
        await ensure_hydrated(room_id)
        resumed = bus.since(room_id, cursor)
        history = bus.history(room_id) if cursor <= 0 else []
        events = list(resumed["events"]) if cursor > 0 else []
        hello = {
            "type": "hello",
            "history": _audience_captions(_captions_since_open(room_id, history)),
            "events": _audience_captions(_captions_since_open(room_id, events)),
            "gap": bool(resumed["gap"]) if cursor > 0 else False,
            "latest_cursor": bus.latest_cursor(room_id),
            "oldest_cursor": resumed["oldest_cursor"],
            "room_id": room_id,
            "epoch": bus.epoch(room_id),
            "host_live": bool(room.get("session_active")),
        }
        wants_backfill = int(replay or 0) == 1 or (cursor > 0 and bool(resumed.get("gap")))
        if wants_backfill:
            # TCP peer only. X-Forwarded-For, X-Real-IP, and Forwarded are not an address.
            # Decide before copying caption state so a refused replay does not pay for it.
            ip = ws.client.host if ws.client is not None else ""
            allowed, retry_ms = replay_gate.allow(ip, _audience_client_id(cid))
            if not allowed:
                # Say so. An omitted backfill used to look like an empty class.
                hello["backfill_deferred"] = True
                hello["retry_after"] = retry_ms
                hello["retry_after_ms"] = retry_ms
            else:
                if int(replay or 0) == 1:
                    source = bus.caption_state(room_id)
                else:
                    source = list(resumed.get("backfill") or [])
                visible = _captions_since_open(room_id, source)
                hello["backfill"] = _audience_backfill(visible)
            # history and events used to repeat the backfill. One hello stays within 100 KiB.
            _cap_replay_hello(hello)
        try:
            await ws.send_json(hello)
        except Exception:
            room["listeners"].discard(conn)
            return
        conn.slot.start()
        ping_task = asyncio.create_task(_ping(conn, settings))
        try:
            while True:
                if cancellation_pending():
                    raise asyncio.CancelledError()
                try:
                    raw = await wait_bounded(ws.receive_text(), settings.idle_timeout_s)
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
                if cancellation_pending():
                    raise asyncio.CancelledError()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "pong":
                    conn.slot.last_pong = time.monotonic()
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            room["listeners"].discard(conn)
            await conn.slot.close()

    _TRACKED.append(app)
    return app


def _caption_target(room_id: str, caption_id: str, session_id: str, seq: int) -> tuple[str, str, int]:
    if caption_id:
        prefix = room_id + ":"
        if not str(caption_id).startswith(prefix):
            raise HTTPException(status_code=400, detail="段落不屬於這個房間")
        session, sep, seq_text = str(caption_id)[len(prefix):].rpartition(":")
        if not sep:
            raise HTTPException(status_code=400, detail="段落代號不正確")
        try:
            parsed = int(seq_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="段落序號不正確") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        return str(caption_id), validate_session_id(session), parsed
    if session_id and seq:
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        session = validate_session_id(session_id)
        return f"{room_id}:{session}:{seq}", session, seq
    raise HTTPException(status_code=400, detail="刪除單段需要 id 或 session_id 與 seq")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="需要 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="需要 JSON 物件")
    return body


async def _ping(conn: Conn, settings: Settings) -> None:
    while True:
        await asyncio.sleep(max(settings.heartbeat_s, 0.05))
        if time.monotonic() - conn.slot.last_pong > settings.idle_timeout_s:
            try:
                await conn.ws.close(code=1001)
            except Exception:
                pass
            return
        if not conn.slot.offer({"type": "ping", "t": time.time()}):
            return


async def shutdown_tracked() -> None:
    while _TRACKED:
        app = _TRACKED.pop()
        shutdown = getattr(app.state, "shutdown", None)
        if shutdown:
            await shutdown()


app = create_app()
