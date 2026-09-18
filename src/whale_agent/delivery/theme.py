"""Email theme layer: colours, brand name, fonts, logo, length and section order.

A theme changes how the email looks. It never changes what the email says about money:
figures are rendered by the same code for every theme, and the egress gate in
`delivery/email.py` screens the themed body exactly as it screened the plain one.

Renderers take a `Theme` and read colours and fonts from it. The daily layout in
`summarization/render_html.py` keeps its palette constants as the "whale" preset, and
`Theme.restyle` maps each constant to the theme value in one pass. The whale preset maps
them to the site palette; an unthemed render uses the whale preset.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)

HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Every section the daily layout knows, in the default order. "events" always renders:
# hiding it would leave an email with no disclosures in it.
SECTION_NAMES = ("masthead", "headline", "coverage", "events", "footer")
DENSITIES = ("short", "detailed")

# The palette and fonts `render_html.py` uses today. Kept as literals here (not imported)
# so this module has no import cycle with the renderer; a test pins them equal.
_WHALE = {
    "ink": "#12100E",
    "ink_soft": "#3C4043",
    "ink_faint": "#6E7378",
    "paper": "#FFFFFF",
    "page": "#F0F1F3",
    "rule": "#DEE1E4",
    "rule_strong": "#B9BEC4",
    "caveat": "#7A5B12",
    "caveat_bg": "#FBF6E9",
    "navy": "#003468",
}
WHALE_SANS = "'Helvetica Neue',Helvetica,Arial,'Segoe UI',Roboto,sans-serif"
WHALE_SERIF = "Georgia,'Times New Roman',Times,serif"

# The site palette (whale-deep) and a condensed display stack. No web font is loaded:
# each name is a face a reader may already have, ending in plain Arial.
SITE = {
    "band": "#2E5696",  # outline blue: header band, section rules, buttons
    "navy": "#141C2D",  # ink
    "muted": "#3A4A63",
    "faint": "#66758C",
    "page": "#E8EEF7",
    "rule": "#D5DEEB",
    "rule_strong": "#AFBDD3",
}
DISPLAY = (
    "'Arial Narrow','Roboto Condensed','Helvetica Neue Condensed',"
    "'Franklin Gothic Medium','Helvetica Neue',Arial,sans-serif"
)


def validate_colour(value: str, name: str = "colour") -> str:
    """Return a hex colour in upper case, or raise ValueError. Only #RRGGBB is accepted:
    a named colour or an rgb() string could carry CSS into an inline style."""
    value = (value or "").strip()
    if not HEX_COLOUR.match(value):
        raise ValueError(f"{name} must be a hex colour like #4A6F93, not {value!r}")
    return value.upper()


def _safe_font(value: str) -> str:
    # A font stack lands inside style="...": refuse anything that could close it.
    if not re.fullmatch(r"[A-Za-z0-9 ,'\-]+", value or ""):
        raise ValueError(f"font stack has characters that are not allowed: {value!r}")
    return value


@dataclass(frozen=True)
class Theme:
    name: str = "whale"
    brand_name: str = "Whale Agent"
    accent: str = SITE["band"]
    background: str = _WHALE["paper"]  # the card the email sits on
    text: str = SITE["navy"]
    font_stack: str = WHALE_SANS  # body and chrome
    heading_font: str = DISPLAY  # masthead, labels, section names
    logo_url: str = ""
    density: str = "detailed"  # "short" | "detailed"
    sections: tuple[str, ...] = SECTION_NAMES
    page: str = SITE["page"]  # outside the card
    muted: str = SITE["muted"]
    faint: str = SITE["faint"]
    rule: str = SITE["rule"]
    rule_strong: str = SITE["rule_strong"]
    caveat: str = _WHALE["caveat"]
    caveat_bg: str = _WHALE["caveat_bg"]

    def __post_init__(self) -> None:
        for attr in (
            "accent",
            "background",
            "text",
            "page",
            "muted",
            "faint",
            "rule",
            "rule_strong",
            "caveat",
            "caveat_bg",
        ):
            object.__setattr__(self, attr, validate_colour(getattr(self, attr), attr))
        _safe_font(self.font_stack)
        _safe_font(self.heading_font)
        if self.density not in DENSITIES:
            raise ValueError(f"density must be one of {DENSITIES}, not {self.density!r}")
        if self.logo_url and not re.fullmatch(r"https://[^\s\"'<>]+", self.logo_url):
            raise ValueError("logo URL must be an https:// address")
        unknown = [s for s in self.sections if s not in SECTION_NAMES]
        if unknown:
            raise ValueError(f"unknown email sections: {', '.join(unknown)}")
        order = list(dict.fromkeys(self.sections))
        if "events" not in order:
            order.append("events")
        object.__setattr__(self, "sections", tuple(order))

    @property
    def detailed(self) -> bool:
        return self.density == "detailed"

    @property
    def brand_html(self) -> str:
        return html.escape(self.brand_name, quote=True)

    def restyle(self, markup: str) -> str:
        """Map the whale palette and fonts in rendered markup to this theme, in one pass.

        One regex pass, not chained `str.replace`, so a theme value that equals another
        whale constant (dark text on white, say) is never replaced a second time.
        """
        mapping = {
            _WHALE["ink"]: self.text,
            _WHALE["ink_soft"]: self.muted,
            _WHALE["ink_faint"]: self.faint,
            _WHALE["paper"]: self.background,
            _WHALE["page"]: self.page,
            _WHALE["rule"]: self.rule,
            _WHALE["rule_strong"]: self.rule_strong,
            _WHALE["caveat"]: self.caveat,
            _WHALE["caveat_bg"]: self.caveat_bg,
            _WHALE["navy"]: self.accent,
            WHALE_SANS: self.font_stack,
            WHALE_SERIF: self.heading_font,
        }
        mapping = {k: v for k, v in mapping.items() if k != v}
        if not mapping:
            return markup
        pattern = re.compile(
            "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
        )
        return pattern.sub(lambda m: mapping[m.group(0)], markup)


PRESETS: dict[str, Theme] = {
    "whale": Theme(),
    "minimal": Theme(
        name="minimal",
        accent="#4A6F93",
        font_stack=WHALE_SANS,
        heading_font=WHALE_SANS,
        page="#FFFFFF",
        rule_strong="#E3E6EA",
        caveat_bg="#F6F7F9",
        text=_WHALE["ink"],
        muted=_WHALE["ink_soft"],
        faint=_WHALE["ink_faint"],
        rule=_WHALE["rule"],
    ),
    "dark": Theme(
        name="dark",
        accent="#7FA3C4",
        background="#1C2024",
        text="#E6E9EC",
        page="#14171A",
        muted="#C2C9D1",
        faint="#9AA4B0",
        rule="#2C3238",
        rule_strong="#3A424A",
        caveat="#E0C877",
        caveat_bg="#2B2612",
        heading_font=WHALE_SANS,
    ),
    "newspaper": Theme(
        name="newspaper",
        accent="#111111",
        background="#FBF8F1",
        text="#111111",
        font_stack=WHALE_SERIF,
        heading_font=WHALE_SERIF,
        page="#EDE8DC",
        rule="#CFC8B8",
        rule_strong="#111111",
        muted=_WHALE["ink_soft"],
        faint=_WHALE["ink_faint"],
    ),
}


def get_preset(name: str) -> Theme:
    key = (name or "whale").strip().lower()
    if key not in PRESETS:
        raise ValueError(f"unknown theme {name!r}; choose one of: {', '.join(PRESETS)}")
    return PRESETS[key]


def build_theme(
    preset: str = "whale",
    accent: str = "",
    brand_name: str = "",
    length: str = "",
    sections: str = "",
) -> Theme:
    """A preset with the user's overrides applied. Raises ValueError on a bad value."""
    theme = get_preset(preset)
    changes: dict = {}
    if accent.strip():
        changes["accent"] = validate_colour(accent, "WHALE_THEME_ACCENT")
    if brand_name.strip():
        changes["brand_name"] = brand_name.strip()[:80]
    if length.strip():
        changes["density"] = length.strip().lower()
    if sections.strip():
        changes["sections"] = tuple(
            p.strip().lower() for p in sections.split(",") if p.strip()
        )
    return replace(theme, **changes) if changes else theme


def theme_from_env(environ: dict | None = None) -> Theme:
    """The theme the settings ask for. A bad value falls back to the whale preset with
    a warning: a typo in a colour must not stop the brief going out."""
    env = os.environ if environ is None else environ
    try:
        return build_theme(
            env.get("WHALE_THEME", "whale") or "whale",
            env.get("WHALE_THEME_ACCENT", ""),
            env.get("WHALE_BRAND_NAME", ""),
            env.get("WHALE_EMAIL_LENGTH", ""),
            env.get("WHALE_EMAIL_SECTIONS", ""),
        )
    except ValueError as exc:
        log.warning("Email theme setting ignored, using whale preset: %s", exc)
        return PRESETS["whale"]
