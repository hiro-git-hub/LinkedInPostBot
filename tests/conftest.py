"""Gemeinsame Fakes: deterministisch pro Variante, weil Normal- und Humor-Version parallel laufen."""

import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).parent))  # "from conftest import ..." in den Testmodulen

from linkedin_bot.collectors.hackernews import ALGOLIA_BY_DATE_URL, ALGOLIA_URL  # noqa: E402
from linkedin_bot.config import load_config  # noqa: E402
from linkedin_bot.db import InMemoryRepository  # noqa: E402
from linkedin_bot.graph import build_graph  # noqa: E402
from linkedin_bot.integrations.linkedin import LinkedInAuth  # noqa: E402
from linkedin_bot.state import (  # noqa: E402
    CarouselSpec, Critique, Fact, HashtagList, ImagePrompt, ItemScore, Research, ScoreBatch, Slide, TopicChoice,
)

FEED_URL = "https://example.com/feed.xml"
NOW = datetime.now(UTC)

RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example AI Blog</title>
<item><title>Neues Modell veröffentlicht</title><link>https://example.com/model?utm_source=rss</link>
<description>Details zum Release</description><pubDate>{NOW:%a, %d %b %Y %H:%M:%S +0000}</pubDate></item>
<item><title>Uralter Artikel</title><link>https://example.com/old</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item>
</channel></rss>"""

HN = {
    "hits": [
        {"objectID": "1", "title": "Neues Modell veröffentlicht", "url": "https://www.example.com/model/",
         "points": 420, "created_at_i": int(NOW.timestamp())},
        {"objectID": "2", "title": "Ask HN: Wie deployt ihr?", "url": None,
         "points": 200, "created_at_i": int(NOW.timestamp())},
    ]
}

ARTICLE_HTML = "<html><body><article><h1>Neues Modell</h1>" + "<p>Das Modell kann 42 Dinge.</p>" * 20 + "</article></body></html>"

RESEARCH = Research(
    summary="Ein neues Modell ist erschienen.",
    facts=[Fact(statement="Das Modell kann 42 Dinge.", source_url="https://example.com/model")],
    context="Branche reagiert positiv.",
)
APPROVE = Critique(approved=True, issues=[], unsupported_claims=[])
HUMOR_MARKER = "HUMORVOLLE VARIANTE"  # steht in prompts/humor.md


def draft(n: int, variant: str = "normal") -> str:
    prefix = "Humor-Entwurf" if variant == "humor" else "Entwurf"
    # Erfüllt alle Formregeln: einzeilige Hook, ~1000 Zeichen, endet mit einer konkreten Frage.
    return f"{prefix} {n}\n\n" + "Ein Satz mit Substanz. " * 43 + "Wie testet ihr RAG-Antworten im Shop?"


def valid_auth(days: int = 30) -> LinkedInAuth:
    return LinkedInAuth("token", datetime.now(UTC) + timedelta(days=days), "urn:li:person:abc", "Jonas")


class FakeModel(FakeListChatModel):
    """FakeListChatModel kann kein Structured Output – wir liefern pro Schema feste Antworten."""

    structured: dict[type, object] = {}
    calls: list = []

    def with_structured_output(self, schema, **kwargs):
        handler = self.structured[schema]

        def run(messages):
            self.calls.append(messages)
            return handler(messages) if callable(handler) else handler
        return RunnableLambda(run)


class VariantWriter(FakeListChatModel):
    """Liefert pro Variante die nächste Antwort aus einer eigenen Liste und merkt sich die Prompts."""

    texts: dict[str, list[str]] = {}
    prompts: dict[str, list[str]] = {}
    lock: object = None

    def __init__(self, texts: dict[str, list[str]] | None = None, **kwargs):
        texts = texts or {v: [draft(n, v) for n in (1, 2, 3, 4)] for v in ("normal", "humor")}
        super().__init__(responses=[""], texts={k: list(v) for k, v in texts.items()},
                         prompts={"normal": [], "humor": []}, lock=threading.Lock(), **kwargs)

    def _call(self, messages, *args, **kwargs):
        variant = "humor" if HUMOR_MARKER in messages[0].content else "normal"
        with self.lock:
            self.prompts[variant].append(messages[-1].content)
            return self.texts[variant].pop(0)


def fake_scores(relevance: dict[str, int]):
    def score(messages):
        titles = [line for line in messages[-1].content.splitlines() if line.startswith("[")]
        return ScoreBatch(scores=[
            ItemScore(index=i, relevance=next((v for k, v in relevance.items() if k in t), 0),
                      category="ki_entwicklung", reason="weil")
            for i, t in enumerate(titles)
        ])
    return score


def critic_by_text(reject_if: Callable[[str], bool] = lambda text: False):
    """Critic-Entscheidung hängt nur vom Entwurfstext ab – unabhängig von der Reihenfolge paralleler Aufrufe."""
    def review(messages):
        text = messages[-1].content.split("Entwurf:\n", 1)[1]
        if reject_if(text):
            return Critique(approved=False, issues=["Einstieg schärfen"], unsupported_claims=[])
        return APPROVE
    return review


class Models:
    """Alle Fake-Modelle eines Tests – als model_factory an build_graph übergeben."""

    def __init__(self, relevance: dict[str, int], reject_if=lambda text: False, writer=None,
                 hashtags=("KI", "#Dev Tools", "ki", "Shopware", "RAG")):
        self.writer = writer or VariantWriter()
        self.hashtags = FakeModel(responses=[""], structured={HashtagList: HashtagList(tags=list(hashtags))}, calls=[])
        self.roles = {
            "scorer": FakeModel(responses=[""], structured={ScoreBatch: fake_scores(relevance)}, calls=[]),
            "selector": FakeModel(responses=[""], structured={TopicChoice: TopicChoice(index=0, angle="These")}, calls=[]),
            "writer": self.writer,
            "critic": FakeModel(responses=[""], structured={Critique: critic_by_text(reject_if)}, calls=[]),
            "hashtags": self.hashtags,
            "image_prompt": FakeModel(responses=[""], structured={
                ImagePrompt: ImagePrompt(prompt="a lighthouse made of circuits", alt_text="Leuchtturm aus Platinen")
            }, calls=[]),
            "carousel": FakeModel(responses=[""], structured={CarouselSpec: CAROUSEL}, calls=[]),
        }

    def __call__(self, _cfg, role):
        return self.roles[role]


def fake_research_agent(calls: list):
    def run(payload):
        calls.append(payload["messages"][0].content)
        return {"structured_response": RESEARCH}
    return RunnableLambda(run)


CAROUSEL = CarouselSpec(title="Shopware 6.7 Upgrade", slides=[
    Slide(kind="title", headline="Hook"), Slide(kind="code", headline="Code", code="echo 1;", language="php"),
] + [Slide(kind="text", headline=f"Punkt {i}") for i in range(10)])  # mehr als slides.max -> wird gekürzt


def fake_image(prompt: str) -> bytes:
    return b"PNG:" + prompt.encode()


def fake_carousel(spec, _cfg, footer) -> bytes:
    return f"PDF:{spec.title}:{len(spec.slides)}:{footer}".encode()


@pytest.fixture
def cfg():
    cfg = load_config()
    cfg.sources.rss = [FEED_URL]
    cfg.linkedin.dry_run = True  # unabhängig von der lokalen config.yaml; Publish-Tests schalten gezielt um
    return cfg


@pytest.fixture
def sources():
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=RSS.encode()))
        respx.get(ALGOLIA_URL).mock(return_value=httpx.Response(200, json=HN))
        respx.get(ALGOLIA_BY_DATE_URL).mock(return_value=httpx.Response(200, json={"hits": []}))
        respx.get(url__regex=r"https://(www\.)?example\.com/model.*").mock(
            return_value=httpx.Response(200, text=ARTICLE_HTML)
        )
        respx.get(url__startswith="https://news.ycombinator.com/item").mock(return_value=httpx.Response(404))
        yield respx


def make_graph(cfg, models, repo=None, calls=None, linkedin=None):
    return build_graph(
        cfg, repo if repo is not None else InMemoryRepository(), InMemorySaver(), models,
        research_agent=fake_research_agent(calls if calls is not None else []),
        linkedin=linkedin, image_generator=fake_image, carousel_renderer=fake_carousel,
    )
