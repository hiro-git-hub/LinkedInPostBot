from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.nodes.rules import form_rules, rule_issues
from linkedin_bot.state import ComposeTask, Critique, Research, Variant


def load_examples(cfg: AppConfig) -> str:
    files = sorted(p for p in cfg.voice_examples_path.glob("*.md") if p.name != "README.md")
    if not files:
        return ""
    posts = "\n\n---\n\n".join(p.read_text().strip() for p in files)
    return f"Beispiele für echte Posts des Autors (übernimm Tonalität und Rhythmus, nicht die Inhalte):\n\n{posts}"


def build_system_prompt(cfg: AppConfig, variant: Variant = "normal") -> str:
    prompt = (cfg.prompts_dir / "writer.md").read_text().format(
        language=cfg.language,
        author=cfg.author,
        audience=cfg.audience,
        perspective=cfg.writing.perspective,
        style_guide=(cfg.prompts_dir / "style_guide.md").read_text(),
        form_rules=form_rules(cfg),
        examples=load_examples(cfg),
        # ~7 Zeichen pro deutschem Wort inkl. Leerzeichen; Ziel bewusst unter dem Maximum.
        target_words=round((cfg.writing.length_chars.min + cfg.writing.length_chars.max) / 2 / 7),
    )
    if variant == "humor":
        prompt += "\n\n" + (cfg.prompts_dir / "humor.md").read_text()
    return prompt


def format_research(research: Research) -> str:
    facts = "\n".join(f"- {f.statement} ({f.source_url})" for f in research.facts)
    return f"Kern: {research.summary}\n\nFakten:\n{facts}\n\nEinordnung: {research.context}"


def bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) or "- keine"


def make_writer(cfg: AppConfig, model: BaseChatModel):
    systems = {variant: build_system_prompt(cfg, variant) for variant in ("normal", "humor")}

    def write(task: ComposeTask, draft: str | None = None, critique: Critique | None = None,
              human_feedback: str | None = None) -> str:
        item = task["selected"].item
        material = (
            f"Thema: {item.title}\n"
            f"Quelle: {item.source} – {item.url}\n"
            f"Aufhänger: {task.get('angle', '-')}\n\n"
            f"{format_research(task['research'])}"
        )
        if human_feedback:
            material += (
                f"\n\nDein bisheriger Entwurf:\n{draft}\n\n"
                f"Überarbeite ihn nach dem Feedback des Autors (hat Vorrang vor allem anderen):\n{human_feedback}"
            )
        elif critique:
            hard = rule_issues(draft, cfg)
            soft = [i for i in critique.issues if i not in hard]
            material += (
                f"\n\nDein bisheriger Entwurf:\n{draft}\n\n"
                f"Überarbeite ihn.\n\nPFLICHT – harte Regeln verletzt (haben Vorrang vor allem anderen, "
                f"notfalls Details streichen):\n{bullets(hard)}\n\n"
                f"Nicht belegte Aussagen – streichen:\n{bullets(critique.unsupported_claims)}\n\n"
                f"Weitere Hinweise – nur umsetzen, wenn der Post dadurch nicht länger wird:\n{bullets(soft)}"
            )
        response = model.invoke([SystemMessage(systems[task["variant"]]), HumanMessage(material)])
        return response.text.strip()

    return write
