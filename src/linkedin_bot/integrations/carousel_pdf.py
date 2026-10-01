"""Rendert ein Karussell (LinkedIn-Dokumentpost) als PDF – eine Seite pro Folie, Hochformat 4:5."""

import io
from pathlib import Path

from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from reportlab.lib.colors import HexColor, toColor
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from linkedin_bot.config import CarouselConfig
from linkedin_bot.state import CarouselSpec, Slide

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
TEXT_COLOR = HexColor("#1B1F24")
MUTED_COLOR = HexColor("#6B7280")
CODE_BACKGROUND = HexColor("#F1F0EA")
CODE_STYLE = "friendly"  # helles Pygments-Theme, passt zum hellen Hintergrund
MARGIN = 96


def _register(family: str, weights: tuple[str, ...]) -> dict[str, str]:
    """Registriert die mitgelieferten TTFs; "JetBrains Mono" -> JetBrainsMono-Regular.ttf usw."""
    names = {}
    base = family.replace(" ", "")
    for weight in weights:
        name = f"{base}-{weight}"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONTS_DIR / f"{name}.ttf")))
        names[weight] = name
    return names


def _fit(text: str, font: str, size: float, width: float, max_height: float, min_size: float) -> tuple[list[str], float]:
    """Bricht Text um und verkleinert die Schrift, bis er in die Fläche passt."""
    while True:
        lines = []
        for paragraph in text.split("\n"):
            lines += simpleSplit(paragraph, font, size, width) or [""]
        if len(lines) * size * 1.3 <= max_height or size <= min_size:
            return lines, size
        size -= 2


class _Renderer:
    def __init__(self, cfg: CarouselConfig, footer: str):
        self.cfg, self.footer = cfg, footer
        self.width, self.height = cfg.page_size
        self.text_font = _register(cfg.theme.font, ("Regular", "SemiBold", "Bold"))
        self.mono_font = _register(cfg.theme.mono_font, ("Regular", "Bold"))
        self.accent = toColor(cfg.theme.accent)
        self.background = toColor(cfg.theme.background)
        self.code_style = get_style_by_name(CODE_STYLE)

    def render(self, spec: CarouselSpec) -> bytes:
        buffer = io.BytesIO()
        canvas = Canvas(buffer, pagesize=(self.width, self.height))
        canvas.setTitle(spec.title)
        for number, slide in enumerate(spec.slides, start=1):
            self._frame(canvas, number, len(spec.slides))
            {"title": self._title, "code": self._code}.get(slide.kind, self._text)(canvas, slide)
            canvas.showPage()
        canvas.save()
        return buffer.getvalue()

    # --- Bausteine ----------------------------------------------------------------------------------

    def _frame(self, canvas: Canvas, number: int, total: int) -> None:
        canvas.setFillColor(self.background)
        canvas.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        canvas.setFillColor(self.accent)
        canvas.rect(0, self.height - 16, self.width, 16, stroke=0, fill=1)
        canvas.setFont(self.text_font["Regular"], 26)
        canvas.setFillColor(MUTED_COLOR)
        canvas.drawString(MARGIN, 64, self.footer)
        canvas.drawRightString(self.width - MARGIN, 64, f"{number}/{total}")

    def _lines(self, canvas: Canvas, lines: list[str], font: str, size: float, y: float, color=TEXT_COLOR) -> float:
        canvas.setFont(font, size)
        canvas.setFillColor(color)
        for line in lines:
            canvas.drawString(MARGIN, y, line)
            y -= size * 1.3
        return y

    def _headline(self, canvas: Canvas, text: str, y: float, size: float, max_height: float) -> float:
        lines, size = _fit(text, self.text_font["Bold"], size, self.width - 2 * MARGIN, max_height, 40)
        return self._lines(canvas, lines, self.text_font["Bold"], size, y - size)

    def _body(self, canvas: Canvas, text: str, y: float, max_height: float) -> float:
        if not text:
            return y
        lines, size = _fit(text, self.text_font["Regular"], 40, self.width - 2 * MARGIN, max_height, 26)
        return self._lines(canvas, lines, self.text_font["Regular"], size, y - size)

    # --- Folientypen --------------------------------------------------------------------------------

    def _title(self, canvas: Canvas, slide: Slide) -> None:
        canvas.setFillColor(self.accent)
        canvas.rect(MARGIN, self.height * 0.62, 120, 12, stroke=0, fill=1)
        y = self._headline(canvas, slide.headline, self.height * 0.62 - 40, 88, self.height * 0.35)
        self._body(canvas, slide.body, y - 30, y - 160)

    def _text(self, canvas: Canvas, slide: Slide) -> None:
        y = self._headline(canvas, slide.headline, self.height - 180, 64, 380)
        self._body(canvas, slide.body, y - 40, y - 160)

    def _code(self, canvas: Canvas, slide: Slide) -> None:
        y = self._headline(canvas, slide.headline, self.height - 180, 56, 260)
        y = self._body(canvas, slide.body, y - 20, 200) - 30
        code_lines = slide.code.rstrip().splitlines() or [""]
        size = min(32.0, (self.width - 2 * MARGIN - 64) / (max(len(line) for line in code_lines) * 0.6 or 1))
        size = max(size, 18.0)
        box_height = len(code_lines) * size * 1.45 + 64
        top = min(y, self.height - 200)
        canvas.setFillColor(CODE_BACKGROUND)
        canvas.roundRect(MARGIN - 8, top - box_height, self.width - 2 * MARGIN + 16, box_height, 24, stroke=0, fill=1)
        self._highlighted(canvas, slide, code_lines, top - 32 - size, size)

    def _highlighted(self, canvas: Canvas, slide: Slide, code_lines: list[str], y: float, size: float) -> None:
        try:
            # startinline: PHP-Snippets ohne "<?php" sonst als HTML-Text behandelt (= ohne Farben)
            lexer = get_lexer_by_name(slide.language, startinline=True) if slide.language else TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        x0, x = MARGIN + 24, MARGIN + 24
        for token_type, value in lex("\n".join(code_lines), lexer):
            style = self.code_style.style_for_token(token_type) if self.cfg.code_highlighting else {}
            canvas.setFillColor(HexColor(f"#{style['color']}") if style.get("color") else TEXT_COLOR)
            font = self.mono_font["Bold" if style.get("bold") else "Regular"]
            canvas.setFont(font, size)
            for i, part in enumerate(value.split("\n")):
                if i:  # Zeilenumbruch im Token
                    x, y = x0, y - size * 1.45
                if part:
                    canvas.drawString(x, y, part)
                    x += pdfmetrics.stringWidth(part, font, size)


def render_carousel(spec: CarouselSpec, cfg: CarouselConfig, footer: str = "") -> bytes:
    return _Renderer(cfg, footer).render(spec)
