import operator
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

# Schlüssel passen zu topic_weights in config.yaml.
Category = Literal["ki_entwicklung", "shopware_ecommerce", "tech_allgemein", "politik", "wissenschaft", "off_topic"]


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
    search_term: str = Field(default="", description="1-3 Wörter, wie Menschen in Deutschland bei Google danach suchen würden, z.B. 'Shopware 6.7' oder 'AI Act'")


class ScoreBatch(BaseModel):
    scores: list[ItemScore]


class ScoredItem(BaseModel):
    item: NewsItem
    relevance: int  # Rohwert des Scorers
    category: Category
    reason: str
    weighted: float = 0.0  # relevance × topic_weights[category].weight (× Trends-Bonus) – nur fürs Ranking
    search_term: str = ""
    trend_growth: float | None = None  # Google-Trends-Wachstum, None = nicht abgefragt


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


class HashtagList(BaseModel):
    tags: list[str] = Field(description="Hashtags ohne #-Zeichen, CamelCase, z.B. KIimHandel")


class ImagePrompt(BaseModel):
    prompt: str = Field(description="Englischer Bild-Prompt für ein Bildmodell")
    alt_text: str = Field(description="Kurzer deutscher Alt-Text (max. 120 Zeichen)")


class Slide(BaseModel):
    kind: Literal["title", "text", "code", "closing"] = Field(
        description="title = erste Folie mit Hook, text = Aussage + kurzer Text, code = Code-Beispiel, closing = Schluss mit Frage")
    headline: str = Field(description="Kurze, starke Überschrift der Folie (max. ~60 Zeichen)")
    body: str = Field(default="", description="1-3 kurze Sätze (max. ~220 Zeichen); bei code optional eine Zeile Erklärung")
    code: str = Field(default="", description="Nur bei kind=code: kurzes Snippet, max. 12 Zeilen à max. 46 Zeichen")
    language: str = Field(default="", description="Nur bei kind=code: Sprache für Syntax-Highlighting, z.B. php, twig, javascript, python, yaml")


class CarouselSpec(BaseModel):
    title: str = Field(description="Dokumenttitel, den LinkedIn über dem Karussell anzeigt (max. ~60 Zeichen)")
    slides: list[Slide]


Variant = Literal["normal", "humor"]
VARIANTS: tuple[Variant, ...] = ("normal", "humor")


class DraftVariant(BaseModel):
    text: str
    critique: Critique | None = None  # None = vom Autor bearbeitet
    revisions: int = 0


def merge_drafts(current: dict | None, update: dict | None) -> dict:
    """Parallel geschriebene Varianten zusammenführen; `None` setzt zurück (neues Thema).

    Ein Eintrag mit Wert `None` entfernt nur diese Variante (z.B. "Ohne Bild")."""
    if update is None:
        return {}
    merged = {**(current or {}), **update}
    return {k: v for k, v in merged.items() if v is not None}


Action = Literal["approve", "edit", "revise", "new_topic", "reject", "image", "no_image", "carousel", "no_carousel"]


class Decision(TypedDict, total=False):
    """Resume-Wert für den Freigabe-Interrupt."""

    action: Action
    variant: Variant  # approve / edit / revise beziehen sich auf eine Variante
    text: str  # edit: neuer Post-Text, revise: Feedback an den Writer


class ComposeTask(TypedDict, total=False):
    """Payload für genau eine Variante (per Send, laufen parallel)."""

    variant: Variant
    selected: ScoredItem
    angle: str
    research: Research
    previous: DraftVariant | None  # bei Überarbeitung durch den Autor
    human_feedback: str | None
    trend_queries: list[str]


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
    trends_status: str | None  # None = aus, "ok" oder Grund, warum Google Trends nicht verfügbar war
    trend_queries: list[str]  # steigende Suchanfragen zum gewählten Thema
    drafts: Annotated[dict[str, DraftVariant], merge_drafts]
    hashtags: list[str]
    # Je Variante ein optionales Bild; die Bytes liegen im Repository (Schlüssel: image_key).
    image_prompts: Annotated[dict[str, ImagePrompt], merge_drafts]
    # Je Variante optional ein Karussell (ersetzt dort das Bild – ein Post hat nur ein Medium); PDF im Repository.
    carousels: Annotated[dict[str, CarouselSpec], merge_drafts]
    # Human-in-the-Loop
    decision: Action | Literal["review"]
    variant: Variant  # gewählte bzw. zu überarbeitende Variante
    human_feedback: str | None
    notice: str | None  # Hinweis an den Autor bei der nächsten Freigabe-Anfrage
    rejected_urls: Annotated[list[str], operator.add]  # per "Anderes Thema" verworfen
    # approved = freigegeben, aber nicht veröffentlicht (Dry-Run); publish_failed = API-Fehler, Text liegt im Archiv
    status: Literal["published", "approved", "publish_failed", "rejected"]
    post_urn: str | None
    publish_error: str | None
    source_commented: bool
    comment_error: str | None  # Post ist online, nur der Quell-Kommentar hat nicht geklappt
