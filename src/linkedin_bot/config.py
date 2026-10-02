from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelConfig(BaseModel, extra="allow"):
    """`model` im Format "provider:model"; alle weiteren Keys gehen als kwargs an das Modell."""

    model: str


class TopicWeight(BaseModel):
    weight: float = 1.0
    max_share: float | None = None  # Anteil an den letzten quota_window_posts, ab dem die Kategorie pausiert


class ScoringConfig(BaseModel):
    scale: int = 10
    min_score: int = 7
    max_candidates: int = 8


class BudgetConfig(BaseModel):
    max_usd_per_run: float = 1.50
    max_writer_iterations: int = 3


class Price(BaseModel):
    """USD pro 1 Mio. Tokens."""

    input: float
    output: float
    cached_input: float | None = None


class LengthConfig(BaseModel):
    min: int = 900
    max: int = 1800


class WritingConfig(BaseModel):
    length_chars: LengthConfig = Field(default_factory=LengthConfig)
    hook_max_lines: int = 1
    hook_max_chars: int = 140
    links_in_post: bool = False
    source_in_first_comment: bool = True
    end_with_question: bool = True
    max_hashtags: int = 3
    emojis: Literal["none", "sparse"] = "none"
    perspective: str = ""
    voice_examples_dir: Path = Path("voice_examples")
    banned_phrases: list[str] = Field(default_factory=list)
    avoid_patterns: list[str] = Field(default_factory=list)


class ImagesConfig(BaseModel):
    model: str = "gpt-image-2"
    size: str = "1536x1024"
    quality: str = "medium"
    style: str = "Fotorealistische Editorial-Fotografie, natürliches Licht, keine Schrift im Bild."


class CarouselTheme(BaseModel):
    font: str = "Inter"
    mono_font: str = "JetBrains Mono"
    background: str = "#FAFAF7"
    accent: str = "#1F6FEB"


class SlideRange(BaseModel):
    min: int = 5
    max: int = 8


class CarouselConfig(BaseModel):
    enabled: bool = True
    model: ModelConfig = Field(default_factory=lambda: ModelConfig(model="openai:gpt-5.4-mini"))
    slides: SlideRange = Field(default_factory=SlideRange)
    format: str = "1080x1350"
    code_highlighting: bool = True
    theme: CarouselTheme = Field(default_factory=CarouselTheme)
    footer: str = ""  # Text unten links auf jeder Folie, z.B. Name und Rolle

    @property
    def page_size(self) -> tuple[int, int]:
        width, height = self.format.lower().split("x")
        return int(width), int(height)


class HNRanking(BaseModel):
    method: Literal["points", "gravity"] = "gravity"
    gravity: float = 1.8


class HNKeywordSearch(BaseModel):
    enabled: bool = False
    min_points: int = 30
    keywords: list[str] = Field(default_factory=list)


class HackerNewsConfig(BaseModel):
    min_points: int = 150
    lookback_hours: int = 24
    max_items: int = 20
    ranking: HNRanking = Field(default_factory=HNRanking)
    keyword_search: HNKeywordSearch = Field(default_factory=HNKeywordSearch)


class SourcesConfig(BaseModel):
    rss: list[str] = Field(default_factory=list)
    hackernews: HackerNewsConfig | None = None


class DedupeConfig(BaseModel):
    ttl_days: int = 30
    similar_title_threshold: float = 0.85


class TrendsConfig(BaseModel):
    enabled: bool = False
    geo: str = "DE"
    timeframe: str = "now 7-d"
    min_growth: float = 0.2   # ab +20 % Suchinteresse gibt es einen Bonus
    max_bonus: float = 0.2    # höchstens +20 % aufs gewichtete Ranking
    rising_queries: int = 5   # steigende Suchanfragen als Kontext für den Writer


class LimitsConfig(BaseModel):
    max_age_hours: int = 48
    max_items_to_score: int = 100
    max_items_per_feed: int = 12  # sonst verdrängen News-Feeds mit vielen Meldungen die Fachquellen


class ResearchConfig(BaseModel):
    max_tool_calls: int = 5


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


class AppConfig(BaseModel):
    audience: str
    topics: str
    topic_weights: dict[str, TopicWeight] = Field(default_factory=dict)
    quota_window_posts: int = 10
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    language: str = "de"
    models: dict[str, ModelConfig]
    model_fallbacks: dict[str, str] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    pricing: dict[str, Price] = Field(default_factory=dict)
    writing: WritingConfig = Field(default_factory=WritingConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    carousel: CarouselConfig = Field(default_factory=CarouselConfig)
    sources: SourcesConfig
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    trends: TrendsConfig = Field(default_factory=TrendsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    prompts_dir: Path = PROJECT_ROOT / "prompts"

    @property
    def author(self) -> str:
        """Rolle und Humor des Autors (prompts/author.md) – für Writer, Critic und Themenwahl."""
        return (self.prompts_dir / "author.md").read_text()

    @property
    def voice_examples_path(self) -> Path:
        path = self.writing.voice_examples_dir
        return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path | None = None) -> AppConfig:
    path = path or PROJECT_ROOT / "config.yaml"
    return AppConfig.model_validate(yaml.safe_load(path.read_text()))
