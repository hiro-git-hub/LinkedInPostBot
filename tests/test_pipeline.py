import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver

from linkedin_bot.collectors.hackernews import ALGOLIA_URL
from linkedin_bot.config import load_config
from linkedin_bot.db import InMemoryRepository
from linkedin_bot.graph import build_graph
from linkedin_bot.integrations.linkedin import POSTS_URL, TOKEN_URL, USERINFO_URL, LinkedInAuth, LinkedInClient, LinkedInError, escape_little
from linkedin_bot.nodes.rules import rule_issues
from linkedin_bot.nodes.dedup import dedupe, normalize_url
from linkedin_bot.runtime import resume, start_run
from linkedin_bot.state import Critique, Decision, Fact, ItemScore, NewsItem, Research, ScoreBatch, TopicChoice

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


def draft(n: int) -> str:
    return f"Entwurf {n}\n\n" + "Ein Satz mit Substanz. " * 45  # ~1000 Zeichen


class FakeModel(FakeListChatModel):
    """FakeListChatModel kann kein Structured Output – wir liefern pro Schema feste Antworten."""

    structured: dict[type, object] = {}

    def with_structured_output(self, schema, **kwargs):
        handler = self.structured[schema]
        return RunnableLambda(lambda messages: handler(messages) if callable(handler) else handler)


def fake_scores(relevance: dict[str, int]):
    def score(messages):
        titles = [line for line in messages[-1].content.splitlines() if line.startswith("[")]
        return ScoreBatch(scores=[
            ItemScore(index=i, relevance=next((v for k, v in relevance.items() if k in t), 0),
                      category="ai_software_dev", reason="weil")
            for i, t in enumerate(titles)
        ])
    return score


class RecordingWriter(FakeListChatModel):
    """Merkt sich, welches Material der Writer bekommen hat."""

    prompts: list[str] = []

    def _call(self, messages, *args, **kwargs):
        self.prompts.append(messages[-1].content)
        return super()._call(messages, *args, **kwargs)


APPROVE = Critique(approved=True, issues=[], unsupported_claims=[])


def model_factory(relevance: dict[str, int], critiques: list[Critique] | None = None, writer=None):
    critiques = list(critiques or [APPROVE])

    def next_critique(_messages):
        return critiques.pop(0) if len(critiques) > 1 else critiques[0]

    def factory(_cfg, role):
        return {
            "scorer": FakeModel(responses=[""], structured={ScoreBatch: fake_scores(relevance)}),
            "selector": FakeModel(responses=[""], structured={TopicChoice: TopicChoice(index=0, angle="These")}),
            "writer": writer or FakeListChatModel(responses=[draft(1), draft(2), draft(3)]),
            "critic": FakeModel(responses=[""], structured={Critique: next_critique}),
        }[role]
    return factory


def fake_research_agent(calls: list):
    def run(payload):
        calls.append(payload["messages"][0].content)
        return {"structured_response": RESEARCH}
    return RunnableLambda(run)


@pytest.fixture
def cfg():
    cfg = load_config()
    cfg.sources.rss = [FEED_URL]
    cfg.linkedin.dry_run = True  # unabhängig von der lokalen config.yaml; Publish-Tests schalten gezielt um
    return cfg


def mock_sources():
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=RSS.encode()))
    respx.get(ALGOLIA_URL).mock(return_value=httpx.Response(200, json=HN))
    respx.get(url__regex=r"https://(www\.)?example\.com/model.*").mock(
        return_value=httpx.Response(200, text=ARTICLE_HTML)
    )
    respx.get(url__startswith="https://news.ycombinator.com/item").mock(return_value=httpx.Response(404))


def make_graph(cfg, factory, repo=None, calls=None, linkedin=None):
    return build_graph(cfg, repo or InMemoryRepository(), InMemorySaver(), factory,
                       research_agent=fake_research_agent(calls if calls is not None else []), linkedin=linkedin)


