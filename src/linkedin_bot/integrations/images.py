import base64

import openai


class OpenAIImageGenerator:
    """Erzeugt ein Bild und liefert die PNG-Bytes. Der OpenAI-Key kommt wie beim Chat aus OPENAI_API_KEY."""

    def __init__(self, model: str, size: str, quality: str):
        self.model, self.size, self.quality = model, size, quality

    def __call__(self, prompt: str) -> bytes:
        result = openai.OpenAI().images.generate(model=self.model, prompt=prompt, size=self.size, quality=self.quality, n=1)
        return base64.b64decode(result.data[0].b64_json)
