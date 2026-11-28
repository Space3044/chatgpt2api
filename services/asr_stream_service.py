"""Live voice-input bridge: browser WebSocket <-> Doubao IME live session.

The browser cannot set an Authorization header on a WebSocket
handshake, so the FastAPI route (``api/asr_input.py``) authenticates
the handshake first (see ``extract_ws_identity``): the browser offers
the User Key base64url-encoded as a ``Sec-WebSocket-Protocol`` value,
which is the same credential the HTTP routes verify with
``require_identity`` — no new credential, no key in the URL (uvicorn
logs request paths), and no key in any log line here.

Since voice.10.4 the Python backend owns the live ASR session
directly, in-process (``services/cometix_asr.py``): there is no Go
sidecar and no loopback service any more. This module owns the
recording lifecycle — one long recording streamed through
(possibly several) short Doubao live sessions:

- The browser streams 16kHz mono s16le PCM frames as they are
  recorded; interim/final transcript events stream back, and on
  stop the accumulated final text is sent as ``{"type":"final"}``.
- Long silences are compressed live (only the first ~0.5s of a
  silence run reaches the session), because the Doubao server ends
  a session after a long silence.
- A Doubao session that ends mid-recording (long silence, a
  transient error, or the ~85s wall-time limit) is restarted
  transparently and the confirmed text is kept, so one recording
  survives pauses and session ends.
- The server's cumulative text (``results[0]``) is tracked directly
  as the session text; per-segment finals are never merged (merging
  duplicated a 170s recording: 913 chars came back as 3120). The
  interim text shown live is the cumulative text minus the
  already-confirmed prefix, so the confirmed part is not printed
  twice.

One recording is capped at 180s. The bridge never invents a
transcript: on any failure the browser shows a clear error.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging

from api.support import require_identity
from services.asr_input_service import MAX_PCM_BYTES
from services.cometix_asr import (
    FRAME_BYTES,
    LiveSession,
    LiveSilenceFilter,
    join_chunk_text,
    strip_confirmed_prefix,
)

logger = logging.getLogger(__name__)

WS_SUBPROTOCOL = "asr"
WS_TOKEN_PREFIX = "asr."
# A recording may last MAX_PCM_SECONDS (180s); the bridge deadline
# allows a small grace for the final transcript after stop.
WS_DEADLINE_SECS = 200
MAX_STREAM_RESTARTS = 10
# How long a finished session's trailing stable / session_final
# events get to arrive before the finished text is folded into the
# connection total and a new session is started.
RESTART_GRACE_SECS = 0.3


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


class AsrLiveStream:
    """One recording streamed through (possibly several) short Doubao
    live sessions. Browser-facing events are put onto ``outbound`` as
    JSON strings: ready / transcript / final / error / close, exactly
    the shapes the Studio composer expects.

    ``session_factory`` builds one live session (default
    :class:`LiveSession`); tests inject a fake. The manager never
    logs recognised text, device ids or tokens.
    """

    def __init__(self, outbound: asyncio.Queue, session_factory=LiveSession) -> None:
        self._outbound = outbound
        self._session_factory = session_factory
        self._session: LiveSession | None = None
        self._ready = False
        self._gen = 0
        self._stopped = False
        self._peer_gone = False
        self._starting = False
        self._start_done: asyncio.Event | None = None
        self._restart_pending = False
        self._restarts = 0
        self._confirmed = ""  # finals from finished sessions
        self._stable = ""  # latest settled text of the current session
        self._queued: list[bytes] = []
        self._partial = bytearray()
        self._filter = LiveSilenceFilter()
        self._final_sent = False
        self._draining = False

    # -- outbound ------------------------------------------------------

    def _send(self, event: dict) -> None:
        self._outbound.put_nowait(json.dumps(event))

    def _current_confirmed(self) -> str:
        return join_chunk_text(self._confirmed, self._stable)

    def _absorb(self) -> None:
        self._confirmed = join_chunk_text(self._confirmed, self._stable)
        self._stable = ""

    # -- session lifecycle ----------------------------------------------

    def start_recording(self) -> None:
        """Begin a fresh recording: drop any previous session state
        and start a Doubao session in the background (the handshake
        takes ~1s; frames arriving meanwhile are queued)."""
        self._reset()
        self._launch_start()

    def _launch_start(self, retried: bool = False) -> None:
        """Begin one start attempt in the background. The starting
        flag and its done-event are set synchronously here, so a
        stop arriving before the attempt task runs still waits for
        it instead of finalising nothing."""
        if self._stopped or self._peer_gone:
            return
        self._gen += 1
        gen = self._gen
        self._starting = True
        done = asyncio.Event()
        self._start_done = done
        asyncio.create_task(self._start_session(gen, done, retried))

    def _reset(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            asyncio.create_task(session.close())
        self._gen += 1
        self._ready = False
        self._stopped = False
        self._restarts = 0
        self._restart_pending = False
        self._confirmed = ""
        self._stable = ""
        self._queued = []
        self._partial = bytearray()
        self._filter = LiveSilenceFilter()
        self._final_sent = False

    async def _start_session(
        self, gen: int, done: asyncio.Event, retried: bool = False
    ) -> None:
        if self._stopped or self._peer_gone:
            if self._start_done is done:
                self._starting = False
            done.set()
            return
        session = self._session_factory(
            lambda event: self._on_session_event(event, gen)
        )
        try:
            await session.start()
        except Exception as exc:
            # Only clear the flag if this attempt is still the
            # current one: a fresh {"type":"start"} may have begun
            # a newer attempt meanwhile (see _reset).
            if self._start_done is done:
                self._starting = False
            done.set()
            logger.warning("asr live session start failed: %s", exc)
            await self._handle_start_failure(retried)
            return
        if self._start_done is done:
            self._starting = False
        done.set()
        # A stop may have arrived during the handshake, or a fresh
        # {"type":"start"} may have moved the generation on; do not
        # use a session the peer no longer wants.
        if self._stopped or self._peer_gone or gen != self._gen:
            await session.close()
            return
        self._session = session

    async def _handle_start_failure(self, retried: bool) -> None:
        """Retry a failed session start once, then report the error
        to the peer (which shows a clear error; there is no
        record-then-transcribe path to fall back to)."""
        if retried:
            self._send(
                {
                    "type": "error",
                    "message": "live session could not start",
                    "code": "session_start_failed",
                }
            )
            return
        await asyncio.sleep(1)
        if self._stopped or self._peer_gone:
            return
        self._launch_start(retried=True)

    def _on_session_event(self, event: dict, gen: int) -> None:
        # Events from a previous generation (a session that already
        # ended) are ignored.
        if gen != self._gen or self._peer_gone:
            return
        event_type = event.get("type")
        if event_type == "ready":
            self._ready = True
            self._send(
                {
                    "type": "ready",
                    "session_id": event.get("session_id"),
                    "mode": event.get("mode"),
                }
            )
            asyncio.create_task(self._drain())
            return
        if event_type == "transcript":
            if event.get("is_final"):
                if event.get("stable_text"):
                    self._stable = event["stable_text"]
                confirmed = self._current_confirmed()
                stage = (
                    "session_final"
                    if event.get("stage") == "session_final"
                    else "stable"
                )
                self._send(
                    {
                        "type": "transcript",
                        "text": event.get("text", ""),
                        "display": event.get("display", ""),
                        "is_final": True,
                        "is_interim": False,
                        "stage": stage,
                        "confirmed": confirmed,
                    }
                )
                return
            confirmed = self._current_confirmed()
            # The interim text is the session's cumulative text, which
            # includes the already-confirmed part. Send only the new
            # tail, so the overlay does not print the confirmed text
            # twice (once settled, once greyed).
            text = strip_confirmed_prefix(confirmed, event.get("text", ""))
            display = strip_confirmed_prefix(confirmed, event.get("display", ""))
            self._send(
                {
                    "type": "transcript",
                    "text": text,
                    "display": display,
                    "is_final": False,
                    "is_interim": True,
                    "stage": "interim",
                    "confirmed": confirmed,
                }
            )
            return
        if event_type == "error":
            # Do not forward the error yet: the session is restarted
            # transparently below, and the peer only hears about it
            # if restarts run out.
            self._maybe_restart()
            return
        if event_type == "close":
            self._maybe_restart()
            return
        # "debug" and anything else: ignored.

    def _maybe_restart(self) -> None:
        """Fold the finished session into the connection total and,
        after a short grace for trailing events, start a new session
        — unless the peer already stopped or went away. Restarts are
        silent: the peer only gets an error if they run out."""
        if self._stopped or self._peer_gone or self._starting or self._restart_pending:
            return
        if self._restarts >= MAX_STREAM_RESTARTS:
            self._send(
                {
                    "type": "error",
                    "message": "live session ended repeatedly; try again",
                    "code": "session_restart_limit",
                }
            )
            return
        self._restart_pending = True
        gen = self._gen
        asyncio.create_task(self._delayed_restart(gen))

    async def _delayed_restart(self, gen: int) -> None:
        await asyncio.sleep(RESTART_GRACE_SECS)
        if gen != self._gen or self._stopped or self._peer_gone or self._starting:
            self._restart_pending = False
            return
        self._absorb()
        self._session = None
        self._ready = False
        self._stable = ""
        self._restarts += 1
        self._restart_pending = False
        self._launch_start()

    # -- feeding audio ---------------------------------------------------

    def feed_bytes(self, data: bytes) -> None:
        """Split incoming bytes into 40ms frames, drop long silence
        runs through the live filter, and queue the rest."""
        self._partial.extend(data)
        frames = []
        while len(self._partial) >= FRAME_BYTES:
            frame = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]
            if self._filter.feed(frame):
                frames.append(frame)
        for frame in frames:
            self._feed_frame(frame)

    def _feed_frame(self, frame: bytes) -> None:
        if self._peer_gone or self._stopped:
            return
        self._queued.append(frame)
        asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Feed queued frames to the current session while one is
        ready. A frame whose session ended under us is re-queued for
        the restarted session."""
        if self._draining:
            return
        self._draining = True
        try:
            while self._queued and self._session is not None and self._ready:
                if self._peer_gone or self._stopped:
                    return
                frame = self._queued.pop(0)
                session = self._session
                try:
                    await session.feed_pcm(frame)
                except Exception:
                    self._queued.insert(0, frame)
                    return
        finally:
            self._draining = False

    # -- stopping ---------------------------------------------------------

    def mark_peer_gone(self) -> None:
        """Stop all session work after the peer disconnects."""
        self._peer_gone = True
        if self._session is not None:
            session, self._session = self._session, None
            asyncio.create_task(session.close())

    async def handle_stop(self) -> None:
        """Finish the recording: flush the partial frame, wait for an
        in-flight or pending session start to settle, feed anything
        queued into the live session, finalise it, then send the
        total confirmed text as {"type":"final"}."""
        if self._partial:
            # A trailing partial frame passes through unfiltered (it
            # is under 40ms, too short to end a session).
            self._feed_frame(bytes(self._partial))
            self._partial = bytearray()
        # A session may have just ended and a restart may be pending;
        # give it a moment to start so the queued tail is transcribed
        # instead of dropped.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.5
        while True:
            if not self._restart_pending and not self._starting:
                break
            if self._starting and self._start_done is not None:
                try:
                    await asyncio.wait_for(self._start_done.wait(), timeout=3)
                except TimeoutError:
                    pass
                break
            if loop.time() > deadline:
                break
            await asyncio.sleep(0.05)
        await self._drain()
        self._stopped = True
        session = self._session
        if session is not None:
            # finalize() waits up to 15s for SessionFinished; the
            # session_final event updates the stable text first.
            await session.finalize()
        self._absorb()
        total = self._confirmed
        sent = self._final_sent
        self._final_sent = True
        if not sent:
            self._send({"type": "final", "text": total})
            self._send({"type": "close"})
        if session is not None:
            await session.close()


