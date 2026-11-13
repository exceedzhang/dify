#!/usr/bin/env python3
"""Poll bilibili QR login status until the user scans + confirms.

Uses the `poll` endpoint (not rate-limited, unlike `check`).
Every cycle also runs a parallel nav API check: if the browser is already
logged in (e.g. the web client finished the login on its own while the /poll
channel is rate-limited and returns a bogus 86038), report OK immediately.
On success (status 86092) it navigates the CDP tab to the returned url so the
browser receives the session cookies, then verifies via the nav API.

Usage: python3 qr_wait.py <qrcode_key> [max_seconds=300]
Exit codes: 0 logged in, 3 qr expired, 4 api error, 5 cdp unreachable, 6 timeout
"""
import json
import sys
import time
import urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222/json"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key="
NAV = "https://api.bilibili.com/x/web-interface/nav"

# bilibili QR poll status codes
NOT_SCANNED = 86101
SCANNED = 86091
SUCCESS = 86092
EXPIRED = 86090


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    if not key:
        print("USAGE: qr_wait.py <qrcode_key> [max_seconds]")
        sys.exit(4)
    max_s = int(sys.argv[2]) if len(sys.argv) > 2 else 300

    try:
        tabs = json.load(urllib.request.urlopen(CDP_HTTP))
    except Exception as e:
        print(f"CDP_UNREACHABLE:{e}")
        sys.exit(5)
    tab = next((x for x in tabs if "bilibili.com" in x.get("url", "")), tabs[0])
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)

    def ev(expr, mid):
        ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == mid:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return "JS_ERR:" + str(r["exceptionDetails"])[:150]
                return r.get("result", {}).get("value")

    def nav_state(m):
        jsn = ("fetch('" + NAV + "', {credentials:'include'})"
               ".then(r=>r.json()).then(j => j.data ? {login: j.data.isLogin, mid: j.data.mid, name: j.data.name || null} : null)")
        return ev(jsn, m)

    deadline = time.time() + max_s
    mid = 0
    while time.time() < deadline:
        mid += 1
        # Parallel nav check: /poll may report a bogus 86038 under risk
        # control while the browser is actually already logged in.
        st = nav_state(mid)
        if isinstance(st, dict) and st.get("login"):
            print(f"OK mid={st.get('mid')} name={st.get('name')} (via nav parallel check)")
            ws.close()
            sys.exit(0)
        mid += 1
        js = ("(async () => { const r = await fetch('" + POLL + key + "', {credentials:'include'});"
              " const j = await r.json(); return JSON.stringify(j); })()")
        v = ev(js, mid)
        try:
            j = json.loads(v) if isinstance(v, str) else v
        except Exception:
            print("PARSE_ERR=" + str(v)[:120])
            sys.exit(4)
        if j.get("code") != 0:
            print("API_ERR=" + json.dumps(j, ensure_ascii=False)[:150])
            sys.exit(4)
        d = j.get("data", {})
        sc = d.get("code")
        if sc == SUCCESS:
            url = d.get("url") or ""
            print("SCANNED_AND_CONFIRMED")
            if url:
                ws.send(json.dumps({"id": mid + 1, "method": "Page.navigate", "params": {"url": url}}))
                time.sleep(6)
            js2 = ("fetch('" + NAV + "', {credentials:'include'})"
                   ".then(r=>r.json()).then(j => j.data ? {login: j.data.isLogin, mid: j.data.mid, name: j.data.name || null} : null)")
            st = ev(js2, mid + 2)
            print("POST=" + json.dumps(st, ensure_ascii=False)[:150])
            if isinstance(st, dict) and st.get("login"):
                print(f"OK mid={st.get('mid')} name={st.get('name')}")
                ws.close()
                sys.exit(0)
            print("NAV_STILL_LOGGED_OUT")
            ws.close()
            sys.exit(2)
        elif sc == EXPIRED:
            print("QR_EXPIRED")
            ws.close()
            sys.exit(3)
        elif sc == SCANNED:
            print("SCANNED_WAITING_CONFIRM")
        elif sc == NOT_SCANNED:
            print("WAITING_FOR_SCAN")
        else:
            print(f"UNKNOWN_STATUS={sc} msg={d.get('message')}")
        time.sleep(5)

    print("TIMEOUT waiting for scan")
    ws.close()
    sys.exit(6)


if __name__ == "__main__":
    main()
