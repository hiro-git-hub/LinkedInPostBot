import httpx
import pytest

from linkedin_bot.db import InMemoryRepository
from linkedin_bot.integrations.linkedin import LinkedInError
from linkedin_bot.nodes.hashtags import clean_tags, split_hashtags
from linkedin_bot.runtime import pending_steps, resume, start_run
from linkedin_bot.state import Decision

from conftest import FEED_URL, Models, VariantWriter, draft, make_graph, valid_auth

TOPIC = {"Neues Modell": 9, "Ask HN": 3}


def approve(variant="normal"):
    return Decision(action="approve", variant=variant)


# --- Bis zur Freigabe ------------------------------------------------------------------------------

def test_run_writes_both_variants_with_hashtags(cfg, sources):
    calls = []
    step = start_run(make_graph(cfg, Models(TOPIC), calls=calls))

    state = step.state
    assert len(state["items"]) == 3  # alter RSS-Eintrag gefiltert
    assert len(state["unique"]) == 2  # RSS + HN zeigen auf denselben Artikel
    assert state["selected"].item.points == 420
    assert "42 Dinge" in calls[0]  # Artikeltext geht an den Recherche-Agenten

    pending = step.pending
    assert pending["drafts"]["normal"].text == draft(1).strip()
    assert pending["drafts"]["humor"].text == draft(1, "humor").strip()
    assert pending["hashtags"] == ["KI", "DevTools", "Shopware"]  # bereinigt, dedupliziert, max_hashtags
    assert pending["images"] == {}


def test_humor_writer_gets_humor_instructions(cfg, sources):
    models = Models(TOPIC)
    start_run(make_graph(cfg, models))
    assert len(models.writer.prompts["normal"]) == 1
    assert len(models.writer.prompts["humor"]) == 1


def test_critic_loop_runs_per_variant(cfg, sources):
    models = Models(TOPIC, reject_if=lambda text: text.startswith("Entwurf 1"))
    step = start_run(make_graph(cfg, models))
    drafts = step.pending["drafts"]
    assert (drafts["normal"].revisions, drafts["normal"].text) == (2, draft(2).strip())
    assert (drafts["humor"].revisions, drafts["humor"].text) == (1, draft(1, "humor").strip())


def test_critic_loop_stops_after_max_iterations(cfg, sources):
    step = start_run(make_graph(cfg, Models(TOPIC, reject_if=lambda text: True)))
    for variant in ("normal", "humor"):
        result = step.pending["drafts"][variant]
        assert result.revisions == cfg.budget.max_writer_iterations
        assert not result.critique.approved  # Autor sieht die offenen Punkte


def test_no_draft_below_threshold(cfg, sources):
    step = start_run(make_graph(cfg, Models({})))
    assert step.pending is None
    assert step.state["selected"] is None


def test_broken_source_does_not_abort_run(cfg, sources):
    sources.get(FEED_URL).mock(return_value=httpx.Response(500))
    assert start_run(make_graph(cfg, Models({"Neues Modell": 8}))).pending


def test_second_run_skips_items_seen_before(cfg, sources):
    repo = InMemoryRepository()
    start_run(make_graph(cfg, Models(TOPIC), repo))
    step = start_run(make_graph(cfg, Models(TOPIC), repo))
    assert step.state["unique"] == []
    assert step.pending is None


# --- Freigabe (Human-in-the-Loop) ------------------------------------------------------------------

def test_approve_humor_archives_humor_text_with_hashtags(cfg, sources):
    repo = InMemoryRepository()
    graph = make_graph(cfg, Models(TOPIC), repo)
    step = resume(graph, start_run(graph).thread_id, approve("humor"))

    assert step.state["status"] == "approved"
    post = repo.posts[0]
    assert post["variant"] == "humor"
    assert post["text"] == draft(1, "humor").strip() + "\n\n#KI #DevTools #Shopware"


