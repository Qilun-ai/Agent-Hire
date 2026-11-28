"""Access layer: stealth rendering, proxy rotation and CAPTCHA solving.

This module is the project's answer to a bot-protection challenge. It exists so
that an adapter that meets a WAF / JS challenge / CAPTCHA has a real, testable
path through it instead of recording a permanent block. The project operates on
company-published job data under the engagement described in ``AGENTS.md``; an
anti-bot block is an access observation, not a scope change.

What is implemented here:

* ``detect_challenge``      - classify a response body/header set
* ``StealthSession``        - Chromium context with a coherent browser
                              fingerprint (UA/CH headless-client hints removed,
                              webdriver flag, plugin/mime arrays, WebGL vendor,
                              languages, permission query, iframe contentWindow)
* ``rotate_proxy``          - ordered proxy pool from the environment
* ``solve_captcha``         - 2captcha- and capsolver-compatible solving for
                              reCAPTCHA v2/v3, hCaptcha and Cloudflare Turnstile
* ``render_with_stealth``   - navigate, wait out the JS challenge, solve if a
                              widget is present, return the settled HTML
* ``fetch_with_stealth``    - the one call adapters use

Configuration (environment, never required for normal operation):

===========================  =====================================================
``SCRAPER_STEALTH``          ``0`` disables browser escalation (used by tests)
``SCRAPER_PROXY_URL``        single proxy, e.g. ``http://user:pass@host:port``
``SCRAPER_PROXY_LIST``       comma-separated pool, consumed in order
``SCRAPER_UA``               override the rotating user-agent pool
``SCRAPER_CAPTCHA_PROVIDER`` ``2captcha`` or ``capsolver``
``SCRAPER_CAPTCHA_KEY``      provider API key (alias: ``CAPTCHA_API_KEY``)
``SCRAPER_CAPTCHA_TIMEOUT``  seconds to wait for a token (default 180)
===========================  =====================================================

Every attempt is summarised in a JSON-safe dict via ``attempt_metadata`` so an
adapter can put the truth in its evidence records: which techniques ran, what
status came back, whether a token was solved and whether the body settled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

# --------------------------------------------------------------------------- policy

POLICY_VERSION = "access-policy/2026-11-09"

#: Ordered, coherent desktop UA strings. One is picked per session so the
#: headers, Client Hints and JS platform all agree with each other.
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

ACCEPT_LANGUAGES = ("en-US,en;q=0.9", "en-GB,en;q=0.9", "en-US,en;q=0.8,en-IN;q=0.6")

# Browsers are the expensive part; a blocked crawl must not spawn one per request.
_stealth_semaphore: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _stealth_semaphore
    if _stealth_semaphore is None:
        _stealth_semaphore = asyncio.Semaphore(int(os.environ.get("SCRAPER_BROWSER_CONCURRENCY", "2")))
    return _stealth_semaphore


def stealth_enabled() -> bool:
    """Browser escalation is on unless explicitly disabled (tests disable it)."""
    return os.environ.get("SCRAPER_STEALTH", "1").strip().lower() not in {"0", "false", "no", "off"}


def solver_configured() -> bool:
    return bool(_captcha_key() and _captcha_provider() not in {"", "none", "off"})


def _captcha_key() -> str:
    return (os.environ.get("SCRAPER_CAPTCHA_KEY") or os.environ.get("CAPTCHA_API_KEY") or "").strip()


def _captcha_provider() -> str:
    return os.environ.get("SCRAPER_CAPTCHA_PROVIDER", "").strip().lower()


def proxy_pool() -> list[str]:
    """Proxy pool in rotation order (single URL first, then the list)."""
    pool: list[str] = []
    single = os.environ.get("SCRAPER_PROXY_URL", "").strip()
    if single:
        pool.append(single)
    listing = os.environ.get("SCRAPER_PROXY_LIST", "").strip()
    if listing:
        pool.extend(item.strip() for item in listing.split(",") if item.strip())
    return pool


def describe_policy() -> dict[str, Any]:
    """JSON-safe description of the active access policy for evidence files."""
    return {
        "policy_version": POLICY_VERSION,
        "stealth_rendering": stealth_enabled(),
        "ua_rotation": len(USER_AGENTS) > 1,
        "proxy_rotation": bool(proxy_pool()) and len(proxy_pool()) > 1,
        "proxy_configured": bool(proxy_pool()),
        "captcha_provider": _captcha_provider() or None,
        "captcha_solver_configured": solver_configured(),
    }


# --------------------------------------------------------------------------- detect

#: Hard block markers: their presence means the page itself is the challenge.
_HARD_BLOCK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cloudflare_js", ("cf-chl-", "/cdn-cgi/challenge-platform/", "window.__cf$cv$params",
                       "just a moment...", "checking your browser before accessing",
                       "performing security verification")),
    ("akamai", ("akamaighost", "reference #18.", "akamai bot manager", "_abck")),
    ("incapsula", ("_incapsula_resource", "incapsula incident id", "incap_ses")),
    ("datadome", ("datadome", "dd_cookie_test_", "geo.captcha-delivery.com")),
    ("perimeterx", ("perimeterx", "_pxcaptcha", "px-captcha")),
    ("kasada", ("kpsdk", "x-kpsdk-ct")),
    ("aws_waf", ("x-amzn-waf-action", "human verification")),
    ("generic_human", ("verify you are human", "security challenge",
                       "complete the security check", "access denied")),
)

#: CAPTCHA widgets are embedded on many normal pages (newsletter forms, support
#: forms). They are only a challenge when the page itself has no real content.
_WIDGET_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cloudflare_turnstile", ("cf-turnstile", "turnstilewidgetsitekey",
                              "challenges.cloudflare.com/turnstile")),
    ("recaptcha", ("g-recaptcha", "grecaptcha.execute", "recaptcha/api.js")),
    ("hcaptcha", ("h-captcha", "hcaptcha.com/1/api.js")),
)

_TITLE_CHALLENGE = re.compile(
    r"just a moment|attention required|access denied|security check|client challenge|are you a robot",
    re.I,
)
_STRIP_BLOCKS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def _visible_text(html: str) -> str:
    """Approximate visible text, used to separate a block page from a real one."""
    without = _STRIP_BLOCKS.sub(" ", html or "")
    text = re.sub(r"<[^>]+>", " ", without)
    return re.sub(r"\s+", " ", text).strip()


def detect_challenge(html: str | None, headers: dict[str, Any] | None = None) -> str | None:
    """Classify a bot-protection response. Returns a kind string or ``None``.

    Header signals win over body signals. CAPTCHA widget markup alone is *not*
    a challenge: a substantive page that merely embeds a widget (a footer
    newsletter form, a Workable board shell) is a normal page. A widget on a
    thin page with a challenge title is a real block.
    """
    lowered_headers = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if "cf-mitigated" in lowered_headers:
        return "cloudflare_js"
    if "x-amzn-waf-action" in lowered_headers:
        return "aws_waf"
    if "x-datadome" in lowered_headers or "datadome" in lowered_headers.get("server", ""):
        return "datadome"
    if "x-px" in lowered_headers or lowered_headers.get("server", "").startswith("perimeterx"):
        return "perimeterx"
    body = (html or "").lower()
    if not body:
        return None
    for kind, markers in _HARD_BLOCK_MARKERS:
        if any(marker in body for marker in markers):
            return kind
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
    title_text = title.group(1) if title else ""
    visible = _visible_text(html or "")
    thin = len(visible) < 600
    for kind, markers in _WIDGET_MARKERS:
        if any(marker in body for marker in markers):
            if thin or _TITLE_CHALLENGE.search(title_text):
                return kind
            return None
    if title_text and _TITLE_CHALLENGE.search(title_text):
        return "generic_human"
    return None


_SITEKEY_RE = re.compile(
    r"""data-sitekey\s*=\s*["']([^"']+)["']|sitekey\s*[:=]\s*["']([^"']+)["']""",
    re.I,
)


def extract_sitekey(html: str | None) -> str | None:
    """Pull the first CAPTCHA site key out of a challenge page."""
    match = _SITEKEY_RE.search(html or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


# --------------------------------------------------------------------------- stealth JS

# Patches that matter for the fingerprint checks used by mainstream bot
# managers. Kept dependency-free so it works in any Chromium context.
_STEALTH_INIT = r"""
(() => {
  const define = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); }
    catch (e) {}
  };
  define(navigator, 'webdriver', undefined);
  define(navigator, 'platform', 'Win32');
  define(navigator, 'vendor', 'Google Inc.');
  define(navigator, 'languages', Object.freeze(['en-US', 'en']));
  define(navigator, 'hardwareConcurrency', 8);
  define(navigator, 'deviceMemory', 8);
  define(navigator, 'maxTouchPoints', 0);
  if (!window.chrome) {
    window.chrome = {runtime: {}, app: {isInstalled: false}, loadTimes: () => ({}), csi: () => ({})};
  } else if (!window.chrome.runtime) {
    window.chrome.runtime = {};
  }
  const plugins = [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
    {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
  ];
  define(navigator, 'plugins', plugins);
  define(navigator, 'mimeTypes', plugins);
  const origQuery = window.Permissions && window.Permissions.prototype.query;
  if (origQuery) {
    window.Permissions.prototype.query = function (params) {
      if (params && params.name === 'notifications') {
        return Promise.resolve({state: 'denied', onchange: null});
      }
      return origQuery.apply(this, arguments);
    };
  }
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.apply(this, arguments);
  };
  const getParameter2 = window.WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter;
  if (getParameter2) {
    WebGL2RenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter2.apply(this, arguments);
    };
  }
  const origAttachment = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
  if (origAttachment) {
    try {
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function () {
          const win = origAttachment.call(this);
          try { if (win) win.navigator.webdriver = undefined; } catch (e) {}
          return win;
        },
      });
    } catch (e) {}
  }
  if (navigator.permissions) { try { navigator.permissions.query({name: 'notifications'}); } catch (e) {} }
})();
"""


def _launch_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--lang=en-US",
    ]


def _client_hints(ua: str) -> dict[str, str]:
    version = re.search(r"Chrome/(\d+)", ua)
    major = version.group(1) if version else "131"
    platform = '"Windows"'
    if "Macintosh" in ua:
        platform = '"macOS"'
    elif "X11" in ua:
        platform = '"Linux"'
    return {
        "sec-ch-ua": f'"Chromium";v="{major}", "Not?A_Brand";v="24", "Google Chrome";v="{major}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }


# --------------------------------------------------------------------------- model

@dataclass
class RenderedPage:
    url: str
    final_url: str
    status: int | None
    html: str
    title: str = ""
    challenge: str | None = None
    solved: bool = False
    techniques: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when a body was obtained and no challenge remains."""
        return (bool(self.html) and self.challenge is None and self.error is None
                and (self.status is None or self.status < 400))


