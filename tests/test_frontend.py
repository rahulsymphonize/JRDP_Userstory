"""UI behaviour that the API tests can't see. Runs the Streamlit script headless."""
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")


class HtmlError(BaseHTTPRequestHandler):
    """A reverse proxy whose upstream is down: HTML instead of the API's JSON errors."""

    def do_GET(self):
        body = b"<html><body><h1>502 Bad Gateway</h1></body></html>"
        self.send_response(502)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_non_json_error_shows_message_not_traceback(monkeypatch):  # L7
    server = HTTPServer(("127.0.0.1", 0), HtmlError)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_AGENT_API", f"http://127.0.0.1:{server.server_port}/api/v1")
    try:
        at = AppTest.from_file(APP, default_timeout=30).run()
    finally:
        server.shutdown()
    assert not at.exception
    assert any("unexpected response" in e.value for e in at.error)


def test_backend_down_shows_one_message(monkeypatch):  # BR-6
    monkeypatch.setenv("LLM_AGENT_API", "http://127.0.0.1:9/api/v1")  # nothing listens on port 9
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.error[0].value.startswith("The backend is not reachable.")
    assert "http://127.0.0.1:9/api/v1" in at.error[0].value


def test_oidc_mode_asks_to_sign_in_before_calling_the_api(monkeypatch):
    monkeypatch.setenv("LLM_AGENT_AUTH", "oidc")
    monkeypatch.setenv("LLM_AGENT_API", "http://127.0.0.1:9/api/v1")  # never reached before sign-in
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["auth"] = {"redirect_uri": "http://localhost:8501/oauth2callback", "cookie_secret": "x" * 32,
                          "client_id": "c", "client_secret": "s", "expose_tokens": "id",
                          "server_metadata_url": "https://idp.example/.well-known/openid-configuration"}
    at.run()
    assert not at.exception
    assert not at.error
    assert [b.label for b in at.button] == ["Sign in"]
