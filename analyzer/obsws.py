"""Minimal OBS WebSocket v5 client (built into OBS 28+)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


def obs_ws_config() -> dict:
    """Read OBS's own WebSocket settings so the user doesn't have to type them."""
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        os.path.join(appdata, "obs-studio", "plugin_config", "obs-websocket", "config.json"),
    ]
    for c in candidates:
        try:
            with open(c, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "enabled": bool(data.get("server_enabled", False)),
                "port": int(data.get("server_port", 4455)),
                "password": data.get("server_password", "") if data.get("auth_required", True) else "",
                "found": True,
            }
        except Exception:
            continue
    return {"enabled": False, "port": 4455, "password": "", "found": False}


class ObsClient:
    def __init__(self, host="127.0.0.1", port=4455, password="", timeout=3.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.ws = None
        self.lock = threading.Lock()
        self.events = []  # list of (eventType, data)

    def connect(self):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")
        self.ws = websocket.create_connection(f"ws://{self.host}:{self.port}", timeout=self.timeout)
        hello = json.loads(self.ws.recv())
        if hello.get("op") != 0:
            raise RuntimeError("Unexpected reply from OBS")
        ident = {"rpcVersion": 1, "eventSubscriptions": (1 << 6) | (1 << 0)}  # Outputs + General
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self.password:
                raise PermissionError("OBS WebSocket needs a password")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self.ws.send(json.dumps({"op": 1, "d": ident}))
        try:
            reply = json.loads(self.ws.recv())
        except Exception:
            raise PermissionError("OBS rejected the WebSocket password")
        if reply.get("op") != 2:
            raise PermissionError("OBS rejected the WebSocket password")
        return reply.get("d", {})

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        finally:
            self.ws = None

    def request(self, rtype: str, data: dict | None = None) -> dict:
        with self.lock:
            rid = uuid.uuid4().hex
            msg = {"op": 6, "d": {"requestType": rtype, "requestId": rid}}
            if data:
                msg["d"]["requestData"] = data
            self.ws.send(json.dumps(msg))
            while True:
                m = json.loads(self.ws.recv())
                if m.get("op") == 7 and m["d"].get("requestId") == rid:
                    st = m["d"].get("requestStatus", {})
                    if not st.get("result"):
                        raise RuntimeError(st.get("comment") or f"{rtype} failed")
                    return m["d"].get("responseData", {}) or {}
                if m.get("op") == 5:
                    self.events.append((m["d"].get("eventType"), m["d"].get("eventData", {})))

    def pop_events(self):
        ev, self.events = self.events, []
        return ev
