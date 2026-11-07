"""Look and feel: colors, type and the few custom styles NiceGUI/Quasar don't cover.

Fonts are bundled in gui/static/fonts (SIL Open Font License) and served by the app
itself, so pages never load anything from the internet.
"""
from pathlib import Path

from nicegui import app, ui

STATIC_DIR = Path(__file__).resolve().parent / "static"

INK = "#1D2B3A"         # text, primary buttons
INK_SOFT = "#506072"    # secondary text
PAPER = "#EEF2F5"       # page background
CARD = "#FFFFFF"
RULE = "#D3DAE1"        # borders
MATCH = "#2C7A4B"       # "match" / Valid
NO_MATCH = "#9A2E4A"    # "not a match" / Invalid
SIGNAL = "#F0B43C"      # keyboard focus and the current step; used sparingly

CSS = f"""
@font-face {{ font-family: 'Atkinson Next'; font-weight: 400; font-display: swap;
  src: url('/static/fonts/atkinson-hyperlegible-next-latin-400-normal.woff2') format('woff2'); }}
@font-face {{ font-family: 'Atkinson Next'; font-weight: 700; font-display: swap;
  src: url('/static/fonts/atkinson-hyperlegible-next-latin-700-normal.woff2') format('woff2'); }}
@font-face {{ font-family: 'Atkinson Mono'; font-weight: 600; font-display: swap;
  src: url('/static/fonts/atkinson-hyperlegible-mono-latin-600-normal.woff2') format('woff2'); }}

:root {{
  --ink: {INK}; --ink-soft: {INK_SOFT}; --paper: {PAPER}; --card: {CARD}; --rule: {RULE};
  --match: {MATCH}; --no-match: {NO_MATCH}; --signal: {SIGNAL};
}}
body, .q-btn, .q-tab {{ font-family: 'Atkinson Next', 'Segoe UI', system-ui, sans-serif; }}
body {{ background: var(--paper); color: var(--ink); font-size: 16px; line-height: 1.5; }}
.nicegui-content {{ padding: 2rem 2.5rem; max-width: 64rem; }}

/* Type scale (1.25 ratio): 13 / 16 / 20 / 25 / 31 */
.t-title {{ font-size: 31px; line-height: 1.2; font-weight: 700; letter-spacing: -0.01em; }}
.t-h2 {{ font-size: 20px; line-height: 1.3; font-weight: 700; }}
.t-small {{ font-size: 13px; color: var(--ink-soft); }}
.t-soft {{ color: var(--ink-soft); }}
.measure {{ max-width: 68ch; }}

/* Keyboard focus is always visible */
:focus-visible, .q-btn:focus-visible, .q-tab:focus-visible {{
  outline: 3px solid var(--signal) !important; outline-offset: 2px; }}

/* Shell */
.app-header {{ background: var(--ink); color: #fff; }}
.app-drawer {{ background: var(--card); border-right: 1px solid var(--rule); }}
.nav-item {{ display: block; padding: .5rem .875rem; border-radius: 6px; color: var(--ink);
  text-decoration: none; }}
.nav-item:hover {{ background: var(--paper); }}
.nav-item.active {{ background: var(--paper); font-weight: 700; box-shadow: inset 3px 0 0 var(--ink); }}
.nav-item.soon {{ color: var(--ink-soft); cursor: default; }}
.nav-item.soon:hover {{ background: none; }}

.q-tab-panel {{ padding: 0; }}

/* Surfaces: one flat style, hierarchy comes from spacing and type */
.panel {{ background: var(--card); border: 1px solid var(--rule); border-radius: 8px; padding: 1.25rem 1.5rem; }}

/* Dashboard: rows of "what needs you" */
.todo-row {{ border-top: 1px solid var(--rule); padding: .875rem 0; }}
.todo-row:first-child {{ border-top: none; }}
.todo-count {{ font-size: 25px; font-weight: 700; min-width: 3.5rem; font-variant-numeric: tabular-nums; }}

/* Application stages: a real sequence, so it's drawn as one */
.stage {{ flex: 1 1 0; padding: .75rem 1rem; border-left: 1px solid var(--rule); }}
.stage:first-child {{ border-left: none; }}
.stage-count {{ font-size: 25px; font-weight: 700; font-variant-numeric: tabular-nums; }}

/* Labeling: the posting is an index card */
.posting-card {{ background: var(--card); border: 1px solid var(--rule); border-radius: 4px;
  padding: 1.5rem 1.75rem; box-shadow: 0 1px 0 var(--rule); }}
.posting-title {{ font-size: 25px; line-height: 1.25; font-weight: 700; }}
.posting-snippet {{ white-space: pre-wrap; overflow-wrap: anywhere; max-width: 68ch; }}

.step {{ padding: .25rem .75rem; border-radius: 999px; color: var(--ink-soft); border: 1px solid var(--rule); }}
.step.current {{ color: var(--ink); border-color: var(--ink); background: var(--card); font-weight: 700;
  box-shadow: 0 0 0 2px var(--signal); }}
.step.done {{ color: var(--ink-soft); text-decoration: line-through; }}

/* The keycaps: the one loud element in the app */
.keycap {{ border-radius: 10px !important; padding: .75rem 1.25rem !important; min-width: 12rem;
  border: 2px solid currentColor; border-bottom-width: 5px; background: var(--card) !important; }}
.keycap .q-btn__content {{ gap: .875rem; flex-wrap: nowrap; }}
.keycap-key {{ font-family: 'Atkinson Mono', ui-monospace, monospace; font-weight: 600; font-size: 20px;
  width: 2.25rem; height: 2.25rem; line-height: 2.25rem; text-align: center; border-radius: 6px;
  background: currentColor; }}
.keycap-key > * {{ color: #fff; }}
.keycap-text {{ font-size: 20px; font-weight: 700; }}
.keycap:active {{ border-bottom-width: 2px; transform: translateY(3px); }}
.keycap.quiet {{ min-width: 0; padding: .375rem .75rem !important;
  border-width: 1px; border-bottom-width: 3px; }}
.keycap.quiet .keycap-key {{ font-size: 15px; width: 1.625rem; height: 1.625rem; line-height: 1.625rem; }}
.keycap.quiet .keycap-text {{ font-size: 15px; font-weight: 400; }}

@media (max-width: 640px) {{
  .nicegui-content {{ padding: 1.25rem 1rem; }}
  .keycap {{ min-width: 0; flex: 1 1 auto; }}
  .stage {{ border-left: none; border-top: 1px solid var(--rule); }}
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ transition: none !important; animation: none !important; }}
  .keycap:active {{ transform: none; }}
}}
"""


def install_static_files() -> None:
    """Serve gui/static at /static. Call once, before ui.run."""
    app.add_static_files("/static", STATIC_DIR)


def apply() -> None:
    """Per page load: colors and styles."""
    ui.colors(primary=INK, secondary=INK_SOFT, accent=SIGNAL,
              positive=MATCH, negative=NO_MATCH, warning=SIGNAL)
    ui.add_css(CSS)
