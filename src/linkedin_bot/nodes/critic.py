from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.nodes.rules import form_rules, rule_issues
from linkedin_bot.nodes.writer import format_research
from linkedin_bot.state import ComposeTask, Critique

HUMOR_NOTE = (
    "\n\nDieser Entwurf ist die humorvolle Variante: Prüfe zusätzlich, ob der Sarkasmus zündet, aus der Sache kommt "
    "und nicht albern wirkt oder einzelne Personen persönlich angreift. Fakten gelten genauso streng."
)


def make_critic(cfg: AppConfig, model: BaseChatModel):
    system = (cfg.prompts_dir / "critic.md").read_text().format(
        author=cfg.author,
        audience=cfg.audience,
        perspective=cfg.writing.perspective,
        style_guide=(cfg.prompts_dir / "style_guide.md").read_text(),
        form_rules=form_rules(cfg),
    )
    structured = model.with_structured_output(Critique)

    def review(task: ComposeTask, draft: str) -> Critique:
        note = HUMOR_NOTE if task["variant"] == "humor" else ""
        result: Critique = structured.invoke([
            SystemMessage(system + note),
            HumanMessage(f"Recherche:\n{format_research(task['research'])}\n\nEntwurf:\n{draft}"),
        ])
        hard_issues = rule_issues(draft, cfg)
        return Critique(
            approved=result.approved and not hard_issues and not result.unsupported_claims,
            issues=hard_issues + result.issues,
            unsupported_claims=result.unsupported_claims,
        )

    return review
