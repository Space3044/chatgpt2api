"""Pure-Python client for the Doubao IME cloud ASR backend (cometix-asr).

Ported from the Go sidecar (``asr-sidecar/asr/``) that it replaces in
voice.10.4: the same protocol, the same behaviour, no Go process and
no loopback service. The Python backend owns the live session and
the standalone RunPostAsr (fmt + NER) in-process.

Protocol (verified live against the Rust cometix-asr addon):

- Device registration (``device_register``) issues a device id, cached
  in ``~/.cache/cometix-asr/did.json``; the settings API returns a
  stable per-device app key used as the WebSocket URL token.
- The live session is a WebSocket to
  ``wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws`` with
  protobuf-framed ``AsrRequest``/``AsrResponse`` messages:
  ``StartTask`` -> ``StartSession`` -> ``TaskRequest`` (Opus, 40ms
  frames) -> ``FinishSession``. Audio is 16kHz mono s16le PCM,
  640-sample frames, Opus-encoded at 64kbps CBR via the system
  libopus (``ctypes``; the image already ships ``libopus0``).
- ``result_json`` carries ``results[0]`` (formatted text) and
  ``results[1]`` (raw text). A final payload has ``is_interim:false``
  and ``is_vad_finished:true``. ``results:null`` is a heartbeat.

The session text is the server's cumulative formatted text
(``results[0]``), tracked directly — never assembled by merging.
The server re-sends ``results[0]`` on every response and revises
earlier words in it as more audio arrives, so merging finals with
string rules kept failing: a revised cumulative result is neither
a pure extension nor an exact repeat, and appending it duplicated
the passage (verified live: a 170s streaming recording came back
as 3120 runes for a 913-rune passage, with whole sentences
repeated). ``results[0]`` itself is the server's running transcript
for the whole session: it grows monotonically (a 1-rune revision
dip aside) and reaches the full text, so the longest version seen
is the session text. Per-segment nonstream_result entries are
subsets of it and are not merged in at all.

Secrets (names only, no values): the post-ASR app key has no
default in this repo and comes only from ``COMETIX_APP_KEY`` in
the environment; ``RunPostAsr`` fails clearly without it. The live
session uses the public Doubao IME client constant as its
protobuf token, as in the captured frames. No secret value is ever
logged: transcripts, tokens and keys never appear in a log line.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import hashlib
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (non-secret client constants, as in the Go port)
# ---------------------------------------------------------------------------

APP_ID = "401734"
APP_KEY = "OrnqKvSSrs"  # public Doubao IME client constant, from captured frames
WS_URL = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
REGISTER_URL = "https://log.snssdk.com/service/2/device_register/"
SETTINGS_URL = "https://is.snssdk.com/service/settings/v3/"
USER_AGENT = (
    "com.bytedance.android.doubaoime/100102018 (Linux; U; Android 16; "
    "en_US; Pixel 7 Pro; Build/BP2A.250605.031.A2; "
    "Cronet/TTNetVersion:94cf429a 2025-11-17 QuicVersion:1f89f732 2025-05-08)"
)
HTTP_TIMEOUT_SECS = 30

FRAME_SAMPLES = 640
FRAME_BYTES = FRAME_SAMPLES * 2  # 1280 bytes of s16le PCM
SAMPLE_RATE = 16000

# Post-ASR client constants (see postasr.go in the removed Go sidecar).
POSTASR_APP_VERSION = "1.3.14"
POSTASR_APP_NAME = "doubaoime"
POSTASR_OS = "Android"
POSTASR_PLATFORM = "android"
POSTASR_RESOURCE_ID = "asr.user.context"
POSTASR_USER_AGENT = "DoubaoIME/1.3.14 (research; doubao-asr)"
POSTASR_FMT_URL = "https://ime.doubao.com/api/v1/asr/fmt"
POSTASR_NER_URL = "https://speech.bytedance.com/api/v3/context/ime/ner"
POSTASR_CONFIG_URL = "https://ime.oceancloudapi.com/api/v1/user/get_config"


class CometixAsrError(Exception):
    """An error in the Doubao IME client (device, session, or post-ASR)."""


# ---------------------------------------------------------------------------
# Protobuf codec for AsrRequest / AsrResponse (see asr-sidecar/asr.proto
# in the removed Go sidecar). Field numbers verified against frames
# captured from the Rust addon.
# ---------------------------------------------------------------------------


def _append_varint(buf: bytearray, value: int) -> None:
    while True:
        if value < 0x80:
            buf.append(value)
            return
        buf.append((value & 0x7F) | 0x80)
        value >>= 7


def _append_tag(buf: bytearray, field_no: int, wire: int) -> None:
    _append_varint(buf, (field_no << 3) | wire)


def _append_string_field(buf: bytearray, field_no: int, value: str) -> None:
    if value == "":
        return
    _append_tag(buf, field_no, 2)
    encoded = value.encode("utf-8")
    _append_varint(buf, len(encoded))
    buf.extend(encoded)


def _append_bytes_field(buf: bytearray, field_no: int, data: bytes) -> None:
    if not data:
        return
    _append_tag(buf, field_no, 2)
    _append_varint(buf, len(data))
    buf.extend(data)


def _append_varint_field(buf: bytearray, field_no: int, value: int) -> None:
    if value == 0:
        return
    _append_tag(buf, field_no, 0)
    _append_varint(buf, value & 0xFFFFFFFF)


def marshal_request(
    token: str,
    method: str,
    payload: str,
    audio: bytes,
    request_id: str,
    frame_no: int,
) -> bytes:
    """Build one AsrRequest. ``token`` is omitted for TaskRequest."""
    buf = bytearray()
    _append_string_field(buf, 2, token)
    _append_string_field(buf, 3, "ASR")
    _append_string_field(buf, 5, method)
    _append_string_field(buf, 6, payload)
    _append_bytes_field(buf, 7, audio)
    _append_string_field(buf, 8, request_id)
    _append_varint_field(buf, 9, frame_no)
    return bytes(buf)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise CometixAsrError("protobuf: truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, pos
        shift += 7
        if shift > 63:
            raise CometixAsrError("protobuf: varint too long")


@dataclass
class AsrResponse:
    request_id: str = ""
    task_id: str = ""
    service_name: str = ""
    message_type: str = ""
    status_code: int = 0
    status_message: str = ""
    result_json: str = ""


def parse_response(data: bytes) -> AsrResponse:
    """Decode one AsrResponse. Unknown fields are ignored."""
    resp = AsrResponse()
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_no = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
            if field_no == 5:
                resp.status_code = value
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise CometixAsrError(f"protobuf: truncated field {field_no}")
            text = data[pos:pos + length].decode("utf-8", "replace")
            pos += length
            if field_no == 1:
                resp.request_id = text
            elif field_no == 2:
                resp.task_id = text
            elif field_no == 3:
                resp.service_name = text
            elif field_no == 4:
                resp.message_type = text
            elif field_no == 6:
                resp.status_message = text
            elif field_no == 7:
                resp.result_json = text
        else:
            raise CometixAsrError(f"protobuf: unsupported wire type {wire}")
    return resp


# ---------------------------------------------------------------------------
# result_json parsing
# ---------------------------------------------------------------------------


@dataclass
class ResultItem:
    text: str = ""
    is_interim: bool | None = None
    is_vad_finished: bool | None = None
    stream_asr_finish: bool = False
    extra: dict | None = None


@dataclass
class ResultJSON:
    results: list[ResultItem] = field(default_factory=list)
    extra: dict | None = None

    def text(self) -> str:
        """The formatted text (results[0])."""
        if not self.results:
            return ""
        return self.results[0].text

    def is_final(self) -> bool:
        """Whether this payload is a final (VAD-finished) result."""
        for item in self.results:
            if item.is_interim is False and item.is_vad_finished:
                return True
            if item.extra is not None and "nonstream_result" in item.extra:
                return True
        return False


def parse_result_json(raw: str) -> ResultJSON:
    """Parse one ``result_json`` payload (heartbeat has results null)."""
    parsed = json.loads(raw)
    result = ResultJSON(extra=parsed.get("extra"))
    items = parsed.get("results") or []
    for item in items:
        result.results.append(
            ResultItem(
                text=item.get("text", "") or "",
                is_interim=item.get("is_interim"),
                is_vad_finished=item.get("is_vad_finished"),
                stream_asr_finish=bool(item.get("stream_asr_finish", False)),
                extra=item.get("extra"),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Pure helpers (recording lifecycle; also used by the stream service)
# ---------------------------------------------------------------------------


def join_chunk_text(acc: str, nxt: str) -> str:
    """Join per-session finals. Chinese needs no separator; between two
    alphanumerics (e.g. English words split across sessions) a single
    space is inserted, matching the composer's insertion rule."""
    if not acc:
        return nxt
    if not nxt:
        return acc
    if acc[-1].isascii() and acc[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum():
        return acc + " " + nxt
    return acc + nxt


def strip_confirmed_prefix(confirmed: str, text: str) -> str:
    """Remove the longest common prefix of confirmed and text: the
    interim text is cumulative, so its head is the confirmed text
    (modulo a revision, which shortens the shared prefix — the rest
    is shown greyed until it settles)."""
    n = 0
    limit = min(len(confirmed), len(text))
    while n < limit and confirmed[n] == text[n]:
        n += 1
    return text[n:]


def frame_is_silent(frame: bytes) -> bool:
    """A 40ms frame counts as silent only when BOTH its peak amplitude
    and its RMS are below conservative thresholds, so quiet speech is
    never stripped."""
    peak = 0
    sum_sq = 0
    count = len(frame) // 2
    for i in range(count):
        value = struct.unpack_from("<h", frame, i * 2)[0]
        if abs(value) > peak:
            peak = abs(value)
        sum_sq += value * value
    if peak >= 500:
        return False
    if count == 0:
        return True
    return sum_sq // count < 200 * 200


class LiveSilenceFilter:
    """Compress long silence runs as frames arrive: the Doubao IME
    server ends a session after a long silence, and in a live stream
    the future audio is not known yet, so only the first ~0.5s of
    each silence run is kept and the rest is dropped. Short pauses
    are unaffected in practice: a run under ~0.5s passes through
    whole, and a longer run still leaves a clear word boundary."""

    KEEP_FRAMES = 12  # ~0.5s of a cut run

    def __init__(self) -> None:
        self.silent_run = 0

    def feed(self, frame: bytes) -> bool:
        if not frame_is_silent(frame):
            self.silent_run = 0
            return True
        self.silent_run += 1
        return self.silent_run <= self.KEEP_FRAMES

# ---------------------------------------------------------------------------
# Opus encoder (ctypes wrapper around the system libopus)
# ---------------------------------------------------------------------------


class OpusEncoder:
    """One encoder: 16kHz mono, 640-sample (40ms) frames at a fixed
    64kbps CBR — exactly the frames the Rust addon sends. Uses the
    system libopus the image already ships (``libopus0``); no new
    Python dependency. The variadic ``opus_encoder_ctl`` is called
    with fixed int32 varargs, as opuslib does."""

    _lib = None

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> None:
        lib = self._load_library()
        err = ctypes.c_int(0)
        enc = lib.opus_encoder_create(
            sample_rate, channels, 2048, ctypes.byref(err)  # OPUS_APPLICATION_VOIP
        )
        if not enc or err.value != 0:
            raise CometixAsrError(f"opus_encoder_create failed: {err.value}")
        self._enc = enc
        # OPUS_SET_BITRATE_REQUEST (4002) -> 64000; OPUS_SET_VBR_REQUEST (4006) -> 0 (CBR).
        if lib.opus_encoder_ctl(enc, 4002, 64000) != 0:
            self.close()
            raise CometixAsrError("opus set bitrate failed")
        if lib.opus_encoder_ctl(enc, 4006, 0) != 0:
            self.close()
            raise CometixAsrError("opus set CBR failed")

    @classmethod
    def _load_library(cls):
        if cls._lib is not None:
            return cls._lib
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        try:
            lib = ctypes.CDLL(name)
        except OSError as exc:
            raise CometixAsrError(f"libopus not available ({name}): {exc}") from exc
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_ctl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int32,
        ]
        lib.opus_encoder_ctl.restype = ctypes.c_int
        lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int32,
        ]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_encoder_destroy.restype = None
        cls._lib = lib
        return lib

    def encode(self, pcm: bytes) -> bytes:
        """Encode exactly 640 int16 samples (1280 bytes, s16le)."""
        if len(pcm) != FRAME_BYTES:
            raise CometixAsrError(
                f"opus: frame must be {FRAME_BYTES} bytes (640 s16le samples)"
            )
        out = ctypes.create_string_buffer(4000)
        samples = (ctypes.c_int16 * FRAME_SAMPLES).from_buffer_copy(pcm)
        n = self._lib.opus_encode(self._enc, samples, FRAME_SAMPLES, out, 4000)
        if n < 0:
            raise CometixAsrError("opus_encode failed")
        return out.raw[:n]

    def close(self) -> None:
        if getattr(self, "_enc", None):
            self._lib.opus_encoder_destroy(self._enc)
            self._enc = None


