"""Voice-input service: talks to the cometix-asr Go sidecar.

The sidecar (asr-sidecar/cmd/asr-server, built into the Docker image and
started by the entrypoint on loopback) owns the live ASR session and the
standalone RunPostAsr (fmt + NER). This service owns only the Python-side
projection: validating the recorded PCM, forwarding it to the sidecar,
and returning the sidecar's result. It never sees device ids, tokens or
any cometix-asr secret: the sidecar reads COMETIX_APP_KEY from the
container environment itself, and the only credential on this path is
the optional ASR_SIDECAR_KEY shared header, also from the environment.

Mock mode is not ported: there is no simulated transcript path here.
If the sidecar is unreachable, transcribe fails with 503 and a clear
message instead of inventing a transcript.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MAX_PCM_SECONDS = 180
MAX_PCM_BYTES = MAX_PCM_SECONDS * 16000 * 2  # 180s of 16kHz mono s16le
SIDECAR_TIMEOUT_SECS = 180


class AsrInputError(Exception):
    """An error the API layer maps to an HTTP status + message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _sidecar_url() -> str:
    return (os.getenv("ASR_SIDECAR_URL") or "http://127.0.0.1:3101").rstrip("/")


def _sidecar_headers(content_type: str) -> dict[str, str]:
    headers = {"Content-Type": content_type}
    key = os.getenv("ASR_SIDECAR_KEY") or ""
    if key:
        headers["X-Asr-Key"] = key
    return headers


def _call_sidecar(path: str, body: bytes, content_type: str) -> dict:
    url = f"{_sidecar_url()}{path}"
    req = urllib.request.Request(
        url, data=body, headers=_sidecar_headers(content_type), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=SIDECAR_TIMEOUT_SECS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            parsed = json.loads(detail)
            message = str(parsed.get("error") or detail)
        except (ValueError, TypeError):
            message = detail or f"语音识别服务返回 HTTP {exc.code}"
        raise AsrInputError(
            502 if exc.code >= 500 else exc.code, message
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AsrInputError(
            503, "语音识别服务不可用，请稍后重试"
        ) from exc


class AsrInputService:
    """Forwards recorded speech to the sidecar; owns no ASR state."""

    def config(self) -> dict[str, object]:
        enabled = False
        try:
            req = urllib.request.Request(f"{_sidecar_url()}/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                enabled = resp.status == 200
        except Exception:
            enabled = False
        return {
            "enabled": enabled,
            "sample_rate": 16000,
            "channels": 1,
            "max_seconds": MAX_PCM_SECONDS,
        }

    def transcribe(self, pcm: bytes, postasr: bool = False) -> dict[str, object]:
        if not pcm:
            raise AsrInputError(400, "没有收到语音数据")
        if len(pcm) % 2 != 0:
            raise AsrInputError(400, "语音数据必须是 16 位单声道 PCM")
        if len(pcm) > MAX_PCM_BYTES:
            raise AsrInputError(400, "语音超过 180 秒，请分段录入")
        path = "/transcribe?postasr=1" if postasr else "/transcribe"
        result = _call_sidecar(path, pcm, "application/octet-stream")
        if not isinstance(result, dict) or "text" not in result:
            raise AsrInputError(502, "语音识别服务返回了无法识别的结果")
        return {
            "text": str(result.get("text") or ""),
            "final_text": str(result.get("final_text") or result.get("text") or ""),
            "interim_count": int(result.get("interim_count") or 0),
            "post_asr": result.get("post_asr"),
        }

    def post_asr(
        self,
        text: str,
        enable_fmt: bool = True,
        enable_ner: bool = False,
        sami_token: str | None = None,
        uid: str = "",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "text": text,
            "enable_fmt": enable_fmt,
            "enable_ner": enable_ner,
            "uid": uid,
        }
        if sami_token is not None:
            payload["sami_token"] = sami_token
        result = _call_sidecar(
            "/postasr", json.dumps(payload).encode("utf-8"), "application/json"
        )
        if not isinstance(result, dict) or "final_text" not in result:
            raise AsrInputError(502, "后处理服务返回了无法识别的结果")
        return result


asr_input_service = AsrInputService()
