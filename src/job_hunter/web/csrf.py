"""CSRF protection: one random token per session, required on every state-changing request.

Pages render it as a hidden form field (`{{ csrf_field() }}`); scripts may send it in the
X-CSRF-Token header instead. A POST without a matching token is refused with 403.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request
from jinja2 import pass_context
from markupsafe import Markup, escape

FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def token(request: Request) -> str:
    """The session's token, created on first use."""
    value = request.session.get(FIELD)
    if not value:
        value = secrets.token_urlsafe(32)
        request.session[FIELD] = value
    return value


async def _sent(request: Request) -> str:
    header = request.headers.get(HEADER, "")
    if header:
        return header
    if "form" not in request.headers.get("content-type", ""):
        return ""
    form = await request.form()  # Starlette caches it, so the route can still read it
    return str(form.get(FIELD, ""))


async def verify_csrf(request: Request) -> None:
    """App-wide dependency: unsafe methods need the session's token."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(FIELD, "")
    sent = await _sent(request)
    if not expected or not hmac.compare_digest(sent.encode(), str(expected).encode()):
        raise HTTPException(403, "missing or invalid CSRF token: reload the page and try again")


@pass_context
def csrf_field(ctx) -> Markup:
    value = token(ctx["request"])
    return Markup(f'<input type="hidden" name="{FIELD}" value="{escape(value)}">')


@pass_context
def csrf_token(ctx) -> str:
    return token(ctx["request"])
