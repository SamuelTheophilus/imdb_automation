from __future__ import annotations

from nicegui import app, ui

from backend.auth import (
    change_password,
    consume_reset_code,
    login,
    request_password_reset,
    signup,
    verify_access_token,
)
from frontend.styles import STYLES

TOKEN_KEY = "access_token"

# ── Shared styles (reused by both forms) ────────────────────────────────────
_BTN_STYLE = (
    "height:44px; background:#6366f1; color:#fff; font-weight:600;"
    "font-size:14px; border-radius:10px; letter-spacing:0.1px;"
    "font-family:Inter,sans-serif; text-transform:none; margin-top:18px;"
    "box-shadow:0 4px 14px rgba(99,102,241,0.28);"
)
_ERROR_STYLE = (
    "font-size:12px; color:#f87171; min-height:16px; margin-bottom:4px;"
    "font-family:Inter,sans-serif;"
)
_LINK_STYLE = "font-size:13px; color:#818cf8; font-family:Inter,sans-serif; text-decoration:none"
_MUTED_STYLE = "font-size:13px; color:#6b6560; font-family:Inter,sans-serif"
_BRAND_STYLE = (
    "font-size:13px; font-weight:600; color:#e8e3dc;"
    "letter-spacing:-0.2px; font-family:Inter,sans-serif"
)


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


def _base():
    ui.dark_mode().enable()
    ui.colors(
        primary="#6366f1",
        secondary="#818cf8",
        positive="#10b981",
        negative="#ef4444",
        warning="#f59e0b",
    )
    ui.add_head_html(STYLES)


# ── Landing page ─────────────────────────────────────────────────────────────

def _landing_page(default_mode: str = "login") -> None:
    """Full-screen split landing page — hero left, auth form right."""
    if current_user():
        ui.navigate.to("/")
        return

    _base()

    with ui.element("div").classes("landing-root"):
        _render_hero()
        _render_form_panel(default_mode)


def _render_hero() -> None:
    with ui.element("div").classes("landing-hero"):
        with ui.element("div").classes("landing-hero-content"):
            # Title
            ui.html(
                '<p class="lp-title">'
                'IMDB Auto-Fill'
                '<span class="lp-title-line2">from Product Images</span>'
                '</p>'
            )

            # Subtitle
            ui.html(
                '<p class="lp-sub">'
                'Upload product photos from any angle and let the AI extract '
                'brand, weight, category, and 10+ fields automatically — '
                'grouped, reviewed, and export-ready in seconds.'
                '</p>'
            )

            # Feature list
            with ui.element("div").classes("lp-features"):
                for text in [
                    "AI-powered field extraction",
                    "Multi-angle image grouping",
                    "CSV and Excel export",
                ]:
                    with ui.element("div").classes("lp-feature"):
                        ui.element("span").classes("lp-feature-dot")
                        ui.html(f'<span>{text}</span>')


