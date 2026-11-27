"""Live voice-input bridge: browser WebSocket <-> cometix-asr sidecar.

The browser cannot reach the sidecar directly (loopback inside the
container) and cannot set an Authorization header on a WebSocket
handshake, so this module owns the relay:

- The FastAPI route (`api/asr_input.py`) authenticates the handshake
  first (see ``extract_ws_identity``): the browser offers the User
  Key base64url-encoded as a ``Sec-WebSocket-Protocol`` value, which
  is the same credential the HTTP routes verify with
  ``require_identity`` — no new credential, no key in the URL (uvicorn
  logs request paths), and no key in any log line here.
- This module then opens the sidecar's ``WS /ws`` (browser-relay
  protocol) on loopback and forwards messages both ways verbatim:
  ``{"type":"start"}`` / binary PCM / ``{"type":"stop"}`` up,
  ``ready`` / ``transcript`` / ``final`` / ``error`` / ``close`` down.
  It owns no ASR state and never sees device ids, tokens or any
  cometix-asr secret; the only credential on this path is the
  optional ASR_SIDECAR_KEY shared header, from the environment.

One recording is capped at the same 180s as the HTTP path. The
sidecar restarts its Doubao session transparently when one ends
mid-recording, so a long pause never loses the tail; this bridge
only has to keep both sockets pumping until the sidecar sends its
``final`` message (or an error), then it closes the browser socket.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from api.support import require_identity
from services.asr_input_service import MAX_PCM_BYTES, _sidecar_headers, _sidecar_url

WS_SUBPROTOCOL = "asr"
WS_TOKEN_PREFIX = "asr."
# A recording may last MAX_PCM_SECONDS (180s); the bridge deadline
# allows a small grace for the final transcript after stop.
WS_DEADLINE_SECS = 200
SIDECAR_CONNECT_TIMEOUT_SECS = 10


class AsrStreamAuthError(Exception):
    """The WebSocket handshake carried no usable User Key."""


def extract_ws_identity(headers) -> dict[str, object]:
    """Verify the User Key offered as a Sec-WebSocket-Protocol value.

    The browser offers two subprotocols, ``asr`` and
    ``asr.<base64url of the key>`` (see ``web-vue/src/api/asrInput.ts``).
    The key itself never appears in a URL or a log: the header is not
    logged by uvicorn, and only the decoded token is handed to
    ``require_identity``, exactly as the HTTP routes do.
    """
    offered = headers.get("sec-websocket-protocol") or ""
    token: str | None = None
    for value in offered.split(","):
        value = value.strip()
        if not value.startswith(WS_TOKEN_PREFIX):
            continue
        encoded = value[len(WS_TOKEN_PREFIX):]
        if not encoded:
            continue
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            token = base64.urlsafe_b64decode(padded).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        break
    if token is None:
        raise AsrStreamAuthError("missing voice-input credential")
    try:
        return require_identity(f"Bearer {token}")
    except Exception as exc:
        raise AsrStreamAuthError("invalid voice-input credential") from exc


def _sidecar_ws_url() -> str:
    url = _sidecar_url()
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url.rstrip("/") + "/ws"


async def run_stream_bridge(browser_ws) -> None:
    """Relay one recording between the browser socket and the sidecar.

    ``browser_ws`` is a Starlette WebSocket, already accepted. The
    bridge returns after the sidecar's ``final`` (or error/close) has
    been forwarded and the browser socket is closed. It never invents
    a transcript: on any failure the browser shows a clear error.
    """
    headers = _sidecar_headers("application/octet-stream")
    try:
        sidecar = await ws_connect(
            _sidecar_ws_url(),
            additional_headers=headers,
            open_timeout=SIDECAR_CONNECT_TIMEOUT_SECS,
            ping_interval=20,
            max_size=2**22,
        )
    except Exception as exc:
        await _send_error(browser_ws, f"语音识别服务不可用：{exc}")
        return

    finished = asyncio.Event()

    async def browser_to_sidecar() -> None:
        sent_bytes = 0
        try:
            while not finished.is_set():
                message = await browser_ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message["type"] != "websocket.receive":
                    continue
                data = message.get("bytes")
                if data is not None:
                    sent_bytes += len(data)
                    if sent_bytes > MAX_PCM_BYTES:
                        await _send_error(browser_ws, "语音超过 180 秒，请分段录入")
                        return
                    await sidecar.send(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                await sidecar.send(text)
        except (ConnectionClosed, asyncio.CancelledError):
            return
        except Exception:
            return

    async def sidecar_to_browser() -> None:
        try:
            async for message in sidecar:
                if isinstance(message, bytes):
                    continue
                await browser_ws.send_text(message)
                try:
                    payload = json.loads(message)
                except ValueError:
                    continue
                if payload.get("type") in ("final", "error", "close"):
                    finished.set()
                    return
        except (ConnectionClosed, asyncio.CancelledError):
            return
        except Exception:
            return

    pump_up = asyncio.create_task(browser_to_sidecar())
    pump_down = asyncio.create_task(sidecar_to_browser())
    try:
        done, _ = await asyncio.wait(
            {pump_up, pump_down},
            timeout=WS_DEADLINE_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pump_up in done and pump_down not in done:
            # The browser went away (or errored) without a final:
            # stop the sidecar session, then give its final a
            # moment instead of transcribing alone until the
            # deadline.
            try:
                await sidecar.send(json.dumps({"type": "stop"}))
            except (ConnectionClosed, asyncio.CancelledError):
                pass
            try:
                await asyncio.wait_for(pump_down, timeout=5)
            except TimeoutError:
                pass
        elif not done:
            await _send_error(browser_ws, "语音识别超时，请重试")
    finally:
        pump_up.cancel()
        pump_down.cancel()
        await asyncio.gather(pump_up, pump_down, return_exceptions=True)
        try:
            await sidecar.close()
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        try:
            await browser_ws.close()
        except (RuntimeError, asyncio.CancelledError):
            pass


async def _send_error(browser_ws, message: str) -> None:
    try:
        await browser_ws.send_text(json.dumps({"type": "error", "message": message}))
    except (RuntimeError, asyncio.CancelledError):
        pass
