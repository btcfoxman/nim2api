"""Use an authenticated native Chromium tab as the Nim Web API transport."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import websocket


BASE = "https://nim.video"
_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}


class BrowserError(Exception):
    pass


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"Nim CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int) -> dict[str, Any]:
    pages = [target for target in _targets(port) if target.get("type") == "page" and
             (urllib.parse.urlsplit(target.get("url") or "").hostname or "") == "nim.video"]
    pages.sort(key=lambda target: int("projectId=" in str(target.get("url") or "")), reverse=True)
    if pages:
        return pages[0]
    raise BrowserError("open and log in to nim.video in the account browser")


def browser_alive(port: int) -> bool:
    try:
        _page(port)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def start_managed_browser(account: dict[str, Any], *, chrome: str, profiles: Path) -> int:
    account_id = int(account["id"])
    port = 19300 + account_id
    profile = profiles / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960"]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = account_proxy_url(account)
    args.append("--proxy-server=" + proxy_url)
    args.append(BASE + "/create")
    with _PROCESS_LOCK:
        if browser_alive(port):
            return port
        existing = _PROCESSES.get(account_id)
        if existing and existing.poll() is None:
            existing.terminate()
            try:
                existing.wait(timeout=4)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=4)
        _remove_stale_profile_locks(profile)
        _PROCESSES[account_id] = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if browser_alive(port):
                return port
            time.sleep(.1)
        try:
            pages = [item for item in _targets(port) if item.get("type") == "page"]
            hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
            redirected = sorted(host for host in hosts if host and host != "nim.video")
            if redirected:
                raise BrowserError(f"Nim browser reached {redirected[0]}; configure an account proxy")
        except BrowserError as exc:
            if "configure an account proxy" in str(exc):
                raise
    raise BrowserError("managed Nim Chrome did not start")


def close_browser_port(port: int) -> None:
    """Ask a local CDP browser to exit, including one orphaned by a restart."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            endpoint = json.load(response).get("webSocketDebuggerUrl")
        if endpoint:
            ws = websocket.create_connection(endpoint, timeout=2, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                ws.close()
    except (OSError, ValueError, KeyError, websocket.WebSocketException):
        pass


def stop_managed_browser(account_id: int) -> None:
    with _PROCESS_LOCK:
        process = _PROCESSES.pop(account_id, None)
    close_browser_port(19300 + account_id)
    if process and process.poll() is None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def clear_managed_profile(account: dict[str, Any], profiles: Path) -> None:
    """Delete only the service-owned profile for this numeric account ID."""
    account_id = int(account["id"])
    port = int(account.get("cdp_port") or 0)
    if port == 19300 + account_id and browser_alive(port):
        try:
            CDPBrowser(port).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port):
                break
            time.sleep(.1)
        if browser_alive(port):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90) -> None:
        self.port = int(port)
        self.timeout = timeout

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                         suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"CDP {method} failed: {type(exc).__name__}") from exc
        finally:
            ws.close()

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression,
                                                   "awaitPromise": await_promise,
                                                   "returnByValue": True})
        if "exceptionDetails" in result:
            raise BrowserError("Nim browser JavaScript request failed")
        value = result.get("result") or {}
        if value.get("subtype") == "error":
            raise BrowserError("Nim browser JavaScript request returned an error")
        return value.get("value")

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Nim API path must be same-origin")
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        expression = (
            "(async()=>{const r=await fetch(" + json.dumps(path) + ","
            "{method:'POST',credentials:'include',headers:{'content-type':'application/json'},"
            "body:JSON.stringify(" + payload + ")});"
            "return JSON.stringify({http:r.status,body:await r.json()})})()"
        )
        value = self.evaluate(expression, await_promise=True)
        try:
            response = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrowserError("Nim returned an unreadable JSON response") from exc
        if int(response.get("http") or 0) >= 400:
            data = response.get("body") or {}
            raise BrowserRejected(f"Nim HTTP {response['http']}: {str(data.get('message') or data.get('error') or '')[:250]}")
        data = response.get("body")
        if not isinstance(data, dict):
            raise BrowserError("Nim returned an invalid JSON response")
        return data

    def get(self, path: str) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Nim API path must be same-origin")
        expression = "(async()=>{let r=await fetch(" + json.dumps(path) + ",{credentials:'include'});return {http:r.status,body:await r.json()}})()"
        result = self.evaluate(expression, await_promise=True)
        if not isinstance(result, dict):
            raise BrowserError("Nim returned an unreadable response")
        if int(result.get("http") or 0) >= 400:
            body = result.get("body") or {}
            raise BrowserRejected(f"Nim HTTP {result['http']}: {str(body.get('message') or body.get('error') or '')[:250]}")
        return result.get("body")

    def screenshot(self) -> str:
        result = self.command("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        if not result.get("data"):
            raise BrowserError("Nim Chrome returned no screenshot")
        return "data:image/png;base64," + result["data"]

    def click(self, x: float, y: float) -> None:
        for count, kind in enumerate(("mousePressed", "mouseReleased"), 1):
            self.command("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                      "button": "left", "clickCount": 1})

    def scroll(self, delta_y: float) -> None:
        self.command("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 700, "y": 500,
                                                  "deltaX": 0, "deltaY": delta_y})

    def cookies(self) -> list[dict[str, Any]]:
        return self.command("Network.getCookies", {"urls": [BASE + "/"]}).get("cookies") or []

    def project_id(self) -> str:
        # Nim creates a default project before the first workflow. New accounts
        # therefore have a project even while recentWorkflows is still empty.
        response = self.get("/api/projects/getLastProject")
        project = response.get("project") if isinstance(response, dict) else None
        if isinstance(project, dict) and project.get("id") and not project.get("isTrashbin"):
            return str(project["id"])
        workflows = self.get("/api/workflows/recentWorkflows")
        if isinstance(workflows, list):
            return next((str(item["projectId"]) for item in workflows
                         if isinstance(item, dict) and item.get("projectId")), "")
        return ""

    def upload_file(self, path: Path) -> dict[str, Any]:
        """Multipart upload through the account browser and its configured proxy."""
        if not path.is_file():
            raise BrowserError("local upload file is missing")
        if path.stat().st_size > 150 * 1024 * 1024:
            raise BrowserError("Nim upload exceeds 150 MiB")
        with _PROCESS_LOCK:
            lock = _UPLOAD_LOCKS.setdefault(self.port, threading.RLock())
        with lock:
            import mimetypes
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.evaluate("window.__nimUploadParts=[]")
            try:
                with path.open("rb") as source:
                    while chunk := source.read(512 * 1024):
                        encoded = base64.b64encode(chunk).decode("ascii")
                        self.evaluate("window.__nimUploadParts.push(Uint8Array.from(atob(" + json.dumps(encoded) + "),c=>c.charCodeAt(0)))")
                expression = """(async()=>{let f=new FormData();f.append('file',new Blob(window.__nimUploadParts,{type:%s}),%s);let r=await fetch('/api/files/upload',{method:'POST',credentials:'include',body:f});return {http:r.status,body:await r.json()}})()""" % (json.dumps(mime), json.dumps(path.name))
                result = self.evaluate(expression, await_promise=True)
            finally:
                try:
                    self.evaluate("delete window.__nimUploadParts")
                except BrowserError:
                    pass
            if not isinstance(result, dict):
                raise BrowserError("Nim upload returned no response")
            body = result.get("body") or {}
            if int(result.get("http") or 0) >= 400:
                raise BrowserRejected(f"Nim upload HTTP {result['http']}: {str(body.get('message') or '')[:250]}")
            if not isinstance(body.get("filePath"), str):
                raise BrowserError("Nim upload did not return filePath")
            return body
