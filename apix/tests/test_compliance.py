"""
Compliance — constitution §2.

Two halves, and both matter:

  BEHAVIOURAL  the robots.txt gate fails closed, and a refusal happens *before*
               any request is sent rather than after a rejection comes back.
  STATIC       the evasion machinery a scraper would need in order to ignore a
               refusal is not present in this codebase, and a test fails the
               moment someone adds it.

The static half is the one that survives contact with a deadline. Anyone can
write "we do not bypass CAPTCHAs" in a README at 3 a.m.; this makes the claim
checkable, and makes adding a bypass a visibly deliberate act rather than a
quiet commit.

Nothing here touches the network. `apix compliance` does that, on demand,
against the live files.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from apix.config.settings import settings
from apix.scrape import base as scrape_base
from apix.scrape.base import RobotsGate, StatusCode, TokenBucket
from apix.scrape.live import REGISTRY
from apix.scrape.live.airindiaexpress import AirIndiaExpressSource
from apix.scrape.live.akasa import AkasaAirSource
from tests.conftest import ANCHOR

PACKAGE = Path(__file__).resolve().parent.parent / "apix"
TRAVEL = ANCHOR + timedelta(days=30)


# ─────────────────────────────────────────────────────────────────────────────
# robots.txt gate
# ─────────────────────────────────────────────────────────────────────────────
def _fake_robots(monkeypatch, *, text: str = "", status: int = 200, raises=None):
    """Stub httpx.get so the gate is tested without a network round trip."""
    calls: list[str] = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if raises is not None:
            raise raises
        return httpx.Response(status, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr(scrape_base.httpx, "get", fake_get)
    return calls


def test_a_disallow_rule_is_obeyed(monkeypatch):
    _fake_robots(monkeypatch, text="User-agent: *\nDisallow: /flight-availability\n")
    decision = RobotsGate().check("https://x.invalid/flight-availability?o=DEL")

    assert decision.allowed is False
    assert "DISALLOWS" in decision.reason
    assert decision.robots_url == "https://x.invalid/robots.txt", (
        "the reason must cite the file it was read from, so the claim is checkable"
    )


def test_an_allow_all_file_permits_the_fetch(monkeypatch):
    _fake_robots(monkeypatch, text="User-agent: *\nDisallow:\n")
    assert RobotsGate().check("https://x.invalid/booking/select-flight").allowed is True


def test_a_rule_aimed_at_another_agent_does_not_apply(monkeypatch):
    """Obeying someone else's Disallow would be superstition, not compliance."""
    _fake_robots(
        monkeypatch,
        text="User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin\n",
    )
    gate = RobotsGate(user_agent="AeroIndex-Research/0.1")
    assert gate.check("https://x.invalid/search").allowed is True
    assert gate.check("https://x.invalid/admin/panel").allowed is False


def test_a_missing_robots_file_means_no_restriction(monkeypatch):
    """RFC 9309: 404 is 'nothing expressed', which is not the same as 'refused'."""
    _fake_robots(monkeypatch, status=404)
    assert RobotsGate().check("https://x.invalid/search").allowed is True


@pytest.mark.parametrize("kwargs", [
    {"status": 500},
    {"status": 403},
    {"raises": httpx.ConnectError("dns failure")},
    {"raises": httpx.ReadTimeout("too slow")},
])
def test_the_gate_fails_closed_when_permission_cannot_be_read(monkeypatch, kwargs):
    """
    Unknown is treated as refused.

    The alternative — scraping when we could not read the policy — means the
    one time the file is unreachable is the one time we ignore it. That is
    exactly the situation the rule exists for.
    """
    _fake_robots(monkeypatch, **kwargs)
    decision = RobotsGate().check("https://x.invalid/search")

    assert decision.allowed is False
    assert "fail closed" in decision.reason.lower()


def test_robots_is_fetched_once_per_origin(monkeypatch):
    """Re-reading robots.txt for every URL would itself be impolite."""
    calls = _fake_robots(monkeypatch, text="User-agent: *\nDisallow:\n")
    gate = RobotsGate()
    for path in ("/a", "/b", "/c"):
        gate.check(f"https://x.invalid{path}")

    assert calls == ["https://x.invalid/robots.txt"]