def test_placeholder_blocks_approval_until_edited(cfg, sources):
    with_placeholder = draft(1).replace("Wie testet", "[EIGENE ERFAHRUNG: Projekt] Wie testet")
    writer = VariantWriter({"normal": [with_placeholder], "humor": [draft(1, "humor")]})
    graph = make_graph(cfg, Models(TOPIC, writer=writer))
    step = start_run(graph)

    step = resume(graph, step.thread_id, approve("normal"))
    assert "Platzhalter" in step.pending["notice"]

    edited = "Mein fertiger Text\n\n#Shopware #KI"
    step = resume(graph, step.thread_id, Decision(action="edit", variant="normal", text=edited))
    assert step.pending["drafts"]["normal"].text == "Mein fertiger Text"
    assert step.pending["drafts"]["normal"].critique is None  # vom Autor bearbeitet
    assert step.pending["hashtags"] == ["Shopware", "KI"]  # Hashtags aus dem bearbeiteten Text übernommen
    assert step.pending["drafts"]["humor"].text == draft(1, "humor").strip()  # andere Variante unberührt

    step = resume(graph, step.thread_id, approve("normal"))
    assert step.state["status"] == "approved"


def test_revise_only_rewrites_chosen_variant(cfg, sources):
    models = Models(TOPIC)
    graph = make_graph(cfg, models)
    step = start_run(graph)

    step = resume(graph, step.thread_id, Decision(action="revise", variant="humor", text="Mehr Selbstironie"))
    assert "Mehr Selbstironie" in models.writer.prompts["humor"][-1]
    assert step.pending["drafts"]["humor"].text == draft(2, "humor").strip()
    assert step.pending["drafts"]["normal"].text == draft(1).strip()
    assert len(models.writer.prompts["normal"]) == 1
    assert len(models.hashtags.calls) == 1  # Thema unverändert -> keine neuen Hashtags


def test_new_topic_picks_another_candidate(cfg, sources):
    models = Models({"Neues Modell": 9, "Ask HN": 7}, hashtags=("KI",))
    graph = make_graph(cfg, models)
    step = start_run(graph)
    assert step.pending["title"] == "Neues Modell veröffentlicht"

    step = resume(graph, step.thread_id, Decision(action="new_topic"))
    assert step.pending["title"] == "Ask HN: Wie deployt ihr?"
    assert step.pending["drafts"]["normal"].text == draft(2).strip()
    assert len(models.hashtags.calls) == 2  # neues Thema -> neue Hashtags

    step = resume(graph, step.thread_id, Decision(action="new_topic"))
    assert step.pending is None  # keine Kandidaten mehr


def test_images_per_variant_and_removal(cfg, sources):
    from linkedin_bot.nodes.image import HUMOR_HINT, image_key

    repo = InMemoryRepository()
    models = Models(TOPIC)
    graph = make_graph(cfg, models, repo)
    step = start_run(graph)

    step = resume(graph, step.thread_id, Decision(action="image", variant="humor"))
    assert step.pending["images"] == {"humor": "Leuchtturm aus Platinen"}
    assert repo.get_image(image_key(step.thread_id, "humor")) == b"PNG:a lighthouse made of circuits"
    prompt_messages = models.roles["image_prompt"].calls[-1]
    assert HUMOR_HINT.strip() in prompt_messages[0].content  # Bildidee darf die Ironie aufgreifen
    assert draft(1, "humor").strip() in prompt_messages[-1].content  # aus dem Humor-Text, nicht dem normalen

    step = resume(graph, step.thread_id, Decision(action="image", variant="normal"))
    assert set(step.pending["images"]) == {"normal", "humor"}

    step = resume(graph, step.thread_id, Decision(action="no_image", variant="humor"))
    assert set(step.pending["images"]) == {"normal"}


def test_pending_steps_lists_only_waiting_threads(cfg, sources):
    graph = make_graph(cfg, Models(TOPIC))
    waiting = start_run(graph)
    done = start_run(graph)  # gleiche Items sind jetzt "gesehen" -> kein Thema, läuft durch
    assert done.pending is None

    steps = pending_steps(graph)
    assert [s.thread_id for s in steps] == [waiting.thread_id]
    assert set(steps[0].pending["drafts"]) == {"normal", "humor"}


# --- Veröffentlichung ------------------------------------------------------------------------------

