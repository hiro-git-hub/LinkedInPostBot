import operator
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

Category = Literal["ai_software_dev", "ai_ecommerce", "trend", "off_topic"]


class NewsItem(BaseModel):
    source: str
    title: str
    url: str
    summary: str = ""
    published: datetime | None = None
    points: int | None = None  # Community-Signal, z.B. HN-Upvotes


class ItemScore(BaseModel):
    index: int = Field(description="Index des Items aus der Eingabeliste")
    relevance: int = Field(description="Relevanz für die Zielgruppe, 0-10")
    category: Category
    reason: str = Field(description="Ein Satz: warum ist das für die Zielgruppe interessant?")


class ScoreBatch(BaseModel):
    scores: list[ItemScore]


class ScoredItem(BaseModel):
    item: NewsItem
    relevance: int
    category: Category
    reason: str


class TopicChoice(BaseModel):
    index: int = Field(description="Index des gewählten Themas aus der Shortlist")
    angle: str = Field(description="Aufhänger / These, mit der der Post das Thema erzählt")


class Fact(BaseModel):
    statement: str = Field(description="Konkrete, überprüfbare Aussage")
    source_url: str = Field(description="URL, in der die Aussage belegt ist")


class Research(BaseModel):
    summary: str = Field(description="Kern der Nachricht in 2-4 Sätzen")
    facts: list[Fact] = Field(description="Belegte Fakten: Zahlen, Namen, Daten, Funktionen")
    context: str = Field(description="Einordnung: Hintergrund, Reaktionen, Folgen für Entwicklung/Handel")


class Critique(BaseModel):
    approved: bool = Field(description="True, wenn der Post ohne Änderung veröffentlicht werden kann")
    issues: list[str] = Field(description="Konkrete, umsetzbare Verbesserungen")
    unsupported_claims: list[str] = Field(description="Aussagen im Post, die nicht durch die Recherche belegt sind")


Action = Literal["approve", "edit", "revise", "new_topic", "reject"]


class Decision(TypedDict, total=False):
    """Resume-Wert für den Freigabe-Interrupt."""

    action: Action
    text: str  # edit: neuer Post-Text, revise: Feedback an den Writer


class SourceTask(TypedDict):
    """Payload, das per Send an genau einen Collector geht."""

    source: str


class State(TypedDict, total=False):
    # Parallele Collector schreiben gleichzeitig -> Reducer hängt Listen an.
    items: Annotated[list[NewsItem], operator.add]
    unique: list[NewsItem]
    scored: list[ScoredItem]
    selected: ScoredItem | None
    angle: str
    article: str
    research: Research
    draft: str
    revisions: int  # Anzahl geschriebener Entwürfe
    critique: Critique | None
    # Human-in-the-Loop
    decision: Action | Literal["review"]
    human_feedback: str | None
    notice: str | None  # Hinweis an den Autor bei der nächsten Freigabe-Anfrage
    rejected_urls: Annotated[list[str], operator.add]  # per "Anderes Thema" verworfen
    # approved = freigegeben, aber nicht veröffentlicht (Dry-Run); publish_failed = API-Fehler, Text liegt im Archiv
    status: Literal["published", "approved", "publish_failed", "rejected"]
    post_urn: str | None
    publish_error: str | None