# ─────────────────────────────────────────────────────────────────────────────
# The refusal happens before the request
# ─────────────────────────────────────────────────────────────────────────────
def test_a_refused_adapter_never_reaches_the_browser(monkeypatch):
    """
    The order of operations is the whole claim.

    A booby-trapped Playwright is installed: if `search()` ever gets as far as
    launching a browser on a disallowed path, the fake raises and this test
    fails. Checking compliance *after* fetching would be no compliance at all.
    """
    monkeypatch.setattr(
        AkasaAirSource, "check_compliance",
        lambda self, o, d, t: scrape_base.ComplianceDecision(
            False, "robots.txt DISALLOWS this path", "https://x.invalid/robots.txt"
        ),
    )

    trap = ModuleType("playwright.async_api")

    def _boom(*_a, **_kw):
        raise AssertionError("the compliance gate was bypassed — a request was attempted")

    trap.async_playwright = _boom
    monkeypatch.setitem(__import__("sys").modules, "playwright.async_api", trap)

    outcome = asyncio.run(AkasaAirSource().search("DEL", "BOM", TRAVEL))

    assert outcome.status is StatusCode.COMPLIANCE_REFUSED
    assert outcome.quotes == []


def test_air_india_express_refuses_and_says_no_request_was_sent(monkeypatch):
    """
    A real site, a real published rule, a real refusal.

    This adapter exists precisely so the compliance gate has something to
    refuse that is not a mock. Its robots.txt disallows the fare-search path,
    so the honest outcome is zero data from it.
    """
    _fake_robots(monkeypatch, text="User-agent: *\nDisallow: /flight-availability\n")
    outcome = asyncio.run(AirIndiaExpressSource().search("DEL", "BOM", TRAVEL))

    assert outcome.status is StatusCode.COMPLIANCE_REFUSED
    assert outcome.quotes == []
    assert "no request was sent" in outcome.note


def test_the_refusal_demonstrator_is_not_a_production_source():
    """
    It is evidence, not capability.

    `airindiaexpress.py` stays in the tree because the test above needs a real
    carrier with a real published Disallow. Being importable must never mean
    being selectable: if it appeared in REGISTRY, `APIX_LIVE_SOURCES` could
    name it and a cycle would spend its time collecting refusals.
    """
    assert "airindiaexpress" not in REGISTRY
    assert AirIndiaExpressSource not in REGISTRY.values()


def test_a_permitted_path_is_not_faked_into_working(monkeypatch):
    """
    If Air India Express lifted the rule tomorrow, the adapter must admit it
    has no fetch logic rather than return something.

    ADAPTER_UNAVAILABLE with an explanation; not EMPTY_NO_INVENTORY, which
    would be a claim about their seats rather than about our code.
    """
    _fake_robots(monkeypatch, text="User-agent: *\nDisallow:\n")
    outcome = asyncio.run(AirIndiaExpressSource().search("DEL", "BOM", TRAVEL))

    assert outcome.status is StatusCode.ADAPTER_UNAVAILABLE
    assert "no fetch logic is implemented" in outcome.note


def test_the_snapshot_in_the_adapter_is_documentation_not_a_decision(monkeypatch):
    """
    The committed robots.txt snapshot must never be what the gate consults.

    Here the live file allows everything while the snapshot still says
    Disallow. The gate must follow the live file — the operator's current
    decision, not our stale copy of it.
    """
    _fake_robots(monkeypatch, text="User-agent: *\nDisallow:\n")
    src = AirIndiaExpressSource()

    assert "Disallow: /flight-availability" in (
        __import__("apix.scrape.live.airindiaexpress", fromlist=["_ROBOTS_SNAPSHOT"])
        ._ROBOTS_SNAPSHOT
    )
    assert src.check_compliance("DEL", "BOM", TRAVEL).allowed is True


# ─────────────────────────────────────────────────────────────────────────────
# Identifying ourselves truthfully
# ─────────────────────────────────────────────────────────────────────────────
def test_the_user_agent_identifies_the_project_and_a_contact():
    """
    An operator who wants us to stop must be able to find out who we are.

    A UA that impersonates a consumer browser is the first step of evasion, so
    it is asserted against by name.
    """
    ua = settings.scraper_user_agent
    assert "AeroIndex" in ua
    assert "contact:" in ua, "an operator needs somewhere to send a complaint"

    lowered = ua.lower()
    for masquerade in ("mozilla/", "applewebkit", "chrome/", "safari/", "gecko"):
        assert masquerade not in lowered, (
            f"the user agent impersonates a browser ({masquerade!r}); "
            "identify the crawler truthfully"
        )


