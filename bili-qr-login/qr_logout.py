#!/usr/bin/env python3
"""Log out of bilibili via the official UI menu (site's own JS handles csrf).

Flow: open user menu (avatar click) -> click 退出登录 -> verify via nav API.
Exit 0 = logged out, 1 = still logged in, 2 = error.
"""
import json
import sys
import time
import urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222/json"


def ev(ws, expr, mid):
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            r = m.get("result", {})
            if "exceptionDetails" in r:
                return "JS_ERR:" + str(r["exceptionDetails"])[:150]
            return r.get("result", {}).get("value")


def main():
    tabs = json.load(urllib.request.urlopen(CDP_HTTP))
    tab = next((x for x in tabs if "bilibili.com" in x.get("url", "")), None)
    if not tab:
        print("NO_BILIBILI_TAB")
        sys.exit(2)
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)

    # ensure we are on the www page (header user menu lives there)
    if "www.bilibili.com" not in tab.get("url", ""):
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://www.bilibili.com/"}}))
        time.sleep(6)

    r = ev(ws, "(() => { const a = document.querySelector('.header-avatar-wrap, [class*=header-avatar]');"
               " if (!a) return 'NO_AVATAR'; const el = a.querySelector('a') || a;"
               " ['mouseover','click'].forEach(t => el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));"
               " return 'opened'; })()", 2)
    time.sleep(1.5)
    r = ev(ws, "(() => { const it = Array.from(document.querySelectorAll('.logout-item, [class*=logout]'))"
               ".find(e => (e.textContent||'').includes('退出登录'));"
               " if (!it) return 'NO_LOGOUT_ITEM'; it.click(); return 'CLICKED_LOGOUT'; })()", 3)
    print("CLICK=" + str(r))
    if str(r) != "CLICKED_LOGOUT":
        ws.close()
        sys.exit(2)
    time.sleep(4)

    js = ("fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include'})"
          ".then(r=>r.json()).then(j => j.data ? j.data.isLogin : null)")
    v = ev(ws, js, 4)
    ws.close()
    if v is False:
        print("LOGOUT_OK")
        sys.exit(0)
    print(f"STILL_LOGGED_IN={v}")
    sys.exit(1)


if __name__ == "__main__":
    main()
