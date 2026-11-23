"""A stand-in Ollama server for browser tests: answers /api/chat with scripted JSON.

Tests set `FakeOllama.reply` (a function of the request body) and `FakeOllama.delay`.
It listens on 127.0.0.1 only and never reaches a real model.
"""

# json reads requests and writes answers; threading runs the server beside the tests.
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self):
        self.calls: list[dict] = []
        self.delay = 0.0
        self.down = False
        self.reply = lambda body: {"reply": "Keep it at a gentle simmer.", "action": "none"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server naming
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(body)
                if outer.down:          # Ollama stopped: the app should say so and keep working
                    self.send_response(503)
                    self.end_headers()
                    return
                if outer.delay:
                    time.sleep(outer.delay)
                content = outer.reply(body)
                text = content if isinstance(content, str) else json.dumps(content)
                data = json.dumps({"message": {"role": "assistant", "content": text}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"models": []}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
