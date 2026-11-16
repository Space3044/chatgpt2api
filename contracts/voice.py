"""Stable voice projections (ported from the Go voice gateway).

The backend owns these JSON shapes; the console only transports them.
Field meanings are fixed by the contract tests in the local ``tests/``
directory. No projection here ever carries an access token, a refresh
token, a proxy URL, or an internal account id.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Public capability document (Go: internal/voice/config.go Config()) ---

DEFAULT_VOICE = "cove"
DEFAULT_VOICE_MODE = "wingman"
DEFAULT_LANGUAGE_CODE = "auto"
STUN_URLS = ("stun:stun.l.google.com:19302", "stun:stun4.l.google.com:19302")
DATA_CHANNEL_LABEL = "oai-events"
DATA_CHANNEL_ID = 0

VOICE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("breeze", "Breeze", "Animated"),
    ("cove", "Cove", "Composed"),
    ("ember", "Ember", "Confident"),
    ("fathom", "Arbor", "Easygoing"),
    ("glimmer", "Sol", "Savvy"),
    ("juniper", "Juniper", "Open"),
    ("maple", "Maple", "Cheerful"),
    ("orbit", "Spruce", "Calm"),
    ("vale", "Vale", "Bright"),
)

VOICE_IDS = frozenset(voice_id for voice_id, _, _ in VOICE_CHOICES)
VOICE_MODE_IDS = frozenset({DEFAULT_VOICE_MODE})

# Aliases the Go gateway accepted for the upstream voice names.
VOICE_ALIASES = {"arbor": "fathom", "sol": "glimmer", "spruce": "orbit"}

LANGUAGE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("auto", "自动检测", "Auto-detect"),
    ("en", "英语", "English"),
    ("es", "西班牙语", "Spanish"),
    ("pt", "葡萄牙语", "Portuguese"),
    ("fr", "法语", "French"),
    ("de", "德语", "German"),
    ("ja", "日语", "Japanese"),
    ("id", "印度尼西亚语", "Indonesian"),
    ("ru", "俄语", "Russian"),
    ("it", "意大利语", "Italian"),
    ("tr", "土耳其语", "Turkish"),
    ("ar", "阿拉伯语", "Arabic"),
    ("hi", "印地语", "Hindi"),
    ("ko", "韩语", "Korean"),
    ("nl", "荷兰语", "Dutch"),
    ("pl", "波兰语", "Polish"),
    ("vi", "越南语", "Vietnamese"),
    ("uk", "乌克兰语", "Ukrainian"),
    ("sv", "瑞典语", "Swedish"),
    ("da", "丹麦语", "Danish"),
    ("nb", "书面挪威语", "Norwegian Bokmål"),
    ("no", "挪威语", "Norwegian"),
    ("th", "泰语", "Thai"),
    ("ro", "罗马尼亚语", "Romanian"),
    ("ms", "马来语", "Malay"),
    ("bn", "孟加拉语", "Bangla"),
    ("mr", "马拉地语", "Marathi"),
    ("ta", "泰米尔语", "Tamil"),
    ("te", "泰卢固语", "Telugu"),
    ("gu", "古吉拉特语", "Gujarati"),
    ("ur", "乌尔都语", "Urdu"),
    ("ml", "马拉雅拉姆语", "Malayalam"),
    ("kn", "卡纳达语", "Kannada"),
    ("sw", "斯瓦希里语", "Swahili"),
    ("zh", "普通话", "Mandarin Chinese"),
    ("af", "南非荷兰语", "Afrikaans"),
    ("hy", "亚美尼亚语", "Armenian"),
    ("az", "阿塞拜疆语", "Azerbaijani"),
    ("be", "白俄罗斯语", "Belarusian"),
    ("bs", "波斯尼亚语", "Bosnian"),
    ("bg", "保加利亚语", "Bulgarian"),
    ("ca", "加泰罗尼亚语", "Catalan"),
    ("hr", "克罗地亚语", "Croatian"),
    ("cs", "捷克语", "Czech"),
    ("et", "爱沙尼亚语", "Estonian"),
    ("fi", "芬兰语", "Finnish"),
    ("gl", "加利西亚语", "Galician"),
    ("ka", "格鲁吉亚语", "Georgian"),
    ("el", "希腊语", "Greek"),
    ("he", "希伯来语", "Hebrew"),
    ("hu", "匈牙利语", "Hungarian"),
    ("is", "冰岛语", "Icelandic"),
    ("kk", "哈萨克语", "Kazakh"),
    ("lv", "拉脱维亚语", "Latvian"),
    ("lt", "立陶宛语", "Lithuanian"),
    ("mk", "马其顿语", "Macedonian"),
    ("mi", "毛利语", "Māori"),
    ("ne", "尼泊尔语", "Nepali"),
    ("fa", "波斯语", "Persian"),
    ("sr", "塞尔维亚语", "Serbian"),
    ("sk", "斯洛伐克语", "Slovak"),
    ("sl", "斯洛文尼亚语", "Slovenian"),
    ("tl", "菲律宾语", "Filipino"),
    ("cy", "威尔士语", "Welsh"),
    ("amh", "阿姆哈拉语", "Amharic"),
    ("mya", "缅甸语", "Burmese"),
    ("yue", "粤语（繁体）", "Cantonese (Traditional Chinese)"),
    ("fil", "菲律宾语", "Filipino"),
    ("gle", "爱尔兰语", "Irish"),
    ("mon", "蒙古语", "Mongolian"),
    ("som", "索马里语", "Somali"),
    ("zh-cn", "简体中文", "Simplified Chinese"),
    ("zh-tw", "繁体中文（台湾）", "Traditional Chinese (Taiwan)"),
    ("zh-hk", "繁体中文（香港）", "Traditional Chinese (Hong Kong)"),
)

LANGUAGE_IDS = frozenset(code for code, _, _ in LANGUAGE_CHOICES)


class VoiceChoice(_StrictModel):
    id: str
    name: str
    description: str


class Choice(_StrictModel):
    id: str
    name: str


class LanguageChoice(_StrictModel):
    id: str
    name_zh: str
    name_en: str


class SessionDefaults(_StrictModel):
    voice: str
    voice_mode: str
    language_code: str


class DataChannelConfig(_StrictModel):
    label: str
    negotiated: bool
    id: int


class IceServerConfig(_StrictModel):
    urls: list[str]


class WebRTCConfig(_StrictModel):
    data_channel: DataChannelConfig
    ice_servers: list[IceServerConfig]
    receive_audio: bool
    receive_video: bool


class PublicConfig(_StrictModel):
    version: str = "v1"
    defaults: SessionDefaults
    voices: list[VoiceChoice]
    voice_modes: list[Choice]
    languages: list[LanguageChoice]
    webrtc: WebRTCConfig


def public_config(
    *,
    default_voice: str = DEFAULT_VOICE,
    default_voice_mode: str = DEFAULT_VOICE_MODE,
    default_language_code: str = DEFAULT_LANGUAGE_CODE,
) -> PublicConfig:
    """Build the non-secret capability document for ``GET /v1/voice/config``."""
    return PublicConfig(
        defaults=SessionDefaults(
            voice=default_voice,
            voice_mode=default_voice_mode,
            language_code=default_language_code,
        ),
        voices=[
            VoiceChoice(id=voice_id, name=name, description=description)
            for voice_id, name, description in VOICE_CHOICES
        ],
        voice_modes=[Choice(id=DEFAULT_VOICE_MODE, name="Standard")],
        languages=[
            LanguageChoice(id=code, name_zh=name_zh, name_en=name_en)
            for code, name_zh, name_en in LANGUAGE_CHOICES
        ],
        webrtc=WebRTCConfig(
            data_channel=DataChannelConfig(
                label=DATA_CHANNEL_LABEL, negotiated=True, id=DATA_CHANNEL_ID
            ),
            ice_servers=[IceServerConfig(urls=[url]) for url in STUN_URLS],
            receive_audio=True,
            receive_video=False,
        ),
    )


def normalize_session_options(
    voice: str | None, voice_mode: str | None, language_code: str | None
) -> SessionDefaults:
    """Apply the Go gateway's defaults/aliases and validate the options.

    Raises ``ValueError`` with a ``unsupported <field>`` message for an
    unknown value, matching the Go ``normalizeSessionOptions`` errors.
    """
    cleaned_voice = (voice or "").strip().lower() or DEFAULT_VOICE
    cleaned_voice = VOICE_ALIASES.get(cleaned_voice, cleaned_voice)
    if cleaned_voice not in VOICE_IDS:
        raise ValueError("unsupported voice")
    cleaned_mode = (voice_mode or "").strip().lower() or DEFAULT_VOICE_MODE
    if cleaned_mode not in VOICE_MODE_IDS:
        raise ValueError("unsupported voice_mode")
    cleaned_language = (language_code or "").strip().lower() or DEFAULT_LANGUAGE_CODE
    if cleaned_language not in LANGUAGE_IDS:
        raise ValueError("unsupported language_code")
    return SessionDefaults(
        voice=cleaned_voice,
        voice_mode=cleaned_mode,
        language_code=cleaned_language,
    )


# --- Session requests / responses ---


class CreateSessionRequest(_StrictModel):
    offer_sdp: str = Field(min_length=1)
    voice: str | None = None
    voice_mode: str | None = None
    language_code: str | None = None
    voice_session_id: str | None = None
    account_id: str | None = None
    upstream_conversation_id: str | None = None
    upstream_parent_message_id: str | None = None
    upstream_voice_session_id: str | None = None


class CreateSessionResponse(_StrictModel):
    """Public create-session result. Never carries account/token/proxy data."""

    answer_sdp: str
    voice_session_id: str
    voice: str
    voice_mode: str
    language_code: str
    upstream_voice_session_id: str = ""
    upstream_conversation_id: str = ""
    upstream_parent_message_id: str = ""


class UpstreamContext(_StrictModel):
    upstream_conversation_id: str = ""
    upstream_parent_message_id: str = ""
    upstream_voice_session_id: str = ""


class UpdateContextRequest(_StrictModel):
    upstream_conversation_id: str | None = None
    upstream_parent_message_id: str | None = None
    upstream_voice_session_id: str | None = None


class UpdateContextResponse(_StrictModel):
    ok: bool = True
    voice_session_id: str
    upstream_conversation_id: str = ""
    upstream_parent_message_id: str = ""
    upstream_voice_session_id: str = ""


class TitleResponse(_StrictModel):
    voice_session_id: str
    upstream_conversation_id: str
    title: str = ""
    has_title: bool = False


# --- Finalized subtitle / text message persistence ---
#
# The console only ever holds the UPSTREAM conversation id (it comes
# back in ``CreateSessionResponse.upstream_conversation_id`` and in the
# context updates), so ``conversation_id`` here IS the upstream id.
# The backend resolves it to the local ``voice_conversations`` row
# (``vc_…``) and stores the message under that local id in
# ``voice_messages``; the upstream id is never written into the
# ``voice_messages.conversation_id`` column. The response echoes the
# upstream id back so the console can keep using the id it already has.


class RecordMessageRequest(_StrictModel):
    """Persist one finalized subtitle/text message.

    ``conversation_id`` is the upstream conversation id, NOT the local
    ``voice_conversations`` id. ``role`` is strict; ``text`` must be
    non-empty after trimming; ``kind`` defaults to ``"text"``
    (subtitles use ``"subtitle"``).
    """

    conversation_id: str = Field(min_length=1)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)
    kind: str = "text"

    @model_validator(mode="after")
    def validate_message(self) -> "RecordMessageRequest":
        if not (self.conversation_id or "").strip():
            raise ValueError("conversation_id must not be empty")
        if not (self.text or "").strip():
            raise ValueError("text must not be empty")
        self.kind = (self.kind or "").strip() or "text"
        if len(self.kind) > 32:
            raise ValueError("kind must be at most 32 characters")
        return self


class RecordMessageResponse(_StrictModel):
    """Result of persisting one message. Never carries message text."""

    ok: bool = True
    voice_session_id: str
    upstream_conversation_id: str
    message_id: int
    role: str
    kind: str = "text"


class ReleaseResponse(_StrictModel):
    ok: bool = True
    released: bool


# --- Admin list (metadata only) ---


class VoiceSessionListItem(_StrictModel):
    """One voice session for the admin list. No account/token/proxy data."""

    voice_session_id: str
    owner: str
    voice: str
    voice_mode: str
    language_code: str
    status: str
    upstream_conversation_id: str = ""
    upstream_voice_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class VoiceSessionListResponse(_StrictModel):
    items: list[VoiceSessionListItem] = Field(default_factory=list)
    total: int = 0


class VoiceError(_StrictModel):
    """Downstream error shape: ``{"error": {"code": ..., "message": ...}}``."""

    code: str
    message: str