class FakeLinkedIn:
    def __init__(self, error: Exception | None = None, comment_error: Exception | None = None):
        self.error, self.comment_error = error, comment_error
        self.posts: list[dict] = []
        self.comments: list[tuple[str, str]] = []

    def comment(self, auth, post_urn, text):
        if self.comment_error:
            raise self.comment_error
        self.comments.append((post_urn, text))

    def create_post(self, auth, text, hashtags=None, image=None, alt_text="", visibility="PUBLIC"):
        if self.error:
            raise self.error
        self.posts.append({"text": text, "hashtags": hashtags, "image": image, "alt_text": alt_text})
        return "urn:li:share:123"


def run_and_approve(cfg, repo, linkedin, variant="normal", with_image=False):
    graph = make_graph(cfg, Models(TOPIC), repo, linkedin=linkedin)
    step = start_run(graph)
    if with_image:
        step = resume(graph, step.thread_id, Decision(action="image", variant=variant))
    return resume(graph, step.thread_id, approve(variant))


def test_dry_run_archives_without_publishing(cfg, sources):
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    repo.save_linkedin_auth(valid_auth())
    assert run_and_approve(cfg, repo, linkedin).state["status"] == "approved"
    assert linkedin.posts == []


def test_publish_sends_variant_hashtags_and_image(cfg, sources):
    cfg.linkedin.dry_run = False
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    repo.save_linkedin_auth(valid_auth())
    step = run_and_approve(cfg, repo, linkedin, variant="humor", with_image=True)

    assert step.state["status"] == "published"
    assert linkedin.posts == [{
        "text": draft(1, "humor").strip(), "hashtags": ["KI", "DevTools", "Shopware"],
        "image": b"PNG:a lighthouse made of circuits", "alt_text": "Leuchtturm aus Platinen",
    }]
    post = repo.posts[0]
    assert (post["post_urn"], post["variant"], post["has_image"]) == ("urn:li:share:123", "humor", True)
    assert repo.recent_topics(7) == ["Neues Modell veröffentlicht"]
    assert linkedin.comments == [("urn:li:share:123", "Quelle: https://example.com/model")]
    assert step.state["source_commented"] is True


def test_failed_source_comment_keeps_post_published(cfg, sources):
    from linkedin_bot.runtime import outcome_message

    cfg.linkedin.dry_run = False
    repo = InMemoryRepository()
    repo.save_linkedin_auth(valid_auth())
    linkedin = FakeLinkedIn(comment_error=LinkedInError("Kommentar erstellen fehlgeschlagen (403)"))
    step = run_and_approve(cfg, repo, linkedin)

    assert step.state["status"] == "published"
    message = outcome_message(step.state)
    assert "403" in message and "https://www.example.com/model/" in message


@pytest.mark.parametrize("auth", [None, valid_auth(days=-1)], ids=["missing", "expired"])
def test_publish_without_valid_login_fails_gracefully(cfg, sources, auth):
    cfg.linkedin.dry_run = False
    repo, linkedin = InMemoryRepository(), FakeLinkedIn()
    if auth:
        repo.save_linkedin_auth(auth)
    step = run_and_approve(cfg, repo, linkedin)

    assert step.state["status"] == "publish_failed"
    assert "/login" in step.state["publish_error"]
    assert linkedin.posts == []
    assert repo.posts[0]["text"].startswith(draft(1).strip())  # Text geht nicht verloren


def test_publish_api_error_is_archived(cfg, sources):
    cfg.linkedin.dry_run = False
    repo = InMemoryRepository()
    repo.save_linkedin_auth(valid_auth())
    step = run_and_approve(cfg, repo, FakeLinkedIn(error=LinkedInError("Post erstellen fehlgeschlagen (500)")))
    assert step.state["status"] == "publish_failed"
    assert repo.posts[0]["error"] == "Post erstellen fehlgeschlagen (500)"


# --- Hashtag-Helfer --------------------------------------------------------------------------------

def test_clean_tags():
    tags = ["#KI", "Dev Tools", "ki", "E-Commerce", "Künstliche_Intelligenz", "a"]
    assert clean_tags(tags) == ["KI", "DevTools", "ECommerce", "KünstlicheIntelligenz", "a"]
    assert clean_tags(tags, limit=3) == ["KI", "DevTools", "ECommerce"]


def test_split_hashtags():
    assert split_hashtags("Text\n\n#KI #Shopware\n") == ("Text", ["KI", "Shopware"])
    assert split_hashtags("Text mit #inline Tag") == ("Text mit #inline Tag", [])
