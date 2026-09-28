from pathlib import Path

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelConfig(BaseModel, extra="allow"):
    """`model` im Format "provider:model"; alle weiteren Keys gehen als kwargs an das Modell."""

    model: str


class HackerNewsConfig(BaseModel):
    min_points: int = 150
    lookback_hours: int = 24


class SourcesConfig(BaseModel):
    rss: list[str] = Field(default_factory=list)
    hackernews: HackerNewsConfig | None = None


class LimitsConfig(BaseModel):
    max_age_hours: int = 48
    max_items_to_score: int = 40
    min_relevance: int = 6
    shortlist_size: int = 5


class ResearchConfig(BaseModel):
    max_tool_calls: int = 5


class WritingConfig(BaseModel):
    min_chars: int = 900
    max_chars: int = 1800
    max_revisions: int = 2


class ScheduleConfig(BaseModel):
    time: str = "07:30"
    timezone: str = "Europe/Berlin"
    days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])


class LinkedInConfig(BaseModel):
    dry_run: bool = True
    api_version: str = "202609"
    visibility: str = "PUBLIC"
    remind_days_before_expiry: int = 7


class AppConfig(BaseModel):
    audience: str
    topics: str
    language: str = "de"
    models: dict[str, ModelConfig]
    sources: SourcesConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    writing: WritingConfig = Field(default_factory=WritingConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    prompts_dir: Path = PROJECT_ROOT / "prompts"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or PROJECT_ROOT / "config.yaml"
    return AppConfig.model_validate(yaml.safe_load(path.read_text()))