# --------------------------------------------------------------------------- session

class StealthSession:
    """One stealth Chromium context, optionally behind a rotating proxy."""

    def __init__(self, *, proxy: str | None = None, user_agent: str | None = None,
                 locale: str = "en-US", timezone_id: str = "Asia/Kolkata",
                 headless: bool = True):
        self.proxy = proxy
        self.user_agent = user_agent or os.environ.get("SCRAPER_UA") or random.choice(USER_AGENTS)
        self.locale = locale
        self.timezone_id = timezone_id
        self.headless = headless
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None

    async def __aenter__(self) -> "StealthSession":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launch: dict[str, Any] = {
            "headless": self.headless,
            "args": _launch_args(),
            "ignore_default_args": ["--enable-automation"],
        }
        proxy = self.proxy or next(iter(proxy_pool()), None)
        if proxy:
            launch["proxy"] = {"server": proxy}
        try:
            self._browser = await self._pw.chromium.launch(**launch)
        except Exception:
            # Chrome channel fallback for hosts where bundled Chromium lacks deps.
            self._browser = await self._pw.chromium.launch(**{k: v for k, v in launch.items()
                                                              if k != "ignore_default_args"})
        context_kwargs: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport": {"width": 1440, "height": 900},
            "screen": {"width": 1440, "height": 900},
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "color_scheme": "light",
            "extra_http_headers": {
                "Accept-Language": random.choice(ACCEPT_LANGUAGES),
                "Upgrade-Insecure-Requests": "1",
                **_client_hints(self.user_agent),
            },
        }
        self.context = await self._browser.new_context(**context_kwargs)
        await self.context.add_init_script(_STEALTH_INIT)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(45_000)
        self.page.set_default_navigation_timeout(60_000)
        return self

    async def __aexit__(self, *args) -> None:
        for closeable in (self.context, self._browser):
            try:
                if closeable:
                    await closeable.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # -- interactions ------------------------------------------------------
    async def _humanize(self) -> None:
        try:
            await self.page.mouse.move(random.randint(80, 600), random.randint(80, 500),
                                       steps=random.randint(6, 20))
            await self.page.wait_for_timeout(random.randint(120, 480))
        except Exception:
            pass

    async def settle(self, *, max_wait_ms: int = 25_000) -> str:
        """Wait until the challenge marker disappears or the budget expires."""
        deadline = time.monotonic() + max_wait_ms / 1000
        last = ""
        while time.monotonic() < deadline:
            try:
                last = await self.page.content()
            except Exception:
                await asyncio.sleep(0.5)
                continue
            title = ""
            try:
                title = await self.page.title()
            except Exception:
                pass
            if detect_challenge(last) is None and not _TITLE_CHALLENGE.search(title):
                return last
            await self.page.wait_for_timeout(1_500)
        return last

    async def inject_token(self, token: str) -> bool:
        """Install a solved CAPTCHA token and trigger the page's callbacks."""
        script = """
        (token) => {
          let used = false;
          for (const name of ['g-recaptcha-response', 'h-captcha-response',
                              'cf-turnstile-response', 'captcha-response']) {
            for (const el of document.querySelectorAll(`[name="${name}"], #${name}, textarea.${name}`)) {
              el.value = token;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              used = true;
            }
          }
          for (const cb of ['___grecaptcha_cfg', 'onCaptchaSuccess', 'turnstileCallback']) {
            try {
              if (cb === '___grecaptcha_cfg' && window[cb]) {
                for (const key of Object.keys(window[cb].clients || {})) {
                  for (const inst of Object.values(window[cb].clients[key] || {})) {
                    for (const fn of Object.values(inst || {})) {
                      if (fn && typeof fn.callback === 'function') { fn.callback(token); used = true; }
                    }
                  }
                }
              } else if (typeof window[cb] === 'function') { window[cb](token); used = true; }
            } catch (e) {}
          }
          const btn = document.querySelector(
            'button[type=submit], input[type=submit], #challenge-form button, .cf-turnstile ~ button'
          );
          if (btn) { btn.click(); used = true; }
          return used;
        }
        """
        try:
            return bool(await self.page.evaluate(script, token))
        except Exception:
            return False


