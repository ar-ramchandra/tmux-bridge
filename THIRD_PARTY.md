# Third-party components

Tmux Bridge uses existing terminal and networking libraries. It does not implement a new terminal emulator, cryptographic primitive, or multiplexer.

- [tmux](https://github.com/tmux/tmux) runs the persistent sessions. It is installed separately.
- [xterm.js](https://github.com/xtermjs/xterm.js) and its fit/web-links addons provide browser terminal rendering (MIT). Pinned assets load from jsDelivr with integrity checks.
- [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/Kludex/starlette), [Uvicorn](https://github.com/encode/uvicorn), [HTTPX](https://github.com/encode/httpx), [websockets](https://github.com/python-websockets/websockets), [cryptography](https://github.com/pyca/cryptography), and [psutil](https://github.com/giampaolo/psutil) are installed through Python dependencies. Their licenses remain their own.
- [cloudflared](https://github.com/cloudflare/cloudflared) is optional. Private node installers download a pinned official release and verify its digest. Its license is separate from this project's MIT license.
- [Playwright](https://github.com/microsoft/playwright) is a development dependency for browser tests.

No third-party project endorses or audits this gateway. Product names used for optional CLI integrations belong to their respective owners.
