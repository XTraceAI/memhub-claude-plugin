"""Shared auth for the plugin's terminal scripts — the SAME OAuth the /mcp
connector uses, so there is no separate CLI token to provision.

Resolution order:
1. ``$MEMHUB_TOKEN`` — explicit bearer for CI / headless runs.
2. OAuth (PKCE, public client) against the MemHub MCP server, using the same
   ``clientId`` / ``callbackPort`` the plugin's ``.mcp.json`` declares for the
   /mcp connector. First run opens the browser once (exactly like
   authenticating in /mcp); tokens are cached at
   ``~/.config/memhub-plugin/tokens-<host>.json`` (0600) and refreshed
   automatically by the MCP SDK's ``OAuthClientProvider``.

Usage from a sibling script:

    from _memhub_auth import resolve_url_and_auth
    url, headers, auth = resolve_url_and_auth()
    async with streamablehttp_client(url, headers=headers, auth=auth) as ...

Self-check:  uv run --with mcp python _memhub_auth.py
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

_SCRIPTS_DIR = Path(__file__).resolve().parent
_CACHE_DIR = Path.home() / ".config" / "memhub-plugin"


def _plugin_mcp_config() -> dict:
    """The memhub server entry from the plugin's .mcp.json (url, oauth)."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    cfg = (Path(root) if root else _SCRIPTS_DIR.parent) / ".mcp.json"
    servers = json.loads(cfg.read_text()).get("mcpServers", {})
    name = next((k for k in servers if k.lower().startswith("memhub")),
                next(iter(servers)) if len(servers) == 1 else None)
    if not name:
        raise RuntimeError(f"no memhub server entry in {cfg}")
    return servers[name]


def default_url() -> str:
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        path = os.environ.get("MEMHUB_MCP_SERVER_PATH", "/mcp-server/mcp")
        return f"{base.rstrip('/')}{path}"
    try:
        url = _plugin_mcp_config().get("url")
        if url:
            return url
    except Exception:  # noqa: BLE001
        pass
    return "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"


class _FileTokenStorage(TokenStorage):
    """Token cache keyed by server host; client info seeded statically from
    .mcp.json so the SDK skips dynamic client registration (the Auth0 app is
    a pre-registered public client — same one /mcp uses)."""

    def __init__(self, url: str, client_id: str, redirect_uri: str):
        host = urlparse(url).netloc.replace(":", "_")
        self._path = _CACHE_DIR / f"tokens-{host}.json"
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    async def get_tokens(self) -> OAuthToken | None:
        try:
            return OAuthToken.model_validate_json(self._path.read_text())
        except Exception:  # noqa: BLE001
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(tokens.model_dump_json())
        self._path.chmod(0o600)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return OAuthClientInformationFull(
            client_id=self._client_id,
            redirect_uris=[self._redirect_uri],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        return None  # static public client — nothing to persist


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        _CallbackHandler.result = {
            "code": q.get("code", [None])[0],
            "state": q.get("state", [None])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>MemHub plugin authenticated."
            b" You can close this tab.</h3></body></html>"
        )

    def log_message(self, *args):  # silence request logging
        return


def build_oauth(url: str) -> OAuthClientProvider:
    cfg = _plugin_mcp_config()
    oauth_cfg = cfg.get("oauth", {})
    client_id = oauth_cfg.get("clientId")
    port = int(oauth_cfg.get("callbackPort", 8765))
    if not client_id:
        raise RuntimeError(".mcp.json has no oauth.clientId")
    redirect_uri = f"http://localhost:{port}/callback"

    done = threading.Event()

    async def redirect_handler(auth_url: str) -> None:
        print(f"Opening browser to authenticate (same flow as /mcp)...\n  {auth_url}")
        webbrowser.open(auth_url)

    async def callback_handler() -> tuple[str, str | None]:
        server = HTTPServer(("localhost", port), _CallbackHandler)

        def serve():
            while not _CallbackHandler.result:
                server.handle_request()
            done.set()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        # Wait for the browser round-trip without blocking the event loop.
        while not done.is_set():
            await asyncio.sleep(0.2)
        server.server_close()
        res = _CallbackHandler.result
        if not res.get("code"):
            raise RuntimeError("OAuth callback carried no authorization code")
        return res["code"], res.get("state")

    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="MemHub Claude Plugin scripts",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=_FileTokenStorage(url, client_id, redirect_uri),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def resolve_url_and_auth(url: str | None = None):
    """Return (url, headers, auth) for streamablehttp_client.

    $MEMHUB_TOKEN (if set) wins as a plain bearer header — CI/headless escape
    hatch. Otherwise an OAuthClientProvider that reuses the cached token,
    refreshes it, or runs the one-time browser flow.
    """
    url = url or default_url()
    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    if token:
        return url, {"Authorization": f"Bearer {token}"}, None
    return url, None, build_oauth(url)


if __name__ == "__main__":
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _check():
        url, headers, auth = resolve_url_and_auth()
        print(f"endpoint : {url}")
        print(f"mode     : {'bearer ($MEMHUB_TOKEN)' if headers else 'oauth (plugin client)'}")
        async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                print(f"AUTH OK — server exposes {len(tools.tools)} tools")

    raise SystemExit(asyncio.run(_check()))
