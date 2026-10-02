"""Verdrahtet Graph, Checkpointer und Datenbank und kapselt Start/Fortsetzen eines Laufs."""

import logging
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
from linkedin_bot import state
from linkedin_bot.budget import CostTracker
from linkedin_bot.integrations.linkedin import post_url
from linkedin_bot.nodes.approval import VARIANT_LABELS, post_text
from linkedin_bot.nodes.hashtags import hashtag_line
from linkedin_bot.state import Decision, Variant

log = logging.getLogger(__name__)

# Pydantic-Modelle im State, die der Checkpointer wiederherstellen darf.
CHECKPOINT_TYPES = [
    (state.__name__, cls.__name__)
    for cls in (state.NewsItem, state.ScoredItem, state.Research, state.Fact, state.Critique,
                state.DraftVariant, state.ImagePrompt, state.CarouselSpec, state.Slide)
]


@dataclass
class Step:
    """Ergebnis eines Graph-Abschnitts: wartet auf Freigabe (`pending`) oder ist fertig."""

    thread_id: str
    pending: dict | None  # Interrupt-Payload aus approval_node
    state: dict
    cost_usd: float = 0.0  # LLM-Kosten dieses Abschnitts (Lauf oder einzelne Aktion)


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


def _run(graph: CompiledStateGraph, thread_id: str, payload, tracker: CostTracker | None) -> Step:
    config = {"configurable": {"thread_id": thread_id}}
    if tracker:
        config["callbacks"] = [tracker]  # gilt für alle Modelle im Graph, auch die parallelen Varianten
    result = graph.invoke(payload, config)
    interrupts = result.get("__interrupt__")
    return Step(thread_id=thread_id, pending=interrupts[0].value if interrupts else None, state=result,
                cost_usd=tracker.usd if tracker else 0.0)


def start_run(graph: CompiledStateGraph, tracker: CostTracker | None = None) -> Step:
    return _run(graph, new_thread_id(), {}, tracker)


def resume(graph: CompiledStateGraph, thread_id: str, decision: Decision, tracker: CostTracker | None = None) -> Step:
    return _run(graph, thread_id, Command(resume=decision), tracker)


def is_compatible(values: dict) -> bool:
    """Passt ein gespeicherter Lauf noch zum aktuellen Schema? Ältere Checkpoints werden beim Laden ohne
    Validierung rekonstruiert (z.B. umbenannte Kategorien) – damit weiterzuarbeiten endet in Folgefehlern."""
    selected = values.get("selected")
    if selected is None:
        return True
    try:
        state.ScoredItem.model_validate(selected.model_dump(warnings=False) if hasattr(selected, "model_dump") else selected)
    except Exception:
        return False
    return True


def is_awaiting_approval(graph: CompiledStateGraph, thread_id: str) -> bool:
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    return "approval" in snapshot.next and is_compatible(snapshot.values)


def pending_steps(graph: CompiledStateGraph) -> list[Step]:
    """Alle Läufe, die gerade auf eine Freigabe warten – z.B. um sie nach einem Neustart erneut zuzustellen."""
    thread_ids = {c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)}
    steps = []
    for thread_id in sorted(thread_ids):
        try:
            snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        except Exception:  # z.B. Checkpoint aus einer älteren Version mit anderem Schema
            log.warning("Lauf %s übersprungen – Zustand nicht lesbar", thread_id, exc_info=True)
            continue
        # Entwürfe aus älteren Versionen (anderes Payload-Format) nicht mehr zustellen.
        if ("approval" in snapshot.next and snapshot.interrupts and "images" in snapshot.interrupts[0].value
                and is_compatible(snapshot.values)):
            steps.append(Step(thread_id=thread_id, pending=snapshot.interrupts[0].value, state=snapshot.values))
    return steps


