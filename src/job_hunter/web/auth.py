"""Google sign-in, by invitation only.

Google proves who someone is; the allowlist (users.Directory) decides whether they get in.
The allowlist is checked again on every request, so removing someone takes effect at once.
Without GOOGLE_CLIENT_ID the app runs in local mode: one user, no sign-in (localhost only).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from job_hunter.users import Directory, User, normalise

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
SESSION_DAYS = 30


@dataclass
class AuthConfig:
    admin_email: str = ""
    session_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    app_url: str = ""
    dev_email: str = "me@localhost"

    @property
    def local(self) -> bool:
        return not self.google_client_id

    @classmethod
    def from_env(cls, env: dict | None = None) -> AuthConfig:
        env = os.environ if env is None else env
        cfg = cls(
            admin_email=normalise(env.get("ADMIN_EMAIL", "")),
            session_secret=env.get("SESSION_SECRET", ""),
            google_client_id=env.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=env.get("GOOGLE_CLIENT_SECRET", ""),
            app_url=env.get("APP_URL", "").rstrip("/"),
        )
        if cfg.local and env.get("JOB_HUNTER_REQUIRE_AUTH") == "1":
            raise RuntimeError("this server needs Google sign-in: set GOOGLE_CLIENT_ID")
        if not cfg.local and not (cfg.admin_email and cfg.session_secret):
            raise RuntimeError("Google sign-in needs ADMIN_EMAIL and SESSION_SECRET as well")
        cfg.session_secret = cfg.session_secret or secrets.token_urlsafe(32)
        return cfg

    def directory(self, root) -> Directory:
        if self.local:
            return Directory(root, self.admin_email or self.dev_email, single_dir=root)
        return Directory(root, self.admin_email)


class LoginRequired(Exception):
    """Raised by current_user; turned into a redirect to the sign-in page."""


def make_oauth(cfg: AuthConfig):
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        "google",
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        server_metadata_url=GOOGLE_METADATA,
        client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
    )
    return oauth


def current_user(request: Request) -> User:
    state = request.app.state
    directory: Directory = state.directory
    if state.auth.local:
        return directory.users()[0]
    email = request.session.get("email", "")
    user = directory.get(email) if email else None
    if user is None:
        request.session.pop("email", None)
        raise LoginRequired()
    return user


def _redirect_uri(request: Request) -> str:
    base = request.app.state.auth.app_url
    return f"{base}/auth/callback" if base else str(request.url_for("auth_callback"))


def install_auth(app: FastAPI, render) -> None:
    @app.exception_handler(LoginRequired)
    async def _to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", name="login")
    def login(request: Request):
        if request.app.state.auth.local:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {})

    @app.get("/auth/google")
    async def auth_google(request: Request):
        return await request.app.state.oauth.google.authorize_redirect(
            request, _redirect_uri(request)
        )

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        try:
            token = await request.app.state.oauth.google.authorize_access_token(request)
        except Exception:  # noqa: BLE001 - authlib raises several error types
            return render(request, "login.html", {"error": "Sign-in was cancelled or failed."})
        info = token.get("userinfo") or {}
        email = normalise(info.get("email", ""))
        if not info.get("email_verified") or request.app.state.directory.get(email) is None:
            return render(request, "not_invited.html", {"email": email}, status_code=403)
        request.session["email"] = email
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
