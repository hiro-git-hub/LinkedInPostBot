import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.state import HashtagList, State

MAX_HASHTAGS = 6
HASHTAG_LINE = re.compile(r"^(\s*#\w+)+\s*$")


def clean_tags(tags: list[str]) -> list[str]:
    """Nur Buchstaben/Ziffern (LinkedIn-Hashtags sind ein Wort), ohne Duplikate, max. 6."""
    result, seen = [], set()
    for tag in tags:
        tag = re.sub(r"[^\w]|_", "", tag.lstrip("#"))
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result[:MAX_HASHTAGS]


def split_hashtags(text: str) -> tuple[str, list[str]]:
    """Trennt eine Hashtag-Zeile am Textende ab – z.B. wenn der Autor den Post inkl. Hashtags bearbeitet."""
    lines = text.rstrip().splitlines()
    tags: list[str] = []
    while lines and HASHTAG_LINE.match(lines[-1]):
        tags = re.findall(r"#(\w+)", lines.pop()) + tags
    return "\n".join(lines).rstrip(), clean_tags(tags)


def hashtag_line(tags: list[str]) -> str:
    return " ".join(f"#{t}" for t in tags)


def make_hashtagger(cfg: AppConfig, model: BaseChatModel):
    system = (cfg.prompts_dir / "hashtags.md").read_text().format(audience=cfg.audience)
    structured = model.with_structured_output(HashtagList)

    def hashtag_node(state: State) -> dict:
        if state.get("hashtags"):
            return {}  # z.B. nach "Überarbeiten": Thema unverändert, Hashtags bleiben
        item = state["selected"].item
        draft = state["drafts"]["normal"].text
        result: HashtagList = structured.invoke([
            SystemMessage(system),
            HumanMessage(f"Thema: {item.title}\n\nPost:\n{draft}"),
        ])
        return {"hashtags": clean_tags(result.tags)}

    return hashtag_node
