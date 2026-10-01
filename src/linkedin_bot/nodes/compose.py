from langchain_core.language_models import BaseChatModel

from linkedin_bot.config import AppConfig
from linkedin_bot.nodes.critic import make_critic
from linkedin_bot.nodes.writer import make_writer
from linkedin_bot.state import ComposeTask, DraftVariant


def make_compose(cfg: AppConfig, writer_model: BaseChatModel, critic_model: BaseChatModel):
    """Schreibt EINE Variante inkl. Critic-Schleife. Läuft per Send für Normal und Humor parallel."""
    write = make_writer(cfg, writer_model)
    review = make_critic(cfg, critic_model)

    def compose_node(task: ComposeTask) -> dict:
        previous = task.get("previous")
        if previous and task.get("human_feedback"):
            text = write(task, draft=previous.text, human_feedback=task["human_feedback"])
            revisions = previous.revisions + 1
        else:
            text, revisions = write(task), 1

        # revisions zählt alle Entwürfe; budget.max_writer_iterations begrenzt die Writer↔Critic-Runden.
        while True:
            critique = review(task, text)
            if critique.approved or revisions >= cfg.budget.max_writer_iterations:
                break
            text = write(task, draft=text, critique=critique)
            revisions += 1
        return {"drafts": {task["variant"]: DraftVariant(text=text, critique=critique, revisions=revisions)}}

    return compose_node
