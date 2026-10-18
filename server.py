"""Jarri module store service: checks for and applies updates to the jarri-apps services.

Backend only. The module store panel in Jarri is a client of this service.

  GET  /status            -> {services: {name: {version, latest, updateAvailable}}}
  POST /update/{name}     -> {ok, updated, version}

Only the services listed in SERVICES can be updated. Updates are fast-forward pulls
of origin/main followed by a restart of that one service. Nothing else is run.
Binds to 127.0.0.1 only and accepts requests from the same allowed origins as the other services.
"""

import json
import os
import re
import subprocess
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARRI_MODULE_STORE_PORT", "8785"))
REPO = os.path.expanduser(os.environ.get("JARRI_APPS_DIR", "~/projects/jarri-apps"))
SERVICES = {
    "terminal": ("terminal-service", "jarri-terminal"),
    "phone": ("phone-service", "jarri-phone"),
    "ai-usage": ("ai-usage-service", "jarri-ai-usage"),
    "claude-history": ("claude-history-service", "jarri-claude-history"),
}
ALLOWED_ORIGINS = {
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:8792",
    "http://127.0.0.1:8792",
}
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
UPDATE_PATH = re.compile(r"^/update/([a-z-]+)$")


def git(*args, timeout=60):
    result = subprocess.run(
        ["git", "-C", REPO, *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git failed")
    return result.stdout.strip()


def version_of(directory, ref="HEAD"):
    return git("log", "-1", "--format=%h", ref, "--", directory) or "unknown"


def status():
    git("fetch", "--quiet", "origin", "main", timeout=30)
    services = {}
    for name, (directory, _unit) in SERVICES.items():
        changed = git("diff", "--name-only", "HEAD", "origin/main", "--", directory)
        services[name] = {
            "version": version_of(directory),
            "latest": version_of(directory, "origin/main"),
            "updateAvailable": bool(changed),
        }
    return {"services": services}


def update(name):
    directory, unit = SERVICES[name]
    before = version_of(directory)
    git("pull", "--ff-only", "origin", "main")
    after = version_of(directory)
    if before == after:
        return {"ok": True, "updated": False, "version": after}
    subprocess.run(["systemctl", "--user", "restart", unit], check=True, timeout=30)
    return {"ok": True, "updated": True, "version": after}


class Handler(BaseHTTPRequestHandler):
    server_version = "jarri-module-store"

    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        return self.headers.get("Host") in ALLOWED_HOSTS

    def _send(self, status_code, body=None):
        self.send_response(status_code)
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        self._send(204)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        if urlparse(self.path).path != "/status":
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, status())
        except Exception as error:
            self._send(500, {"error": str(error)})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        match = UPDATE_PATH.match(urlparse(self.path).path)
        if not match or match.group(1) not in SERVICES:
            return self._send(404, {"error": "unknown service"})
        try:
            self._send(200, update(match.group(1)))
        except Exception as error:
            self._send(500, {"error": str(error)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"jarri module store service on http://{HOST}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
