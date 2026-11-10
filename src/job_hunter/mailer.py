"""Send each user their daily digest by email over SMTP (e.g. Gmail with an app password).

Configured from the environment: SMTP_HOST, SMTP_PORT (587 STARTTLS, 465 SSL), SMTP_USER,
SMTP_PASSWORD, SMTP_FROM (defaults to SMTP_USER) and APP_URL for links into the board.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger("job_hunter.mailer")


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""

    @classmethod
    def from_env(cls, env: dict | None = None) -> SmtpConfig | None:
        env = os.environ if env is None else env
        if not env.get("SMTP_HOST"):
            return None
        user = env.get("SMTP_USER", "")
        return cls(
            host=env["SMTP_HOST"],
            port=int(env.get("SMTP_PORT") or 587),
            user=user,
            password=env.get("SMTP_PASSWORD", ""),
            sender=env.get("SMTP_FROM") or user,
        )


def digest_message(sender: str, to: str, digest: str, app_url: str = "") -> EmailMessage:
    first = digest.splitlines()[0].lstrip("# ").strip() if digest else "Job digest"
    msg = EmailMessage()
    msg["Subject"] = first or "Job digest"
    msg["From"] = sender
    msg["To"] = to
    footer = f"\n\nOpen your board: {app_url.rstrip('/')}/\n" if app_url else ""
    msg.set_content(digest + footer)
    return msg


class Mailer:
    def __init__(self, config: SmtpConfig, smtp_factory=None):
        self.config = config
        self.smtp_factory = smtp_factory

    def _connect(self) -> smtplib.SMTP:
        c = self.config
        if self.smtp_factory:
            return self.smtp_factory(c.host, c.port)
        if c.port == 465:
            return smtplib.SMTP_SSL(c.host, c.port, timeout=30)
        smtp = smtplib.SMTP(c.host, c.port, timeout=30)
        smtp.starttls()
        return smtp

    def send(self, msg: EmailMessage) -> bool:
        """Deliver one message. Never raises: a mail problem must not fail the daily run."""
        try:
            with self._connect() as smtp:
                if self.config.user:
                    smtp.login(self.config.user, self.config.password)
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("sending digest to %s failed: %s", msg["To"], exc)
            return False
        return True

    def send_digest(self, to: str, digest: str, app_url: str = "") -> bool:
        return self.send(digest_message(self.config.sender, to, digest, app_url))