# --- Bausteine -------------------------------------------------------------------------------------

def test_normalize_url_strips_tracking_and_www():
    assert normalize_url("http://www.Example.com/model/?utm_source=x&id=3") == "https://example.com/model?id=3"


def test_dedupe_keeps_item_with_more_points():
    a = NewsItem(source="rss", title="A", url="https://example.com/a?utm_medium=x")
    b = NewsItem(source="hn", title="A", url="https://www.example.com/a/", points=99)
    assert dedupe([a, b]) == [b]


def test_rule_issues(cfg):
    assert rule_issues(draft(1), cfg) == []
    too_long = "x" * (cfg.writing.max_chars + 1)
    assert "Zu lang" in rule_issues(too_long, cfg)[0]
    checklist = draft(1) + "\n- a\n- b\n• c\n1. d"
    assert any("Aufzählungszeilen" in i for i in rule_issues(checklist, cfg))
    assert any("Hashtags" in i for i in rule_issues(draft(1) + "\n#KI", cfg))


# --- Pipeline bis zur Freigabe ---------------------------------------------------------------------

@respx.mock
def test_run_researches_article_and_waits_for_approval(cfg):
    mock_sources()
    calls = []
    step = start_run(make_graph(cfg, model_factory({"Neues Modell": 9, "Ask HN": 3}), calls=calls))

    state = step.state
    assert len(state["items"]) == 3  # alter RSS-Eintrag gefiltert
    assert len(state["unique"]) == 2  # RSS + HN zeigen auf denselben Artikel
    assert state["selected"].item.points == 420
    assert "42 Dinge" in state["article"]
    assert "42 Dinge" in calls[0]  # Artikeltext geht an den Recherche-Agenten
    assert step.pending["draft"] == draft(1).strip()
    assert step.pending["critique"].approved


@respx.mock
def test_critic_loop_revises_until_approved(cfg):
    mock_sources()
    reject = Critique(approved=False, issues=["Einstieg schärfen"], unsupported_claims=[])
    step = start_run(make_graph(cfg, model_factory({"Neues Modell": 9}, [reject, APPROVE])))
    assert step.state["revisions"] == 2
    assert step.pending["draft"] == draft(2).strip()


@respx.mock
def test_critic_loop_stops_after_max_revisions(cfg):
    mock_sources()
    reject = Critique(approved=False, issues=["immer noch schlecht"], unsupported_claims=[])
    step = start_run(make_graph(cfg, model_factory({"Neues Modell": 9}, [reject])))
    assert step.state["revisions"] == cfg.writing.max_revisions + 1
    assert not step.pending["critique"].approved  # Autor sieht die offenen Punkte


@respx.mock
def test_no_draft_below_threshold(cfg):
    mock_sources()
    step = start_run(make_graph(cfg, model_factory({})))
    assert step.pending is None
    assert step.state["selected"] is None


@respx.mock
def test_broken_source_does_not_abort_run(cfg):
    mock_sources()
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    step = start_run(make_graph(cfg, model_factory({"Neues Modell": 8})))
    assert step.pending


# --- Freigabe (Human-in-the-Loop) ------------------------------------------------------------------

@respx.mock
def test_approve_archives_post(cfg):
    mock_sources()
    repo = InMemoryRepository()
    graph = make_graph(cfg, model_factory({"Neues Modell": 9}), repo)
    step = resume(graph, start_run(graph).thread_id, Decision(action="approve"))
    assert step.pending is None
    assert step.state["status"] == "approved"
    assert [(p["status"], p["text"]) for p in repo.posts] == [("approved", draft(1).strip())]