def current_draft(graph: CompiledStateGraph, thread_id: str, variant: Variant) -> str:
    drafts = graph.get_state({"configurable": {"thread_id": thread_id}}).values.get("drafts") or {}
    return drafts[variant].text if variant in drafts else ""


def studio_graph():
    """Einstiegspunkt für `langgraph dev` – die Dev-Runtime bringt ihren eigenen Checkpointer mit."""
    return build_graph(load_config(), InMemoryRepository(), checkpointer=None)


VARIANT_ICONS = {"normal": "🅰️", "humor": "🅱️"}


def render_header(pending: dict) -> str:
    """Kopf der Freigabe-Anfrage (Thema, Hashtags, Bild, Hinweis) – gemeinsam für Konsole und Telegram."""
    lines = [f"📝 Neuer Entwurf: {pending['title']}", pending["url"], ""]
    if pending.get("notice"):
        lines += [f"⚠️ {pending['notice']}", ""]
    lines.append(f"🏷️ {hashtag_line(pending['hashtags']) or '(keine Hashtags)'}")
    trend = pending.get("trend") or {}
    if trend.get("status") == "ok" and trend.get("growth") is not None:
        line = f"📈 Google-Suchinteresse {trend['growth']:+.0%} (7 Tage, DE)"
        if trend.get("queries"):
            line += " – steigend: " + ", ".join(trend["queries"][:3])
        lines.append(line)
    elif trend.get("status") not in (None, "ok"):
        lines.append("📈 Google Trends nicht verfügbar – Auswahl ohne Trend-Bonus")
    lines.append("\nUnten stehen beide Versionen – gib die gewünschte frei.")
    return "\n".join(lines)


def render_variant(pending: dict, variant: Variant) -> str:
    draft = pending["drafts"][variant]
    lines = [f"{VARIANT_ICONS[variant]} {VARIANT_LABELS[variant]}-Version", "", draft.text, "",
             f"— {len(draft.text)} Zeichen"]
    if variant in pending["images"]:
        lines.append(f"🖼️ Mit Bild: {pending['images'][variant]}")
    if variant in pending.get("carousels", {}):
        lines.append(f"📑 Mit Karussell: {pending['carousels'][variant]}")
    critique = draft.critique
    if critique is None:
        lines.append("Von dir bearbeitet ✏️")
    elif critique.approved:
        lines.append("Critic: freigegeben ✓")
    else:
        lines.append("Critic: offene Punkte")
        lines += [f"• {i}" for i in critique.issues[:3]]
        lines += [f"• nicht belegt: {c}" for c in critique.unsupported_claims[:3]]
    return "\n".join(lines)


def render_pending(pending: dict) -> str:
    return "\n\n".join([render_header(pending)] + [render_variant(pending, v) for v in pending["drafts"]])


def outcome_message(state: dict) -> str:
    """Abschlussmeldung eines Laufs – gemeinsam für Konsole und Telegram."""
    status = state.get("status")
    if status == "published":
        message = f"✅ Veröffentlicht: {post_url(state['post_urn'])}"
        if state.get("source_commented"):
            return message + "\n💬 Quelle als erster Kommentar gepostet."
        reason = f" ({state['comment_error']})" if state.get("comment_error") else ""
        return (message + f"\n\n⚠️ Quell-Kommentar nicht automatisch möglich{reason}.\n"
                f"Bitte selbst als ersten Kommentar posten:\n{state['selected'].item.url}")
    if status == "approved":
        return "✅ Freigegeben und archiviert (Dry-Run – nicht auf LinkedIn veröffentlicht)."
    if status == "publish_failed":
        return (f"⚠️ Veröffentlichung fehlgeschlagen: {state['publish_error']}\n\n"
                "Der Text ist archiviert – hier zum manuellen Posten:\n\n" + post_text(state, state["variant"]))
    if status == "rejected":
        return "🗑️ Verworfen."
    return "🤷 Kein Thema über der Relevanz-Schwelle – kein Entwurf."
