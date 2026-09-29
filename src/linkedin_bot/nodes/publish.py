import logging

import httpx
from langchain_core.runnables import RunnableConfig

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.integrations.linkedin import LinkedInClient, LinkedInError
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

        image_prompt = state.get("image_prompt")
        image = repo.get_image(config["configurable"]["thread_id"]) if image_prompt else None
        try:
            # Erst hier erzeugen: im Dry-Run braucht es keine LinkedIn-Credentials.
            urn = (client or LinkedInClient(cfg.linkedin.api_version)).create_post(
                auth,
                state["drafts"][state["variant"]].text,
                hashtags=state.get("hashtags") or [],
                image=image,
                alt_text=image_prompt.alt_text if image_prompt else "",
                visibility=cfg.linkedin.visibility,
            )
        except (LinkedInError, httpx.HTTPError) as exc:
            log.exception("Veröffentlichung fehlgeschlagen")
            return {"status": "publish_failed", "post_urn": None, "publish_error": str(exc)}
        return {"status": "published", "post_urn": urn, "publish_error": None}

    return publish_node
