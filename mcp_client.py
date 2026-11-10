"""Minimal MCP (Model Context Protocol) client — JSON-RPC 2.0 over Streamable HTTP.

Standard library only: no `mcp` package, no `requests`, no npm. Written to the
Streamable HTTP transport rules the MCP spec defines for 2025-03-26 and later:

  * one POST endpoint, JSON-RPC 2.0 request bodies
  * ``Accept: application/json, text/event-stream``
  * the server answers with either a JSON body or an SSE stream whose
    ``data:`` frames carry the JSON-RPC response
  * ``Mcp-Session-Id`` from the ``initialize`` response is echoed on every
    later request; ``Mcp-Protocol-Version`` carries the negotiated version
  * a session the server no longer knows (404) is re-initialized once

Failure policy: every public method raises :class:`McpError` — never a raw
``URLError``/``JSONDecodeError``/``TimeoutError``. Callers that must not raise
into the agent loop should use :meth:`McpHttpClient.call_tool_safe`, which
returns the stable envelope ``{"ok": bool, ...}``.
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "hermes-agentpass", "version": "1.0.0"}

# HTTP statuses worth another attempt. 408/425/429 are "try again"; 5xx is
# "the server is having a moment". 400/401/403/404/422 are deterministic.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class McpError(Exception):
    """Every failure mode of this client, normalised.

    ``kind`` is one of: ``network``, ``timeout``, ``http``, ``protocol``,
    ``rpc``, ``config``. ``retryable`` records whether the attempt that raised
    it was worth repeating.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "protocol",
        code: int | None = None,
        data: Any = None,
        retryable: bool = False,
        status: int | None = None,
        method: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.code = code
        self.data = data
        self.retryable = retryable
        self.status = status
        self.method = method

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.code is not None:
            out["code"] = self.code
        if self.status is not None:
            out["http_status"] = self.status
        if self.method:
            out["method"] = self.method
        if self.data is not None:
            out["data"] = self.data
        return out


def _parse_sse(raw: bytes) -> list[dict]:
    """Extract JSON-RPC messages from an SSE body.

    Frames are separated by a blank line; every ``data:`` line in a frame is
    concatenated (the spec allows multi-line payloads). Comment lines (``:``)
    and non-``data`` fields are ignored. A frame that is not JSON is skipped
    rather than fatal — servers legitimately send keep-alive comments.
    """
    messages: list[dict] = []
    for frame in raw.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].lstrip() if line.startswith("data:") else None
            for line in frame.split("\n")
        ]
        payload = "\n".join(line for line in data_lines if line is not None).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            messages.extend(m for m in parsed if isinstance(m, dict))
        elif isinstance(parsed, dict):
            messages.append(parsed)
    return messages


def _pick_message(candidates: list[dict], request_id: Any) -> dict | None:
    for msg in candidates:
        if "id" in msg and msg.get("id") == request_id:
            return msg
    for msg in candidates:  # tolerate servers that echo the id as a string
        if "id" in msg and str(msg.get("id")) == str(request_id):
            return msg
    for msg in candidates:  # a single unsolicited response is unambiguous
        if "result" in msg or "error" in msg:
            return msg
    return None


