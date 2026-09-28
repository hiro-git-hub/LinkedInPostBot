from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.nodes.rules import rule_issues
from linkedin_bot.state import Research, State


def load_examples(cfg: AppConfig) -> str:
    files = sorted(p for p in (cfg.prompts_dir / "examples").glob("*.md") if p.name != "README.md")
    if not files:
        return ""
    posts = "\n\n---\n\n".join(p.read_text().strip() for p in files)
    return f"Beispiele für echte Posts des Autors (übernimm Tonalität und Rhythmus, nicht die Inhalte):\n\n{posts}"


def build_system_prompt(cfg: AppConfig) -> str:
    return (cfg.prompts_dir / "writer.md").read_text().format(
        language=cfg.language,
        audience=cfg.audience,
        style_guide=(cfg.prompts_dir / "style_guide.md").read_text(),
        examples=load_examples(cfg),
        min_chars=cfg.writing.min_chars,
        max_chars=cfg.writing.max_chars,
        # ~7 Zeichen pro deutschem Wort inkl. Leerzeichen; Ziel bewusst unter dem Maximum.
        target_words=round((cfg.writing.min_chars + cfg.writing.max_chars) / 2 / 7),
    )


def format_research(research: Research) -> str:
    facts = "\n".join(f"- {f.statement} ({f.source_url})" for f in research.facts)
    return f"Kern: {research.summary}\n\nFakten:\n{facts}\n\nEinordnung: {research.context}"


def bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) or "- keine"


def make_writer(cfg: AppConfig, model: BaseChatModel):
    system = build_system_prompt(cfg)

    def writer_node(state: State) -> dict:
        item = state["selected"].item
        material = (
            f"Thema: {item.title}\n"
            f"Quelle: {item.source} – {item.url}\n"
            f"Aufhänger: {state.get('angle', '-')}\n\n"
            f"{format_research(state['research'])}"
        )
        critique = state.get("critique")
        if state.get("human_feedback"):
            material += (
                f"\n\nDein bisheriger Entwurf:\n{state['draft']}\n\n"
                f"Überarbeite ihn nach dem Feedback des Autors (hat Vorrang vor allem anderen):\n{state['human_feedback']}"
            )
        elif critique and not critique.approved:
            hard = rule_issues(state["draft"], cfg)
            soft = [i for i in critique.issues if i not in hard]
            material += (
                f"\n\nDein bisheriger Entwurf:\n{state['draft']}\n\n"
                f"Überarbeite ihn.\n\nPFLICHT – harte Regeln verletzt (haben Vorrang vor allem anderen, "
                f"notfalls Details streichen):\n{bullets(hard)}\n\n"
                f"Nicht belegte Aussagen – streichen:\n{bullets(critique.unsupported_claims)}\n\n"
                f"Weitere Hinweise – nur umsetzen, wenn der Post dadurch nicht länger wird:\n{bullets(soft)}"
            )
        response = model.invoke([SystemMessage(system), HumanMessage(material)])
        return {"draft": response.text.strip(), "revisions": state.get("revisions", 0) + 1, "human_feedback": None}

    return writer_node