@respx.mock
def test_approve_blocked_while_placeholder_present(cfg):
    mock_sources()
    writer = FakeListChatModel(responses=[draft(1) + "\n[EIGENE ERFAHRUNG: Projektbeispiel]"])
    graph = make_graph(cfg, model_factory({"Neues Modell": 9}, writer=writer))
    step = start_run(graph)

    step = resume(graph, step.thread_id, Decision(action="approve"))
    assert "Platzhalter" in step.pending["notice"]

    step = resume(graph, step.thread_id, Decision(action="edit", text="Mein fertiger Text"))
    assert step.pending["draft"] == "Mein fertiger Text"
    step = resume(graph, step.thread_id, Decision(action="approve"))
    assert step.state["status"] == "approved"


@respx.mock
def test_revise_passes_author_feedback_to_writer(cfg):
    mock_sources()
    writer = RecordingWriter(responses=[draft(1), draft(2)], prompts=[])
    graph = make_graph(cfg, model_factory({"Neues Modell": 9}, writer=writer))
    step = start_run(graph)

    step = resume(graph, step.thread_id, Decision(action="revise", text="Mehr Bezug zu Shopware"))
    assert "Mehr Bezug zu Shopware" in writer.prompts[-1]
    assert step.pending["draft"] == draft(2).strip()


@respx.mock
def test_new_topic_picks_another_candidate(cfg):
    mock_sources()
    graph = make_graph(cfg, model_factory({"Neues Modell": 9, "Ask HN": 7}))
    step = start_run(graph)
    assert step.pending["title"] == "Neues Modell veröffentlicht"

    step = resume(graph, step.thread_id, Decision(action="new_topic"))
    assert step.pending["title"] == "Ask HN: Wie deployt ihr?"

    step = resume(graph, step.thread_id, Decision(action="new_topic"))
    assert step.pending is None  # keine Kandidaten mehr


@respx.mock
def test_second_run_skips_items_seen_before(cfg):
    mock_sources()
    repo = InMemoryRepository()
    factory = model_factory({"Neues Modell": 9})
    start_run(make_graph(cfg, factory, repo))
    step = start_run(make_graph(cfg, factory, repo))
    assert step.state["unique"] == []
    assert step.pending is None


# --- Veröffentlichung auf LinkedIn -----------------------------------------------------------------

def valid_auth(days: int = 30) -> LinkedInAuth:
    return LinkedInAuth("token", datetime.now(UTC) + timedelta(days=days), "urn:li:person:abc", "Jonas")


class FakeLinkedIn:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.posts: list[str] = []

    def create_post(self, auth, text, visibility="PUBLIC"):
        if self.error:
            raise self.error
        self.posts.append(text)
        return "urn:li:share:123"


def approve_first_draft(cfg, repo, linkedin):
    graph = make_graph(cfg, model_factory({"Neues Modell": 9}), repo, linkedin=linkedin)
    return resume(graph, start_run(graph).thread_id, Decision(action="approve"))


@respx.mock
def test_dry_run_archives_without_publishing(cfg):
    mock_sources()
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    repo.save_linkedin_auth(valid_auth())
    step = approve_first_draft(cfg, repo, linkedin)
    assert step.state["status"] == "approved"
    assert linkedin.posts == []


@respx.mock
def test_publish_stores_post_urn(cfg):
    mock_sources()
    cfg.linkedin.dry_run = False
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    repo.save_linkedin_auth(valid_auth())
    step = approve_first_draft(cfg, repo, linkedin)

    assert step.state["status"] == "published"
    assert linkedin.posts == [draft(1).strip()]
    assert repo.posts[0]["post_urn"] == "urn:li:share:123"
    assert repo.recent_topics(7) == ["Neues Modell veröffentlicht"]


@respx.mock
@pytest.mark.parametrize("auth", [None, valid_auth(days=-1)], ids=["missing", "expired"])
def test_publish_without_valid_login_fails_gracefully(cfg, auth):
    mock_sources()
    cfg.linkedin.dry_run = False
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    if auth:
        repo.save_linkedin_auth(auth)
    step = approve_first_draft(cfg, repo, linkedin)

    assert step.state["status"] == "publish_failed"
    assert "/login" in step.state["publish_error"]
    assert linkedin.posts == []
    assert repo.posts[0]["text"] == draft(1).strip()  # Text geht nicht verloren


