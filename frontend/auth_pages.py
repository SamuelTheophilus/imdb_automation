from __future__ import annotations

from nicegui import app, ui

from backend.auth import login, signup, verify_access_token
from frontend.styles import STYLES

TOKEN_KEY = "access_token"

# ── Shared colours (Zen-inspired warm charcoal palette) ──────────────────────
_BG        = "background:#1a1816;"
_BG_GLOW   = ("background-image:radial-gradient(ellipse 600px 500px at 50% 38%,"
               " rgba(99,102,241,0.07) 0%, transparent 60%);")
_CARD_BG   = "background:#242220;"
_CARD_BORDER = "border:1px solid rgba(240,225,205,0.09);"
_CARD_SHADOW = ("box-shadow:0 32px 72px rgba(0,0,0,0.55),"
                " 0 0 0 1px rgba(240,225,205,0.03);")
_DIVIDER_STYLE = "height:1px; background:rgba(240,225,205,0.08); margin:22px 0 20px"
_TITLE_STYLE   = ("font-size:22px; font-weight:700; color:#f0ebe5;"
                  "letter-spacing:-0.5px; font-family:Inter,sans-serif; line-height:1.2")
_SUB_STYLE     = ("font-size:13px; color:#6b6560; margin-top:5px; margin-bottom:20px;"
                  "font-family:Inter,sans-serif")
_BRAND_STYLE   = ("font-size:13px; font-weight:600; color:#e8e3dc;"
                  "letter-spacing:-0.2px; font-family:Inter,sans-serif")
_BTN_STYLE     = ("height:44px; background:#6366f1; color:#fff; font-weight:600;"
                  "font-size:14px; border-radius:10px; letter-spacing:0.1px;"
                  "font-family:Inter,sans-serif; text-transform:none; margin-top:20px;"
                  "box-shadow:0 4px 14px rgba(99,102,241,0.28);")
_LINK_STYLE    = ("font-size:13px; color:#818cf8; font-family:Inter,sans-serif;"
                  "text-decoration:none")
_MUTED_STYLE   = "font-size:13px; color:#6b6560; font-family:Inter,sans-serif"
_ERROR_STYLE   = ("font-size:12px; color:#f87171; min-height:16px; margin-bottom:6px;"
                  "font-family:Inter,sans-serif")


def current_user() -> dict | None:
    """Return the logged-in user for the current browser session."""
    return verify_access_token(app.storage.user.get(TOKEN_KEY))


def require_user() -> dict | None:
    """Protect a page by redirecting anonymous users to the login page."""
    user = current_user()
    if not user:
        ui.navigate.to("/login")
        return None
    return user


def logout() -> None:
    """Clear the JWT from NiceGUI user storage and return to login."""
    app.storage.user.pop(TOKEN_KEY, None)
    ui.navigate.to("/login")


def _auth_base():
    ui.dark_mode().enable()
    ui.colors(
        primary="#6366f1",
        secondary="#818cf8",
        positive="#10b981",
        negative="#ef4444",
        warning="#f59e0b",
    )
    ui.add_head_html(STYLES)


@ui.page("/login")
def login_page():
    if current_user():
        ui.navigate.to("/")
        return

    _auth_base()

    with ui.column().classes("w-full h-screen items-center justify-center").style(
        _BG + _BG_GLOW
    ):
        with ui.column().style(
            f"width:400px; {_CARD_BG} {_CARD_BORDER} border-radius:20px;"
            f"padding:44px 40px; gap:0; align-items:stretch; {_CARD_SHADOW}"
        ):
            # Brand mark
            with ui.row().classes("items-center gap-2"):
                ui.label("⬡").style("color:#6366f1; font-size:1.3rem; line-height:1")
                ui.label("IMDB AutoFill").style(_BRAND_STYLE)

            ui.element("div").style(_DIVIDER_STYLE)

            ui.label("Welcome back").style(_TITLE_STYLE)
            ui.label("Sign in to continue to your workspace").style(_SUB_STYLE)

            error_label = ui.label("").style(_ERROR_STYLE)

            username = ui.input("Username").classes("w-full").props("dark outlined dense")
            ui.element("div").style("height:10px")
            password = (
                ui.input("Password", password=True, password_toggle_button=True)
                .classes("w-full")
                .props("dark outlined dense")
            )

            def submit() -> None:
                error_label.text = ""
                ok, message, token = login(username.value or "", password.value or "")
                if not ok or not token:
                    error_label.text = message
                    return
                app.storage.user[TOKEN_KEY] = token
                ui.navigate.to("/")

            username.on("keydown.enter", submit)
            password.on("keydown.enter", submit)

            ui.button("Sign in", on_click=submit).classes("w-full").style(_BTN_STYLE)

            with ui.row().classes("justify-center items-center gap-1").style("margin-top:16px"):
                ui.label("New here?").style(_MUTED_STYLE)
                ui.link("Create an account", "/signup").style(_LINK_STYLE)


@ui.page("/signup")
def signup_page():
    if current_user():
        ui.navigate.to("/")
        return

    _auth_base()

    with ui.column().classes("w-full h-screen items-center justify-center").style(
        _BG + _BG_GLOW
    ):
        with ui.column().style(
            f"width:400px; {_CARD_BG} {_CARD_BORDER} border-radius:20px;"
            f"padding:44px 40px; gap:0; align-items:stretch; {_CARD_SHADOW}"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.label("⬡").style("color:#6366f1; font-size:1.3rem; line-height:1")
                ui.label("IMDB AutoFill").style(_BRAND_STYLE)

            ui.element("div").style(_DIVIDER_STYLE)

            ui.label("Create your account").style(_TITLE_STYLE)
            ui.label("Get started — it only takes a moment").style(_SUB_STYLE)

            error_label = ui.label("").style(_ERROR_STYLE)

            username = ui.input("Username").classes("w-full").props("dark outlined dense")
            ui.element("div").style("height:10px")
            password = (
                ui.input("Password", password=True, password_toggle_button=True)
                .classes("w-full")
                .props("dark outlined dense")
            )

            def submit() -> None:
                error_label.text = ""
                ok, message, token = signup(username.value or "", password.value or "")
                if not ok or not token:
                    error_label.text = message
                    return
                app.storage.user[TOKEN_KEY] = token
                ui.navigate.to("/")

            username.on("keydown.enter", submit)
            password.on("keydown.enter", submit)

            ui.button("Create account", on_click=submit).classes("w-full").style(_BTN_STYLE)

            with ui.row().classes("justify-center items-center gap-1").style("margin-top:16px"):
                ui.label("Already have an account?").style(_MUTED_STYLE)
                ui.link("Sign in", "/login").style(_LINK_STYLE)
