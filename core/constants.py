"""
Global constants and option lists shared by the web dashboard, CLI and encoder.
"""

APP_TITLE = "AV1 Encoder"
APP_VERSION = "3.0.0"

# Rate Control Modes
RATE_CONTROL_CQ = "Constant Quality (CQ)"
RATE_CONTROL_SIZE = "Target File Size"
RATE_CONTROL_OPTIONS = [RATE_CONTROL_CQ, RATE_CONTROL_SIZE]
TARGET_SIZE_UNITS = ["GB", "MB"]

# Resolution / Scaling Options
RESOLUTION_SOURCE = "Source (Original / No Scaling)"
RESOLUTION_1080P = "1080p (1920x1080 / Auto Height)"
RESOLUTION_720P = "720p (1280x720 / Auto Height)"
RESOLUTION_1440P = "1440p (2560x1440 / Auto Height)"
RESOLUTION_4K = "4K (3840x2160 / Auto Height)"
RESOLUTION_OPTIONS = [
    RESOLUTION_SOURCE,
    RESOLUTION_1080P,
    RESOLUTION_720P,
    RESOLUTION_1440P,
    RESOLUTION_4K
]

# Color & HDR Tone-Mapping Profiles
COLOR_FORMAT_AUTO = "Auto (10-bit / Match Source HDR/SDR)"
COLOR_FORMAT_10BIT_SDR = "10-bit SDR (Auto Tonemap HDR to BT.709)"
COLOR_FORMAT_10BIT_HDR = "10-bit HDR10 (BT.2020 PQ)"
COLOR_FORMAT_8BIT_SDR = "8-bit SDR (BT.709 yuv420p)"
COLOR_FORMAT_OPTIONS = [
    COLOR_FORMAT_AUTO,
    COLOR_FORMAT_10BIT_SDR,
    COLOR_FORMAT_10BIT_HDR,
    COLOR_FORMAT_8BIT_SDR
]

# Supported video file extensions
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.webm', '.ts', '.mov', '.flv', '.wmv'}

# Audio normalization defaults
DEFAULT_TARGET_LUFS = -20.0
DEFAULT_TRUE_PEAK = -1.5
DEFAULT_LRA = 11.0

# Worker thread options
WORKER_OPTIONS = [
    "1 Worker (Sequential)",
    "2 Workers",
    "3 Workers",
    "4 Workers",
    "5 Workers",
    "6 Workers",
    "8 Workers"
]

# Encoder presets & defaults
VIDEO_CQ_OPTIONS = ["18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "30", "32", "34"]
SVT_PRESET_OPTIONS = ["4", "5", "6", "7", "8"]
FILM_GRAIN_OPTIONS = [
    "Disabled",
    "Light (8)",
    "Medium (16)",
    "Heavy (24)"
]

# Audio options
OPUS_BITRATE_OPTIONS = ["64", "80", "96", "112", "128", "160", "192", "256", "320"]
AUDIO_CHANNELS_OPTIONS = [
    "Stereo (2.0)",
    "5.1 Surround (Original/Preserve)"
]

NORM_MODE_OPTIONS = [
    "2-Pass Linear (-20 LUFS)",
    "1-Pass Dynamic (-20 LUFS)",
    "1-Pass Dynamic (-23 LUFS)",
    "Disabled"
]

# Which audio tracks to keep. "Preferred language" is the audio_language setting;
# "original" is the first audio track in the source.
AUDIO_TRACKS_PREFERRED = "Preferred language only"
AUDIO_TRACKS_PREFERRED_AND_ORIGINAL = "Preferred language + original (preferred is default)"
AUDIO_TRACKS_ORIGINAL_AND_PREFERRED = "Original + preferred language (original is default)"
AUDIO_TRACKS_FIRST = "First track only"
AUDIO_TRACKS_ALL = "All tracks"
AUDIO_TRACK_OPTIONS = [
    AUDIO_TRACKS_PREFERRED,
    AUDIO_TRACKS_PREFERRED_AND_ORIGINAL,
    AUDIO_TRACKS_ORIGINAL_AND_PREFERRED,
    AUDIO_TRACKS_FIRST,
    AUDIO_TRACKS_ALL,
]

DEFAULT_AUDIO_LANGUAGE = "eng"

# ISO 639 codes as they appear in container metadata, mapped to one canonical
# three-letter code so "en", "eng" and "English" settings all match.
LANGUAGE_ALIASES = {
    "en": "eng", "english": "eng",
    "ja": "jpn", "japanese": "jpn",
    "es": "spa", "spanish": "spa",
    "fr": "fra", "fre": "fra", "french": "fra",
    "de": "deu", "ger": "deu", "german": "deu",
    "it": "ita", "italian": "ita",
    "pt": "por", "portuguese": "por",
    "ru": "rus", "russian": "rus",
    "zh": "zho", "chi": "zho", "chinese": "zho",
    "ko": "kor", "korean": "kor",
    "nl": "nld", "dut": "nld", "dutch": "nld",
    "pl": "pol", "polish": "pol",
    "hi": "hin", "hindi": "hin",
    "ar": "ara", "arabic": "ara",
}

LANGUAGE_NAMES = {
    "eng": "English", "jpn": "Japanese", "spa": "Spanish", "fra": "French",
    "deu": "German", "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "zho": "Chinese", "kor": "Korean", "nld": "Dutch", "pol": "Polish",
    "hin": "Hindi", "ara": "Arabic",
}


def normalize_language(code: str) -> str:
    """Returns a canonical three-letter language code ('und' if empty)."""
    code = (code or "").strip().lower()
    if not code:
        return "und"
    return LANGUAGE_ALIASES.get(code, code)


def language_name(code: str) -> str:
    code = normalize_language(code)
    return LANGUAGE_NAMES.get(code, code.upper())
