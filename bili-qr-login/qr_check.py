#!/usr/bin/env python3
"""Check bilibili login state via CDP + same-origin nav API.

Exit 0 when logged in, 1 when not logged in. Prints LOGIN=true/false and mid.
"""
import json
import sys
import urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222/json"

try:
    tabs = json.load(urllib.request.urlopen(CDP_HTTP))
except Exception as e:
    print(f"CDP_UNREACHABLE:{e}")
    sys.exit(2)

tab = next((x for x in tabs if "bilibili.com" in x.get("url", "")), None)
if not tab:
    # any tab works for a same-origin fetch to api.bilibili.com only if it is
    # a bilibili page; otherwise navigate one in (create a fresh tab).
    ws0 = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=30)
    ws0.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://www.bilibili.com/"}}))
    import time
    time.sleep(5)
    ws0.close()
    tabs = json.load(urllib.request.urlopen(CDP_HTTP))
    tab = next((x for x in tabs if "bilibili.com" in x.get("url", "")), tabs[0])

ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)
js = ("fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include'})"
      ".then(r=>r.json()).then(j => j.data ? {login: j.data.isLogin, mid: j.data.mid, name: j.data.name || null} : null)"
      ".catch(e => ({login: null, err: String(e)}))")
ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 2:
        v = m.get("result", {}).get("result", {}).get("value")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        ws.close()
        v = v or {}
        if v.get("login") is None:
            print(f"CHECK_ERROR:{str(v)[:150]}")
            sys.exit(2)
        print(f"LOGIN={'true' if v.get('login') else 'false'}")
        if v.get("login"):
            print(f"MID={v.get('mid')}")
            print(f"NAME={v.get('name')}")
        sys.exit(0 if v.get("login") else 1)
