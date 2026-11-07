# Security

InternshipSearch is a single-user app that runs on your own computer. This page
describes what it protects, what it defends against, and how.

## What's worth protecting

- **API keys** for Gemini and Adzuna, in `.env`
- **Your résumé profile** and search criteria, in `config.json`
- **Your application tracker**, labels and notes, in `matches.db`

All three are gitignored and never leave your computer, except that the profile and
posting text are sent to Gemini for scoring.

## Threats and mitigations

| Threat | Mitigation | Where |
|---|---|---|
| Another device on the same network (campus or café Wi-Fi) opens the app | The server only listens on `127.0.0.1`, and refuses to start on any other address because the app has no login | `gui/security.py` `require_loopback`, `app.py` |
| A website in your browser reaches the app through DNS rebinding | Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`; anything else gets 403 | `LocalOnlyMiddleware` |
| A website opens the app's live websocket (NiceGUI allows any origin by default) | Requests and websockets with another site's `Origin` are rejected before the connection is accepted | `LocalOnlyMiddleware` |
| Clickjacking: a site loads the app in a hidden frame | `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` | `SECURITY_HEADERS` |
| Hostile posting text (HTML or scripts in a title or snippet from Adzuna, or in Gemini's reasoning) | Outside text is only shown with `ui.label`, which escapes it, never with `ui.html` or `ui.markdown`. The CSP blocks connections, images and scripts from other sites, so injected markup couldn't send data anywhere | `gui/components.py`, CSP |
| Malicious links (`javascript:`, `data:`, `file:`) | Only plain `http(s)` URLs become links. Links open with `noopener noreferrer`, so the job site can't control the app's tab | `safe_url`, `external_link`, `open_in_new_tab` |
| API keys leaking into the UI or logs (Adzuna puts keys in the URL) | Text from errors and runs passes through `redact()`, which removes key values and `app_key=`-style query parameters. Unexpected errors show a short message in the UI; details go to the terminal | `redact`, `run_action` |
| Leaking which pages you use to third parties | No CDNs: fonts and all scripts are served by the app itself, and `Referrer-Policy: no-referrer` is set | `gui/static/fonts`, headers |
| SQL injection | Every value is passed as a query parameter. The few f-string SQL statements only insert fixed, hard-coded names | `db.py` |
| Bad input from the page | Values are validated on the server, not only by the browser: audit sample size 1–200, notes 1–5,000 characters, statuses and filters only from the known list (an unknown `?status=` falls back to Open), and a note can only be edited or deleted through the posting it belongs to | `gui/labeling_page.py`, `tracking.py`, `gui/tracker_page.py` |
| HTML in the tracker table | Cells use Quasar's default text rendering. The one custom cell passes the title as a `:label` binding, never `v-html` | `gui/tracker_page.py` |

Also off by default: FastAPI's `/docs` and `/openapi.json`, and NiceGUI's `on_air`
(which would publish the app on the internet).

## Known limits

- The CSP must allow `'unsafe-inline'` and `'unsafe-eval'` scripts, because NiceGUI's
  Vue front end needs them. Escaping all outside text is the main defense against
  script injection; the CSP limits the damage if escaping ever fails.
- `matches.db` isn't encrypted. Use full-disk encryption (BitLocker on Windows).
- Anything else running as your user account on your computer can read these files.
  That's outside this app's threat model.
- Remote access (e.g. from your phone) isn't supported. It would need a login with
  hashed passwords, and HTTPS, first.

## Testing

`tests/test_security.py` covers binding, the middleware (foreign Host, cross-site
Origin, non-local clients, websockets), link filtering and redaction.
`tests/test_ui.py` checks that a posting containing HTML and a `javascript:` link
is shown as plain text and never linked.

Dependencies: run `pip-audit` now and then, and turn on Dependabot alerts in the
GitHub repo's settings.
