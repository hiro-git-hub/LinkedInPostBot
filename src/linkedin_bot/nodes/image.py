from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.state import ImagePrompt, State

ImageGenerator = Callable[[str], bytes]


def make_imager(cfg: AppConfig, model: BaseChatModel, repo: Repository, generate: ImageGenerator):
    system = (cfg.prompts_dir / "image.md").read_text().format(style=cfg.images.style)
    structured = model.with_structured_output(ImagePrompt)

    def image_node(state: State, config: RunnableConfig) -> dict:
        item = state["selected"].item
        prompt: ImagePrompt = structured.invoke([
            SystemMessage(system),
            HumanMessage(f"Thema: {item.title}\n\nPost:\n{state['drafts']['normal'].text}"),
        ])
        repo.save_image(config["configurable"]["thread_id"], generate(prompt.prompt), prompt.prompt)
        return {"image_prompt": prompt, "notice": None}

    return image_node
