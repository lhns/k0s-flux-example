# Fake Matrix homeserver for the relay: records every message PUT, served back at GET /_recorded.
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOM = "!room:test"
messages = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/_recorded":
            return self.reply(messages)
        if self.path.endswith("/joined_rooms"):
            return self.reply({"joined_rooms": [ROOM]})
        self.reply({})

    def do_DELETE(self):
        messages.clear()
        self.reply({})

    def do_PUT(self):
        content = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        messages.append({k: content.get(k) for k in ("body", "formatted_body", "m.mentions")})
        self.reply({"event_id": "$e%d" % len(messages)})


ThreadingHTTPServer(("0.0.0.0", 8008), Handler).serve_forever()
