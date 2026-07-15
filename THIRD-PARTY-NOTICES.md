# Third-party notices

Warpyard vendors its frontend dependencies (no CDN scripts by design). They remain
under their own licenses:

| Component | Path | License | Upstream |
|---|---|---|---|
| noVNC | `app/static/novnc/` | [MPL-2.0](https://github.com/novnc/noVNC/blob/master/LICENSE.txt) | https://github.com/novnc/noVNC |
| pako (noVNC vendored dep) | `app/static/novnc/vendor/pako/` | MIT & Zlib (see `LICENSE` in that dir) | https://github.com/nodeca/pako |
| htmx | `app/static/htmx.min.js` | [BSD-2-Clause](https://github.com/bigskysoftware/htmx/blob/master/LICENSE) | https://htmx.org |
| idiomorph | `app/static/idiomorph-ext.min.js` | [BSD-2-Clause](https://github.com/bigskysoftware/idiomorph/blob/main/LICENSE) | https://github.com/bigskysoftware/idiomorph |

`app/static/wycharts.js` and `app/static/favicon.svg` are original Warpyard code/assets
(AGPL-3.0, like the rest of the repo).

Python dependencies are declared in `requirements.txt` and installed from PyPI under
their respective licenses (FastAPI, SQLAlchemy, httpx, etc. — all permissive).
