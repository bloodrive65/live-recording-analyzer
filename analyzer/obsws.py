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
    """Read OBS's own WebSocket settings (fresh from disk) so the user doesn't have to type them."""
    from .obs_config import websocket_settings
    data = websocket_settings()
    if not data:
        return {"enabled": False, "port": 4455, "password": "", "found": False, "source": ""}
    try:
        port = int(data.get("server_port", 4455))
    except (TypeError, ValueError):
        port = 4455
    return {"enabled": bool(data.get("server_enabled", False)), "port": port,
            "password": data.get("server_password", "") if data.get("auth_required", True) else "",
            "found": True, "source": data.get("_source", "")}


def obs_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n in ("obs64.exe", "obs32.exe", "obs.exe", "obs"):
                return True
    except Exception:
        pass
    return False


def connection_status(port: int, password: str, host: str = "127.0.0.1") -> dict:
    """Work out, in plain terms, whether the app can talk to OBS right now.

    state: connected | obs_closed | ws_off | auth | port | error
    """
    import socket
    cfg = obs_ws_config()
    running = obs_running()
    open_ = False
    try:
        with socket.create_connection((host, port), timeout=0.6):
            open_ = True
    except OSError:
        open_ = False
    if not open_:
        if not running:
            return {"state": "obs_closed", "label": "OBS isn't running",
                    "detail": "Open OBS. The app connects automatically within a few seconds.", "cfg": cfg}
        if cfg["found"] and not cfg["enabled"]:
            return {"state": "ws_off", "label": "WebSocket is turned off in OBS",
                    "detail": "In OBS: Tools → WebSocket Server Settings → tick “Enable WebSocket server” → OK.", "cfg": cfg}
        if cfg["found"] and cfg["port"] != port:
            return {"state": "port", "label": f"OBS uses port {cfg['port']}, not {port}",
                    "detail": f"Change the port here to {cfg['port']}.", "cfg": cfg}
        return {"state": "ws_off", "label": "OBS is running but not accepting connections",
                "detail": "In OBS: Tools → WebSocket Server Settings → make sure “Enable WebSocket server” is ticked, then click OK. "
                          "If you just ticked it, click OK (not only Apply).", "cfg": cfg}
    try:
        c = ObsClient(host, port, password, timeout=2.5)
        c.connect()
        v = c.request("GetVersion")
        c.close()
        return {"state": "connected", "label": f"Connected to OBS {v.get('obsVersion', '')}".strip(),
                "detail": "Live skip tracking and the Pause/Resume buttons are available.", "cfg": cfg, "version": v.get("obsVersion", "")}
    except PermissionError:
        return {"state": "auth", "label": "OBS rejected the password",
                "detail": "In OBS: Tools → WebSocket Server Settings → Show Connect Info → copy the password and paste it here.", "cfg": cfg}
    except Exception as e:
        return {"state": "error", "label": "Couldn’t talk to OBS", "detail": str(e)[:160], "cfg": cfg}


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