def _render_form_panel(default_mode: str) -> None:
    with ui.element("div").classes("landing-form-panel"):
        with ui.element("div").classes("lp-form-wrap"):

            # ── Tab toggle ───────────────────────────────────────────────────
            with ui.element("div").classes("lp-tabs"):
                with ui.element("div").classes(
                    "lp-tab" + (" lp-tab-active" if default_mode == "login" else "")
                ) as t_signin:
                    ui.html('<span style="pointer-events:none">Sign in</span>')

                with ui.element("div").classes(
                    "lp-tab" + (" lp-tab-active" if default_mode == "signup" else "")
                ) as t_signup:
                    ui.html('<span style="pointer-events:none">Create account</span>')

            # ── Sign-in form ─────────────────────────────────────────────────
            signin_section = ui.column().classes("w-full gap-0")
            signin_section.set_visibility(default_mode == "login")
            with signin_section:
                ui.html('<p class="lp-form-title">Welcome back</p>')
                ui.html('<p class="lp-form-sub">Sign in to continue to your workspace</p>')

                signin_error = ui.label("").style(_ERROR_STYLE)
                signin_user = ui.input("Username").classes("w-full").props("dark outlined dense")
                ui.element("div").style("height:10px")
                signin_pass = (
                    ui.input("Password", password=True, password_toggle_button=True)
                    .classes("w-full")
                    .props("dark outlined dense")
                )

                def do_signin() -> None:
                    signin_error.text = ""
                    ok, msg, token = login(signin_user.value or "", signin_pass.value or "")
                    if not ok or not token:
                        signin_error.text = msg
                        return
                    app.storage.user[TOKEN_KEY] = token
                    ui.navigate.to("/")

                signin_user.on("keydown.enter", do_signin)
                signin_pass.on("keydown.enter", do_signin)
                ui.button("Sign in", on_click=do_signin).classes("w-full").style(_BTN_STYLE)

                with ui.row().classes("justify-center").style("margin-top:14px"):
                    ui.link("Forgot password?", "/reset-password").style(_LINK_STYLE)

            # ── Sign-up form ─────────────────────────────────────────────────
            signup_section = ui.column().classes("w-full gap-0")
            signup_section.set_visibility(default_mode == "signup")
            with signup_section:
                ui.html('<p class="lp-form-title">Create your account</p>')
                ui.html('<p class="lp-form-sub">Get started — it only takes a moment</p>')

                signup_error = ui.label("").style(_ERROR_STYLE)
                signup_user = ui.input("Username").classes("w-full").props("dark outlined dense")
                ui.element("div").style("height:10px")
                signup_email = ui.input("Email address").classes("w-full").props("dark outlined dense type=email")
                ui.element("div").style("height:10px")
                signup_pass = (
                    ui.input("Password", password=True, password_toggle_button=True)
                    .classes("w-full")
                    .props("dark outlined dense")
                )

                def do_signup() -> None:
                    signup_error.text = ""
                    ok, msg, token = signup(
                        signup_user.value or "",
                        signup_pass.value or "",
                        signup_email.value or "",
                    )
                    if not ok or not token:
                        signup_error.text = msg
                        return
                    app.storage.user[TOKEN_KEY] = token
                    ui.navigate.to("/")

                signup_user.on("keydown.enter", do_signup)
                signup_pass.on("keydown.enter", do_signup)
                ui.button("Create account", on_click=do_signup).classes("w-full").style(_BTN_STYLE)

            # ── Wire up tab clicks (after both sections exist) ───────────────
            def _switch(mode: str) -> None:
                if mode == "login":
                    t_signin.classes("lp-tab-active")
                    t_signup.classes(remove="lp-tab-active")
                    signin_section.set_visibility(True)
                    signup_section.set_visibility(False)
                else:
                    t_signup.classes("lp-tab-active")
                    t_signin.classes(remove="lp-tab-active")
                    signin_section.set_visibility(False)
                    signup_section.set_visibility(True)

            t_signin.on("click", lambda: _switch("login"))
            t_signup.on("click", lambda: _switch("signup"))


# ── Routes ───────────────────────────────────────────────────────────────────

@ui.page("/login")
def login_page():
    _landing_page("login")


@ui.page("/signup")
def signup_page():
    _landing_page("signup")


