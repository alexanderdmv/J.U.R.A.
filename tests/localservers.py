"""Локальные HTTP-серверы для тестов коннекторов (настоящая сеть, но на этой машине)."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


def make_auth_server(user: str, password: str, redirect_to: str | None = None):
    """Сервер: /ok требует Basic user:password; /redir отвечает 302 на другой адрес. Запоминает заголовки."""
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    seen: list[str | None] = []

    class H(_Quiet):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            if self.path == "/redir" and redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
            elif self.headers.get("Authorization") == expected:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="t"')
                self.end_headers()

    return _start(H), seen


def make_recording_server():
    seen: list[str | None] = []

    class H(_Quiet):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    return _start(H), seen


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True).start()
    return server


def stop(server) -> None:
    server.shutdown()
    server.server_close()


def url(server, path="/ok") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def make_llm_server(kind: str, *, reply: str = "ок", status: int = 200, error_body: dict | None = None,
                    response: dict | None = None, models: tuple[str, ...] = ("model-a", "model-b"),
                    redirect_to: str | None = None):
    """Сервер, отвечающий в формате провайдера (kind: anthropic | openai | gemini). Запоминает каждый запрос.

    Форматы взяты из документации провайдеров. Возвращает (server, seen), где seen — список
    {method, path, headers (в нижнем регистре), body}."""
    seen: list[dict] = []

    def post_response() -> dict:
        if response is not None:
            return response
        if kind == "anthropic":
            return {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": reply}], "usage": {"input_tokens": 12, "output_tokens": 7}}
        if kind == "openai":
            return {"choices": [{"message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5}}
        return {"candidates": [{"content": {"parts": [{"text": reply}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}}

    def list_response() -> dict:
        if kind == "gemini":
            return {"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]} for m in models]
                    + [{"name": "models/embedder", "supportedGenerationMethods": ["embedContent"]}]}
        return {"data": [{"id": m} for m in models]}

    class H(_Quiet):
        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            seen.append({"method": method, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": json.loads(raw) if raw else None})
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            payload = (error_body or {"error": {"message": "ошибка"}}) if status != 200 else (
                list_response() if method == "GET" else post_response())
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self._handle("POST")

        def do_GET(self):
            self._handle("GET")

    return _start(H), seen


def base(server, suffix="") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{suffix}"
