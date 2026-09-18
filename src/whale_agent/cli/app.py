"""Interactive start screen and settings menu: `./whale` with no arguments, in a terminal.

Everything the menu changes is written to `.env`, the same file `config.load_env_file`
already reads, so there is one place a setting lives. Lines the menu does not touch,
including comments, are kept exactly as they were.

No third-party UI library: plain `input()` and ANSI colour, switched off when stdout is
not a terminal or NO_COLOR is set (https://no-color.org).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Deep blues, from the whale icon (light body, dark outline) and a navy terminal look.
_BODY = (111, 160, 230)
_OUTLINE = (46, 86, 150)
_EYE = (20, 28, 45)
_TEXT_BLUE = (111, 143, 208)

# Traced from the whale icon (42 x 36 pixels). One character per pixel: D outline,
# L body, E eye, . empty. Half blocks turn two pixel rows into one terminal row.
WHALE_PIXELS = (
    "..........................................",
    ".......LLLLL..............LL..........LL..",
    ".....LDDDDDDDDL..........LDDD........DDDL.",
    "....LDDLLLLLLDDD.........DDLDDDDLLDDDDLDL.",
    "...LDDLLLLLLLLLDDL.......DDLLLLDDDDLLLLDL.",
    "..LDDLLLLLLLLLLLDDL......LDLLLLLDLLLLLLDL.",
    "..DDLLLLLLLLLLLLLLDL.....LDLLLLLLLLLLLLD..",
    "..DLLLLLLLLLLLLLLLLDL.....DDLLLLLLLLLLDL..",
    ".LDLLLLLLLLLLLLLLLLDDL.....DDLLLLLLLDDD...",
    ".LDLLLLLLLLLLLLLLLLLDD......LDDDLLDDDL....",
    ".LDDLLLLLLLLLEDLLLLLLDD.......LDLLD.......",
    ".LDDDLLLLLLLDEEDLLLLLLDD......DDLLD.......",
    ".LDDDLLLLLLLLEELLLLLLLLDDL...LDLLLD.......",
    ".LDLLDDLLLLLLLLLLLLLLLLLDDDDDDLLLLD.......",
    "..DLLLDDLLLLLLLLLLLLLLLLLLLDLLLLLDD.......",
    "..DDLLLDDDLLLLLLLLLLLLLLLLLLLLLLLDL.......",
    "..LDLLLLLDDDDDLLLLLLLLLLLLLLLLLLLDL.......",
    "...DDLLLLLLLLLLLLLLLLLLLLLLLLLLLLD........",
    "...LDLLLLLLLLLLLLLLLLLLLLLLLLLLLDL........",
    "....DDLLLLLLLLLLLLLLLLLDLLLLLLLDD.........",
    ".....DDLLLLLLLLLLLLLLLDDLLLLLLLDL.........",
    ".....LDDLLLLLLLLLLLLLLDLLLLLLLDL..........",
    "......LDDLLLLLLDDLLLLLDLLLLLDDL...........",
    ".......LDDLLLLLDDLLLLLDDLLLDD.............",
    ".......LDDDDLLLLDLLLLLDDLDDL..............",
    ".......DDDDDDDLLDLLLLLDDDL................",
    "......LDDDDDDDDDDDLLLLDD..................",
    "......DDDDDL...LDDLLLLDL..................",
    "......DDDL......LDLLLLD...................",
    ".................DLLLDD...................",
    ".................DDLLDL...................",
    ".................LDLDD....................",
    ".................LDDD.....................",
    "..................DD......................",
    "..................L.......................",
    "..........................................",
)

# Small version for narrow terminals.
WHALE_PIXELS_SMALL = (
    "......................",
    "...............DD..DD.",
    "...............DLDDLD.",
    "....DDDDDD......DLLD..",
    "..DDLLLLLLDD.....LL...",
    ".DLLLLLLLLLLDD...LL...",
    ".DLLLLEELLLLLLDDDLL...",
    "DDLLLLEELLLLLLLLLLD...",
    "DDDLLLLLLLLLLLLLLLD...",
    "DLLDDDDLLLLLLLLLLD....",
    ".DLLLLLLLDLLLLLLD.....",
    "..DDLLLLLDLLLLDD......",
    "..D.DDDDDDLLDD........",
    ".DD.......DD..........",
)
_PIXEL_RGB = {"D": _OUTLINE, "L": _BODY, "E": _EYE}


def render_whale(color: bool, pixels: tuple[str, ...] = WHALE_PIXELS) -> list[str]:
    """Whale as terminal rows. Colour: filled half blocks. No colour: plain shape."""
    rows = []
    for top, bottom in zip(pixels[0::2], pixels[1::2], strict=True):
        out = []
        for a, b in zip(top, bottom, strict=True):
            if not color:
                out.append(" " if a == "." and b == "." else "#")
                continue
            ta, tb = _PIXEL_RGB.get(a), _PIXEL_RGB.get(b)
            # Split on `ta` first so both colours are narrowed to a tuple below.
            if ta is None:
                if tb is None:
                    out.append(" ")
                else:
                    out.append(f"\033[38;2;{tb[0]};{tb[1]};{tb[2]}m\u2584\033[0m")
                continue
            if tb is None or ta == tb:
                ch = "\u2588" if ta == tb else "\u2580"
                out.append(f"\033[38;2;{ta[0]};{ta[1]};{ta[2]}m{ch}\033[0m")
            else:
                out.append(
                    f"\033[38;2;{ta[0]};{ta[1]};{ta[2]};48;2;{tb[0]};{tb[1]};{tb[2]}m"
                    "\u2580\033[0m"
                )
        rows.append("".join(out))
    return rows


def use_color(stream=None, environ=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    environ = environ if environ is not None else os.environ
    if environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _blue(text: str, color: bool) -> str:
    if not color:
        return text
    r, g, b = _TEXT_BLUE
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def _bold(text: str, color: bool) -> str:
    return f"\033[1m{text}\033[0m" if color else text


def _dim(text: str, color: bool) -> str:
    return f"\033[2m{text}\033[0m" if color else text


@dataclass(frozen=True)
class Setting:
    key: str  # environment variable name, as in .env.example
    label: str
    kind: str  # "text" | "money" | "flag" | "choice" | "secret" | "list"
    default: str = ""
    choices: tuple[str, ...] = ()
    help: str = ""
    section: str = ""


SECTIONS = ("Delivery", "Filters", "AI writer", "Sources", "Keys", "Email design")

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "WHALE_EMAIL_TO",
        "Send the brief to",
        "text",
        help="one address, or several separated by commas",
        section="Delivery",
    ),
    Setting(
        "WHALE_CADENCE",
        "Which emails go out",
        "choice",
        "both",
        ("daily", "weekly", "both"),
        "daily brief, weekly report, or both",
        section="Delivery",
    ),
    Setting(
        "WHALE_EMAIL_PROVIDER",
        "Email sender",
        "choice",
        "smtp",
        ("smtp", "resend", "none"),
        section="Delivery",
    ),
    Setting("SMTP_USERNAME", "Gmail address", "text", section="Delivery"),
    Setting(
        "WHALE_THRESHOLD_USD",
        "Dollar threshold",
        "money",
        "5000000",
        help="filings smaller than this are left out of the email",
        section="Filters",
    ),
    Setting(
        "WHALE_WATCH_FILERS",
        "Watchlist: filers",
        "list",
        help="names separated by commas, blank = everyone",
        section="Filters",
    ),
    Setting(
        "WHALE_WATCH_TICKERS",
        "Watchlist: tickers",
        "list",
        help="symbols separated by commas, blank = every ticker",
        section="Filters",
    ),
    Setting(
        "WHALE_LLM_PROVIDER",
        "AI writer",
        "choice",
        "gemini",
        ("none", "gemini", "anthropic", "openai"),
        "none = fixed template, gemini = free tier, anthropic = paid, "
        "openai = OpenAI, OpenRouter or Ollama",
        section="AI writer",
    ),
    Setting(
        "OPENAI_BASE_URL",
        "OpenAI-compatible base URL",
        "text",
        "https://api.openai.com/v1",
        help="OpenRouter: https://openrouter.ai/api/v1, Ollama: http://localhost:11434/v1",
        section="AI writer",
    ),
    Setting(
        "OPENAI_MODEL",
        "OpenAI-compatible model",
        "text",
        "gpt-5.6-luna",
        help="pin a model id; Ollama example: llama3.1",
        section="AI writer",
    ),
    Setting(
        "WHALE_SEC_USER_AGENT",
        "SEC contact (name and email)",
        "text",
        help="the SEC requires a contact string; no key needed",
        section="Sources",
    ),
    Setting("WHALE_ENABLE_JAPAN", "Japan filings (EDINET)", "flag", "1", section="Sources"),
    Setting("WHALE_ENABLE_TAIWAN", "Taiwan filings (MOPS)", "flag", "1", section="Sources"),
    Setting("WHALE_ENABLE_FMP", "FMP insider feed (paid key)", "flag", "1", section="Sources"),
    Setting(
        "GEMINI_API_KEY",
        "Gemini API key (free)",
        "secret",
        help="https://aistudio.google.com/apikey",
        section="Keys",
    ),
    Setting("ANTHROPIC_API_KEY", "Anthropic API key (paid)", "secret", section="Keys"),
    Setting(
        "OPENAI_API_KEY",
        "OpenAI / OpenRouter API key",
        "secret",
        help="not needed for Ollama on localhost",
        section="Keys",
    ),
    Setting(
        "SMTP_PASSWORD",
        "Gmail app password",
        "secret",
        help="16 characters from Google account > App passwords, not your login",
        section="Keys",
    ),
    Setting("JAPAN_EDINET_API_KEY", "EDINET API key (free)", "secret", section="Keys"),
    Setting("FMP_API_KEY", "FMP API key", "secret", section="Keys"),
    Setting(
        "WHALE_ENABLE_QUIVER",
        "Quiver congress feed (paid key)",
        "flag",
        "1",
        section="Sources",
    ),
    Setting("WHALE_ENABLE_ARKHAM", "Arkham on-chain (key)", "flag", "1", section="Sources"),
    Setting(
        "WHALE_ENABLE_WHALE_ALERT",
        "Whale Alert on-chain (key)",
        "flag",
        "1",
        section="Sources",
    ),
    Setting(
        "WHALE_ENABLE_FINNHUB",
        "Finnhub insider feed (paid key)",
        "flag",
        "0",
        section="Sources",
    ),
    Setting(
        "WHALE_ENABLE_UNUSUAL_WHALES",
        "Unusual Whales congress feed (paid key)",
        "flag",
        "0",
        section="Sources",
    ),
    Setting("QUIVER_QUANT_API_KEY", "Quiver API key", "secret", section="Keys"),
    Setting("ARKHAM_API_KEY", "Arkham API key", "secret", section="Keys"),
    Setting("WHALE_ALERT_API_KEY", "Whale Alert API key", "secret", section="Keys"),
    Setting(
        "FINNHUB_API_KEY",
        "Finnhub API key",
        "secret",
        help="https://finnhub.io/dashboard",
        section="Keys",
    ),
    Setting("UNUSUAL_WHALES_API_KEY", "Unusual Whales API key", "secret", section="Keys"),
    Setting(
        "WHALE_THEME",
        "Email theme",
        "choice",
        "whale",
        ("whale", "minimal", "dark", "newspaper"),
        section="Email design",
    ),
    Setting(
        "WHALE_THEME_ACCENT",
        "Accent colour",
        "text",
        help="hex colour like #4a6f93, blank = the theme's own",
        section="Email design",
    ),
    Setting(
        "WHALE_BRAND_NAME",
        "Brand name",
        "text",
        help="name at the top of the email, blank = Whale Digest",
        section="Email design",
    ),
    Setting(
        "WHALE_EMAIL_LENGTH",
        "Email length",
        "choice",
        "detailed",
        ("short", "detailed"),
        "short leaves out the prose under each filing",
        section="Email design",
    ),
    Setting(
        "WHALE_EMAIL_SECTIONS",
        "Sections, in order",
        "list",
        help="masthead,headline,coverage,events,footer; leave one out to hide it",
        section="Email design",
    ),
)


# -- .env reading and writing ---------------------------------------------------------


def _parse_value(raw: str) -> str:
    return raw.split("  #", 1)[0].strip().strip("'\"")


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = _parse_value(value)
    return values


def _format_value(value: str) -> str:
    return f'"{value}"' if any(c in value for c in " #\"'") else value


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set keys in a .env file in place. Other lines and comments are kept as they were."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in pending and not stripped.startswith("#"):
            out.append(f"{key}={_format_value(pending.pop(key))}")
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={_format_value(value)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)  # secrets live here


# -- validation and display -----------------------------------------------------------


def normalize(setting: Setting, raw: str) -> str:
    """Return the value to store, or raise ValueError with a message for the user."""
    value = raw.strip()
    if setting.kind == "money":
        cleaned = value.replace("$", "").replace(",", "").replace("_", "").upper()
        mult = 1
        if cleaned.endswith("M"):
            mult, cleaned = 1_000_000, cleaned[:-1]
        elif cleaned.endswith("K"):
            mult, cleaned = 1_000, cleaned[:-1]
        try:
            amount = float(cleaned) * mult
        except ValueError:
            raise ValueError("enter a dollar amount, for example 5M or 250000") from None
        if amount <= 0:
            raise ValueError("the threshold must be more than zero")
        return str(int(amount)) if amount.is_integer() else str(amount)
    if setting.kind == "flag":
        low = value.lower()
        if low in {"1", "y", "yes", "on", "true"}:
            return "1"
        if low in {"0", "n", "no", "off", "false"}:
            return "0"
        raise ValueError("enter on or off")
    if setting.key == "WHALE_THEME_ACCENT" and value:
        from whale_agent.delivery.theme import validate_colour

        return validate_colour(value, "accent colour").lower()
    if setting.key == "WHALE_EMAIL_SECTIONS" and value:
        from whale_agent.delivery.theme import build_theme

        build_theme(sections=value)  # raises ValueError on an unknown name
    if setting.kind == "list":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if setting.key == "WHALE_WATCH_TICKERS":
            parts = [p.upper() for p in parts]
        return ",".join(parts)
    if setting.kind == "choice":
        low = value.lower()
        if low not in setting.choices:
            raise ValueError("choose one of: " + ", ".join(setting.choices))
        return low
    return value


def display(setting: Setting, value: str) -> str:
    if value == "":
        return "(not set)"
    if setting.kind == "secret":
        return "set (" + "*" * 4 + value[-4:] + ")" if len(value) > 8 else "set"
    if setting.kind == "flag":
        return "on" if value == "1" else "off"
    if setting.kind == "money":
        try:
            amount = float(value)
        except ValueError:
            return value
        if amount >= 1_000_000:
            return f"${amount / 1_000_000:g}M"
        if amount >= 1_000:
            return f"${amount / 1_000:g}K"
        return f"${amount:g}"
    if setting.kind == "list":
        return ", ".join(value.split(","))
    return value


def current_value(setting: Setting, file_values: dict[str, str]) -> str:
    return file_values.get(setting.key, setting.default)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("whale-agent")
    except Exception:
        return "dev"


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(width - 1, 0)] + "\u2026"


TAGLINE = "Reads free public filings. Emails you one brief."


def _where(cwd: Path | None) -> str:
    where = str(cwd or Path.cwd())
    home = str(Path.home())
    return "~" + where[len(home) :] if where.startswith(home) else where


def _style(text: str, style: str, room: int, color: bool) -> str:
    """Cut plain text to `room` columns first, then add colour, so width stays exact."""
    if style == "rule":
        return _dim("\u2500" * max(min(room, 44), 0), color)
    if style == "sel":
        return _blue(_bold(_fit("> " + text, room), color), color)
    if style == "item":
        return _fit("  " + text, room)
    if style == "head":
        return _blue(_bold(_fit(text, room), color), color)
    cut = _fit(text, room)
    if style == "title":
        return _blue(_bold(cut, color), color)
    if style == "bold":
        return _bold(cut, color)
    if style == "blue":
        return _blue(cut, color)
    if style == "dim":
        return _dim(cut, color)
    return cut


def compose(info: list[tuple[str, str]], color: bool, width: int | None = None) -> str:
    """Whale on the left and `info` on the right when there is room; otherwise the
    info goes under a whale that fits, or stands alone. No line is wider than `width`."""
    import shutil

    width = width or shutil.get_terminal_size((100, 24)).columns
    margin, gap, min_info = 2, 4, 34
    layouts = (
        (WHALE_PIXELS, True),
        (WHALE_PIXELS_SMALL, True),
        (WHALE_PIXELS, False),
        (WHALE_PIXELS_SMALL, False),
    )
    for pixels, beside in layouts:
        art_w = len(pixels[0])
        if width < margin + art_w + (gap + min_info if beside else 0):
            continue
        art = render_whale(color, pixels)
        if beside:
            room = width - margin - art_w - gap
            height = max(len(art), len(info))
            art_top = (height - len(art)) // 2
            info_top = (height - len(info)) // 2
            lines = []
            for i in range(height):
                a = i - art_top
                row = art[a] if 0 <= a < len(art) else " " * art_w
                j = i - info_top
                right = _style(*info[j], room, color) if 0 <= j < len(info) else ""
                lines.append((" " * margin + row + " " * gap + right).rstrip())
            return "\n".join(lines)
        room = width - margin
        lines = [" " * margin + row for row in art] + [""]
        lines += [(" " * margin + _style(t, st, room, color)).rstrip() for t, st in info]
        return "\n".join(lines)
    room = max(width - margin, 1)
    return "\n".join((" " * margin + _style(t, st, room, color)).rstrip() for t, st in info)


def _summary(file_values: dict[str, str]) -> list[tuple[str, str]]:
    by_key = {s.key: s for s in SETTINGS}

    def show(key: str) -> str:
        return display(by_key[key], current_value(by_key[key], file_values))

    return [
        (
            f"by Patrick Taylor \u00b7 {show('WHALE_THRESHOLD_USD')} threshold"
            f" \u00b7 AI writer {show('WHALE_LLM_PROVIDER')}",
            "dim",
        ),
        (f"brief to {show('WHALE_EMAIL_TO')}", "dim"),
    ]


def render_banner(
    file_values: dict[str, str], color: bool, cwd: Path | None = None, width: int | None = None
) -> str:
    info = [
        ("Whale Agent", "title"),
        (TAGLINE, "plain"),
        ("", "plain"),
        ("v" + _version(), "dim"),
    ]
    info += _summary(file_values) + [(_where(cwd), "dim")]
    return compose(info, color, width)


def render_home(
    file_values: dict[str, str],
    color: bool,
    selected: int,
    cwd: Path | None = None,
    width: int | None = None,
) -> str:
    info = [
        ("Whale Agent", "title"),
        (TAGLINE, "plain"),
        ("", "plain"),
        ("v" + _version(), "dim"),
        ("", "rule"),
        ("", "plain"),
        ("Get started", "bold"),
        ("", "plain"),
    ]
    for i, (_key, label) in enumerate(MENU):
        info.append((label, "sel" if i == selected else "item"))
    info += [("", "plain")] + _summary(file_values)
    info += [("\u2191\u2193 move \u00b7 enter select \u00b7 q quit", "dim")]
    return compose(info, color, width)


def settings_rows() -> list[tuple[str, Setting | None]]:
    """Rows of the settings picker: section headings (not selectable) and settings."""
    rows: list[tuple[str, Setting | None]] = []
    for section in SECTIONS:
        items = [s for s in SETTINGS if s.section == section]
        if items:
            rows.append((section, None))
            rows.extend((s.label, s) for s in items)
    rows.append(("Back", None))
    return rows


def render_settings_screen(
    values: dict[str, str], color: bool, selected: int, width: int | None = None
) -> str:
    info: list[tuple[str, str]] = [
        ("Settings", "title"),
        ("saved to .env", "dim"),
        ("", "rule"),
    ]
    for i, (label, s) in enumerate(settings_rows()):
        if s is None and label != "Back":
            info += [("", "plain"), (label, "head")]
            continue
        if label == "Back":
            info.append(("", "plain"))
            text = "Back"
        else:
            assert s is not None  # the None rows are the headings handled above
            text = f"{label:<29} {display(s, current_value(s, values))}"
        info.append((text, "sel" if i == selected else "item"))
    info += [("", "plain"), ("\u2191\u2193 move \u00b7 enter change \u00b7 q back", "dim")]
    return compose(info, color, width)


def render_options_screen(
    setting: Setting,
    options: list[str],
    current: str,
    color: bool,
    selected: int,
    width: int | None = None,
) -> str:
    info: list[tuple[str, str]] = [(setting.label, "title")]
    if setting.help:
        info.append((setting.help, "dim"))
    info.append(("", "rule"))
    info.append(("", "plain"))
    for i, option in enumerate(options):
        mark = "  (current)" if option == current else ""
        info.append((option + mark, "sel" if i == selected else "item"))
    info += [("", "plain"), ("\u2191\u2193 move \u00b7 enter choose \u00b7 q back", "dim")]
    return compose(info, color, width)


# -- the interactive loop -------------------------------------------------------------

MENU = (
    ("1", "Settings"),
    ("2", "Preview a sample brief (offline, no email)"),
    ("3", "Run today's brief (no email)"),
    ("4", "Run today's brief and email it"),
    ("5", "Send a test email"),
    ("6", "Show every source and what is on or off"),
    ("7", "Check setup (whale doctor)"),
    ("8", "Schedule automatic emails"),
    ("9", "Preview email design"),
    ("q", "Quit"),
)


def render_settings(values: dict[str, str], color: bool) -> list[str]:
    """Settings grouped by section, numbered in SETTINGS order."""
    lines: list[str] = []
    for section in SECTIONS:
        rows = [(i, s) for i, s in enumerate(SETTINGS, start=1) if s.section == section]
        if not rows:
            continue
        lines.append("")
        lines.append("  " + _blue(_bold(section, color), color))
        for i, s in rows:
            shown = display(s, current_value(s, values))
            if shown == "(not set)":
                shown = _dim(shown, color)
            lines.append(f"  {_blue(f'{i:>2}', color)}  {s.label:<30} {shown}")
    return lines


def settings_menu(
    env_path: Path, ask: Callable[[str], str], say: Callable[[str], None], color: bool
) -> None:
    while True:
        values = read_env_values(env_path)
        say("")
        say(_bold("Settings", color) + _dim(f"  (saved to {env_path.name})", color))
        for line in render_settings(values, color):
            say(line)
        say("")
        say(_dim("  enter a number to change it, or press Enter to go back", color))
        choice = ask("> ").strip()
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(SETTINGS):
            say("  Not an option.")
            continue
        s = SETTINGS[int(choice) - 1]
        if s.help:
            say(_dim(f"  {s.help}", color))
        if s.choices:
            say(_dim("  options: " + ", ".join(s.choices), color))
        if s.kind in {"list", "text"}:
            say(_dim("  enter - to clear it", color))
        raw = ask(f"  {s.label}: ")
        if raw.strip() == "-" and s.kind in {"list", "text"}:
            raw = ""
            update_env_file(env_path, {s.key: ""})
            os.environ[s.key] = ""
            say(f"  Cleared: {s.label}")
            continue
        if raw.strip() == "":
            say("  Not changed.")
            continue
        try:
            value = normalize(s, raw)
        except ValueError as exc:
            say(f"  Not saved: {exc}.")
            continue
        update_env_file(env_path, {s.key: value})
        os.environ[s.key] = value
        say(f"  Saved: {s.label} = {display(s, value)}")


def _run_job(args: list[str]) -> None:
    subprocess.run([sys.executable, "-m", *args], check=False)


def send_test_email() -> str:
    from whale_agent.config import Settings
    from whale_agent.delivery.email import send_digest_email

    settings = Settings.from_env()
    if not settings.email_enabled:
        return "Email is off. Set 'Send the brief to' and the email sender first."
    result = send_digest_email(
        "This is a test email from Whale Agent. If you can read this, email delivery works.",
        settings=settings,
        subject="Whale Agent test email",
    )
    return str(result)


def _pause(color: bool) -> None:
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input(_dim("\n  Press Enter to go back ", color))


def _options_for(setting: Setting) -> list[str]:
    if setting.kind == "flag":
        return ["on", "off"]
    return list(setting.choices)


def settings_picker(env_path: Path, color: bool, key_source=None) -> None:
    from whale_agent.cli import picker

    rows = settings_rows()
    selectable = [s is not None or label == "Back" for label, s in rows]
    keys = {"key_source": key_source} if key_source else {}
    index = 0
    while True:
        values = read_env_values(env_path)
        chosen = picker.run_picker(
            lambda i: render_settings_screen(values, color, i),  # noqa: B023
            selectable,
            index,
            **keys,
        )
        if chosen is None or rows[chosen][0] == "Back":
            return
        index = chosen
        s = rows[chosen][1]
        assert s is not None
        current = current_value(s, values)
        if s.kind in {"flag", "choice"}:
            options = _options_for(s)
            shown = display(s, current)
            start = options.index(shown) if shown in options else 0
            pick = picker.run_picker(
                lambda i: render_options_screen(s, options, shown, color, i),  # noqa: B023
                [True] * len(options),
                start,
                **keys,
            )
            if pick is None:
                continue
            value = normalize(s, options[pick])
        else:
            sys.stdout.write("\033[H\033[2J")
            print(render_options_screen(s, [], "", color, -1))
            print("  now: " + display(s, current))
            if s.kind in {"list", "text"}:
                print(_dim("  type - to clear it, or press Enter to keep it", color))
            try:
                raw = input("  new value: ")
            except (EOFError, KeyboardInterrupt):
                continue
            if raw.strip() == "":
                continue
            if raw.strip() == "-" and s.kind in {"list", "text"}:
                value = ""
            else:
                try:
                    value = normalize(s, raw)
                except ValueError as exc:
                    print(f"  Not saved: {exc}.")
                    _pause(color)
                    continue
        update_env_file(env_path, {s.key: value})
        os.environ[s.key] = value


def interactive(env_path: Path, color: bool) -> int:
    """Full-screen app with the arrow-key picker (terminals that support it)."""
    from whale_agent.cli import picker

    sys.stdout.write(picker.ALT_SCREEN_ON)
    index = 0
    try:
        while True:
            values = read_env_values(env_path)
            chosen = picker.run_picker(
                lambda i: render_home(values, color, i),  # noqa: B023
                [True] * len(MENU),
                index,
            )
            if chosen is None or MENU[chosen][0] == "q":
                return 0
            index = chosen
            key = MENU[chosen][0]
            if key == "1":
                settings_picker(env_path, color)
                continue
            if key == "8":
                schedule_picker(color)
                continue
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.flush()
            _dispatch(key, env_path, print, color)
            _pause(color)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(picker.ALT_SCREEN_OFF)
        sys.stdout.flush()


def _dispatch(choice: str, env_path: Path, say: Callable[[str], None], color: bool) -> None:
    if choice == "2":
        _run_job(["whale_agent.jobs.digest_daily", "--demo"])
    elif choice == "3":
        _run_job(["whale_agent.jobs.digest_daily", "--dry-run"])
    elif choice == "4":
        _run_job(["whale_agent.jobs.digest_daily"])
    elif choice == "5":
        say("  " + send_test_email())
    elif choice == "6":
        _run_job(["whale_agent.jobs.digest_daily", "--show-config"])
    elif choice == "7":
        from whale_agent.cli.doctor import run_doctor
        from whale_agent.config import Settings

        run_doctor(Settings.from_env(), env_path=env_path, say=say, color=color)
    elif choice == "8":
        from whale_agent.cli import schedule

        schedule.main(["status"], say=say)
        say("  Turn it on or off with `whale schedule on|off [--time HH:MM]`.")
    elif choice == "9":
        from whale_agent.cli.main import preview_email

        say("  " + preview_email([]))


SCHEDULE_SETTING = Setting(
    "WHALE_SCHEDULE",
    "Schedule automatic emails",
    "choice",
    help="on: install the daily/weekly job (07:00); off: remove it; status: show it",
)


def schedule_picker(color: bool, key_source=None) -> None:
    from whale_agent.cli import picker, schedule

    keys = {"key_source": key_source} if key_source else {}
    options = ["on", "off", "status"]
    state = (
        "on"
        if schedule.installed(schedule.platform_kind(), schedule.run_command, Path.home())
        else "off"
    )
    pick = picker.run_picker(
        lambda i: render_options_screen(SCHEDULE_SETTING, options, state, color, i),
        [True] * len(options),
        options.index(state),
        **keys,
    )
    if pick is None:
        return
    sys.stdout.write("\033[H\033[2J")
    schedule.main([options[pick]])
    _pause(color)


def main(
    argv: list[str] | None = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    env_path: Path | None = None,
) -> int:
    env_path = env_path or Path(".env")
    color = use_color()
    from whale_agent.config import load_env_file

    load_env_file(str(env_path))
    from whale_agent.cli import picker

    if ask is input and say is print and picker.supported():
        return interactive(env_path, color)
    say("")
    say(render_banner(read_env_values(env_path), color))
    if not env_path.is_file():
        say(_dim("  No .env yet. Open Settings to create one.", color))
    while True:
        say("")
        for key, label in MENU:
            say(f"  {_blue(key, color)}  {label}")
        try:
            choice = ask("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0
        if choice == "1":
            settings_menu(env_path, ask, say, color)
        elif choice in {"2", "3", "4", "5", "6", "7", "8", "9"}:
            _dispatch(choice, env_path, say, color)
        elif choice in {"q", "quit", "exit"}:
            return 0
        else:
            say("  Not an option.")


if __name__ == "__main__":
    raise SystemExit(main())
