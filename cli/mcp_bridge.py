# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

"""stdio ↔ streamable-HTTP bridge for MCP clients that only launch local
servers (Claude Desktop).

Every JSON-RPC message read from stdin is POSTed to the server's ``/mcp``
endpoint and whatever comes back is written to stdout.  The endpoint is
stateless, so nothing is lost when the server restarts mid-conversation: a
failed POST starts the local server again and is retried.

stdout carries protocol messages only — all diagnostics go to stderr.
"""

import asyncio
import json
import sys
import threading

import httpx


def _log(msg: str):
    print(f"[be-conductor mcp] {msg}", file=sys.stderr, flush=True)


def load_remote_servers() -> dict[str, dict]:
    """Named remote be-conductor servers from ``mcp.servers`` in config.yaml.

    Each entry: {name, url, token? | token_env?}.  They become the values of
    the optional ``server`` argument the bridge adds to every tool.
    """
    import os
    import be_conductor.utils.config as cfg
    out: dict[str, dict] = {}
    for entry in cfg.MCP_CONFIG.get("servers") or []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            continue
        token = entry.get("token") or os.environ.get(str(entry.get("token_env") or ""), "") or None
        out[str(entry["name"])] = {"url": str(entry["url"]).rstrip("/"), "token": token}
    return out


