"""Headless-browser transport for crawlers that can't be served by
the plain ``requests`` session.

Seven Tranche 1 publishers ship their episode lists from React /
Next.js / Drupal SPAs (`deutsche_bank`, `mckinsey`, `rbc_disruptors`,
`rics`, `ted_worklife`, `thomson_reuters`) or are gated by WAF
rules that 403 every non-browser fingerprint (`wef_radio_davos`).
For those we need a real browser to render the page, execute the
client-side JS, and hand us the post-hydration DOM. Everything else
in the crawl layer stays on ``requests`` — the browser path is
opt-in per crawler.

This module exposes a single function — :func:`fetch_rendered` —
that drives a singleton headless Chromium via Playwright's sync
API and returns ``(html_bytes, content_type)``. The contract is
deliberately narrow so the rest of :class:`BaseCrawler` doesn't
care which transport rendered the bytes: same robots.txt check,
same rate limiter, same downstream HTML parsing.

Design notes:

* **Singleton browser / context.** Spinning up Chromium per request
  costs ~1 s of warm-up and several MB of RAM. We open one
  ``BrowserContext`` for the lifetime of the process and reuse it
  across all crawlers in a run. ``atexit`` tears it down so a
  ``Ctrl-C`` doesn't leak Chromium processes.
* **Realistic fingerprint.** Default headless Chromium leaks the
  ``HeadlessChrome`` UA token, ``navigator.webdriver = true``, and
  a synthetic ``Sec-CH-UA`` brand list — all signals WAFs hash on.
  We set a real macOS Chrome UA, real ``Sec-CH-UA-*`` client hints,
  a 1440x900 viewport, an ``en-US`` locale, and a New York
  timezone. We also pass ``--disable-blink-features=AutomationControlled``
  so ``navigator.webdriver`` reports ``false``. None of this
  defeats a serious bot wall (CAPTCHA, Cloudflare Turnstile,
  fingerprint scoring), but it gets us past the boilerplate WAF
  rules that just sniff the obvious bot tokens — which is what
  blocks several of these publishers today.
* **No JS evaluation.** We only fetch HTML. The caller does the
  parsing with BeautifulSoup as usual.
* **Page caching.** The same URL is often visited twice in a run
  (discovery + per-episode fetch). We keep a small in-process
  LRU so the browser hits each URL at most once.

This module is intentionally thin — sub-200 lines of straight-line
Playwright glue. We deliberately do not pull in ``playwright-extra``,
``stealth``, or any other anti-bot middleware: every dependency we
add is one more thing CI has to install, and the fingerprint we
set here is enough to get past the WAF rules we actually hit. If a
publisher's wall hardens to the point where this isn't enough we
will revisit with a residential-proxy egress, not with more JS
shims.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright

LOG = logging.getLogger(__name__)

# A realistic Chrome 122 / macOS Sonoma user-agent string. Updated
# in lockstep with the ``Sec-CH-UA*`` headers below so the WAF
# fingerprint stays internally consistent — mismatched UA/hints is
# itself a signal some firewalls flag on.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_DEFAULT_SEC_CH_UA = '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"'
_DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
_DEFAULT_LOCALE = "en-US"
_DEFAULT_TIMEZONE = "America/New_York"

# Page-cache cap. Discovery + per-episode fetch is ~2× the
# admitted-episode count per publisher; 256 entries comfortably
# spans an entire Tranche 1 run (~400 episodes total) without
# memory growth getting interesting.
_PAGE_CACHE_MAX = 256


class _BrowserState:
    """Lazy singleton holding the live Playwright / browser /
    context handles. The split lets us reuse one Chromium across
    every crawler in a run while still being safe to import the
    module without paying for the browser if no crawler actually
    needs it (the `requests`-based crawlers are unaffected).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # `OrderedDict` for LRU-on-access semantics: cache.move_to_end
        # promotes the most-recently-used entry.
        self._cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        atexit.register(self._shutdown)

    def context(self) -> BrowserContext:
        """Return a live :class:`BrowserContext`, opening Chromium
        on the first call. Safe to call from multiple threads
        (though the rest of the crawler is single-threaded today).
        """
        with self._lock:
            if self._context is not None:
                return self._context
            from playwright.sync_api import sync_playwright

            LOG.info("browser: opening headless Chromium (one-time)")
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    # The flag that flips ``navigator.webdriver`` off
                    # in modern Chromium builds. Without it every
                    # rendered page leaks the bot signal that almost
                    # every WAF checks for first.
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    # Smaller fingerprint surface in CI sandboxes.
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                user_agent=os.environ.get("SN_MDM_BROWSER_UA", _DEFAULT_USER_AGENT),
                viewport=_DEFAULT_VIEWPORT,
                locale=_DEFAULT_LOCALE,
                timezone_id=_DEFAULT_TIMEZONE,
                extra_http_headers={
                    "Sec-CH-UA": _DEFAULT_SEC_CH_UA,
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"macOS"',
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Block the heavyweight resource types we never look at
            # — images, fonts, media. Cuts page-load wall-clock by
            # roughly half on the slow SPAs (TED, McKinsey) and
            # keeps the per-context memory bounded.
            self._context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
            return self._context

    def cache_get(self, url: str) -> tuple[bytes, str] | None:
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None:
                self._cache.move_to_end(url)
            return hit

    def cache_put(self, url: str, value: tuple[bytes, str]) -> None:
        with self._lock:
            self._cache[url] = value
            self._cache.move_to_end(url)
            while len(self._cache) > _PAGE_CACHE_MAX:
                self._cache.popitem(last=False)

    def _shutdown(self) -> None:
        # Best-effort teardown on interpreter exit. We swallow any
        # exception because Playwright sometimes raises inside its
        # own shutdown when the event loop is already torn down.
        if self._context is None and self._browser is None and self._playwright is None:
            return
        LOG.info("browser: shutting down Chromium")
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001  - best-effort cleanup
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001  - best-effort cleanup
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001  - best-effort cleanup
            pass


_STATE = _BrowserState()


def fetch_rendered(
    url: str,
    *,
    wait_for_selector: str | None = None,
    wait_for_states: Iterable[str] = ("domcontentloaded",),
    timeout_ms: int = 30_000,
    use_cache: bool = True,
) -> tuple[bytes, str]:
    """Render ``url`` in a headless Chromium and return the
    post-hydration HTML.

    Parameters
    ----------
    url:
        Absolute URL to fetch.
    wait_for_selector:
        CSS selector that must be present in the DOM before we
        return. Use this on SPAs where the episode list only
        appears after the React tree has rendered (e.g. the RBC
        Disruptors archive). ``None`` (the default) skips the
        selector wait.
    wait_for_states:
        Load states to wait for via :meth:`Page.wait_for_load_state`.
        Defaults to ``("domcontentloaded",)``. For SPAs that fetch
        data after the initial paint, pass
        ``("domcontentloaded", "networkidle")`` to let the network
        settle before snapshotting.
    timeout_ms:
        Total navigation timeout in milliseconds. Defaults to 30 s
        which matches the ``requests`` session timeout. Sites that
        are reliably slow may raise this per-call.
    use_cache:
        Whether to consult the in-process LRU. Defaults to
        ``True``; set ``False`` to force a re-render (useful when
        a previous render returned a transient error page).

    Returns
    -------
    (bytes, str)
        Tuple of ``(html_bytes, content_type)``. The content-type
        is always ``"text/html; charset=utf-8"`` because Chromium
        normalises everything it renders to UTF-8 HTML.

    Raises
    ------
    playwright.sync_api.Error
        If Chromium can't reach the URL, hits a navigation
        timeout, or the ``wait_for_selector`` never appears. The
        caller — usually :meth:`BaseCrawler._fetch_rendered` — is
        responsible for catching and falling back to
        ``requests`` or skipping the slug.
    """
    if use_cache:
        cached = _STATE.cache_get(url)
        if cached is not None:
            return cached

    context = _STATE.context()
    page = context.new_page()
    try:
        started = time.monotonic()
        # ``wait_until`` is the FIRST load-state we accept. We then
        # walk the rest of the requested states (e.g. networkidle)
        # explicitly so the timeout budget covers the whole chain.
        first_state, *rest_states = wait_for_states
        page.goto(url, wait_until=first_state, timeout=timeout_ms)
        for state in rest_states:
            remaining = timeout_ms - int((time.monotonic() - started) * 1000)
            page.wait_for_load_state(state, timeout=max(remaining, 1_000))
        if wait_for_selector:
            remaining = timeout_ms - int((time.monotonic() - started) * 1000)
            page.wait_for_selector(wait_for_selector, timeout=max(remaining, 1_000))
        html = page.content()
    finally:
        page.close()

    value = (html.encode("utf-8"), "text/html; charset=utf-8")
    if use_cache:
        _STATE.cache_put(url, value)
    return value


def warmup_origin(origin: str) -> None:
    """Hit an origin's root once before any sensitive request.

    A handful of WAFs (notably the WEF firewall) attach an
    ``__cfduid``-shaped cookie on the first response and then
    require that cookie on subsequent requests to the same origin.
    Calling this with the publisher root URL before the first
    discovery / fetch primes that cookie inside the singleton
    context so subsequent ``fetch_rendered`` calls already have
    it. Safe to call repeatedly — the singleton context dedupes
    cookie writes per-origin.

    Used today by :class:`crawl.crawlers.wef_radio_davos.WefRadioDavosCrawler`.
    """
    parsed = urllib.parse.urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"warmup_origin: not an absolute origin: {origin!r}")
    root = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        fetch_rendered(root, use_cache=False, timeout_ms=20_000)
    except Exception as exc:  # noqa: BLE001 - warmup is best-effort
        LOG.warning("browser: warmup_origin(%s) failed: %s", root, exc)


__all__ = ["fetch_rendered", "warmup_origin"]