class McpHttpClient:
    """One MCP session against one Streamable HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 45.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        auth_token: str | None = None,
        extra_headers: dict | None = None,
        user_agent: str = "hermes-agentpass/1.0.0 (+https://agentpass.dev)",
    ) -> None:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise McpError(f"endpoint must be an absolute http(s) URL, got {url!r}", kind="config")
        self.url = url
        self.timeout = max(1.0, float(timeout))
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.protocol_version = protocol_version
        self.auth_token = auth_token if auth_token is not None else os.environ.get("AGENTPASS_API_KEY") or None
        self.extra_headers = dict(extra_headers or {})
        self.user_agent = user_agent

        self._session_id: str | None = None
        self._initialized = False
        self._negotiated_version: str | None = None
        self._init_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0

        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.stats = {"requests": 0, "retries": 0, "sse_responses": 0, "json_responses": 0}

    # ---------------------------------------------------------------- plumbing

    def _new_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _headers(self, *, include_protocol: bool = True) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.user_agent,
        }
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._negotiated_version or self.protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        headers.update(self.extra_headers)
        return headers

    def _backoff(self, attempt: int) -> None:
        delay = self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base / 2)
        time.sleep(min(delay, 5.0))

    def _single_attempt(self, payload: dict, *, expect_response: bool) -> dict | None:
        """One POST. Retries are handled by the caller."""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        method = payload.get("method")
        request = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        self.stats["requests"] += 1

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session
                raw = response.read()
                content_type = (response.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - body may be unreadable
                detail = ""
            raise McpError(
                f"HTTP {exc.code} from {self.url}" + (f": {detail}" if detail else ""),
                kind="http",
                status=exc.code,
                method=method,
                retryable=exc.code in _RETRYABLE_STATUS,
            ) from None
        except (socket.timeout, TimeoutError):
            raise McpError(
                f"{method or 'request'} timed out after {self.timeout:g}s", kind="timeout", method=method, retryable=True
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(reason, (socket.timeout, TimeoutError))
            raise McpError(
                f"cannot reach {self.url}: {reason}", kind="timeout" if timed_out else "network", method=method, retryable=True
            ) from None
        except OSError as exc:  # connection reset, DNS failure, TLS error
            raise McpError(f"transport error to {self.url}: {exc}", kind="network", method=method, retryable=True) from None

        if status == 202 or not expect_response or not raw.strip():
            return None

        if "text/event-stream" in content_type:
            self.stats["sse_responses"] += 1
            message = _pick_message(_parse_sse(raw), payload.get("id"))
            if message is None:
                raise McpError("server-sent event stream carried no JSON-RPC response", kind="protocol", method=method)
            return message

        self.stats["json_responses"] += 1
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            snippet = raw[:300].decode("utf-8", "replace")
            raise McpError(f"response was not JSON ({content_type or 'no content-type'}): {snippet}", kind="protocol", method=method) from None
        if isinstance(decoded, list):
            message = _pick_message(decoded, payload.get("id"))
            if message is None:
                raise McpError("JSON-RPC batch contained no matching response", kind="protocol", method=method)
            return message
        return decoded

    def _request(self, payload: dict, *, expect_response: bool = True, retry: bool = True) -> dict | None:
        attempts = (self.max_retries + 1) if retry else 1
        last: McpError | None = None
        for attempt in range(attempts):
            try:
                message = self._single_attempt(payload, expect_response=expect_response)
                if message is None:
                    return None
                if "error" in message and message["error"] is not None:
                    error = message["error"] or {}
                    raise McpError(
                        str(error.get("message") or "JSON-RPC error"),
                        kind="rpc",
                        code=error.get("code"),
                        data=error.get("data"),
                        method=payload.get("method"),
                    )
                return message.get("result") or {}
            except McpError as exc:
                last = exc
                if not exc.retryable or attempt == attempts - 1:
                    raise
                # A session the server forgot: re-initialize once, then retry.
                if exc.status == 404 and self._session_id:
                    self._session_id = None
                    self._initialized = False
                    self.ensure_initialized()
                self.stats["retries"] += 1
                self._backoff(attempt)
        raise last or McpError("request failed", kind="protocol")

    # ------------------------------------------------------------------- verbs

    def ensure_initialized(self) -> dict:
        with self._init_lock:
            if self._initialized:
                return {"protocolVersion": self._negotiated_version, "serverInfo": self.server_info}
            result = self._request(
                {
                    "jsonrpc": "2.0",
                    "id": self._new_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self.protocol_version,
                        "capabilities": {"roots": {"listChanged": False}},
                        "clientInfo": CLIENT_INFO,
                    },
                }
            )
            result = result or {}
            negotiated = result.get("protocolVersion")
            self._negotiated_version = negotiated if negotiated in SUPPORTED_PROTOCOL_VERSIONS else self.protocol_version
            self.server_info = result.get("serverInfo") or {}
            self.server_capabilities = result.get("capabilities") or {}
            # Notification: no id, no response body (202 is the normal answer).
            try:
                self._request({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False, retry=False)
            except McpError:
                pass
            self._initialized = True
            return result

    def list_tools(self) -> list[dict]:
        self.ensure_initialized()
        result = self._request({"jsonrpc": "2.0", "id": self._new_id(), "method": "tools/list", "params": {}})
        tools = (result or {}).get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool_raw(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool and return the raw ``tools/call`` result object."""
        self.ensure_initialized()
        result = self._request(
            {
                "jsonrpc": "2.0",
                "id": self._new_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        return result or {}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool and flatten the MCP result into a predictable shape.

        Returns ``{"ok", "tool", "text", "content", "structured", "is_error"}``.
        ``text`` is the concatenation of every ``text`` content part, which is
        where an MCP server conventionally puts its JSON payload.
        """
        raw = self.call_tool_raw(name, arguments)
        content = raw.get("content") or []
        texts: list[str] = []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
        is_error = bool(raw.get("isError"))
        return {
            "ok": not is_error,
            "tool": name,
            "text": "\n".join(texts),
            "content": content,
            "structured": raw.get("structuredContent"),
            "is_error": is_error,
        }

    def call_tool_safe(self, name: str, arguments: dict | None = None) -> dict:
        """Never raises. Envelope: ``{"ok": bool, ...}``.

        On failure the envelope carries ``error`` (a serialisable dict) and the
        transport stats, so a caller can show the user exactly what happened.
        """
        started = time.monotonic()
        try:
            result = self.call_tool(name, arguments)
            result["duration_ms"] = round((time.monotonic() - started) * 1000)
            result["stats"] = dict(self.stats)
            result["endpoint"] = self.url
            if result.get("is_error") and "error" not in result:
                # MCP's own failure channel: a result with isError=true. The
                # tool ran and said no — surface it like any other failure.
                detail = result.get("text") or "tool reported isError"
                result["error"] = {"kind": "tool", "message": detail[:500]}
            return result
        except McpError as exc:
            return {
                "ok": False,
                "tool": name,
                "error": exc.to_dict(),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "stats": dict(self.stats),
                "endpoint": self.url,
            }
        except Exception as exc:  # noqa: BLE001 - last line of defence
            return {
                "ok": False,
                "tool": name,
                "error": {"kind": "unexpected", "message": f"{type(exc).__name__}: {exc}"},
                "duration_ms": round((time.monotonic() - started) * 1000),
                "stats": dict(self.stats),
                "endpoint": self.url,
            }

    def close(self) -> None:
        """No sockets are kept open; drop the session so the next call re-inits."""
        self._session_id = None
        self._initialized = False
