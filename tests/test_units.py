import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from linkedin_bot.integrations.linkedin import COMMENTS_URL, DOCUMENTS_URL, IMAGES_URL, POSTS_URL, TOKEN_URL, USERINFO_URL, LinkedInAuth, LinkedInClient, LinkedInError, escape_little
from linkedin_bot.nodes.dedup import dedupe, normalize_url
from linkedin_bot.nodes.rules import rule_issues
from linkedin_bot.db import InMemoryRepository
from linkedin_bot.state import NewsItem

from conftest import draft, valid_auth

def test_normalize_url_strips_tracking_and_www():
    assert normalize_url("http://www.Example.com/model/?utm_source=x&id=3") == "https://example.com/model?id=3"


def test_dedupe_keeps_item_with_more_points():
    a = NewsItem(source="rss", title="A", url="https://example.com/a?utm_medium=x")
    b = NewsItem(source="hn", title="A", url="https://www.example.com/a/", points=99)
    assert dedupe([a, b]) == [b]


def test_rule_issues(cfg):
    assert rule_issues(draft(1), cfg) == []
    too_long = "x" * (cfg.writing.length_chars.max + 1)
    assert any(i.startswith("Zu lang") for i in rule_issues(too_long, cfg))
    checklist = draft(1) + "\n- a\n- b\n• c\n1. d"
    assert any("Aufzählungszeilen" in i for i in rule_issues(checklist, cfg))
    assert any("Hashtags" in i for i in rule_issues(draft(1) + "\n#KI", cfg))


# --- Pipeline bis zur Freigabe ---------------------------------------------------------------------


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
    urn = LinkedInClient("202609").create_post(valid_auth(), "Hallo (Welt)", hashtags=["KI", "ECommerce"])

    assert urn == "urn:li:share:9"
    request = route.calls.last.request
    assert request.headers["LinkedIn-Version"] == "202609"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert request.headers["Authorization"] == "Bearer token"
    body = json.loads(request.content)
    assert body["author"] == "urn:li:person:abc"
    assert body["commentary"] == "Hallo \\(Welt\\)\n\n{hashtag|\\#|KI} {hashtag|\\#|ECommerce}"
    assert body["lifecycleState"] == "PUBLISHED"
    assert "content" not in body


