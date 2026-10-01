import logging

import httpx
from langchain_core.runnables import RunnableConfig

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.integrations.linkedin import LinkedInClient, LinkedInError
from linkedin_bot.nodes.dedup import normalize_url
from linkedin_bot.nodes.carousel import carousel_key
from linkedin_bot.nodes.image import image_key
from linkedin_bot.state import State

log = logging.getLogger(__name__)

LOGIN_HINT = "Keine gültige LinkedIn-Anmeldung – bitte im Telegram-Chat /login schicken."


def make_publisher(cfg: AppConfig, repo: Repository, client: LinkedInClient | None = None):
    def publish_node(state: State, config: RunnableConfig) -> dict:
        if cfg.linkedin.dry_run:
            return {"status": "approved", "post_urn": None, "publish_error": None}

        auth = repo.get_linkedin_auth()
        if auth is None or auth.expired:
            return {"status": "publish_failed", "post_urn": None, "publish_error": LOGIN_HINT}

        variant, thread_id = state["variant"], config["configurable"]["thread_id"]
        image_prompt = (state.get("image_prompts") or {}).get(variant)
        image = repo.get_image(image_key(thread_id, variant)) if image_prompt else None
        carousel = (state.get("carousels") or {}).get(variant)
        document = repo.get_image(carousel_key(thread_id, variant)) if carousel else None
        # Erst hier erzeugen: im Dry-Run braucht es keine LinkedIn-Credentials.
        linkedin = client or LinkedInClient(cfg.linkedin.api_version)
        try:
            urn = linkedin.create_post(
                auth,
                state["drafts"][variant].text,
                hashtags=state.get("hashtags") or [],
                image=image,
                alt_text=image_prompt.alt_text if image_prompt else "",
                visibility=cfg.linkedin.visibility,
                document=document,
                document_title=carousel.title if carousel else "",
            )
        except (LinkedInError, httpx.HTTPError) as exc:
            log.exception("Veröffentlichung fehlgeschlagen")
            return {"status": "publish_failed", "post_urn": None, "publish_error": str(exc)}

        result = {"status": "published", "post_urn": urn, "publish_error": None,
                  "source_commented": False, "comment_error": None}
        if cfg.writing.source_in_first_comment:
            try:
                linkedin.comment(auth, urn, f"Quelle: {normalize_url(state['selected'].item.url)}")  # ohne utm_-Tracking
                result["source_commented"] = True
            except (LinkedInError, httpx.HTTPError) as exc:
                # Der Post ist online – ein fehlender Kommentar ist kein Grund, ihn als fehlgeschlagen zu werten.
                log.warning("Quell-Kommentar fehlgeschlagen: %s", exc)
                result["comment_error"] = str(exc)
        return result

    return publish_node
