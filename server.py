"""Jarri module store service: checks for and applies updates to the jarri-apps services.

Backend only. The module store panel in Jarri is a client of this service.

  GET  /status            -> {services: {name: {version, latest, updateAvailable, canUndo}}}
  POST /update/{name}     -> {ok, updated, version}
  POST /undo/{name}       -> {ok, version}

How it works: each service runs from its own folder, not from the git checkout.

  ~/.local/share/jarri-apps/<name>/current/server.py   runs now
  ~/.local/share/jarri-apps/<name>/previous/server.py  the version before the last update

Update copies the latest code from origin/main into the folder and makes it current.
Undo swaps current and previous. Git is only read, never changed.

Only the services in SERVICES can be updated. Binds to 127.0.0.1 and accepts requests
from the same allowed origins as the other services.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARRI_MODULE_STORE_PORT", "8785"))
REPO = os.path.expanduser(os.environ.get("JARRI_APPS_DIR", "~/projects/jarri-apps"))
INSTALL_ROOT = os.path.expanduser(os.environ.get("JARRI_INSTALL_ROOT", "~/.local/share/jarri-apps"))
SKIP_RESTART = os.environ.get("JARRI_SKIP_RESTART") == "1"  # for tests only
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
ACTION_PATH = re.compile(r"^/(update|undo)/([a-z-]+)$")


def git(*args, timeout=60):
    result = subprocess.run(
        ["git", "-C", REPO, *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git failed")
    return result.stdout.strip()


def latest_version(directory):
    return git("log", "-1", "--format=%h", "origin/main", "--", directory) or "unknown"


def service_dir(name):
    return os.path.join(INSTALL_ROOT, name)


def read_version(path):
    try:
        with open(path, encoding="utf-8") as file:
            return file.read().strip()
    except OSError:
        return "unknown"


def installed(name):
    base = service_dir(name)
    return read_version(os.path.join(base, "current.version"))


def can_undo(name):
    return os.path.isfile(os.path.join(service_dir(name), "previous", "server.py"))


def restart(unit):
    if not SKIP_RESTART:
        subprocess.run(["systemctl", "--user", "restart", unit], check=True, timeout=30)


def status():
    git("fetch", "--quiet", "origin", "main", timeout=30)
    services = {}
    for name, (directory, _unit) in SERVICES.items():
        version = installed(name)
        latest = latest_version(directory)
        services[name] = {
            "version": version,
            "latest": latest,
            "updateAvailable": version != latest,
            "canUndo": can_undo(name),
        }
    return {"services": services}


def update(name):
    directory, unit = SERVICES[name]
    git("fetch", "--quiet", "origin", "main", timeout=30)
    latest = latest_version(directory)
    base = service_dir(name)
    if installed(name) == latest:
        return {"ok": True, "updated": False, "version": latest}

    # Build the new version next to the current one, then swap it in.
    staging = os.path.join(base, "staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    code = git("show", f"origin/main:{directory}/server.py")
    with open(os.path.join(staging, "server.py"), "w", encoding="utf-8") as file:
        file.write(code + "\n")
    with open(os.path.join(staging, "version.txt"), "w", encoding="utf-8") as file:
        file.write(latest + "\n")

    previous = os.path.join(base, "previous")
    current = os.path.join(base, "current")
    shutil.rmtree(previous, ignore_errors=True)
    if os.path.isdir(current):
        os.rename(current, previous)
        shutil.copy(os.path.join(base, "current.version"), os.path.join(base, "previous.version"))
    os.rename(staging, current)
    with open(os.path.join(base, "current.version"), "w", encoding="utf-8") as file:
        file.write(latest + "\n")

    restart(unit)
    return {"ok": True, "updated": True, "version": latest}


def undo(name):
    _directory, unit = SERVICES[name]
    if not can_undo(name):
        raise RuntimeError("nothing to undo")
    base = service_dir(name)
    current = os.path.join(base, "current")
    previous = os.path.join(base, "previous")
    swap = os.path.join(base, "swap")

    # Swap the two folders. Nothing is deleted.
    shutil.rmtree(swap, ignore_errors=True)
    os.rename(current, swap)
    os.rename(previous, current)
    os.rename(swap, previous)

    current_version = os.path.join(base, "current.version")
    previous_version = os.path.join(base, "previous.version")
    tmp = os.path.join(base, "version.tmp")
    shutil.copy(current_version, tmp)
    shutil.copy(previous_version, current_version)
    shutil.move(tmp, previous_version)

    restart(unit)
    return {"ok": True, "version": installed(name)}


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
        match = ACTION_PATH.match(urlparse(self.path).path)
        if not match or match.group(2) not in SERVICES:
            return self._send(404, {"error": "unknown service"})
        action, name = match.group(1), match.group(2)
        try:
            result = update(name) if action == "update" else undo(name)
            self._send(200, result)
        except Exception as error:
            self._send(500, {"error": str(error)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"jarri module store service on http://{HOST}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
