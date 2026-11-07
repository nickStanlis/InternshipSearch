"""Start the Internship Search UI:  python app.py

Opens http://127.0.0.1:8080 in your browser. The server only listens on this
computer; see gui/security.py for what that protects against.

Options:
  --port 8081   use another port
  --dev         reload the app when a .py file is saved (don't use while a run is going:
                a reload restarts the server and stops the run)
  --no-browser  don't open a browser tab
  --db FILE     use another database file (e.g. a demo copy for screenshots)
"""
from __future__ import annotations

import argparse
import logging
import sys

from nicegui import app, ui

from gui import data, layout, theme
from gui.security import DEFAULT_PORT, HOST, InsecureBindError, LocalOnlyMiddleware, require_loopback


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internship Search UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dev", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", help="database file (default: matches.db)")
    # NiceGUI's reloader re-runs this file with its own argv; ignore what we don't know.
    args, _ = parser.parse_known_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    return args


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        require_loopback(HOST)
    except InsecureBindError as e:
        sys.exit(str(e))

    if args.db:
        data.use_database(args.db)
    app.add_middleware(LocalOnlyMiddleware, port=args.port)
    theme.install_static_files()
    ui.run(
        root=layout.root,
        host=HOST,
        port=args.port,
        title="Internship Search",
        reload=args.dev,
        show=not args.no_browser,
        show_welcome_message=True,
        fastapi_docs=False,              # no /docs or /openapi.json
        endpoint_documentation="none",
        # Never set on_air: it publishes the app on the internet through NiceGUI's relay.
    )


if __name__ in {"__main__", "__mp_main__"}:  # __mp_main__: NiceGUI's reloader process
    main(sys.argv[1:])
