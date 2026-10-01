from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelConfig(BaseModel, extra="allow"):
    """`model` im Format "provider:model"; alle weiteren Keys gehen als kwargs an das Modell."""

    model: str


class HackerNewsConfig(BaseModel):
    min_points: int = 150
    lookback_hours: int = 24
    max_items: int = 20


class SourcesConfig(BaseModel):
    rss: list[str] = Field(default_factory=list)
    hackernews: HackerNewsConfig | None = None


class LimitsConfig(BaseModel):
    max_age_hours: int = 48
    max_items_to_score: int = 100
    max_items_per_feed: int = 12  # sonst verdrängen News-Feeds mit vielen Meldungen die Fachquellen
    min_relevance: int = 6
    shortlist_size: int = 5


class ResearchConfig(BaseModel):
    max_tool_calls: int = 5


class WritingConfig(BaseModel):
    min_chars: int = 900
    max_chars: int = 1800
    max_revisions: int = 2
    # Hook = alles vor der ersten Leerzeile; LinkedIn zeigt mobil nur ~2 Zeilen vor "…mehr".
    hook_max_lines: int = 2
    hook_max_chars: int = 150
    # Abgenutzte Formulierungen – kommt eine davon vor, muss der Writer umformulieren.
    banned_phrases: list[str] = Field(default_factory=list)


class ScheduledRun(BaseModel):
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    time: str  # "HH:MM"


class ScheduleConfig(BaseModel):
    timezone: str = "Europe/Berlin"
    runs: list[ScheduledRun] = Field(default_factory=lambda: [
        ScheduledRun(day="wed", time="09:00"), ScheduledRun(day="sun", time="18:00"),
    ])


class LinkedInConfig(BaseModel):
    dry_run: bool = True
    api_version: str = "202609"
    visibility: str = "PUBLIC"
    remind_days_before_expiry: int = 7
    comment_source: bool = True  # Quell-Link automatisch als ersten Kommentar posten


class ImagesConfig(BaseModel):
    model: str = "gpt-image-1"
    size: str = "1536x1024"
    quality: str = "medium"
    style: str = "Fotorealistische Editorial-Fotografie, natürliches Licht, keine Schrift im Bild."


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
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    prompts_dir: Path = PROJECT_ROOT / "prompts"

    @property
    def author(self) -> str:
        """Rolle und Humor des Autors (prompts/author.md) – für Writer, Critic und Themenwahl."""
        return (self.prompts_dir / "author.md").read_text()


def load_config(path: Path | None = None) -> AppConfig:
    path = path or PROJECT_ROOT / "config.yaml"
    return AppConfig.model_validate(yaml.safe_load(path.read_text()))
