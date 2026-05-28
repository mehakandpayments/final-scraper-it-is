"""Detect when a response is a firewall / bot-wall rather than real content.

This does **not** defeat protections — it *recognises* them so the scraper can
(a) escalate a static fetch to a real browser when that might legitimately help,
and (b) tell the user clearly *why* a public page couldn't be read (e.g. a
CAPTCHA or login wall, which are intentional access controls we don't bypass).
"""

from __future__ import annotations

import re

# Named CAPTCHA systems we recognise. Each entry maps marker strings (matched
# case-insensitively in the HTML) to a specific vendor label, so the user sees
# "reCAPTCHA challenge" / "hCaptcha challenge" / ... instead of a generic name.
# Order is "first match wins". Vendor-specific markers go FIRST; reCAPTCHA is
# last because hCaptcha (and others) ship "drop-in" compatibility shims that
# reuse the `g-recaptcha` CSS class — without this ordering, hCaptcha pages
# would be mislabelled as reCAPTCHA.
_CAPTCHA_SYSTEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("h-captcha", "hcaptcha.com"),                                "hCaptcha challenge"),
    (("cf-turnstile", "challenges.cloudflare.com/turnstile"),      "Cloudflare Turnstile challenge"),
    (("arkoselabs.com", "funcaptcha-token", "client-api.arkoselabs.com"),
                                                                   "Arkose / FunCAPTCHA challenge"),
    (("geetest.com", "initgeetest", "gt_captcha"),                 "GeeTest challenge"),
    (("recaptcha/api.js", "g-recaptcha"),                          "reCAPTCHA challenge"),
)

# Reasons are split into "soft" (a real browser / retry may get through) and
# "hard" (intentional human-only gate — automation shouldn't/can't pass).
HARD_REASONS: set[str] = {label for _, label in _CAPTCHA_SYSTEMS} | {
    "Login/authentication required",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# An interstitial/challenge page is sparse; a real page that merely *mentions*
# "captcha"/"turnstile" (e.g. a vendor's marketing site) is not. Gating
# content-based detection on this avoids false positives.
_INTERSTITIAL_MAX_TEXT = 2000


def _has(haystack: str, *needles: str) -> bool:
    return any(n in haystack for n in needles)


def _visible_text_len(html: str) -> int:
    no_scripts = _SCRIPT_STYLE_RE.sub(" ", html or "")
    return len(re.sub(r"\s+", " ", _TAG_RE.sub(" ", no_scripts)).strip())


def detect_block(status_code: int | None, html: str, headers: dict | None = None) -> str | None:
    """Return a short reason string if this looks blocked, else ``None``."""
    raw = html or ""
    h = raw.lower()
    hdr = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    is_cf = "cloudflare" in hdr.get("server", "") or "cf-ray" in hdr or "cf-chl" in h
    sparse = _visible_text_len(raw) < _INTERSTITIAL_MAX_TEXT

    # Human-only gates — surfaced, never bypassed. Require an actual widget
    # marker AND a sparse interstitial page (not a content page name-dropping it).
    if sparse:
        for markers, label in _CAPTCHA_SYSTEMS:
            if _has(h, *markers):
                return label
    if sparse and _has(h, "datadome"):
        return "DataDome bot wall"
    if sparse and _has(h, "perimeterx", "px-captcha", "_pxhd"):
        return "PerimeterX bot wall"
    if status_code == 401 or (sparse and _has(h, "please log in", "sign in to continue",
                                              "login required", "please sign in")):
        return "Login/authentication required"

    # Soft blocks — a real browser and/or a retry may legitimately get through.
    # The classic challenge text is CF-specific, so detect it from content alone
    # (the browser path has no response headers to read).
    cf_text = _has(h, "just a moment", "cf-browser-verification",
                   "checking your browser before accessing", "challenge-platform")
    if sparse and (cf_text or (is_cf and _has(h, "checking your browser",
                                              "attention required",
                                              "enable javascript and cookies"))):
        return "Cloudflare JS challenge"
    if status_code == 429:
        return "Rate limited (HTTP 429)"
    if status_code == 503 and (is_cf or (sparse and _has(h, "just a moment", "temporarily unavailable"))):
        return "Service challenge (HTTP 503)"
    if status_code == 403:
        return "Cloudflare 403 (bot block)" if is_cf else "Forbidden (HTTP 403)"
    if _has(h, "access denied") and _visible_text_len(raw) < 1500:
        return "Access denied page"

    return None


def is_soft_block(reason: str | None) -> bool:
    """Whether escalating to a browser / retrying is worth trying."""
    return reason is not None and reason not in HARD_REASONS


def detect_signals(html: str) -> list[str]:
    """Informational (non-blocking) findings about a page.

    These don't fail the scrape or change behaviour — they just tell the user
    something useful about how the page works. Example: reCAPTCHA v3 leaves no
    widget in the DOM but loads ``recaptcha/api.js?render=<sitekey>`` to score
    visitors silently; the page may serve real content *and* be quietly scored.
    """
    h = (html or "").lower()
    signals: list[str] = []
    if "recaptcha/api.js?render=" in h:
        signals.append("reCAPTCHA v3 (silent scoring)")
    return signals