@respx.mock
def test_publish_api_error_is_archived(cfg):
    mock_sources()
    cfg.linkedin.dry_run = False
    repo = InMemoryRepository()
    repo.save_linkedin_auth(valid_auth())
    step = approve_first_draft(cfg, repo, FakeLinkedIn(error=LinkedInError("Post erstellen fehlgeschlagen (500)")))
    assert step.state["status"] == "publish_failed"
    assert repo.posts[0]["error"] == "Post erstellen fehlgeschlagen (500)"


def test_escape_little_reserved_characters():
    assert escape_little("Neu (Beta) #KI @Team [x] a_b ~ * <3 | {x} \\") == (
        "Neu \\(Beta\\) \\#KI \\@Team \\[x\\] a\\_b \\~ \\* \\<3 \\| \\{x\\} \\\\"
    )
    assert escape_little("Kund:innen – »ok« 50%") == "Kund:innen – »ok« 50%"


@pytest.fixture
def linkedin_env(monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "secret")
    monkeypatch.setenv("LINKEDIN_REDIRECT_URI", "http://localhost:8765/callback")


@respx.mock
def test_linkedin_client_create_post_request(linkedin_env):
    route = respx.post(POSTS_URL).mock(return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"}))
    urn = LinkedInClient("202609").create_post(valid_auth(), "Hallo (Welt)")

    assert urn == "urn:li:share:9"
    request = route.calls.last.request
    assert request.headers["LinkedIn-Version"] == "202609"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert request.headers["Authorization"] == "Bearer token"
    body = json.loads(request.content)
    assert body["author"] == "urn:li:person:abc"
    assert body["commentary"] == "Hallo \\(Welt\\)"
    assert body["lifecycleState"] == "PUBLISHED"


@respx.mock
def test_linkedin_client_exchange_code(linkedin_env):
    token = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 5184000}))
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(200, json={"sub": "xyz", "name": "Jonas F"}))
    auth = LinkedInClient("202609").exchange_code("the-code")

    assert (auth.access_token, auth.person_urn, auth.name) == ("tok", "urn:li:person:xyz", "Jonas F")
    assert 59 <= (auth.expires_at - datetime.now(UTC)).days <= 60
    assert b"code=the-code" in token.calls.last.request.content


@respx.mock
def test_linkedin_client_raises_on_api_error(linkedin_env):
    respx.post(POSTS_URL).mock(return_value=httpx.Response(403, text='{"code":"ACCESS_DENIED"}'))
    with pytest.raises(LinkedInError, match="403"):
        LinkedInClient("202609").create_post(valid_auth(), "x")


@respx.mock
def test_pending_steps_lists_only_waiting_threads(cfg):
    from linkedin_bot.runtime import pending_steps

    mock_sources()
    graph = make_graph(cfg, model_factory({"Neues Modell": 9}))
    waiting = start_run(graph)
    done = start_run(graph)  # gleiche Items sind jetzt "gesehen" -> kein Thema, läuft durch
    assert done.pending is None

    steps = pending_steps(graph)
    assert [s.thread_id for s in steps] == [waiting.thread_id]
    assert steps[0].pending["draft"] == draft(1).strip()


def test_code_from_callback_validates_state():
    from linkedin_bot.integrations.linkedin import code_from_callback, params_from_url

    url = "http://localhost:8765/callback?code=abc123&state=s3cret"
    assert code_from_callback(params_from_url(url), "s3cret") == "abc123"
    with pytest.raises(LinkedInError, match="state"):
        code_from_callback(params_from_url(url), "other")
    with pytest.raises(LinkedInError, match="abgelehnt"):
        code_from_callback(params_from_url("http://localhost:8765/callback?error=user_cancelled_authorize&state=s3cret"), "s3cret")
