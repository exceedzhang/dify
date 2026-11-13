---
name: bili-qr-login
description: B站（bilibili）二维码登录确定性脚本集。通过 CDP 直驱沙箱内浏览器 + bilibili passport 登录 API（generate/poll/nav），无需 UI 点击即可完成：登录态检查、二维码生成与上传、扫码等待与自动完成登录、登出。当任务需要 B 站登录、显示 B 站二维码、或确认 B 站登录状态时，先 pull 本技能再执行其中脚本。
---

# bili-qr-login

确定性 B 站二维码登录脚本集。脚本只依赖 Python 标准库 + `websocket` + `qrcode` + `pillow`（沙箱缺时先 `pip install --target ./deps qrcode pillow`，调用加 `PYTHONPATH=./deps`）。

## 用法

脚本由运行时自动物化到工作目录的 `.dify_conf/skills/bili-qr-login/`（在 run 工作区根目录下执行）：

```bash
S=.dify_conf/skills/bili-qr-login
```

1. `python3 $S/qr_check.py` — 查登录态。输出 `LOGIN=true/false`（+ MID/NAME）。exit 0=已登录，1=未登录。
2. `python3 $S/qr_gen.py` — 生成新二维码：调 bilibili `generate` API 取 `qrcode_key`，本地渲染 `./qr.png`（当前工作目录），并尝试 `dify-agent file upload`（在 agent shell 中成功，输出 `DOWNLOAD_URL=` 即平台交付链接，并输出可直接粘贴的 `MD_LINE=![B站登录二维码](<url>)` 一行）。若输出 `UPLOAD_FAILED/UPLOAD_SKIPPED`，则手动 `dify-agent file upload qr.png` 取 `public_download_url`，自己拼出 `![B站登录二维码](<url>)`。
3. 把脚本输出里的 `MD_LINE=` 那行的**值原样**粘贴进给用户的回复（不要改写 URL，不要粘贴 base64，不要在沙箱内验证该链接）。
4. `python3 $S/qr_wait.py <QR_KEY> 290` — 轮询 bilibili `poll` 端点等待用户扫码（每个周期同时并行查 `nav` 登录态：即使 `/poll` 被风控返回 86038，只要浏览器实际已登录就立即报 `OK`）：
   - `OK mid=... name=...`（exit 0）→ 登录成功，浏览器已自动完成登录
   - `SCANNED_WAITING_CONFIRM`（exit 1）→ 已扫码待手机确认，提醒用户
   - `WAITING_FOR_SCAN`（exit 6，超时）→ 未扫码，提醒用户再扫
   - `QR_EXPIRED`（exit 3）→ 二维码过期，回第 2 步生成新码
   - `CDP_UNREACHABLE`（exit 5）→ 浏览器不可达，如实说明
5. `python3 $S/qr_logout.py` — 需要重置为未登录态时使用（UI 登出）。

## 原理

- 沙箱内 Chromium 的 CDP 端口 127.0.0.1:9222 可直接访问，脚本用 `Runtime.evaluate` 同源 fetch 调 bilibili passport API，不依赖 UI 选择器。
- `generate`：`GET passport.bilibili.com/x/passport-login/web/qrcode/generate` → `data.url`（扫码 URL）+ `data.qrcode_key`。二维码图片由 `qrcode` 库本地渲染。
- `poll`（非 check）：`GET .../qrcode/poll?qrcode_key=` 状态码 86101=未扫码 / 86091=已扫待确认 / 86092=成功（返回跳转 URL，脚本自动导航完成登录）/ 86090=过期。
- 登录态检测用 `nav` API（`data.isLogin`），不受风控限制。