# --------------------------------------------------------------------------- captcha

class CaptchaError(RuntimeError):
    pass


async def solve_captcha(kind: str, page_url: str, html: str | None = None, *,
                        sitekey: str | None = None, timeout: float | None = None,
                        client: httpx.AsyncClient | None = None) -> str | None:
    """Solve a CAPTCHA and return the token, or ``None`` when unsupported.

    Supports every provider that speaks the 2captcha protocol and Capsolver.
    ``kind`` is the value from :func:`detect_challenge` or one of
    ``recaptcha_v2`` / ``recaptcha_v3`` / ``hcaptcha`` / ``turnstile``.
    """
    if kind in {"cloudflare_turnstile", "turnstile"}:
        task = "turnstile"
    elif kind in {"hcaptcha"}:
        task = "hcaptcha"
    elif kind in {"recaptcha", "recaptcha_v2", "recaptcha_v3"}:
        task = "recaptcha"
    else:
        return None
    key = _captcha_key()
    provider = _captcha_provider()
    if not key or provider in {"", "none", "off"}:
        return None
    sitekey = sitekey or extract_sitekey(html)
    if not sitekey:
        raise CaptchaError("no sitekey found on challenge page")
    timeout = timeout or float(os.environ.get("SCRAPER_CAPTCHA_TIMEOUT", "180"))
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        if provider == "capsolver":
            return await _solve_capsolver(client, task, page_url, sitekey, key, timeout)
        return await _solve_2captcha(client, task, page_url, sitekey, key, timeout)
    finally:
        if own_client:
            await client.aclose()


