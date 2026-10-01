import base64
import logging

import openai
from langchain_core.callbacks import dispatch_custom_event

from linkedin_bot.budget import COST_EVENT, price_for, token_cost
from linkedin_bot.config import Price

log = logging.getLogger(__name__)


class OpenAIImageGenerator:
    """Erzeugt ein Bild und liefert die PNG-Bytes. Der OpenAI-Key kommt wie beim Chat aus OPENAI_API_KEY."""

    def __init__(self, model: str, size: str, quality: str, pricing: dict[str, Price] | None = None):
        self.model, self.size, self.quality = model, size, quality
        self.price = price_for(pricing or {}, model)

    def __call__(self, prompt: str) -> bytes:
        result = openai.OpenAI().images.generate(model=self.model, prompt=prompt, size=self.size, quality=self.quality, n=1)
        usage = getattr(result, "usage", None)
        if usage and self.price:
            # Läuft innerhalb des Graph-Nodes -> landet beim CostTracker dieses Laufs.
            dispatch_custom_event(COST_EVENT, {"usd": token_cost(self.price, usage.input_tokens, usage.output_tokens)})
        return base64.b64decode(result.data[0].b64_json)
