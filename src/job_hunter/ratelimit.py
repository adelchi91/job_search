"""Stay inside free-tier LLM quotas: read rate-limit headers, throttle, retry, fail over.

Groq (console.groq.com/docs/rate-limits): `x-ratelimit-{limit,remaining,reset}-requests` are
per DAY, `x-ratelimit-{limit,remaining,reset}-tokens` per MINUTE, resets look like `2m59.56s`,
and `retry-after` (seconds) comes only with a 429. There is no tokens-per-day header.
Mistral publishes no header names; any `x-ratelimit*-{limit,remaining,reset}-<window>` is read.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from job_hunter import usage

log = logging.getLogger("job_hunter.ratelimit")

RETRY_DEFAULT_S = 2.0
RETRY_MAX_S = 30.0
BLOCK_MARGIN_S = 60
WINDOWS = ("second", "minute", "hour", "day", "month")
# Window of a header that does not name one, by provider and unit.
IMPLICIT_WINDOW = {"groq": {"requests": "day", "tokens": "minute"}}
FALLBACK_WINDOW = {"requests": "minute", "tokens": "minute"}
_HEADER = re.compile(r"^(?:x-)?ratelimit(bysize)?-(limit|remaining|reset)(?:-(.*))?$")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


class QuotaExceeded(RuntimeError):
    """No provider has quota left; the caller falls back to its template or heuristic."""


class RateLimited(RuntimeError):
    """A provider answered 429."""

    def __init__(self, provider: str, retry_after: float | None, state: RateState | None = None):
        super().__init__(f"{provider}: rate limited (retry after {retry_after}s)")
        self.provider, self.retry_after, self.state = provider, retry_after, state


def parse_duration(text: str) -> float | None:
    """`2m59.56s` -> 179.56, `7.66s`, `450ms`, `1h2m`, or a bare number of seconds."""
    text = (text or "").strip().lower()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _UNIT_S[u] for n, u in parts)


@dataclass
class RateState:
    """Latest rate-limit headers of one provider, keyed by "<unit>/<window>"."""

    provider: str
    windows: dict[str, dict] = field(default_factory=dict)
    retry_after: float | None = None

    def exhausted_until(self, now: datetime) -> datetime | None:
        """Reset time of a day/month window with nothing left, if that is more than 60 s away."""
        for key, win in self.windows.items():
            if not key.endswith(("/day", "/month")) or win.get("remaining") != 0:
                continue
            reset = win.get("reset_at")
            until = datetime.fromisoformat(reset) if reset else now + timedelta(days=1)
            if until > now + timedelta(seconds=BLOCK_MARGIN_S):
                return until
        return None

    def to_json(self) -> dict:
        return {"windows": self.windows, "retry_after": self.retry_after}


def _classify(name: str, provider: str) -> tuple[str, str] | None:
    """(kind, "<unit>/<window>") of a rate-limit header name, or None if it is not one."""
    m = _HEADER.match(name.lower())
    if not m:
        return None
    by_size, kind, rest = m.groups()
    parts = set((rest or "").split("-"))
    unit = "tokens" if by_size or parts & {"tokens", "token", "size"} else "requests"
    implicit = IMPLICIT_WINDOW.get(provider, FALLBACK_WINDOW)[unit]
    window = next((w for w in WINDOWS if w in parts), implicit)
    return kind, f"{unit}/{window}"


def parse_rate_headers(provider: str, headers, now: datetime | None = None) -> RateState:
    now = now or datetime.now(UTC)
    state = RateState(provider)
    for name, value in headers.items():
        if name.lower() == "retry-after":
            state.retry_after = parse_duration(value)
            continue
        found = _classify(name, provider)
        if not found:
            continue
        kind, key = found
        win = state.windows.setdefault(key, {})
        if kind == "reset":
            secs = parse_duration(value)
            if secs is not None:
                win["reset_at"] = (now + timedelta(seconds=secs)).isoformat(timespec="seconds")
        else:
            try:
                win[kind] = int(float(value))
            except ValueError:
                continue
    return state


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Limiter:
    """Sliding windows for requests per second / minute and tokens per minute.

    `acquire(tokens)` sleeps just long enough for the next call to stay under every cap.
    """

    def __init__(
        self,
        rpm: int | None = None,
        rps: int | None = None,
        tpm: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rpm, self.rps, self.tpm = rpm, rps, tpm
        self.clock, self.sleep = clock, sleep
        self.events: list[list[float]] = []  # [time, tokens]

    @classmethod
    def from_limits(cls, caps: dict, **kw) -> Limiter:
        return cls(rpm=caps.get("rpm"), rps=caps.get("rps"), tpm=caps.get("tpm"), **kw)

    def wait_time(self, tokens: int, now: float) -> float:
        waits = [0.0]
        if self.rps:
            waits.append(self._request_wait(now, 1.0, self.rps))
        if self.rpm:
            waits.append(self._request_wait(now, 60.0, self.rpm))
        if self.tpm:
            waits.append(self._token_wait(now, tokens))
        return max(waits)

    def _request_wait(self, now: float, window: float, cap: int) -> float:
        recent = [t for t, _ in self.events if t > now - window]
        if len(recent) < cap:
            return 0.0
        return recent[len(recent) - cap] + window - now

    def _token_wait(self, now: float, tokens: int) -> float:
        recent = [(t, n) for t, n in self.events if t > now - 60.0]
        used, wait = sum(n for _, n in recent), 0.0
        for t, n in recent:
            if used + tokens <= self.tpm:
                break
            used, wait = used - n, t + 60.0 - now
        return wait

    def acquire(self, tokens: int = 1) -> float:
        """Block until a call of `tokens` fits; returns the seconds slept."""
        slept = 0.0
        while (wait := self.wait_time(tokens, self.clock())) > 0:
            self.sleep(wait)
            slept += wait
        now = self.clock()
        self.events = [e for e in self.events if e[0] > now - 60.0]
        self.events.append([now, tokens])
        return slept

    def settle(self, tokens: int) -> None:
        """Replace the estimate of the last call with the tokens it really used."""
        if self.events and tokens:
            self.events[-1][1] = tokens


@dataclass
class Slot:
    """One provider behind the guard: its client, the model to ask, its limiter."""

    name: str
    llm: object  # has .json(prompt, *, model, system) and raises RateLimited on 429
    model: str
    limiter: Limiter
    blocked_until: datetime | None = None

    def blocked(self, now: datetime) -> bool:
        return self.blocked_until is not None and self.blocked_until > now

    def after_success(self, now: datetime) -> None:
        """Settle the token estimate; stop calling a provider whose daily quota just ran out."""
        self.limiter.settle(getattr(self.llm, "last_tokens", 0))
        state = getattr(self.llm, "last_state", None)
        until = state.exhausted_until(now) if state else None
        if until is not None:
            self.blocked_until = until

    def note(self, exc: RateLimited, now: datetime) -> None:
        until = exc.state.exhausted_until(now) if exc.state else None
        if until is None and (exc.retry_after or 0) > RETRY_MAX_S:
            until = now + timedelta(seconds=exc.retry_after)
        if until is not None:
            self.blocked_until = until


class QuotaGuard:
    """An LLM made of several providers: throttle each, retry a 429 once, then fail over.

    When every provider is rate limited it raises QuotaExceeded and tells the usage scope,
    so the job is marked "prepared without AI (quota reached)".
    """

    def __init__(
        self,
        slots: list[Slot],
        *,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.slots, self.sleep, self.wall = slots, sleep, wall
        self.fallbacks = 0

    def json(self, prompt: str, *, model: str, system: str = "") -> dict:
        for slot in self.slots:
            if slot.blocked(self.wall()):
                continue
            try:
                return self._call(slot, prompt, system)
            except RateLimited as exc:
                log.warning("%s out of quota, trying the next provider: %s", slot.name, exc)
        self.fallbacks += 1
        usage.note_quota_fallback()
        raise QuotaExceeded("every LLM provider is out of quota")

    def _call(self, slot: Slot, prompt: str, system: str) -> dict:
        estimate = estimate_tokens(system + prompt)
        for attempt in (1, 2):
            slot.limiter.acquire(estimate)
            try:
                out = slot.llm.json(prompt, model=slot.model, system=system)
            except RateLimited as exc:
                slot.note(exc, self.wall())
                if attempt == 2 or slot.blocked(self.wall()):
                    raise
                self.sleep(min(exc.retry_after or RETRY_DEFAULT_S, RETRY_MAX_S))
                continue
            slot.after_success(self.wall())
            return out
        raise AssertionError("unreachable")