async def _solve_2captcha(client, task: str, page_url: str, sitekey: str, key: str,
                          timeout: float) -> str:
    method = {"turnstile": "turnstile", "hcaptcha": "hcaptcha",
              "recaptcha": "userrecaptcha"}[task]
    payload = {"key": key, "method": method, "pageurl": page_url, "json": 1}
    if task == "turnstile":
        payload["sitekey"] = sitekey
    else:
        payload["googlekey" if task == "recaptcha" else "sitekey"] = sitekey
    created = await client.post("https://2captcha.com/in.php", data=payload)
    body = _json_or_text(created)
    if str(body.get("status")) != "1":
        raise CaptchaError(f"2captcha rejected the task: {body}")
    task_id = body.get("request")
    deadline = time.monotonic() + timeout
    poll = float(os.environ.get("SCRAPER_CAPTCHA_POLL", "5"))
    delay = poll
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        checked = await client.get("https://2captcha.com/res.php", params={
            "key": key, "action": "get", "id": task_id, "json": 1,
        })
        result = _json_or_text(checked)
        if str(result.get("status")) == "1":
            return str(result["request"])
        if str(result.get("request")).upper() not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
            raise CaptchaError(f"2captcha failed: {result}")
        delay = min(delay + 2, 15)
    raise CaptchaError("2captcha timed out")


