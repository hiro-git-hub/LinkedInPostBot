from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.nodes.rules import rule_issues
from linkedin_bot.nodes.writer import format_research
from linkedin_bot.state import Critique, State

def make_critic(cfg: AppConfig, model: BaseChatModel):
    system = (cfg.prompts_dir / "critic.md").read_text().format(
        audience=cfg.audience,
        style_guide=(cfg.prompts_dir / "style_guide.md").read_text(),
    )
    structured = model.with_structured_output(Critique)

    def critic_node(state: State) -> dict:
        draft = state["draft"]
        review: Critique = structured.invoke([
            SystemMessage(system),
            HumanMessage(f"Recherche:\n{format_research(state['research'])}\n\nEntwurf:\n{draft}"),
        ])
        hard_issues = rule_issues(draft, cfg)
        return {"critique": Critique(
            approved=review.approved and not hard_issues and not review.unsupported_claims,
            issues=hard_issues + review.issues,
            unsupported_claims=review.unsupported_claims,
        )}

    return critic_node