@ui.page("/reset-password")
def reset_password_page():
    if current_user():
        ui.navigate.to("/")
        return

    _base()

    _CARD = (
        "width:420px; background:#242220; border:1px solid rgba(240,225,205,0.09);"
        "border-radius:20px; padding:44px 40px; gap:0; align-items:stretch;"
        "box-shadow:0 32px 72px rgba(0,0,0,0.55);"
    )
    _DIVIDER = "height:1px; background:rgba(240,225,205,0.08); margin:22px 0 20px"

    with ui.column().classes("w-full h-screen items-center justify-center").style(
        "background:#1a1816;"
        "background-image:radial-gradient(ellipse 600px 500px at 50% 38%,"
        " rgba(99,102,241,0.07) 0%, transparent 60%);"
    ):
        with ui.column().style(_CARD):
            ui.link("← Back to sign in", "/login").style(_LINK_STYLE)
            ui.element("div").style(_DIVIDER)
            ui.html('<p style="font-size:20px;font-weight:700;color:#e8e3dc;'
                    'font-family:Inter,sans-serif;letter-spacing:-0.4px;margin-bottom:6px">'
                    'Reset your password</p>')
            ui.html('<p style="font-size:13px;color:#52504c;font-family:Inter,sans-serif;'
                    'margin-bottom:20px;line-height:1.5">'
                    'Enter your username to receive a one-time reset code.</p>')

            step1_col = ui.column().classes("w-full gap-0")
            step2_col = ui.column().classes("w-full gap-0")
            step2_col.set_visibility(False)

            # ── Step 1: username ─────────────────────────────────────────────
            with step1_col:
                s1_error = ui.label("").style(_ERROR_STYLE)
                s1_user = ui.input("Username").classes("w-full").props("dark outlined dense")

                def do_request() -> None:
                    s1_error.text = ""
                    ok, err = request_password_reset(s1_user.value or "")
                    if not ok:
                        s1_error.text = err
                        return
                    step1_col.set_visibility(False)
                    step2_col.set_visibility(True)

                s1_user.on("keydown.enter", do_request)
                ui.button("Send reset code", on_click=do_request).classes("w-full").style(
                    _BTN_STYLE
                )

            # ── Step 2: enter code received by email + new password ──────────
            with step2_col:
                ui.html(
                    '<div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);'
                    'border-radius:10px;padding:14px 16px;margin-bottom:20px;">'
                    '<p style="margin:0;font-size:13px;color:#6ee7b7;font-family:Inter,sans-serif;'
                    'line-height:1.5">'
                    'Reset code sent. Check your email — it expires in 10 minutes.'
                    '</p></div>'
                )
                s2_error = ui.label("").style(_ERROR_STYLE)
                s2_code = ui.input("Reset code").classes("w-full").props("dark outlined dense")
                ui.element("div").style("height:10px")
                s2_pass = (
                    ui.input("New password", password=True, password_toggle_button=True)
                    .classes("w-full")
                    .props("dark outlined dense")
                )

                def do_reset() -> None:
                    s2_error.text = ""
                    ok, msg = consume_reset_code(s2_code.value or "", s2_pass.value or "")
                    if not ok:
                        s2_error.text = msg
                        return
                    ui.notify("Password updated — please sign in", type="positive", position="center")
                    ui.navigate.to("/login")

                s2_code.on("keydown.enter", do_reset)
                s2_pass.on("keydown.enter", do_reset)
                ui.button("Set new password", on_click=do_reset).classes("w-full").style(
                    _BTN_STYLE
                )


def render_change_password_dialog() -> None:
    """Create the change-password dialog for logged-in users.

    Call this once per page (e.g. from render_header). The dialog is opened
    programmatically via the button wired up in render_header.
    """
    user = current_user()
    if not user:
        return

    with ui.dialog() as dialog, ui.card().classes("p-8 gap-0").style("min-width:360px"):
        ui.html('<p style="font-size:18px;font-weight:700;color:#e8e3dc;'
                'font-family:Inter,sans-serif;letter-spacing:-0.3px;margin-bottom:4px">'
                'Change password</p>')
        ui.html('<p style="font-size:13px;color:#52504c;font-family:Inter,sans-serif;'
                'margin-bottom:20px">Enter your current password then choose a new one.</p>')

        err = ui.label("").style(_ERROR_STYLE)
        cur_pass = (
            ui.input("Current password", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("dark outlined dense")
        )
        ui.element("div").style("height:10px")
        new_pass = (
            ui.input("New password", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("dark outlined dense")
        )

        def submit() -> None:
            err.text = ""
            ok, msg = change_password(user["id"], cur_pass.value or "", new_pass.value or "")
            if not ok:
                err.text = msg
                return
            dialog.close()
            ui.notify("Password changed", type="positive", position="center")

        cur_pass.on("keydown.enter", submit)
        new_pass.on("keydown.enter", submit)

        with ui.row().classes("justify-end gap-2 w-full").style("margin-top:18px"):
            ui.button("Cancel", on_click=dialog.close).props("flat color=white").classes("text-xs")
            ui.button("Update", on_click=submit).props("unelevated color=indigo-5").style(
                "font-size:13px; font-weight:600; padding:0 20px; height:36px"
            )

    return dialog
