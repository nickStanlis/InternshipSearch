"""The app frame: header, navigation and page routing.

ui.run(root=layout.root) builds this once per browser tab; ui.sub_pages swaps
the page content when you navigate, without reloading the frame.
"""
from __future__ import annotations

from typing import Callable

from nicegui import PageArguments, ui

from gui import dashboard, labeling_page, theme

# (path, nav label, page builder or None while the page isn't built yet)
PAGES: list[tuple[str, str, Callable[[PageArguments], None] | None]] = [
    ("/", "Home", dashboard.page),
    ("/label", "Labeling", labeling_page.page),
    ("/tracker", "Tracker", None),
    ("/review", "Review", None),
    ("/metrics", "Metrics", None),
    ("/runs", "Runs", None),
    ("/settings", "Settings", None),
]


def root() -> None:
    theme.apply()
    nav_links: dict[str, ui.link] = {}

    with ui.header().classes("app-header items-center px-4 py-2"):
        ui.button(icon="menu", on_click=lambda: drawer.toggle()) \
            .props("flat round dense color=white aria-label='Show or hide navigation'") \
            .classes("lt-md")
        ui.label("Internship Search").classes("t-h2")

    with ui.left_drawer(value=None, bordered=False).classes("app-drawer q-pa-md") \
            .props("width=208 breakpoint=1024 show-if-above") as drawer:
        with ui.element("nav").props('aria-label="Pages"').classes("column gap-1"):
            for path, label, builder in PAGES:
                if builder is None:
                    with ui.element("div").classes("nav-item soon").props('aria-disabled="true"'):
                        ui.label(label)
                        ui.label("Coming soon").classes("t-small")
                else:
                    nav_links[path] = ui.link(label, path).classes("nav-item")

    def mark_active(path: str) -> None:
        for p, link in nav_links.items():
            link.classes(add="active" if p == path else None, remove=None if p == path else "active")
            if p == path:
                link.props('aria-current="page"')
            else:
                link.props(remove="aria-current")

    def route(path: str, builder: Callable[[PageArguments], None]) -> Callable[[PageArguments], None]:
        def show(args: PageArguments) -> None:  # the annotation tells NiceGUI to pass the route info
            mark_active(path)
            builder(args)
        return show

    ui.sub_pages({path: route(path, builder) for path, _, builder in PAGES if builder})
