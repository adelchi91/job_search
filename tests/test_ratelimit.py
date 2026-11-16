from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_hunter import ratelimit as rl

NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)

GROQ_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-requests": "998",
    "x-ratelimit-remaining-tokens": "7400",
    "x-ratelimit-reset-requests": "2m59.56s",
    "x-ratelimit-reset-tokens": "7.66s",
    "content-type": "application/json",
}


@pytest.mark.parametrize(
    ("text", "secs"),
    [("2m59.56s", 179.56), ("7.66s", 7.66), ("450ms", 0.45), ("1h2m", 3720), ("12", 12)],
)
def test_parse_duration(text, secs):
    assert rl.parse_duration(text) == pytest.approx(secs)


@pytest.mark.parametrize("text", ["", "soon", "2x", "3s later"])
def test_parse_duration_rejects_garbage(text):
    assert rl.parse_duration(text) is None


def test_groq_headers_requests_per_day_tokens_per_minute():
    state = rl.parse_rate_headers("groq", GROQ_HEADERS, NOW)
    day, minute = state.windows["requests/day"], state.windows["tokens/minute"]
    assert day == {"limit": 1000, "remaining": 998, "reset_at": "2026-11-16T14:02:59+00:00"}
    assert minute["limit"] == 8000 and minute["remaining"] == 7400
    assert state.retry_after is None and state.exhausted_until(NOW) is None


def test_generic_headers_and_retry_after():
    headers = {
        "x-ratelimitbysize-limit-minute": "500000",
        "x-ratelimitbysize-remaining-minute": "499000",
        "x-ratelimit-limit-req-month": "100",
        "x-ratelimit-remaining-req-month": "0",
        "ratelimit-reset": "bogus",
        "retry-after": "3",
    }
    state = rl.parse_rate_headers("mistral", headers, NOW)
    assert state.windows["tokens/minute"]["remaining"] == 499000
    assert state.windows["requests/month"]["remaining"] == 0
    assert state.retry_after == 3
    # remaining 0 on a month window with no reset known: blocked for a day
    assert state.exhausted_until(NOW) == NOW + timedelta(days=1)
    assert state.to_json()["retry_after"] == 3


def test_exhausted_ignores_a_window_that_resets_soon():
    soon = {"requests/day": {"remaining": 0, "reset_at": (NOW + timedelta(seconds=30)).isoformat()}}
    assert rl.RateState("groq", soon).exhausted_until(NOW) is None


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_limiter_requests_per_minute():
    clock = Clock()
    lim = rl.Limiter(rpm=2, clock=clock, sleep=clock.sleep)
    assert lim.acquire() == 0 and lim.acquire() == 0
    assert lim.acquire() == pytest.approx(60)


def test_limiter_requests_per_second_and_tokens_per_minute():
    clock = Clock()
    lim = rl.Limiter(rps=1, tpm=100, clock=clock, sleep=clock.sleep)
    lim.acquire(60)
    assert lim.acquire(30) == pytest.approx(1)  # 1 request per second
    lim.settle(10)  # the 30-token estimate really used 10
    clock.t = 5
    assert lim.acquire(30) == 0  # 60 + 10 + 30 fits in 100
    assert lim.acquire(1) == pytest.approx(55)  # waits for the 60 tokens of t=0 to expire
    assert lim.from_limits({"rpm": 3}).rpm == 3


def test_limiter_token_wait_drops_oldest_calls():
    clock = Clock()
    lim = rl.Limiter(tpm=100, clock=clock, sleep=clock.sleep)
    lim.acquire(80)
    clock.t = 10
    assert lim.acquire(50) == pytest.approx(50)  # waits until the 80 leave the window


def test_estimate_tokens():
    assert rl.estimate_tokens("") == 1 and rl.estimate_tokens("x" * 400) == 100
