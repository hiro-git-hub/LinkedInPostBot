"""`linkedin-bot linkedin-login`: OAuth-Login im Browser, Callback auf einem kurzlebigen lokalen Server."""

import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from linkedin_bot.config import AppConfig
from linkedin_bot.integrations.linkedin import LinkedInClient, LinkedInError, code_from_callback
from linkedin_bot.runtime import open_repo

LOGIN_TIMEOUT_SECONDS = 300


def wait_for_callback(host: str, port: int, path: str) -> dict[str, str]:
    """Blockiert, bis LinkedIn den Browser auf die Redirect-URL schickt, und liefert die Query-Parameter."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path != path:
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Fertig – du kannst dieses Fenster schließen.</h2>".encode())

        def log_message(self, *_):
            pass  # keine Request-Logs mit Auth-Code im Terminal

    deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
    with HTTPServer((host, port), Handler) as server:
        server.timeout = 1
        # Schleife statt einzelnem handle_request: Browser fragen z.B. zusätzlich /favicon.ico ab.
        while not result:
            if time.monotonic() > deadline:
                sys.exit("Keine Antwort von LinkedIn innerhalb von 5 Minuten – bitte erneut versuchen.")
            server.handle_request()
    return result


def linkedin_login(cfg: AppConfig) -> None:
    client = LinkedInClient(cfg.linkedin.api_version)
    redirect = urlsplit(client.redirect_uri)
    if redirect.hostname not in ("localhost", "127.0.0.1"):
        sys.exit(f"LINKEDIN_REDIRECT_URI muss für den lokalen Login auf localhost zeigen (ist: {client.redirect_uri}).")

    state = secrets.token_urlsafe(16)
    url = client.authorize_url(state)
    print(f"Öffne LinkedIn im Browser … Falls nichts passiert, diese URL öffnen:\n{url}\n")
    webbrowser.open(url)

    params = wait_for_callback(redirect.hostname, redirect.port or 80, redirect.path)
    try:
        auth = client.exchange_code(code_from_callback(params, state))
    except LinkedInError as exc:
        sys.exit(str(exc))
    with open_repo() as repo:
        repo.save_linkedin_auth(auth)
    print(f"✅ Angemeldet als {auth.name} ({auth.person_urn}), gültig bis {auth.expires_at:%d.%m.%Y}.")