@respx.mock
def test_linkedin_client_uploads_image_before_post(linkedin_env):
    upload_url = "https://www.linkedin.com/dms-uploads/abc/uploaded-image/0"
    init = respx.post(IMAGES_URL).mock(return_value=httpx.Response(200, json={
        "value": {"uploadUrl": upload_url, "image": "urn:li:image:C4E"}}))
    put = respx.put(upload_url).mock(return_value=httpx.Response(201))
    post = respx.post(POSTS_URL).mock(return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"}))

    LinkedInClient("202609").create_post(valid_auth(), "Text", image=b"PNGDATA", alt_text="Alt")

    assert json.loads(init.calls.last.request.content) == {"initializeUploadRequest": {"owner": "urn:li:person:abc"}}
    assert put.calls.last.request.content == b"PNGDATA"
    assert json.loads(post.calls.last.request.content)["content"] == {"media": {"id": "urn:li:image:C4E", "altText": "Alt"}}


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


def test_code_from_callback_validates_state():
    from linkedin_bot.integrations.linkedin import code_from_callback, params_from_url

    url = "http://localhost:8765/callback?code=abc123&state=s3cret"
    assert code_from_callback(params_from_url(url), "s3cret") == "abc123"
    with pytest.raises(LinkedInError, match="state"):
        code_from_callback(params_from_url(url), "other")
    with pytest.raises(LinkedInError, match="abgelehnt"):
        code_from_callback(params_from_url("http://localhost:8765/callback?error=user_cancelled_authorize&state=s3cret"), "s3cret")


def test_healthcheck_heartbeat_age(tmp_path):
    import time

    from linkedin_bot.healthcheck import is_healthy, write_heartbeat

    beat = tmp_path / "heartbeat"
    assert is_healthy(beat) == (False, "kein Heartbeat")
    write_heartbeat(beat)
    assert is_healthy(beat)[0]
    beat.write_text(str(time.time() - 600))
    healthy, reason = is_healthy(beat)
    assert not healthy and "600s" in reason


def _openai_error(cls, status, code):
    import openai

    response = openai._base_client.httpx2.Response(status, request=openai._base_client.httpx2.Request("POST", "https://api.openai.com"))
    return cls("boom", response=response, body={"code": code})


@pytest.mark.parametrize(("cls_name", "status", "code", "expected"), [
    ("AuthenticationError", 401, "invalid_api_key", "API-Key ungültig"),
    ("RateLimitError", 429, "credit_balance_exhausted", "Guthaben aufgebraucht"),
    ("RateLimitError", 429, "rate_limit_exceeded", "Rate-Limit"),
    ("InternalServerError", 500, None, "OpenAI-Fehler (500)"),
])
def test_describe_error_openai(cls_name, status, code, expected):
    import openai

    from linkedin_bot.errors import describe_error

    cause = _openai_error(getattr(openai, cls_name), status, code)
    try:
        try:
            raise cause
        except Exception as inner:
            raise RuntimeError("wrapped by langchain") from inner
    except RuntimeError as outer:
        assert expected in describe_error(outer)


def test_describe_error_fallback_and_linkedin():
    from linkedin_bot.errors import describe_error

    assert "LinkedIn: Post erstellen" in describe_error(LinkedInError("Post erstellen fehlgeschlagen (403)"))
    assert describe_error(ValueError("x")) == "Unerwarteter Fehler (ValueError) – Details im Log."


def test_schedule_config_runs():
    from linkedin_bot.config import load_config

    runs = [(r.day, r.time) for r in load_config().schedule.runs]
    assert runs == [("wed", "09:00"), ("sun", "18:00")]


@respx.mock
def test_linkedin_client_comment_request(linkedin_env):
    route = respx.post(COMMENTS_URL.format(urn="urn%3Ali%3Ashare%3A9")).mock(return_value=httpx.Response(201))
    LinkedInClient("202609").comment(valid_auth(), "urn:li:share:9", "Quelle: https://example.com")
    assert json.loads(route.calls.last.request.content) == {
        "actor": "urn:li:person:abc", "object": "urn:li:share:9", "message": {"text": "Quelle: https://example.com"},
    }


def test_rule_issues_flags_banned_phrases(cfg):
    cfg.writing.banned_phrases = ["Aber kurz von vorn"]
    issues = rule_issues(draft(1) + "\naber kurz von vorn: …", cfg)
    assert any("Aber kurz von vorn" in i for i in issues)


def test_parse_feed_keeps_newest_per_feed():
    from linkedin_bot.collectors.rss import parse_feed

    now = datetime.now(UTC)
    entries = "".join(
        f"<item><title>T{i}</title><link>https://x.de/{i}</link>"
        f"<pubDate>{(now - timedelta(hours=i)):%a, %d %b %Y %H:%M:%S +0000}</pubDate></item>"
        for i in range(5)
    )
    rss = f'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>{entries}</channel></rss>'
    assert [i.title for i in parse_feed(rss.encode(), "https://x.de/feed", 48, max_items=2)] == ["T0", "T1"]


def test_author_profile_reaches_writer_critic_and_selector(cfg):
    from linkedin_bot.nodes.writer import build_system_prompt

    assert "Shopware" in cfg.author
    assert cfg.author in build_system_prompt(cfg, "normal")
    for name in ("critic.md", "selector.md"):
        assert "{author}" in (cfg.prompts_dir / name).read_text()


def test_hook_rules(cfg):
    from linkedin_bot.nodes.rules import hook_of

    body = "\n\n" + "Ein Satz mit Substanz. " * 45
    assert hook_of("Zeile 1\nZeile 2\n\nText") == "Zeile 1\nZeile 2"
    assert not [i for i in rule_issues("Kurze, harte Hook." + body, cfg) if "Hook" in i]
    assert any("Hook zu lang (3 Zeilen" in i for i in rule_issues("Eins\nZwei\nDrei" + body, cfg))
    assert any("Hook zu lang (1 Zeilen, 200" in i for i in rule_issues("x" * 200 + body, cfg))
    # Ohne Leerzeile ist der ganze Text "Hook" -> fällt auf
    assert any("Hook zu lang" in i for i in rule_issues("Ein Satz mit Substanz. " * 45, cfg))


# --- Formregeln aus der Config ----------------------------------------------------------------------

def test_form_rules_emojis_links_question_and_patterns(cfg):
    from linkedin_bot.nodes.rules import form_rules, phrase_pattern

    ok = draft(1)
    assert rule_issues(ok, cfg) == []
    assert any("Emojis" in i for i in rule_issues(ok.replace("Ein Satz", "🚀 Ein Satz", 1), cfg))
    assert any("Links" in i for i in rule_issues(ok.replace("Ein Satz", "Siehe https://x.de. Ein Satz", 1), cfg))
    assert any("Frage" in i for i in rule_issues(ok.rstrip("?") + ".", cfg))
    assert phrase_pattern("Das ist kein X – das ist Y").search("Das ist kein Bug, das ist ein Feature")
    rules = form_rules(cfg)
    assert "Keine Emojis." in rules and "140 Zeichen" in rules and "Gedankenstrich-Kaskaden" in rules


# --- Gewichtung und Quoten --------------------------------------------------------------------------

def test_blocked_categories_respect_max_share(cfg):
    from linkedin_bot.nodes.select import blocked_categories

    recent = ["politik", "politik", "ki_entwicklung", "wissenschaft"]  # 2 von 10 = 20 % Politik
    assert blocked_categories(cfg, recent) == {"politik"}
    assert blocked_categories(cfg, ["ki_entwicklung"] * 10) == set()


def test_selector_uses_raw_threshold_weights_and_quota(cfg):
    from linkedin_bot.nodes.select import make_selector
    from linkedin_bot.state import ScoredItem, TopicChoice
    from conftest import FakeModel

    def scored(title, relevance, category):
        weight = cfg.topic_weights[category].weight
        return ScoredItem(item=NewsItem(source="s", title=title, url=f"https://x.de/{title}"),
                          relevance=relevance, category=category, reason="r", weighted=relevance * weight)

    repo = InMemoryRepository()
    for _ in range(2):  # Politik-Quote ausgeschöpft
        repo.save_post("t", NewsItem(source="s", title="p", url="https://p.de"), "x", "published", category="politik")
    selector = FakeModel(responses=[""], structured={TopicChoice: TopicChoice(index=0, angle="a")}, calls=[])
    state = {"scored": [
        scored("wissen", 10, "wissenschaft"),     # 6.0 gewichtet – Rohwert 10 >= 7, kommt durch
        scored("shop", 8, "shopware_ecommerce"),  # 9.6 gewichtet -> Platz 1
        scored("ki", 9, "ki_entwicklung"),        # 9.0
        scored("politik", 10, "politik"),         # Quote ausgeschöpft
        scored("schwach", 6, "ki_entwicklung"),   # unter min_score
    ]}
    result = make_selector(cfg, selector, repo)(state)
    shortlist = selector.calls[-1][-1].content
    assert result["selected"].item.title == "shop"
    assert "wissen" in shortlist and "politik" not in shortlist and "schwach" not in shortlist
    assert shortlist.index("shop") < shortlist.index("ki") < shortlist.index("wissen")


# --- Kostenbremse und Fallback ----------------------------------------------------------------------

def _llm_result(model_name: str, input_tokens: int, output_tokens: int):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    message = AIMessage("x", response_metadata={"model_name": model_name},
                        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": 0})
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def test_cost_tracker_prices_by_model_prefix_and_stops(cfg):
    from linkedin_bot.budget import BudgetExceeded, CostTracker

    tracker = CostTracker(cfg)
    tracker.on_llm_end(_llm_result("gpt-5.5-2026-04-23", 10_000, 2_000))  # 0.05 + 0.06
    assert tracker.usd == pytest.approx(0.11)
    with pytest.raises(BudgetExceeded):
        tracker.on_llm_end(_llm_result("gpt-5.5", 0, 50_000))  # +1.50 -> über dem Limit


def test_fallback_on_api_error_but_not_on_budget(cfg):
    import openai
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from linkedin_bot.budget import BudgetExceeded
    from linkedin_bot.models import ModelWithFallback

    class Failing(FakeListChatModel):
        error: object = None

        def _call(self, *args, **kwargs):
            raise self.error

    api_error = openai.APIConnectionError(request=openai._base_client.httpx2.Request("POST", "https://api.openai.com"))
    model = ModelWithFallback(Failing(responses=[""], error=api_error), FakeListChatModel(responses=["vom Fallback"]))
    assert model.invoke("hi").content == "vom Fallback"

    model = ModelWithFallback(Failing(responses=[""], error=BudgetExceeded(2.0, 1.5)), FakeListChatModel(responses=["nein"]))
    with pytest.raises(BudgetExceeded):
        model.invoke("hi")


def test_get_model_adds_fallback_from_config(cfg, monkeypatch):
    from linkedin_bot.models import ModelWithFallback, get_model

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    writer = get_model(cfg, "writer")
    assert isinstance(writer, ModelWithFallback)
    assert writer.primary.model_name == "gpt-5.5" and writer.fallback.model_name == "gpt-5.4-mini"
    assert not isinstance(get_model(cfg, "scorer"), ModelWithFallback)  # Fallback == Primärmodell


# --- Hacker News ------------------------------------------------------------------------------------

def test_hn_gravity_prefers_fresh_stories_and_exact_keywords():
    from linkedin_bot.collectors.hackernews import gravity_score, title_matches

    now = datetime.now(UTC)
    old = NewsItem(source="hn", title="old", url="https://a", points=1000, published=now - timedelta(hours=100))
    fresh = NewsItem(source="hn", title="fresh", url="https://b", points=200, published=now - timedelta(hours=3))
    assert gravity_score(fresh, 1.8, now.timestamp()) > gravity_score(old, 1.8, now.timestamp())
    assert title_matches("Building RAG on Shopware", "rag")
    assert not title_matches("Distributed storage engines", "rag")
    assert title_matches("Claude Code ships agents", "claude code")


@respx.mock
def test_linkedin_client_uploads_document_for_carousel(linkedin_env):
    upload_url = "https://www.linkedin.com/dms-uploads/doc/0"
    respx.post(DOCUMENTS_URL).mock(return_value=httpx.Response(200, json={
        "value": {"uploadUrl": upload_url, "document": "urn:li:document:D1"}}))
    put = respx.put(upload_url).mock(return_value=httpx.Response(201))
    post = respx.post(POSTS_URL).mock(return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"}))

    LinkedInClient("202609").create_post(valid_auth(), "Text", image=b"PNG", document=b"%PDF", document_title="Upgrade")

    assert put.calls.last.request.content == b"%PDF"
    assert json.loads(post.calls.last.request.content)["content"] == {"media": {"id": "urn:li:document:D1", "title": "Upgrade"}}


def test_render_carousel_produces_one_page_per_slide(cfg):
    import re

    from linkedin_bot.integrations.carousel_pdf import render_carousel
    from linkedin_bot.state import CarouselSpec, Slide

    spec = CarouselSpec(title="Test", slides=[
        Slide(kind="title", headline="Hook " * 20, body="Sehr langer Text " * 40),  # muss schrumpfen, nicht crashen
        Slide(kind="code", headline="Code", code="public function x(): string\n{\n    return 'y';\n}", language="php"),
        Slide(kind="code", headline="Unbekannte Sprache", code="foo bar", language="klingonisch"),
        Slide(kind="closing", headline="Frage?"),
    ])
    pdf = render_carousel(spec, cfg.carousel, cfg.carousel.footer)
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) == 4
