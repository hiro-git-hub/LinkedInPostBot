from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.state import ImagePrompt, State

ImageGenerator = Callable[[str], bytes]

HUMOR_HINT = (
    "\n\nDas Bild gehört zur sarkastischen Version des Posts: Das Motiv darf die Ironie aufgreifen – "
    "eine leicht absurde, aber glaubwürdige Szene oder ein visueller Widerspruch. Der Stil bleibt verbindlich."
)


def image_key(thread_id: str, variant: str) -> str:
    return f"{thread_id}:{variant}"


def make_imager(cfg: AppConfig, model: BaseChatModel, repo: Repository, generate: ImageGenerator):
    system = (cfg.prompts_dir / "image.md").read_text().format(style=cfg.images.style)
    structured = model.with_structured_output(ImagePrompt)

    def image_node(state: State, config: RunnableConfig) -> dict:
        variant = state["variant"]
        item = state["selected"].item
        prompt: ImagePrompt = structured.invoke([
            SystemMessage(system + (HUMOR_HINT if variant == "humor" else "")),
            HumanMessage(f"Thema: {item.title}\n\nPost:\n{state['drafts'][variant].text}"),
        ])
        key = image_key(config["configurable"]["thread_id"], variant)
        repo.save_image(key, generate(prompt.prompt), prompt.prompt)
        replaced = variant in (state.get("carousels") or {})
        return {
            "image_prompts": {variant: prompt},
            "carousels": {variant: None},  # ein Post hat nur ein Medium
            "notice": "Das Bild ersetzt das Karussell dieser Version." if replaced else None,
        }

    return image_node
