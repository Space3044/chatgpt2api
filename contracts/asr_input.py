"""Stable projections for voice input (cometix-asr sidecar).

The browser records audio and the Studio composer inserts the recognised
text. The backend projection is the only place that decides what the
frontend may show: availability, the audio format, and the transcript
result. It never carries device ids, tokens or upstream credentials.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AsrConfigView(_StrictModel):
    """Voice-input availability and audio format for the Studio composer."""

    enabled: bool
    sample_rate: int = 16000
    channels: int = 1
    max_seconds: int = 60


class AsrTranscribeResponse(_StrictModel):
    """Result of one push-to-talk utterance."""

    text: str
    final_text: str
    interim_count: int = 0
    post_asr: dict[str, Any] | None = None


class AsrPostAsrRequest(_StrictModel):
    """Standalone post-ASR (fmt + NER) on already recognised text."""

    text: str = Field(min_length=1, max_length=10000)
    enable_fmt: bool = True
    enable_ner: bool = False
    sami_token: str | None = None
    uid: str = ""


class AsrPostAsrResponse(_StrictModel):
    """PostAsrResult JSON, as returned by the sidecar (see docs/asr-input.md)."""

    input: str
    fmt_text: str | None = None
    final_text: str
    ner_raw: Any = None
    sami_token_ok: bool = False
    steps: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
