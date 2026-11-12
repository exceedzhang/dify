"""Local mock of the Enterprise backend service for e2e runs (stdlib only).

Serves the minimal enterprise API surface the web-app QR login flow needs:
- GET  /info                      license/feature info (EnterpriseService.get_info)
- GET  /webapp/access-mode(/id)   per-app webapp access mode (default private_all)
- POST /webapp/access-mode        update per-app webapp access mode
- GET  /webapp/permission         single visitor permission check (always allowed)
- POST /webapp/permission/batch   batch visitor permission check (always allowed)

Usage: python3 e2e/scripts/mock_enterprise.py [port]   (default 5060)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCESS_MODES: dict[str, str] = {}
DEFAULT_ACCESS_MODE = (
    "private_all"  # == UI ORGANIZATION, the mode the QR panel renders for
)


class MockEnterpriseHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path.rstrip("/")
        if path == "/info":
            self._send(
                200,
                {
                    "License": {
                        "status": "active",
                        "expiredAt": "2027-12-31T00:00:00Z",
                        "workspaces": {"enabled": False, "limit": 0, "size": 0},
                    },
                    "EnableEmailPasswordLogin": True,
                    "Branding": {"enabled": False},
                },
            )
        elif path in ("/webapp/access-mode", "/webapp/access-mode/id"):
            app_id = qs.get("appId") or qs.get("id") or ""
            self._send(
                200, {"accessMode": ACCESS_MODES.get(app_id, DEFAULT_ACCESS_MODE)}
            )
        elif path == "/webapp/permission":
            self._send(200, {"result": True})
        elif path in ("/sso/workspace/last-update-time", "/sso/app/last-update-time"):
            # Body is a JSON-encoded ISO-8601 string; parsed via datetime.fromisoformat upstream.
            self._send_raw(200, json.dumps("1970-01-01T00:00:00+00:00").encode())
        else:
            self._send(404, {"error": f"unknown path {path}"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path.rstrip("/")
        if path in ("/webapp/access-mode", "/webapp/app/access-mode"):
            app_id = str(body.get("appId") or "")
            ACCESS_MODES[app_id] = body.get("accessMode", DEFAULT_ACCESS_MODE)
            self._send(200, {"result": True})
        elif path == "/webapp/permission/batch":
            self._send(200, {"permissions": {a: True for a in body.get("appIds", [])}})
        else:
            self._send(404, {"error": f"unknown path {path}"})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[mock-enterprise] {fmt % args}", flush=True)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5060
    print(f"[mock-enterprise] listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), MockEnterpriseHandler).serve_forever()


if __name__ == "__main__":
    main()