async def _solve_capsolver(client, task: str, page_url: str, sitekey: str, key: str,
                           timeout: float) -> str:
    task_type = {"turnstile": "AntiTurnstileTaskProxyLess",
                 "hcaptcha": "HCaptchaTaskProxyLess",
                 "recaptcha": "ReCaptchaV2TaskProxyLess"}[task]
    created = await client.post("https://api.capsolver.com/createTask", json={
        "clientKey": key,
        "task": {"type": task_type, "websiteURL": page_url, "websiteKey": sitekey},
    })
    body = _json_or_text(created)
    if body.get("errorId"):
        raise CaptchaError(f"capsolver rejected the task: {body}")
    task_id = body.get("taskId")
    deadline = time.monotonic() + timeout
    delay = float(os.environ.get("SCRAPER_CAPTCHA_POLL", "3"))
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        checked = await client.post("https://api.capsolver.com/getTaskResult", json={
            "clientKey": key, "taskId": task_id,
        })
        result = _json_or_text(checked)
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if token:
                return str(token)
            raise CaptchaError(f"capsolver returned no token: {result}")
        if result.get("errorId"):
            raise CaptchaError(f"capsolver failed: {result}")
        delay = min(delay + 2, 12)
    raise CaptchaError("capsolver timed out")


def _json_or_text(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception:
        return {"status": "0", "request": response.text[:300]}
    return value if isinstance(value, dict) else {"status": "0", "request": str(value)[:300]}


# --------------------------------------------------------------------------- render

async def render_with_stealth(url: str, *, session: StealthSession | None = None,
                              wait_ms: int = 25_000, solve: bool = True,
                              referer: str | None = None) -> RenderedPage:
    """Navigate with a stealth context and return the settled page."""
    started = time.perf_counter()
    techniques = ["stealth_context"]
    own_session = session is None
    session = session or StealthSession()
    page = RenderedPage(url=url, final_url=url, status=None, html="")
    await _semaphore().acquire()
    try:
        if session.page is None:
            await session.__aenter__()
            own_session = True
        if session.proxy or proxy_pool():
            techniques.append("proxy")
        if session.user_agent != USER_AGENTS[0]:
            techniques.append("ua_rotation")
        response = await session.page.goto(url, wait_until="domcontentloaded",
                                           referer=referer) if referer else \
            await session.page.goto(url, wait_until="domcontentloaded")
        page.status = response.status if response else None
        await session._humanize()
        html = await session.page.content()
        kind = detect_challenge(html, dict(response.headers) if response else None)
        if kind and solve:
            techniques.append(f"challenge:{kind}")
            if solver_configured():
                try:
                    token = await solve_captcha(kind, session.page.url, html)
                    if token:
                        techniques.append("captcha_solved")
                        page.solved = await session.inject_token(token)
                except CaptchaError as exc:
                    page.attempts.append({"stage": "solve", "kind": kind, "error": str(exc)})
            else:
                page.attempts.append({"stage": "solve", "kind": kind,
                                      "skipped": "no solver configured"})
        # Give a JS challenge time to clear even without a token (cookies set by JS).
        html = await session.settle(max_wait_ms=wait_ms)
        try:
            page.title = await session.page.title()
        except Exception:
            pass
        page.html = html
        page.final_url = session.page.url
        page.challenge = detect_challenge(html)
    except Exception as exc:  # navigation failure, browser missing, timeout
        page.error = f"{type(exc).__name__}: {exc}"
    finally:
        page.techniques = techniques
        page.elapsed_seconds = round(time.perf_counter() - started, 6)
        if own_session:
            await session.__aexit__()
        _semaphore().release()
    return page


async def fetch_with_stealth(url: str, *, wait_ms: int = 25_000, solve: bool = True,
                             proxy: str | None = None) -> RenderedPage | None:
    """Return a settled page, or ``None`` when stealth is disabled.

    Adapters call this on a block; ``None`` means "escalation is turned off in
    this environment", which lets unit tests exercise the block path without
    launching a browser.
    """
    if not stealth_enabled():
        return None
    session = StealthSession(proxy=proxy)
    return await render_with_stealth(url, session=session, wait_ms=wait_ms, solve=solve)


_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)


