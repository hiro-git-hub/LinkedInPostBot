import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from linkedin_bot.integrations.linkedin import COMMENTS_URL, IMAGES_URL, POSTS_URL, TOKEN_URL, USERINFO_URL, LinkedInAuth, LinkedInClient, LinkedInError, escape_little
from linkedin_bot.nodes.dedup import dedupe, normalize_url
from linkedin_bot.nodes.rules import rule_issues
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
    too_long = "x" * (cfg.writing.max_chars + 1)
    assert "Zu lang" in rule_issues(too_long, cfg)[0]
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
