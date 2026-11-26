"""Settings: data root, API key, per-step models, blend weights, style note, render options.

Resolution order for the data root: --data-root flag, GOODFEELING_DATA_ROOT, the settings
file's data_root, then ~/Movies/Good Feeling. The settings file lives outside the data root
(GOODFEELING_CONFIG, or the platform config dir) so it can point the data root elsewhere.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr

DEFAULT_DATA_ROOT = Path.home() / "Movies" / "Good Feeling"


class StepModels(BaseModel):
    score: str = "claude-sonnet-5-5"
    moments: str = "claude-sonnet-5-5"
    understand: str = "claude-opus-5-5"
    direct: str = "claude-opus-5-5"


class StepEffort(BaseModel):
    score: str = "low"
    moments: str = "low"
    understand: str = "medium"
    direct: str = "medium"


class Weights(BaseModel):
    claude: float = 0.7
    motion: float = 0.15
    sharpness: float = 0.1
    exposure: float = 0.05


class RenderSettings(BaseModel):
    encoder: str = "auto"
    x264_preset: str = "medium"
    x264_crf: int = 18
    bitrate_1080: str = "24M"
    bitrate_4k: str = "80M"
    videotoolbox_quality: int | None = None
    audio_bitrate: str = "320k"


class Settings(BaseModel):
    data_root: Path | None = None
    anthropic_api_key: SecretStr | None = None
    models: StepModels = Field(default_factory=StepModels)
    effort: StepEffort = Field(default_factory=StepEffort)
    weights: Weights = Field(default_factory=Weights)
    style_note: str = ""
    pace: str = "normal"
    lut: str | None = None
    stabilized_dir: str | None = None
    flight_gap_s: float = 90.0
    props_penalty: float = 0.0
    score_batch_size: int = 4
    max_cost_usd: float = 5.0
    render: RenderSettings = Field(default_factory=RenderSettings)

    def resolved_data_root(self, override: str | Path | None = None) -> Path:
        chosen = override or os.environ.get("GOODFEELING_DATA_ROOT") or self.data_root or DEFAULT_DATA_ROOT
        return Path(chosen).expanduser().resolve()

    def api_key(self) -> str | None:
        env = os.environ.get("ANTHROPIC_API_KEY")
        if env:
            return env
        return self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None


def config_path() -> Path:
    env = os.environ.get("GOODFEELING_CONFIG")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Good Feeling" / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "goodfeeling" / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        return Settings()
    return Settings.model_validate(json.loads(path.read_text()))