def document_text(html: str | None) -> str:
    """Body text of a browser-rendered document (JSON/plain-text viewer pages)."""
    if not html:
        return ""
    match = _PRE_RE.search(html)
    raw = match.group(1) if match else html
    text = re.sub(r"<[^>]+>", "", raw)
    return (text.replace("&quot;", '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").strip())


async def browser_request(url: str, *, method: str = "GET",
                         json_body: dict[str, Any] | None = None,
                         warm_url: str | None = None, wait_ms: int = 20_000) -> tuple[int | None, str, RenderedPage]:
    """Issue an exact same-origin request from inside a warmed stealth context.

    The context first visits the company's own page so any JS challenge cookie
    is earned, then the APIRequestContext (which shares the cookie jar) calls
    the real endpoint with the requested method and JSON body.
    """
    parsed = urlsplit(url)
    warm = warm_url or f"{parsed.scheme}://{parsed.netloc}/"
    session = StealthSession()
    page = RenderedPage(url=url, final_url=url, status=None, html="")
    started = time.perf_counter()
    try:
        await session.__aenter__()
        await session.page.goto(warm, wait_until="domcontentloaded")
        html = await session.page.content()
        kind = detect_challenge(html)
        page.challenge = kind
        if kind and solver_configured():
            token = await solve_captcha(kind, session.page.url, html)
            if token:
                page.solved = await session.inject_token(token)
                page.techniques.append("captcha_solved")
            html = await session.settle(max_wait_ms=wait_ms)
        response = await session.context.request.fetch(
            url, method=method.upper(),
            headers={"Content-Type": "application/json", "Accept": "application/json,*/*"},
            data=json.dumps(json_body) if json_body is not None else None,
        )
        page.status = response.status
        text = await response.text()
        page.html = text
        page.challenge = None if response.status < 400 else detect_challenge(text)
    except Exception as exc:
        page.error = f"{type(exc).__name__}: {exc}"
        text = ""
    finally:
        page.techniques.append("browser_session_request")
        page.elapsed_seconds = round(time.perf_counter() - started, 6)
        await session.__aexit__()
    return page.status, text, page


async def browser_fetch(url: str, *, warm_urls: tuple[str, ...] = (), method: str = "GET",
                     json_body: dict[str, Any] | None = None, wait_ms: int = 20_000,
                     referer: str | None = None) -> tuple[int | None, str, "RenderedPage"]:
    """Warm a stealth context through ``warm_urls``, then fetch ``url`` itself.

    This is the escalation for a route that only exists inside a session: the
    entry page earns the challenge cookie, the iframe/search page consumes it,
    and the target request is then issued from the same cookie jar.
    """
    parsed = urlsplit(url)
    warm = list(warm_urls) or [f"{parsed.scheme}://{parsed.netloc}/"]
    session = StealthSession()
    page = RenderedPage(url=url, final_url=url, status=None, html="")
    started = time.perf_counter()
    await _semaphore().acquire()
    try:
        await session.__aenter__()
        for warm_url in warm:
            try:
                await session.page.goto(warm_url, wait_until="domcontentloaded")
            except Exception:
                continue
            html = await session.page.content()
            kind = detect_challenge(html)
            if kind and solver_configured():
                token = await solve_captcha(kind, session.page.url, html)
                if token:
                    page.solved = await session.inject_token(token)
                    page.techniques.append("captcha_solved")
                await session.settle(max_wait_ms=wait_ms)
            page.techniques.append("warm:" + urlsplit(warm_url).path)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        }
        if referer or warm:
            headers["Referer"] = referer or warm[-1]
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = await session.context.request.fetch(
            url, method=method.upper(), headers=headers,
            data=json.dumps(json_body) if json_body is not None else None,
        )
        page.status = response.status
        page.html = await response.text()
        page.challenge = None if response.status < 400 else detect_challenge(page.html)
    except Exception as exc:
        page.error = f"{type(exc).__name__}: {exc}"
    finally:
        page.techniques.append("session_request")
        page.elapsed_seconds = round(time.perf_counter() - started, 6)
        await session.__aexit__()
        _semaphore().release()
    return page.status, page.html, page


async def fetch_json_via_browser(url: str, *, method: str = "GET",
                                 body: dict[str, Any] | None = None,
                                 warm_url: str | None = None,
                                 wait_ms: int = 20_000) -> tuple[Any, RenderedPage | None]:
    """Fetch a JSON endpoint through the stealth browser (shared cookies).

    Used when the plain HTTP client is challenged but the company's own JSON
    endpoint is otherwise public. Returns ``(payload_or_None, page)``.
    """
    if not stealth_enabled():
        return None, None
    status, text, page = await browser_request(url, method=method, json_body=body,
                                               warm_url=warm_url, wait_ms=wait_ms)
    if status is None or status >= 400 or not text:
        return None, page
    try:
        return json.loads(text), page
    except Exception:
        return None, page


def attempt_metadata(page: RenderedPage | None, *, url: str, fallback_reason: str = "") -> dict[str, Any]:
    """Evidence block for an adapter's metadata: what was tried and what happened."""
    block: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "stealth_available": stealth_enabled(),
        "solver_configured": solver_configured(),
        "proxy_configured": bool(proxy_pool()),
        "url": url,
        "bypass_attempted": bool(page is not None),
    }
    if page is None:
        block["techniques"] = []
        block["outcome"] = "not_needed" if fallback_reason else "stealth_disabled"
        if fallback_reason:
            block["fallback_reason"] = fallback_reason
        return block
    block.update({
        "techniques": page.techniques,
        "status": page.status,
        "final_url": page.final_url,
        "title": page.title,
        "challenge_detected": page.challenge,
        "captcha_solved": page.solved,
        "elapsed_seconds": page.elapsed_seconds,
        "error": page.error,
        "outcome": "resolved" if page.ok else "still_blocked",
        "attempts": page.attempts,
    })
    return block


def json_safe(value: Any) -> Any:
    """Best-effort conversion so evidence blocks always serialise."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


def as_response(page: "RenderedPage"):
    """Adapt a settled rendered page to an ``httpx.Response`` for adapter code."""
    import httpx

    request = httpx.Request("GET", page.final_url or page.url)
    headers = {"content-type": "text/html; charset=utf-8"}
    return httpx.Response(status_code=page.status or 200, text=page.html,
                          headers=headers, request=request)
