"""Widgets shared by several pages.

Security rule for every page: text from outside the app (posting titles,
companies, snippets, Gemini reasoning, notes) is shown with ui.label, which
escapes HTML. Never pass it to ui.html or ui.markdown. Links go through
external_link(), which only links plain http(s) URLs.
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from nicegui import ui
from nicegui.events import KeyEventArguments

from labeling import LabelingError
from tracking import TrackingError
from gui.security import redact, safe_url

log = logging.getLogger("internship_ui")

# Errors from the services whose messages are written for the user.
USER_ERRORS = (LabelingError, TrackingError)


def run_action(fn: Callable[..., Any], *args, **kwargs) -> tuple[bool, Any]:
    """Call a service function from a button or key press. Returns (ok, result).

    Service errors are shown as a notification. Anything unexpected is logged to
    the terminal (with API keys scrubbed) and the page shows a short message
    instead of a stack trace.
    """
    try:
        return True, fn(*args, **kwargs)
    except USER_ERRORS as e:
        ui.notify(str(e), type="warning")
    except Exception:
        log.error(redact(traceback.format_exc()))
        ui.notify("That didn't work. The details are in the terminal window.", type="negative")
    return False, None


def open_in_new_tab(url: str | None) -> None:
    """Open a posting from a key press. 'noopener' stops the job site's page from
    controlling this tab (window.open doesn't add it by default, unlike links)."""
    href = safe_url(url)
    if href is None:
        ui.notify("This posting has no usable link.", type="warning")
        return
    ui.run_javascript(f"window.open({json.dumps(href)}, '_blank', 'noopener,noreferrer')")


def external_link(text: str, url: str | None) -> None:
    """A link that opens in a new tab, or plain text if the URL isn't a safe http(s) link."""
    href = safe_url(url)
    if href is None:
        ui.label("This posting has no usable link.").classes("t-soft")
        return
    ui.link(text, href, new_tab=True).props('rel="noopener noreferrer"')


def posting_card(title: str, company: str | None, location: str | None, snippet: str) -> None:
    """The posting as an index card. Every value is untrusted text, shown escaped."""
    with ui.element("div").classes("posting-card w-full"):
        ui.label(title or "(untitled posting)").classes("posting-title")
        where = ", ".join(x for x in (company, location) if x)
        if where:
            ui.label(where).classes("t-soft q-mt-xs")
        ui.label(snippet).classes("posting-snippet q-mt-md")


def keycap(key: str, text: str, on_click: Callable[[], Any], tone: str = "quiet") -> ui.button:
    """A button drawn as a keyboard key, so the shortcut is part of the button.

    tone: 'match', 'no-match' or 'quiet'.
    """
    color = {"match": "positive", "no-match": "negative"}.get(tone, "primary")
    with ui.button(on_click=on_click, color=color).props("flat no-caps unelevated") \
            .classes(f"keycap {tone}") as button:
        with ui.element("span").classes("keycap-key"):
            ui.label(key)
        ui.label(text).classes("keycap-text")
    button.props(f'aria-keyshortcuts="{key.lower()}"')
    return button


def shortcut_key(e: KeyEventArguments) -> str | None:
    """The plain letter pressed, or None for key-ups, repeats and Ctrl/Alt/Cmd combos
    (so browser shortcuts like Ctrl+U are left alone)."""
    if not e.action.keydown or e.action.repeat:
        return None
    if e.modifiers.ctrl or e.modifiers.meta or e.modifiers.alt:
        return None
    return str(e.key.name).lower()


def shortcuts(actions: dict[str, Callable[[], Any]]) -> ui.keyboard:
    """Page-wide single-letter shortcuts. Typing in a text box never triggers them."""
    def on_key(e: KeyEventArguments) -> None:
        action = actions.get(shortcut_key(e) or "")
        if action:
            action()
    # 'button' is left out of ignore so shortcuts keep working after a mouse click.
    return ui.keyboard(on_key=on_key, ignore=["input", "select", "textarea"])


def local_date(text: str | None) -> str:
    """A date as 'Nov 4, 2026'.

    Plain dates ('2026-11-04', written with date.today()) are already local. Full
    timestamps come from SQLite's CURRENT_TIMESTAMP, which is UTC, so they're
    converted to this computer's time zone first.
    """
    if not text:
        return ""
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return text
    if len(text) > 10:
        d = d.replace(tzinfo=timezone.utc).astimezone()
    return f"{d:%b} {d.day}, {d.year}"


def local_time(utc_text: str | None) -> str:
    """SQLite CURRENT_TIMESTAMP (UTC) shown in this computer's time zone, e.g. 'Nov 5, 9:14 PM'."""
    if not utc_text:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(utc_text).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return utc_text
    return f"{dt:%b} {dt.day}, {dt:%I:%M %p}".replace(" 0", " ")


def word(n: int, one: str, many: str | None = None) -> str:
    """'posting' or 'postings' to go with n."""
    return one if n == 1 else (many or one + "s")


def plural(n: int, one: str, many: str | None = None) -> str:
    """'1 posting', '3 postings'."""
    return f"{n} {word(n, one, many)}"