class Bridge:
    def __init__(self, base_url: str, token: str | None, local: bool, verify: bool = True,
                 remotes: dict[str, dict] | None = None):
        self.base_url = base_url.rstrip("/")
        self.local = local
        self.remotes = remotes or {}
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(
            verify=verify, timeout=httpx.Timeout(10.0, read=None, write=30.0))
        self._out_lock = threading.Lock()
        self._ensure_lock = asyncio.Lock()
        self._inflight: dict = {}   # request id → task

    # ── stdout ────────────────────────────────────────────────────────────

    def _emit(self, message):
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._out_lock:
            sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()

    def _emit_error(self, msg, code: int, text: str):
        for m in (msg if isinstance(msg, list) else [msg]):
            if isinstance(m, dict) and "id" in m and "method" in m:
                self._emit({"jsonrpc": "2.0", "id": m["id"],
                            "error": {"code": code, "message": text}})

    # ── server lifecycle ──────────────────────────────────────────────────

    async def _healthy(self) -> bool:
        try:
            r = await self.client.get(f"{self.base_url}/health", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    async def ensure_server(self) -> bool:
        """Make sure the server answers; start the local daemon if it does not."""
        async with self._ensure_lock:
            if await self._healthy():
                return True
            if not self.local:
                return False
            # It may be mid-restart (service manager, `be-conductor restart`).
            for _ in range(6):
                await asyncio.sleep(0.5)
                if await self._healthy():
                    return True
            _log("server is down — starting it")
            from cli.main import start_server_daemon
            loop = asyncio.get_running_loop()
            started = await loop.run_in_executor(None, start_server_daemon)
            if not started:
                return await self._healthy()
            return True

    # ── forwarding ────────────────────────────────────────────────────────

    def _route(self, msg) -> tuple[str, dict, object]:
        """Pick the target server for *msg*: a tool call may name one with a
        ``server`` argument, which is stripped before forwarding."""
        if (self.remotes and isinstance(msg, dict) and msg.get("method") == "tools/call"):
            args = (msg.get("params") or {}).get("arguments")
            name = args.get("server") if isinstance(args, dict) else None
            if name and name not in ("local", "default"):
                remote = self.remotes.get(name)
                if remote is None:
                    raise LookupError(
                        f"unknown server '{name}' — known: local, {', '.join(self.remotes)}")
                msg = {**msg, "params": {**msg["params"],
                                         "arguments": {k: v for k, v in args.items() if k != "server"}}}
                headers = {k: v for k, v in self.headers.items() if k != "Authorization"}
                if remote["token"]:
                    headers["Authorization"] = f"Bearer {remote['token']}"
                return remote["url"], headers, msg
            if isinstance(args, dict) and "server" in args:
                msg = {**msg, "params": {**msg["params"],
                                         "arguments": {k: v for k, v in args.items() if k != "server"}}}
        return self.base_url, self.headers, msg

    def _advertise_servers(self, message: dict):
        """Add the optional ``server`` argument to every tool in a tools/list result."""
        tools = (message.get("result") or {}).get("tools")
        if not self.remotes or not isinstance(tools, list):
            return
        prop = {"type": "string", "enum": ["local", *self.remotes],
                "description": "Which be-conductor server to run this on (default: local)."}
        for tool in tools:
            schema = tool.setdefault("inputSchema", {"type": "object"})
            schema.setdefault("properties", {})["server"] = prop

    async def _post(self, msg):
        try:
            base_url, headers, msg = self._route(msg)
        except LookupError as e:
            self._emit_error(msg, -32602, str(e))
            return
        async with self.client.stream(
                "POST", f"{base_url}/mcp", headers=headers, json=msg) as resp:
            if resp.status_code == 202:
                return
            if resp.status_code == 401:
                await resp.aread()
                self._emit_error(msg, -32001,
                                 "be-conductor rejected the token — set BE_CONDUCTOR_TOKEN "
                                 "for the bridge (or pass --token)")
                return
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                try:
                    detail = json.loads(body)
                    # A JSON-RPC error from the MCP layer passes through as-is.
                    if isinstance(detail, dict) and "jsonrpc" in detail:
                        self._emit(detail)
                        return
                    body = detail.get("detail", body) if isinstance(detail, dict) else body
                except ValueError:
                    pass
                self._emit_error(msg, -32002, f"be-conductor: {body.strip()[:500]}")
                return

            ctype = resp.headers.get("content-type", "")
            if ctype.startswith("text/event-stream"):
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "" and data_lines:
                        self._forward_payload("\n".join(data_lines))
                        data_lines = []
                if data_lines:
                    self._forward_payload("\n".join(data_lines))
            else:
                self._forward_payload((await resp.aread()).decode("utf-8"))

    def _forward_payload(self, payload: str):
        payload = payload.strip()
        if not payload:
            return
        try:
            message = json.loads(payload)
        except ValueError:
            _log(f"dropping non-JSON payload from server: {payload[:120]!r}")
            return
        if isinstance(message, dict):
            result = message.get("result")
            if isinstance(result, dict) and result.get("protocolVersion"):
                self.headers["MCP-Protocol-Version"] = str(result["protocolVersion"])
            self._advertise_servers(message)
        self._emit(message)

    async def handle(self, msg):
        try:
            try:
                await self._post(msg)
            except (httpx.TransportError, httpx.StreamError) as first:
                if not await self.ensure_server():
                    raise first
                await self._post(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"request failed: {e!r}")
            self._emit_error(msg, -32003,
                             f"be-conductor server at {self.base_url} is not reachable: {e}")

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self):
        await self.ensure_server()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def reader():
            for raw in sys.stdin.buffer:
                loop.call_soon_threadsafe(queue.put_nowait, raw)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, daemon=True).start()

        tasks: set[asyncio.Task] = set()
        while True:
            raw = await queue.get()
            if raw is None:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self._emit({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
                continue

            # A cancel notice ends our in-flight POST for that request; the
            # closed connection is what tells the (stateless) server.
            if isinstance(msg, dict) and msg.get("method") == "notifications/cancelled":
                rid = (msg.get("params") or {}).get("requestId")
                task = self._inflight.pop(rid, None)
                if task:
                    task.cancel()
                continue

            task = asyncio.create_task(self.handle(msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            if isinstance(msg, dict) and "id" in msg and "method" in msg:
                rid = msg["id"]
                self._inflight[rid] = task
                task.add_done_callback(lambda _t, rid=rid: self._inflight.pop(rid, None))

        # stdin closed — the client is gone.  Runs it started keep going on
        # the server; only our pending HTTP calls are dropped.
        for task in list(tasks):
            task.cancel()
        await self.client.aclose()


def run_bridge(base_url: str, token: str | None, local: bool, verify: bool = True):
    try:
        remotes = load_remote_servers() if local else {}
        # Remote be-conductors are usually on self-signed certs.
        asyncio.run(Bridge(base_url, token, local, verify=verify and not remotes,
                           remotes=remotes).run())
    except KeyboardInterrupt:
        pass
