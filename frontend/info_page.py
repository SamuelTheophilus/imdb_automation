import os

from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

_COMING_SOON = os.getenv("COMING_SOON_MODE", "no").strip().lower() == "yes"

_PASS_THROUGH_PREFIXES = ("/_nicegui", "/uploads", "/favicon", "/info")

_STYLES = """<style>
@keyframes bounce {
    0%, 100% { transform: translateY(0); }
    50%       { transform: translateY(-10px); }
}
@keyframes fade-in {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
}

.info-overlay {
    position: fixed;
    inset: 0;
    background: #0f0f14;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    z-index: 9999;
}
.info-inner {
    text-align: center;
    animation: fade-in 0.5s ease both;
}
.info-icon {
    font-size: 3rem;
    display: block;
    margin-bottom: 1.5rem;
    animation: bounce 2.4s ease-in-out infinite;
}
.info-title {
    color: #e2e2f0;
    font-size: 1.75rem;
    font-weight: 700;
    margin: 0 0 0.4rem;
    letter-spacing: -0.02em;
}
.info-subtitle {
    color: #6b7280;
    font-size: 0.875rem;
    margin: 0 0 2rem;
}
.info-tag {
    display: inline-block;
    color: #9ca3af;
    font-size: 0.8rem;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
}
</style>"""

_INFO_HTML = """
<div class="info-overlay">
  <div class="info-inner">
    <span class="info-icon">🔎</span>
    <h1 class="info-title">IMDB AutoFill</h1>
    <p class="info-subtitle">AI-powered retail product cataloging</p>
    <span class="info-tag">Coming Soon</span>
  </div>
</div>
"""


@ui.page("/info")
def info_page() -> None:
    ui.dark_mode().enable()
    ui.add_head_html(_STYLES)
    ui.html(_INFO_HTML)


class _ComingSoonMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        is_passthrough = any(path.startswith(p) for p in _PASS_THROUGH_PREFIXES)
        is_websocket = request.headers.get("upgrade", "").lower() == "websocket"
        if not is_passthrough and not is_websocket:
            return RedirectResponse(url="/info", status_code=302)
        return await call_next(request)


def register_coming_soon() -> None:
    if _COMING_SOON:
        app.add_middleware(_ComingSoonMiddleware)