# ---------------------------------------------------------------------------
# Device registration + settings token
# ---------------------------------------------------------------------------


@dataclass
class Credentials:
    did: str
    install_id: str
    ssid: str = ""
    cdid: str = ""
    openudid: str = ""
    clientudid: str = ""
    device_brand: str = "google"
    device_type: str = "Pixel 7 Pro"
    os_version: str = "16"
    ts: int = 0
    source: str = ""


def _cache_path() -> Path:
    return Path.home() / ".cache" / "cometix-asr" / "did.json"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _new_hex_id(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


def load_cached_credentials() -> Credentials:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CometixAsrError(f"no cached device credentials: {exc}") from exc
    creds = Credentials(
        did=data.get("did", ""),
        install_id=data.get("install_id", ""),
        ssid=data.get("ssid", ""),
        cdid=data.get("cdid", ""),
        openudid=data.get("openudid", ""),
        clientudid=data.get("clientudid", ""),
        device_brand=data.get("device_brand", "google"),
        device_type=data.get("device_type", "Pixel 7 Pro"),
        os_version=data.get("os_version", "16"),
        ts=data.get("ts", 0) or 0,
        source=data.get("source", ""),
    )
    if not creds.did or not creds.cdid:
        raise CometixAsrError("cached device credentials are incomplete")
    return creds


def get_cached_did() -> str:
    """The cached device id, or "" if none is cached."""
    try:
        return load_cached_credentials().did
    except CometixAsrError:
        return ""


def _save_credentials(creds: Credentials) -> None:
    path = _cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "did": creds.did,
        "install_id": creds.install_id,
        "ssid": creds.ssid,
        "cdid": creds.cdid,
        "openudid": creds.openudid,
        "clientudid": creds.clientudid,
        "device_brand": creds.device_brand,
        "device_type": creds.device_type,
        "os_version": creds.os_version,
        "ts": creds.ts,
        "source": creds.source,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _http_post_json(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def register_device() -> Credentials:
    """Register a fresh device. Returns the server-issued credentials."""
    cdid = _new_uuid()
    openudid = _new_hex_id(8)  # 16 hex chars
    clientudid = _new_uuid()
    now = int(time.time() * 1000)
    header = {
        "display_name": "Doubao IME",
        "device_id": 0,
        "install_id": 0,
        "device_platform": "android",
        "device_type": "Pixel 7 Pro",
        "device_brand": "google",
        "device_model": "Pixel 7 Pro",
        "os": "android",
        "os_version": "16",
        "os_api": "34",
        "resolution": "1080*2400",
        "dpi": "420",
        "language": "zh",
        "timezone": 8,
        "access": "wifi",
        "app_name": "oime",
        "version_code": 100102018,
        "version_name": "1.1.2",
        "channel": "official",
        "package": "com.bytedance.android.doubaoime",
        "openudid": openudid,
        "clientudid": clientudid,
        "cdid": cdid,
        "update_version_code": 100102018,
        "manifest_version_code": 100102018,
        "rom": "UP1A.231005.007",
        "rom_version": "UP1A.231005.007",
    }
    body = json.dumps(
        {"magic_tag": "ss_app_log", "header": header, "gen_time": now}
    ).encode("utf-8")
    params = {
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "_rticket": str(now),
        "cdid": cdid,
        "aid": APP_ID,
        "app_name": "oime",
        "version_code": "100102018",
        "channel": "official",
        "device_type": "Pixel 7 Pro",
        "device_brand": "google",
        "resolution": "1080*2400",
        "dpi": "420",
        "language": "zh",
        "os_api": "34",
        "os_version": "16",
        "timezone": 8,
        "access": "wifi",
        "rom": "UP1A.231005.007",
        "rom_version": "UP1A.231005.007",
    }
    url = REGISTER_URL + "?" + urllib.parse.urlencode(params)
    try:
        status, data = _http_post_json(
            url, body, {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CometixAsrError(f"device_register network: {exc}") from exc
    if status != 200:
        raise CometixAsrError(f"device_register HTTP {status}: {data.strip()}")
    try:
        parsed = json.loads(data)
    except ValueError as exc:
        raise CometixAsrError(f"device_register: bad response: {exc}") from exc
    did = str(parsed.get("device_id_str") or parsed.get("device_id") or "")
    iid = str(parsed.get("install_id_str") or parsed.get("install_id") or "")
    if not did or did == "0":
        raise CometixAsrError(f"device_register: no did in body: {data.strip()}")
    return Credentials(
        did=did,
        install_id=iid,
        ssid=_new_uuid(),
        cdid=cdid,
        openudid=openudid,
        clientudid=clientudid,
        device_brand="google",
        device_type="Pixel 7 Pro",
        os_version="16",
        ts=int(time.time() * 1000),
        source="register",
    )


def ensure_did(force: bool = False) -> str:
    """Return a server-issued device id, registering a fresh device
    when forced or when nothing is cached."""
    if not force:
        try:
            creds = load_cached_credentials()
        except CometixAsrError:
            creds = None
        if creds is not None and creds.did:
            return creds.did
    creds = register_device()
    _save_credentials(creds)
    return creds.did


def fetch_app_key(creds: Credentials) -> str:
    """Call the settings API; return data.settings.asr_config.app_key.
    The app key is stable per device and is used as the wss URL token.
    The value itself is never logged."""
    now = int(time.time() * 1000)
    params = {
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "_rticket": str(now),
        "cdid": creds.cdid,
        "device_id": creds.did,
        "aid": APP_ID,
        "app_name": "oime",
        "version_code": "100102018",
        "version_name": "1.1.2",
        "channel": "official",
    }
    body = b"body=null"
    stub = hashlib.md5(body).hexdigest().upper()
    url = SETTINGS_URL + "?" + urllib.parse.urlencode(params)
    try:
        status, data = _http_post_json(
            url, body, {"User-Agent": USER_AGENT, "x-ss-stub": stub}
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CometixAsrError(f"settings network: {exc}") from exc
    if status != 200:
        raise CometixAsrError(f"settings HTTP {status}")
    try:
        parsed = json.loads(data)
    except ValueError as exc:
        raise CometixAsrError(f"settings: bad response: {exc}") from exc
    app_key = (
        parsed.get("data", {}).get("settings", {}).get("asr_config", {}).get("app_key")
        or ""
    )
    if not app_key:
        raise CometixAsrError("settings: no app_key in response")
    return app_key

# ---------------------------------------------------------------------------
# Live session
# ---------------------------------------------------------------------------


def _debug_log_result_json(raw: str) -> None:
    """Append a raw result_json payload to /tmp/cometix-asr-result.jsonl
    when COMETIX_ASR_DEBUG_JSON is set. result_json contains no
    credentials; it is only ever written to a local file for protocol
    debugging, never printed to stdout."""
    try:
        with open("/tmp/cometix-asr-result.jsonl", "a", encoding="utf-8") as fh:
            os.chmod("/tmp/cometix-asr-result.jsonl", 0o600)
            fh.write(raw + "\n")
    except OSError:
        pass


def build_ws_url(creds: Credentials, token: str) -> str:
    """Mirror the URL the Rust addon connects to (verified by capture).
    ``token`` is the settings app_key for this device."""
    params = {
        "aid": APP_ID,
        "app_name": "doubaoime",
        "channel": "official",
        "device_brand": "Xiaomi",
        "device_id": creds.did,
        "device_platform": "android",
        "device_type": "Mi Note 10",
        "did": creds.did,
        "iid": creds.install_id,
        "install_id": creds.install_id,
        "os_version": "12",
        "update_version_code": "100314018",
        "version_code": "100314018",
        "version_name": "1.3.14",
        "appkey": APP_KEY,
        "token": token,
    }
    return WS_URL + "?" + urllib.parse.urlencode(sorted(params.items()))


def ws_headers() -> dict[str, str]:
    return {
        "x-ss-dp": APP_ID,
        "Origin": "wss://frontier-audio-ime-ws.doubao.com",
        "x-keepalive-timeout": "3600",
        "x-keepalive-interval": "3",
        "x-custom-keepalive": "true",
        "proto-version": "v2",
    }


def build_session_payload(creds: Credentials, context: str) -> str:
    """The StartSession JSON, verbatim from the captured frame, with
    only did and context substituted."""
    payload = {
        "audio_info": {"channel": 1, "format": "speech_opus", "sample_rate": 16000},
        "enable_punctuation": True,
        "enable_speech_rejection": False,
        "extra": {
            "app_name": "com.bytedance.android.doubaoime",
            "app_version": "1.3.14",
            "asr_params": '{"enable_global_tracking": true}',
            "cell_compress_rate": 8,
            "context": context,
            "did": creds.did,
            "disable_user_words": False,
            "enable_asr_threepass": True,
            "enable_asr_twopass": True,
            "enable_print_chinese": False,
            "end_smooth_window_ms": 800,
            "finish_wait_offline_time": 1000,
            "input_mode": "tool",
            "join_user_experience_improve_program": False,
            "max_wait_switch_offline_time": 1000,
            "network_change": {
                "switch_network_ping_timeout": 2000,
                "switch_network_quality_threshold": 4,
                "switch_network_rtt_threshold": 273,
            },
            "offline_wait_online_interval_time": 5000,
            "offline_wait_online_time": 5000,
            "os": "Android",
            "os_version": "14",
            "remove_space_between_han_eng": False,
            "remove_space_between_han_num": False,
            "retry_server_code": [40100000, 40100004, 50000000, 50000104, 50700000],
            "s2a_send_commands": ["帮我发送"],
            "s2a_send_enable": False,
            "strong_ddc": False,
            "use_twopass_retry": True,
        },
    }
    return json.dumps(payload)


class LiveSession:
    """One live ASR session. Events are delivered to ``on_event`` as
    dicts shaped like the Go ``Event``: type is one of "ready",
    "transcript", "error", "close", "debug".

    Only the live mode exists; there is no simulated transcript path.
    """

    def __init__(self, on_event, context: str = "") -> None:
        self._on_event = on_event
        self._context = context
        self.session_id = _new_uuid()
        self._token = APP_KEY  # protobuf token (field 2): the public appkey
        self._ws = None
        self._encoder: OpusEncoder | None = None
        self._pcm_buf = bytearray()
        self._frame_no = 0
        self._ready = False
        self._finalized = False
        self._closed = False
        self._inbox: asyncio.Queue[AsrResponse] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._finished = asyncio.Event()
        self._cumulative_text = ""  # longest results[0] seen
        self._settled_text = ""  # longest results[0] seen in a final payload

    def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            self._on_event(event)

    async def start(self) -> None:
        """Connect, run the StartTask/StartSession handshake, and start
        the read loop. Raises CometixAsrError on failure."""
        from websockets.asyncio.client import connect as ws_connect

        await asyncio.to_thread(ensure_did, False)
        creds = load_cached_credentials()
        token = await asyncio.to_thread(fetch_app_key, creds)
        try:
            self._encoder = OpusEncoder()
        except CometixAsrError:
            raise
        url = build_ws_url(creds, token)
        try:
            self._ws = await ws_connect(
                url,
                additional_headers=ws_headers(),
                open_timeout=15,
                ping_interval=20,
                max_size=2**22,
            )
        except Exception as exc:
            self._encoder.close()
            raise CometixAsrError(f"dial asr websocket: {exc}") from exc
        self._emit({"type": "debug", "message": "connecting wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"})
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._send_request("StartTask", "", None, 0, with_token=True)
            await self._wait_for("TaskStarted", 15)
            payload = build_session_payload(creds, self._context)
            await self._send_request("StartSession", payload, None, 0, with_token=True)
            await self._wait_for("SessionStarted", 15)
        except Exception:
            await self.close()
            raise
        self._ready = True
        self._emit(
            {"type": "ready", "session_id": self.session_id, "mode": "live"}
        )
        self._emit({"type": "debug", "message": "handshake OK; audio enabled"})
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    continue
                try:
                    resp = parse_response(message)
                except CometixAsrError:
                    continue
                self._inbox.put_nowait(resp)
        except Exception:
            pass
        finally:
            # The socket closed under us; unblock any handshake waiter
            # and the dispatcher so the session ends instead of hanging.
            self._inbox.put_nowait(AsrResponse(message_type="__closed__"))

    async def _dispatch_loop(self) -> None:
        while not self._closed:
            resp = await self._inbox.get()
            if resp.message_type == "__closed__":
                if not self._finished.is_set():
                    self._emit_close()
                return
            self._handle_response(resp)

    async def _wait_for(self, want: str, timeout: float) -> None:
        """Read responses until one with the given message_type arrives.
        Any other message during the handshake is ignored, as in the
        Go client (a result arriving early is handled by the read loop
        once it starts)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CometixAsrError(f"timeout waiting for {want}")
            try:
                resp = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            except TimeoutError as exc:
                raise CometixAsrError(f"timeout waiting for {want}") from exc
            if resp.message_type in ("TaskFailed", "SessionFailed"):
                raise CometixAsrError(
                    f"{resp.message_type}: code={resp.status_code} {resp.status_message}"
                )
            if resp.message_type == "__closed__":
                raise CometixAsrError(f"waiting for {want}: connection closed")
            if resp.message_type == want:
                if resp.status_code not in (0, 20000000):
                    raise CometixAsrError(
                        f"{want}: unexpected status code {resp.status_code} {resp.status_message}"
                    )
                self._emit(
                    {"type": "debug", "message": f"{want} OK code={resp.status_code}"}
                )
                return
            # Any other message during the handshake is ignored.

    async def _send_request(
        self,
        method: str,
        payload: str,
        audio: bytes | None,
        frame_no: int,
        with_token: bool,
    ) -> None:
        token = self._token if with_token else ""
        data = marshal_request(
            token, method, payload, audio or b"", self.session_id, frame_no
        )
        async with self._send_lock:
            if self._ws is None:
                raise CometixAsrError("session: no websocket")
            await self._ws.send(data)

    def _handle_response(self, resp: AsrResponse) -> None:
        if resp.message_type in ("TaskFailed", "SessionFailed"):
            self._emit(
                {
                    "type": "error",
                    "message": resp.status_message,
                    "code": str(resp.status_code),
                }
            )
            return
        if resp.message_type == "SessionFinished":
            self._emit_session_final()
            self._emit_close()
            return
        if not resp.result_json:
            return  # heartbeat / lifecycle message without results
        if os.environ.get("COMETIX_ASR_DEBUG_JSON"):
            _debug_log_result_json(resp.result_json)
        try:
            parsed = parse_result_json(resp.result_json)
        except ValueError:
            return
        if not parsed.results:
            return  # heartbeat (results:null)
        text = parsed.text()
        if not text:
            return  # VAD start (empty text)
        is_final = parsed.is_final()
        if len(text) > len(self._cumulative_text):
            self._cumulative_text = text
        if is_final and len(text) > len(self._settled_text):
            self._settled_text = text
        if is_final:
            # Stage "stable" here; the matching "session_final"
            # event is emitted when SessionFinished arrives.
            self._emit(
                {
                    "type": "transcript",
                    "text": text,
                    "display": text,
                    "stage": "stable",
                    "is_final": True,
                    "is_interim": False,
                    "is_vad_finished": True,
                    "stable_text": self._settled_text,
                    "pass_count": 2,
                }
            )
            return
        self._emit(
            {
                "type": "transcript",
                "text": text,
                "display": text,
                "stage": "interim",
                "is_final": False,
                "is_interim": True,
                "is_vad_finished": False,
                "pass_count": 2,
            }
        )

    def _emit_session_final(self) -> None:
        final_text = self._cumulative_text or self._settled_text
        if not final_text:
            return
        self._emit(
            {
                "type": "transcript",
                "text": final_text,
                "display": final_text,
                "stage": "session_final",
                "is_final": True,
                "is_interim": False,
                "is_vad_finished": True,
                "stable_text": final_text,
                "pass_count": 1,
            }
        )

    def _emit_close(self) -> None:
        if self._finished.is_set():
            return
        self._finished.set()
        self._emit({"type": "close"})

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def cumulative_text(self) -> str:
        return self._cumulative_text

    @property
    def settled_text(self) -> str:
        return self._settled_text

    async def feed_pcm(self, pcm: bytes) -> None:
        """Feed s16le 16kHz mono PCM. Any length is accepted; it is
        buffered into 640-sample frames and Opus-encoded."""
        if self._closed:
            raise CometixAsrError("session closed")
        if self._encoder is None:
            raise CometixAsrError("session not started")
        self._pcm_buf.extend(pcm)
        while len(self._pcm_buf) >= FRAME_BYTES:
            frame = bytes(self._pcm_buf[:FRAME_BYTES])
            del self._pcm_buf[:FRAME_BYTES]
            try:
                opus = self._encoder.encode(frame)
            except CometixAsrError as exc:
                raise CometixAsrError(f"opus encode: {exc}") from exc
            self._frame_no += 1
            payload = json.dumps(
                {"extra": {}, "timestamp_ms": int(time.time() * 1000)}
            )
            await self._send_request(
                "TaskRequest", payload, opus, self._frame_no, with_token=False
            )

    async def finalize(self) -> None:
        """Send FinishSession and wait for the final transcript /
        SessionFinished, bounded so a stalled server can't hang us."""
        if self._closed or self._finalized:
            return
        self._finalized = True
        # Flush a partial trailing frame as silence-padded audio.
        if self._pcm_buf:
            pad = bytes(FRAME_BYTES - len(self._pcm_buf))
            await self.feed_pcm(pad)
            self._pcm_buf = bytearray()
        await self._send_request("FinishSession", "", None, 0, with_token=True)
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=15)
        except TimeoutError:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finished.set()
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None


# ---------------------------------------------------------------------------
# RunPostAsr — standalone post-ASR: fmt + NER
# ---------------------------------------------------------------------------


def _postasr_env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value else default


def _postasr_app_key() -> str:
    """The post-ASR app key from COMETIX_APP_KEY. There is no default
    value in this repo: the key must be supplied via the environment
    (on the VPS, in /opt/chatgpt2api/.env)."""
    value = os.environ.get("COMETIX_APP_KEY")
    if value:
        return value
    raise CometixAsrError("COMETIX_APP_KEY is not set")


def _postasr_app_id() -> str:
    return _postasr_env("COMETIX_APP_ID", APP_ID)


def _postasr_fmt_url() -> str:
    return _postasr_env("COMETIX_FMT_URL", POSTASR_FMT_URL)


def _postasr_ner_url() -> str:
    return _postasr_env("COMETIX_NER_URL", POSTASR_NER_URL)


def _postasr_config_url() -> str:
    return _postasr_env("COMETIX_CONFIG_URL", POSTASR_CONFIG_URL)


@dataclass
class PostAsrConfig:
    """The five fields are did / enable_fmt / enable_ner / sami_token /
    uid. did is required; enable_fmt defaults to true, enable_ner to
    false. sami_token is None when missing/null (fetch via
    get_config); "" counts as "provided" (it will fail auth) and does
    NOT trigger get_config."""

    did: str
    enable_fmt: bool = True
    enable_ner: bool = False
    sami_token: str | None = None
    uid: str = ""


def _postasr_post_json(
    url: str, body: dict, did: str, token: str | None
) -> tuple[int, str, str]:
    """Post a JSON body with the headers from the Rust post_json.
    Auth headers (x-api-*) are only added when token is not None.
    Returns (status, status text, body text)."""
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": POSTASR_USER_AGENT,
        "Accept": "*/*",
        "app_version": POSTASR_APP_VERSION,
        "app_id": _postasr_app_id(),
        "os_type": POSTASR_OS,
        "did": did,
    }
    if token is not None:
        app_key = _postasr_app_key()
        headers["x-api-resource-id"] = POSTASR_RESOURCE_ID
        headers["x-api-app-key"] = app_key
        headers["x-api-token"] = token
        headers["x-api-request-id"] = _new_uuid()
        headers["x-api-sequence"] = "-1"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
            return resp.status, f"{resp.status} {resp.reason}", resp.read().decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            f"{exc.code} {exc.reason}",
            exc.read().decode("utf-8", "replace"),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CometixAsrError(str(exc)) from exc


def _find_string(value, keys: list[str]) -> str | None:
    """Search a parsed JSON value for the first non-empty string under
    one of keys, recursing into data/Data/result/Result."""
    if not isinstance(value, dict):
        return None
    for key in keys:
        found = value.get(key)
        if isinstance(found, str) and found:
            return found
    for key in ("data", "Data", "result", "Result"):
        if key in value:
            found = _find_string(value[key], keys)
            if found is not None:
                return found
    return None


def _postasr_get_config(cfg: PostAsrConfig) -> str | None:
    """Exchange the app key for a short-lived token. Body:
    {"sami_app_key": <app key>}. The token is found recursively under
    sami_token / samiToken / token in data/Data/result/Result."""
    body = {"sami_app_key": _postasr_app_key()}
    try:
        status, status_text, resp = _postasr_post_json(
            _postasr_config_url(), body, cfg.did, None
        )
    except CometixAsrError as exc:
        raise CometixAsrError(f"get_config: {exc}") from exc
    if not 200 <= status < 300:
        raise CometixAsrError(f"get_config HTTP {status_text}: {resp}")
    try:
        parsed = json.loads(resp)
    except ValueError as exc:
        raise CometixAsrError(f"get_config json: {exc}; body={resp}") from exc
    return _find_string(parsed, ["sami_token", "samiToken", "token"])


def _postasr_fmt(cfg: PostAsrConfig, text: str) -> str:
    """Post the text for formatting. The service currently answers
    {"code":0,"data":""}; an empty data means keep the original text."""
    body = {
        "app_id": _postasr_app_id(),
        "app_version": POSTASR_APP_VERSION,
        "did": cfg.did,
        "os_type": POSTASR_OS,
        "text": text,
    }
    try:
        status, status_text, resp = _postasr_post_json(
            _postasr_fmt_url(), body, cfg.did, None
        )
    except CometixAsrError as exc:
        raise CometixAsrError(f"fmt: {exc}") from exc
    if not 200 <= status < 300:
        raise CometixAsrError(f"fmt HTTP {status_text}: {resp}")
    try:
        parsed = json.loads(resp)
    except ValueError as exc:
        raise CometixAsrError(f"fmt json: {exc}; body={resp}") from exc
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, (int, float)) and code != 0:
            raise CometixAsrError(f"fmt code {int(code)}: {resp}")
        data = parsed.get("data")
        if isinstance(data, str) and data:
            return data
    return text


def _postasr_ner(cfg: PostAsrConfig, token: str, text: str) -> str:
    """Post the text for entity extraction; return the response
    verbatim as ner_raw."""
    body = {
        "additions": {},
        "text": text,
        "user": {
            "app_name": POSTASR_APP_NAME,
            "app_version": POSTASR_APP_VERSION,
            "did": cfg.did,
            "experience_improve": False,
            "platform": POSTASR_PLATFORM,
            "sdk_version": "",
            "uid": cfg.uid,
        },
    }
    try:
        status, status_text, resp = _postasr_post_json(
            _postasr_ner_url(), body, cfg.did, token
        )
    except CometixAsrError as exc:
        raise CometixAsrError(f"ner: {exc}") from exc
    if not 200 <= status < 300:
        raise CometixAsrError(f"ner HTTP {status_text}: {resp}")
    try:
        json.loads(resp)
    except ValueError as exc:
        raise CometixAsrError(f"ner json: {exc}; body={resp}") from exc
    return resp


def run_post_asr(cfg: PostAsrConfig, text: str) -> dict:
    """Standalone post-ASR (fmt + NER). Returns the PostAsrResult dict:
    {"input","fmt_text","final_text","ner_raw","sami_token_ok",
    "steps","errors"} with steps "fmt ok"/"ner ok" on success and
    "fmt fail"/"ner fail"/"sami_token fail" on failure. A config
    error is raised; fmt/NER/get_config failures are reported in
    steps/errors."""
    if not cfg.did:
        raise CometixAsrError("missing field `did`")
    steps: list[str] = []
    errors: list[str] = []
    fmt_text: str | None = None
    ner_raw = None
    token = cfg.sami_token

    # fmt first; it uses no token at all.
    if cfg.enable_fmt:
        try:
            fmt_text = _postasr_fmt(cfg, text)
            steps.append("fmt ok")
        except CometixAsrError as exc:
            steps.append("fmt fail")
            errors.append(str(exc))

    # get_config is only needed by NER, and only when no token was given.
    if cfg.enable_ner and token is None:
        try:
            token = _postasr_get_config(cfg)
        except CometixAsrError as exc:
            steps.append("sami_token fail")
            errors.append(str(exc))

    if cfg.enable_ner and token is not None:
        try:
            ner_raw = json.loads(_postasr_ner(cfg, token, text))
            steps.append("ner ok")
        except CometixAsrError as exc:
            steps.append("ner fail")
            errors.append(str(exc))

    return {
        "input": text,
        "fmt_text": fmt_text,
        "final_text": fmt_text if fmt_text is not None else text,
        "ner_raw": ner_raw,
        "sami_token_ok": bool(cfg.enable_ner and token is not None),
        "steps": steps,
        "errors": errors,
    }
