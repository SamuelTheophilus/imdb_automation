from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

from backend.auth import login, signup, verify_access_token


TOKEN_KEY = "access_token"


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


@contextmanager
def _auth_card(title: str):
    """Shared visual shell for login/signup forms."""
    ui.dark_mode().enable()
    with ui.column().classes("w-full h-screen items-center justify-center gap-4"):
        with ui.card().classes("w-96 p-6 gap-4"):
            ui.label(title).classes("text-xl font-medium")
            yield


@ui.page("/login")
def login_page():
    """Minimal username/password login page."""
    if current_user():
        ui.navigate.to("/")
        return

    with _auth_card("Log in"):
        username = ui.input("Username").classes("w-full")
        password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")

        def submit() -> None:
            ok, message, token = login(username.value or "", password.value or "")
            if not ok or not token:
                ui.notify(message, type="negative")
                return
            app.storage.user[TOKEN_KEY] = token
            ui.navigate.to("/")

        ui.button("Log in", on_click=submit).classes("w-full")
        ui.button("Create account", on_click=lambda: ui.navigate.to("/signup")).props("flat").classes("w-full")


@ui.page("/signup")
def signup_page():
    """Minimal username/password signup page."""
    if current_user():
        ui.navigate.to("/")
        return

    with _auth_card("Create account"):
        username = ui.input("Username").classes("w-full")
        password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")

        def submit() -> None:
            ok, message, token = signup(username.value or "", password.value or "")
            if not ok or not token:
                ui.notify(message, type="negative")
                return
            app.storage.user[TOKEN_KEY] = token
            ui.navigate.to("/")

        ui.button("Sign up", on_click=submit).classes("w-full")
        ui.button("Back to login", on_click=lambda: ui.navigate.to("/login")).props("flat").classes("w-full")
