"""Verdrahtet Graph, Checkpointer und Datenbank und kapselt Start/Fortsetzen eines Laufs."""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from linkedin_bot.config import AppConfig, load_config
from linkedin_bot.db import InMemoryRepository, PostgresRepository, Repository
from linkedin_bot.graph import build_graph
from linkedin_bot.integrations.linkedin import post_url
from linkedin_bot import state
from linkedin_bot.state import Decision

# Pydantic-Modelle im State, die der Checkpointer wiederherstellen darf.
CHECKPOINT_TYPES = [
    (state.__name__, cls.__name__)
    for cls in (state.NewsItem, state.ScoredItem, state.Research, state.Fact, state.Critique)
]


@dataclass
class Step:
    """Ergebnis eines Graph-Abschnitts: wartet auf Freigabe (`pending`) oder ist fertig."""

    thread_id: str
    pending: dict | None  # Interrupt-Payload aus approval_node
    state: dict


@dataclass
class Runtime:
    graph: CompiledStateGraph
    repo: Repository


@contextmanager
def open_pool() -> Iterator[ConnectionPool]:
    with ConnectionPool(
        os.environ["DATABASE_URL"],
        # Vorgaben von PostgresSaver: autocommit + dict_row.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        yield pool


@contextmanager
def open_repo() -> Iterator[PostgresRepository]:
    with open_pool() as pool:
        repo = PostgresRepository(pool)
        repo.setup()
        yield repo


@contextmanager
def open_runtime(cfg: AppConfig, use_db: bool = True) -> Iterator[Runtime]:
    if not use_db:
        repo = InMemoryRepository()
        yield Runtime(build_graph(cfg, repo, InMemorySaver()), repo)
        return
    with open_pool() as pool:
        checkpointer = PostgresSaver(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
        checkpointer.setup()
        repo = PostgresRepository(pool)
        repo.setup()
        yield Runtime(build_graph(cfg, repo, checkpointer), repo)


def new_thread_id() -> str:
    return f"{date.today():%Y-%m-%d}-{uuid.uuid4().hex[:6]}"


def _run(graph: CompiledStateGraph, thread_id: str, payload) -> Step:
    result = graph.invoke(payload, {"configurable": {"thread_id": thread_id}})
    interrupts = result.get("__interrupt__")
    return Step(thread_id=thread_id, pending=interrupts[0].value if interrupts else None, state=result)


def start_run(graph: CompiledStateGraph) -> Step:
    return _run(graph, new_thread_id(), {})


def resume(graph: CompiledStateGraph, thread_id: str, decision: Decision) -> Step:
    return _run(graph, thread_id, Command(resume=decision))


def is_awaiting_approval(graph: CompiledStateGraph, thread_id: str) -> bool:
    return "approval" in graph.get_state({"configurable": {"thread_id": thread_id}}).next


def pending_steps(graph: CompiledStateGraph) -> list[Step]:
    """Alle Läufe, die gerade auf eine Freigabe warten – z.B. um sie nach einem Neustart erneut zuzustellen."""
    thread_ids = {c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)}
    steps = []
    for thread_id in sorted(thread_ids):
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        if "approval" in snapshot.next and snapshot.interrupts:
            steps.append(Step(thread_id=thread_id, pending=snapshot.interrupts[0].value, state=snapshot.values))
    return steps


def current_draft(graph: CompiledStateGraph, thread_id: str) -> str:
    return graph.get_state({"configurable": {"thread_id": thread_id}}).values.get("draft", "")


def studio_graph():
    """Einstiegspunkt für `langgraph dev` – die Dev-Runtime bringt ihren eigenen Checkpointer mit."""
    return build_graph(load_config(), InMemoryRepository(), checkpointer=None)


def render_pending(pending: dict) -> str:
    """Text für die Freigabe-Anfrage – gemeinsam für Konsole und Telegram."""
    critique = pending["critique"]
    lines = [f"📝 Entwurf: {pending['title']}", pending["url"], ""]
    if pending.get("notice"):
        lines += [f"⚠️ {pending['notice']}", ""]
    lines += [pending["draft"], "", f"— {len(pending['draft'])} Zeichen"]
    if critique.approved:
        lines.append("Critic: freigegeben ✓")
    else:
        lines.append("Critic: offene Punkte")
        lines += [f"• {i}" for i in critique.issues[:3]]
        lines += [f"• nicht belegt: {c}" for c in critique.unsupported_claims[:3]]
    return "\n".join(lines)


def outcome_message(state: dict) -> str:
    """Abschlussmeldung eines Laufs – gemeinsam für Konsole und Telegram."""
    status = state.get("status")
    if status == "published":
        return (f"✅ Veröffentlicht: {post_url(state['post_urn'])}\n\n"
                f"Quelle für den ersten Kommentar:\n{state['selected'].item.url}")
    if status == "approved":
        return "✅ Freigegeben und archiviert (Dry-Run – nicht auf LinkedIn veröffentlicht)."
    if status == "publish_failed":
        return (f"⚠️ Veröffentlichung fehlgeschlagen: {state['publish_error']}\n\n"
                "Der Text ist archiviert – hier zum manuellen Posten:\n\n" + state["draft"])
    if status == "rejected":
        return "🗑️ Verworfen."
    return "🤷 Kein Thema über der Relevanz-Schwelle – kein Entwurf."
