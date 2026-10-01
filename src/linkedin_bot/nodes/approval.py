from langchain_core.runnables import RunnableConfig
from langgraph.types import Send, interrupt

from linkedin_bot.db import Repository
from linkedin_bot.nodes.hashtags import hashtag_line, split_hashtags
from linkedin_bot.state import ComposeTask, Decision, DraftVariant, State

PLACEHOLDER = "[EIGENE ERFAHRUNG"
VARIANT_LABELS = {"normal": "Normal", "humor": "Humor"}


def post_text(state: State, variant: str) -> str:
    """Fertiger Post wie er erscheint – Text plus Hashtag-Zeile (für Archiv und manuelles Posten)."""
    text = state["drafts"][variant].text
    tags = state.get("hashtags") or []
    return f"{text}\n\n{hashtag_line(tags)}" if tags else text


def approval_node(state: State) -> dict:
    """Pausiert den Graph, bis der Autor entscheidet. Der Resume-Wert ist eine `Decision`."""
    item = state["selected"].item
    images = state.get("image_prompts") or {}
    decision: Decision = interrupt({
        "title": item.title,
        "url": item.url,
        "drafts": state["drafts"],
        "hashtags": state.get("hashtags") or [],
        "images": {variant: prompt.alt_text for variant, prompt in images.items()},  # Variante -> Alt-Text
        "notice": state.get("notice"),
    })
    action, variant = decision["action"], decision.get("variant", "normal")

    if action == "edit":
        body, tags = split_hashtags(decision["text"])
        update = {
            "drafts": {variant: DraftVariant(text=body, critique=None, revisions=state["drafts"][variant].revisions)},
            "decision": "review",
            "notice": f"Deine Fassung ({VARIANT_LABELS[variant]}) – bitte final freigeben.",
        }
        if tags:
            update["hashtags"] = tags
        return update
    if action == "approve" and PLACEHOLDER in state["drafts"][variant].text:
        return {"decision": "review", "notice": f"In der {VARIANT_LABELS[variant]}-Version steht noch ein "
                                                "[EIGENE ERFAHRUNG]-Platzhalter – bitte über ✏️ Bearbeiten füllen oder entfernen."}
    if action == "no_image":
        return {"decision": "review", "image_prompts": {variant: None},
                "notice": f"Bild der {VARIANT_LABELS[variant]}-Version entfernt."}
    if action == "new_topic":
        return {
            "decision": "new_topic",
            "rejected_urls": [item.url],
            "drafts": None, "hashtags": [], "image_prompts": None, "notice": None,
        }
    return {"decision": action, "variant": variant, "human_feedback": decision.get("text"), "notice": None}


def compose_tasks(state: State, variants, previous: bool = False) -> list[Send]:
    return [
        Send("compose", ComposeTask(
            variant=v,
            selected=state["selected"],
            angle=state.get("angle", ""),
            research=state["research"],
            previous=state["drafts"][v] if previous else None,
            human_feedback=state.get("human_feedback") if previous else None,
        ))
        for v in variants
    ]


def route_after_approval(state: State):
    decision = state["decision"]
    if decision == "revise":
        return compose_tasks(state, [state["variant"]], previous=True)
    return {
        "review": "approval",
        "new_topic": "select",
        "approve": "publish",
        "reject": "archive",
        "image": "image",
    }[decision]


def make_archive(repo: Repository):
    def archive_node(state: State, config: RunnableConfig) -> dict:
        approved = state["decision"] == "approve"
        variant = state.get("variant", "normal")
        repo.save_post(
            config["configurable"]["thread_id"], state["selected"].item, post_text(state, variant),
            state["status"] if approved else "rejected",
            post_urn=state.get("post_urn"), error=state.get("publish_error"),
            variant=variant if approved else None,
            has_image=approved and variant in (state.get("image_prompts") or {}),
            category=state["selected"].category,
        )
        return {"status": state["status"] if approved else "rejected"}

    return archive_node
