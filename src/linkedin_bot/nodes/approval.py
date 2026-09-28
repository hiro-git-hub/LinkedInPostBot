from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from linkedin_bot.db import Repository
from linkedin_bot.state import Decision, State

PLACEHOLDER = "[EIGENE ERFAHRUNG"


def approval_node(state: State) -> dict:
    """Pausiert den Graph, bis der Autor entscheidet. Der Resume-Wert ist eine `Decision`."""
    item = state["selected"].item
    decision: Decision = interrupt({
        "title": item.title,
        "url": item.url,
        "draft": state["draft"],
        "critique": state["critique"],
        "notice": state.get("notice"),
    })
    action = decision["action"]

    if action == "edit":
        return {"draft": decision["text"], "decision": "review", "notice": "Deine Fassung – bitte final freigeben."}
    if action == "approve" and PLACEHOLDER in state["draft"]:
        return {"decision": "review", "notice": "Da steht noch ein [EIGENE ERFAHRUNG]-Platzhalter – bitte über ✏️ Bearbeiten füllen oder entfernen."}
    if action == "revise":
        return {"decision": "revise", "human_feedback": decision["text"], "notice": None}
    if action == "new_topic":
        return {
            "decision": "new_topic",
            "rejected_urls": [item.url],
            "draft": "", "critique": None, "revisions": 0, "notice": None,
        }
    return {"decision": action, "notice": None}


def route_after_approval(state: State) -> str:
    return {
        "review": "approval",
        "revise": "writer",
        "new_topic": "select",
        "approve": "publish",
        "reject": "archive",
    }[state["decision"]]


def make_archive(repo: Repository):
    def archive_node(state: State, config: RunnableConfig) -> dict:
        status = state["status"] if state["decision"] == "approve" else "rejected"
        repo.save_post(
            config["configurable"]["thread_id"], state["selected"].item, state["draft"], status,
            post_urn=state.get("post_urn"), error=state.get("publish_error"),
        )
        return {"status": status}

    return archive_node
