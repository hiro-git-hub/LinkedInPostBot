from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from linkedin_bot.config import AppConfig, CarouselConfig
from linkedin_bot.db import Repository
from linkedin_bot.integrations.carousel_pdf import render_carousel
from linkedin_bot.nodes.writer import format_research
from linkedin_bot.state import CarouselSpec, State

CarouselRenderer = Callable[[CarouselSpec, CarouselConfig, str], bytes]


def carousel_key(thread_id: str, variant: str) -> str:
    return f"{thread_id}:{variant}:carousel"


def make_carousel_maker(cfg: AppConfig, model: BaseChatModel, repo: Repository,
                        render: CarouselRenderer = render_carousel):
    system = (cfg.prompts_dir / "carousel.md").read_text().format(
        author=cfg.author, min_slides=cfg.carousel.slides.min, max_slides=cfg.carousel.slides.max)
    structured = model.with_structured_output(CarouselSpec)

    def carousel_node(state: State, config: RunnableConfig) -> dict:
        variant = state["variant"]
        spec: CarouselSpec = structured.invoke([
            SystemMessage(system),
            HumanMessage(f"Post ({variant}-Version):\n{state['drafts'][variant].text}\n\n"
                         f"Recherche:\n{format_research(state['research'])}"),
        ])
        spec = spec.model_copy(update={"slides": spec.slides[: cfg.carousel.slides.max]})
        pdf = render(spec, cfg.carousel, cfg.carousel.footer)
        repo.save_image(carousel_key(config["configurable"]["thread_id"], variant), pdf, spec.title)
        replaced = variant in (state.get("image_prompts") or {})
        return {
            "carousels": {variant: spec},
            "image_prompts": {variant: None},  # ein Post hat nur ein Medium
            "notice": "Das Karussell ersetzt das Bild dieser Version." if replaced else None,
        }

    return carousel_node
