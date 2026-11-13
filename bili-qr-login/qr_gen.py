#!/usr/bin/env python3
"""Deterministic bilibili QR-login step 1: generate QR code + upload.

Uses CDP (127.0.0.1:9222) + same-origin fetch inside the browser to call
bilibili's passport API (no UI clicking, no LLM in the loop).
Writes the QR PNG to ./qr.png in the current working directory (the run
workspace), then uploads it with the agent CLI (inherits Agent Stub env from
the shell job) and prints the result.
"""
import json
import subprocess
import sys
import urllib.request

import qrcode
import websocket

CDP_HTTP = "http://127.0.0.1:9222/json"
PNG_PATH = "qr.png"


def cdp_eval(ws, expr, mid):
    ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
    }))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            r = m.get("result", {})
            if "exceptionDetails" in r:
                raise RuntimeError(str(r["exceptionDetails"])[:200])
            return r.get("result", {}).get("value")


def main():
    tabs = json.load(urllib.request.urlopen(CDP_HTTP))
    tab = next((x for x in tabs if "bilibili.com" in x.get("url", "")), None)
    if not tab:
        print("NO_BILIBILI_TAB tabs=" + ",".join(x.get("url", "?")[:60] for x in tabs))
        sys.exit(5)

    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)
    js = ("fetch('https://passport.bilibili.com/x/passport-login/web/qrcode/generate',"
          " {credentials:'include'})"
          ".then(r => r.json()).catch(e => ({code: -1, message: String(e)}))")
    gen = cdp_eval(ws, js, 3)
    if isinstance(gen, str):
        gen = json.loads(gen)
    ws.close()

    if gen.get("code") != 0 or not gen.get("data"):
        print("GENERATE_FAILED:" + json.dumps(gen)[:200])
        sys.exit(1)

    data = gen["data"]
    key = data["qrcode_key"]
    scan_url = data["url"]
    img = qrcode.make(scan_url)
    img.save(PNG_PATH)

    print(f"QR_KEY={key}")
    print(f"SCAN_URL={scan_url}")
    print(f"PNG_PATH={PNG_PATH}")

    # Upload via the agent CLI (works when run inside an agent shell job,
    # which has the Agent Stub env injected by the runtime).
    try:
        out = subprocess.run(["dify-agent", "file", "upload", PNG_PATH],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            print("UPLOAD_FAILED:" + (out.stderr or out.stdout)[:200].strip())
            sys.exit(1)
        up = json.loads(out.stdout[out.stdout.find("{"):])
        url = up.get("public_download_url") or up.get("download_url") or up.get("url")
        print(f"PNG_BYTES={up.get('size', len(open(PNG_PATH, 'rb').read()))}")
        if url:
            print(f"DOWNLOAD_URL={url}")
            print(f"MD_LINE=![B站登录二维码]({url})")
        else:
            print("DOWNLOAD_URL_MISSING raw=" + out.stdout[:200])
    except Exception as e:  # env not injected (e.g. manual docker exec)
        print("UPLOAD_SKIPPED:" + str(e)[:120])


if __name__ == "__main__":
    main()
