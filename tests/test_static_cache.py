"""Host and listener pages, and /static, must revalidate. Secrets and exports stay no-store.

Served pages stamp /static/*.js imports with ?v=<VERSION>-<sha256 prefix> so a
browser that cached the script before Cache-Control existed does not reuse it.
"""

import re
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.server import ROOT, STATIC, create_app, stamp_static_imports, static_asset_token
from app.settings import Settings
from app.translate import Translator

_VERSIONED_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport(?:\s*\(\s*|\s+))(?P<quote>["'])"""
    r"""/static/(?P<name>[^"'?#]+\.js)\?v=(?P<token>[^"'&#]+)(?P=quote)"""
)
_BARE_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport(?:\s*\(\s*|\s+))(?P<quote>["'])"""
    r"""(?P<url>/static/[^"'?#]+\.js)(?P=quote)"""
)


class IdleAsr:
    def health(self) -> bool:
        return True


def make_app():
    return create_app(
        Settings(allow_testclient=True, translate=False),
        asr=IdleAsr(),
        translator=Translator(enabled=False),
    )


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1:8780"}


def import_versions(text: str, static_dir: Path) -> dict[str, str]:
    bare = [match.group("url") for match in _BARE_IMPORT.finditer(text)]
    assert bare == [], bare
    found: dict[str, str] = {}
    for match in _VERSIONED_IMPORT.finditer(text):
        name = match.group("name")
        token = match.group("token")
        assert token == static_asset_token(static_dir / name), name
        found[name] = token
    assert found, "expected at least one /static/*.js import"
    return found


async def assert_revalidates(client: AsyncClient, path: str) -> Response:
    resp = await client.get(path)
    assert resp.status_code == 200, path
    assert resp.headers["cache-control"] == "no-cache", path
    assert resp.headers["etag"], path
    assert resp.headers["last-modified"], path
    etag = resp.headers["etag"]
    cached = await client.get(path, headers={"if-none-match": etag})
    assert cached.status_code == 304, path
    assert cached.content == b"", path
    assert cached.headers["cache-control"] == "no-cache", path
    assert cached.headers["etag"] == etag, path
    mismatch = await client.get(path, headers={"if-none-match": '"not-the-file"'})
    assert mismatch.status_code == 200, path
    assert mismatch.headers["cache-control"] == "no-cache", path
    return resp


@pytest.mark.anyio
async def test_pages_and_static_files_revalidate():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        pages = {
            "/": "禪譯主持",
            "/r/class": "禪譯聽眾",
        }
        scripts = sorted(STATIC.glob("*.js"))
        names = {script.name for script in scripts}
        assert {"room_client.js", "host_caption.js", "recorder_machine.js"} <= names
        for script in scripts:
            pages[f"/static/{script.name}"] = "export "
        for path, marker in pages.items():
            resp = await assert_revalidates(client, path)
            assert marker in resp.text, path
            assert int(resp.headers["content-length"]) == len(resp.content), path
            if path.startswith("/static/") and path.endswith(".js"):
                assert resp.text == (STATIC / path.removeprefix("/static/")).read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_module_imports_carry_a_content_version():
    app = make_app()
    transport = ASGITransport(app=app)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        host = await assert_revalidates(client, "/")
        assert host.headers["content-type"].startswith("text/html")
        assert host.text == stamp_static_imports((STATIC / "host.html").read_text(encoding="utf-8"), STATIC)
        host_versions = import_versions(host.text, STATIC)
        assert set(host_versions) == {"recorder_machine.js", "room_client.js", "host_caption.js"}

        room = await assert_revalidates(client, "/r/class")
        assert room.headers["content-type"].startswith("text/html")
        assert room.text == stamp_static_imports((STATIC / "room.html").read_text(encoding="utf-8"), STATIC)
        room_versions = import_versions(room.text, STATIC)
        assert set(room_versions) == {"room_client.js"}
        assert room_versions["room_client.js"] == host_versions["room_client.js"]

        for page in ("/static/host.html", "/static/room.html"):
            direct = await assert_revalidates(client, page)
            assert import_versions(direct.text, STATIC) == (
                host_versions if page.endswith("host.html") else room_versions
            )

        for name, token in host_versions.items():
            prefix, suffix = token.rsplit("-", 1)
            assert prefix == version, token
            assert len(suffix) == 8 and all(ch in "0123456789abcdef" for ch in suffix)
            url = f"/static/{name}?v={token}"
            served = await assert_revalidates(client, url)
            assert served.text == (STATIC / name).read_text(encoding="utf-8")
            ranged = await client.get(url, headers={"range": "bytes=0-9"})
            assert ranged.status_code == 206, url
            assert ranged.headers["cache-control"] == "no-cache", url
            assert ranged.content == served.content[:10]
            ignored = await client.get(f"/static/{name}?v=not-a-real-token")
            assert ignored.status_code == 200
            assert ignored.text == served.text
            assert ignored.headers["etag"] == served.headers["etag"]
            assert ignored.headers["cache-control"] == "no-cache"


@pytest.mark.anyio
async def test_changed_module_changes_its_import_version(monkeypatch, tmp_path):
    import app.server as server_mod

    copied = tmp_path / "static"
    shutil.copytree(server_mod.STATIC, copied)
    client_js = copied / "room_client.js"
    client_js.write_text(
        'import { needsEnglishRetry } from "/static/host_caption.js?v=stale";\n'
        + client_js.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_mod, "STATIC", copied)
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        first = await client.get("/")
        assert first.status_code == 200
        before = import_versions(first.text, copied)
        assert set(before) == {"recorder_machine.js", "room_client.js", "host_caption.js"}
        nested = await client.get("/static/room_client.js?v=" + before["room_client.js"])
        assert nested.status_code == 200
        assert nested.headers["cache-control"] == "no-cache"
        assert "v=stale" not in nested.text
        assert import_versions(nested.text, copied)["host_caption.js"] == before["host_caption.js"]
        nested_etag = nested.headers["etag"]

        caption = copied / "host_caption.js"
        caption.write_bytes(caption.read_bytes() + b"\n// touched\n")

        second = await client.get("/", headers={"if-none-match": first.headers["etag"]})
        assert second.status_code == 200
        assert second.headers["cache-control"] == "no-cache"
        assert second.headers["etag"] != first.headers["etag"]
        after = import_versions(second.text, copied)
        assert after["host_caption.js"] != before["host_caption.js"]
        assert after["recorder_machine.js"] == before["recorder_machine.js"]
        assert after["room_client.js"] == before["room_client.js"]
        fresh_page = await client.get("/", headers={"if-none-match": second.headers["etag"]})
        assert fresh_page.status_code == 304
        assert fresh_page.headers["etag"] == second.headers["etag"]

        url = "/static/room_client.js?v=" + after["room_client.js"]
        refreshed = await client.get(url)
        assert refreshed.status_code == 200
        assert refreshed.headers["etag"] != nested_etag
        assert import_versions(refreshed.text, copied)["host_caption.js"] == after["host_caption.js"]
        stale = await client.get(url, headers={"if-none-match": nested_etag})
        assert stale.status_code == 200
        renewed = await client.get(url, headers={"if-none-match": refreshed.headers["etag"]})
        assert renewed.status_code == 304
        assert renewed.content == b""
        assert renewed.headers["cache-control"] == "no-cache"
        assert renewed.headers["etag"] == refreshed.headers["etag"]

        caption_url = "/static/host_caption.js?v=" + after["host_caption.js"]
        caption_resp = await assert_revalidates(client, caption_url)
        assert "// touched" in caption_resp.text


@pytest.mark.anyio
async def test_host_token_readiness_and_export_stay_no_store():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token_resp = await client.get("/api/host-token")
        assert token_resp.status_code == 200, token_resp.text
        assert token_resp.headers["cache-control"] == "no-store"
        health = await client.get("/api/health")
        assert health.headers["cache-control"] == "no-store"
        exported = await client.get(
            "/api/export",
            params={"room_id": "class", "kind": "srt"},
            headers=auth(token_resp.json()["token"]),
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["cache-control"] == "no-store"