async def _send_error(browser_ws, message: str) -> None:
    try:
        await browser_ws.send_text(json.dumps({"type": "error", "message": message}))
    except (RuntimeError, asyncio.CancelledError):
        pass


async def run_stream_bridge(browser_ws) -> None:
    """Relay one recording between the browser socket and the live
    ASR session.

    ``browser_ws`` is a Starlette WebSocket, already accepted. The
    bridge returns after the ``final`` (or error/close) has been
    forwarded and the browser socket is closed. It never invents a
    transcript: on any failure the browser shows a clear error.
    """
    outbound: asyncio.Queue[str] = asyncio.Queue()
    stream = AsrLiveStream(outbound)
    finished = asyncio.Event()
    stop_handled = False

    async def browser_to_stream() -> None:
        nonlocal stop_handled
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
                    stream.feed_bytes(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if payload.get("type") == "start":
                    stream.start_recording()
                elif payload.get("type") == "stop":
                    await stream.handle_stop()
                    stop_handled = True
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def stream_to_browser() -> None:
        try:
            while not finished.is_set():
                message = await outbound.get()
                await browser_ws.send_text(message)
                try:
                    payload = json.loads(message)
                except ValueError:
                    continue
                if payload.get("type") in ("final", "error", "close"):
                    finished.set()
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            return

    pump_up = asyncio.create_task(browser_to_stream())
    pump_down = asyncio.create_task(stream_to_browser())
    try:
        done, _ = await asyncio.wait(
            {pump_up, pump_down},
            timeout=WS_DEADLINE_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pump_up in done and pump_down not in done:
            # The browser went away (or errored) without a final:
            # stop the session, then give its final a moment instead
            # of transcribing alone until the deadline. A normal
            # {"type":"stop"} already queued the final, so it only
            # needs the sender to flush it.
            if not stop_handled:
                stream.mark_peer_gone()
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
            await browser_ws.close()
        except (RuntimeError, asyncio.CancelledError):
            pass
