"""
Runtime settings loaded from config.json with environment variable overrides.

Precedence (highest first): environment variable, config.json, built-in default.
"""

import json
import os
from typing import Any, Dict, Optional

from .constants import DEFAULT_AUDIO_LANGUAGE, normalize_language

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")

DEFAULTS: Dict[str, Any] = {
    "input_dir": "downloads/raw",
    "output_dir": "downloads/encoded",
    "audio_language": DEFAULT_AUDIO_LANGUAGE,
}

ENV_VARS = {
    "input_dir": "AV1_INPUT_DIR",
    "output_dir": "AV1_OUTPUT_DIR",
    "audio_language": "AV1_AUDIO_LANGUAGE",
}


def load_settings(config_path: Optional[str] = None) -> Dict[str, Any]:
    settings = dict(DEFAULTS)

    path = config_path or CONFIG_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            settings.update({k: v for k, v in data.items() if k in DEFAULTS and v})
        except (OSError, ValueError):
            pass

    for key, env_name in ENV_VARS.items():
        value = os.environ.get(env_name)
        if value:
            settings[key] = value

    # Relative folders are resolved against the project root so the app
    # behaves the same regardless of the working directory it was started from.
    for key in ("input_dir", "output_dir"):
        settings[key] = os.path.abspath(os.path.join(PROJECT_ROOT, os.path.expanduser(settings[key])))

    settings["audio_language"] = normalize_language(settings["audio_language"])
    return settings
