"""Voice-input service: pure-Python cometix-asr client, in-process.

Since voice.10.4 the Python backend owns the live ASR session and
the standalone RunPostAsr (fmt + NER) directly
(``services/cometix_asr.py``); there is no Go sidecar and no loopback
service any more. This service owns only the Python-side projection
for the non-streaming calls: the config probe and the standalone
post-ASR. The live streaming path lives in
``services/asr_stream_service.py``; the record-then-transcribe HTTP
path was removed in voice.10.3.

The post-ASR app key has no default in the repo and comes only from
``COMETIX_APP_KEY`` in the container environment; without it, live
transcription still works and only RunPostAsr fails, with a clear
error. No device id, token or cometix-asr secret ever appears in a
log on this path.

Mock mode is not ported: there is no simulated transcript path here.
"""
from __future__ import annotations

from services.cometix_asr import (
    CometixAsrError,
    PostAsrConfig,
    ensure_did,
    get_cached_did,
    run_post_asr,
)

MAX_PCM_SECONDS = 180
MAX_PCM_BYTES = MAX_PCM_SECONDS * 16000 * 2  # 180s of 16kHz mono s16le


class AsrInputError(Exception):
    """An error the API layer maps to an HTTP status + message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AsrInputService:
    """Voice-input config probe and standalone post-ASR."""

    def config(self) -> dict[str, object]:
        # The ASR client runs in-process now, so there is no sidecar
        # to probe: the feature is available whenever this code is.
        return {
            "enabled": True,
            "sample_rate": 16000,
            "channels": 1,
            "max_seconds": MAX_PCM_SECONDS,
        }

    def post_asr(
        self,
        text: str,
        enable_fmt: bool = True,
        enable_ner: bool = False,
        sami_token: str | None = None,
        uid: str = "",
    ) -> dict[str, object]:
        did = get_cached_did()
        if not did:
            try:
                did = ensure_did(False)
            except CometixAsrError as exc:
                raise AsrInputError(503, "语音识别服务不可用，请稍后重试") from exc
        try:
            result = run_post_asr(
                PostAsrConfig(
                    did=did,
                    enable_fmt=enable_fmt,
                    enable_ner=enable_ner,
                    sami_token=sami_token,
                    uid=uid,
                ),
                text,
            )
        except CometixAsrError as exc:
            raise AsrInputError(400, str(exc)) from exc
        if not isinstance(result, dict) or "final_text" not in result:
            raise AsrInputError(502, "后处理服务返回了无法识别的结果")
        return result


asr_input_service = AsrInputService()