def test_the_same_user_agent_is_used_for_robots_and_for_fetching():
    """
    Reading robots.txt as one agent and fetching as another would make the
    permission check meaningless — we would be asking about someone else.
    """
    assert RobotsGate().user_agent == settings.scraper_user_agent


# ─────────────────────────────────────────────────────────────────────────────
# Politeness primitives
# ─────────────────────────────────────────────────────────────────────────────
def test_the_token_bucket_spaces_requests_out():
    bucket = TokenBucket(rate_per_min=6)
    assert bucket._interval == pytest.approx(10.0)
    assert TokenBucket(rate_per_min=0).rate_per_min >= 1, (
        "a misconfigured rate must not become an unthrottled loop"
    )


def test_backoff_is_capped_and_never_negative():
    """Exponential backoff without a cap is a denial-of-service on a slow host."""
    import inspect

    sig = inspect.signature(scrape_base.backoff_sleep)
    cap = sig.parameters["cap"].default
    base = sig.parameters["base"].default
    assert cap <= 60.0, "a backoff longer than a minute stalls the cycle instead of easing off"
    for attempt in range(12):
        assert 0.0 < min(cap, base * 2**attempt) <= cap


# ─────────────────────────────────────────────────────────────────────────────
# Static: the evasion toolkit is absent
# ─────────────────────────────────────────────────────────────────────────────
#: Libraries whose entire purpose is defeating bot detection or solving
#: challenges. Importing any of them is the anti-requirement, in code.
BANNED_IMPORTS = {
    "playwright_stealth",
    "selenium_stealth",
    "undetected_chromedriver",
    "undetected_playwright",
    "twocaptcha",
    "capsolver",
    "anticaptchaofficial",
    "python_anticaptcha",
    "deathbycaptcha",
    "rotating_proxies",
    "fake_useragent",
    "cloudscraper",
    "curl_cffi",
    "tls_client",
}

#: Keyword arguments that would route traffic through a rotating identity.
BANNED_KWARGS = {"proxy", "proxies"}


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_no_module_imports_an_evasion_library():
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0].replace("-", "_").lower()
                if root in BANNED_IMPORTS:
                    hits.append(f"{name} (line {node.lineno})")
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        f"anti-detection libraries imported: {offenders}. Constitution §2 forbids "
        "CAPTCHA bypass, headless evasion and proxy rotation. On a block we stop."
    )


def test_no_call_routes_traffic_through_a_proxy():
    """
    `proxy=` / `proxies=` is how IP rotation actually looks in a diff.

    Grepping for the word "proxy" would flag prose; this flags the keyword
    argument, which is the thing that would change behaviour.
    """
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = [
            f"{kw.arg}= (line {node.lineno})"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg in BANNED_KWARGS
        ]
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        f"proxy configuration found: {offenders}. IP rotation is an "
        "anti-requirement — a block is a stop signal, not an obstacle."
    )


#: The predicates an adapter may use to conclude "we are being challenged".
#: `looks_challenged` is Akasa's hardened, vendor-marker-based check;
#: `looks_blocked` is the generic one in base.py. Both are listed because a
#: guard that stops being scanned stops being a guard — when the Akasa adapter
#: moved from one to the other, this test found zero call sites and would have
#: passed vacuously if it did not assert `checked >= 1`.
BLOCK_PREDICATES = {"looks_blocked", "looks_challenged"}


def test_block_detection_is_wired_to_a_stop_not_to_a_retry():
    """
    A block predicate is only useful if the branch it guards gives up.

    Read structurally: in every module that calls one, the guarded branch must
    return a BLOCKED_CAPTCHA outcome. A version that logged the block and
    looped would pass a substring check and violate the constitution.
    """
    checked = 0
    for path in _python_files():
        if path.name == "base.py":
            continue  # where the predicates are defined, not used
        source = path.read_text(encoding="utf-8")
        if not any(p in source for p in BLOCK_PREDICATES):
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            calls = {
                n.func.id
                for n in ast.walk(node.test)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if not (calls & BLOCK_PREDICATES):
                continue
            checked += 1
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            assert "BLOCKED_CAPTCHA" in body, (
                f"{path.name}: the block branch does not report BLOCKED_CAPTCHA"
            )
            assert any(isinstance(n, ast.Return) for n in ast.walk(node)), (
                f"{path.name}: a detected block must return, not continue the loop"
            )

    assert checked >= 1, "no adapter checks for a block page — the guard is missing"
