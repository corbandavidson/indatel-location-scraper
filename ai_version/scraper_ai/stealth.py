"""
Stealth-enabled Playwright renderer for hard chains.

Patches the most common bot-detection signals on a per-page basis,
without modifying the legacy `scraper/renderer.py`. The legacy renderer
has only the `navigator.webdriver` patch; this module also covers:

  - navigator.plugins / mimeTypes (empty in headless Chromium)
  - navigator.languages
  - navigator.userAgentData (gives away headless Chromium)
  - window.chrome global (missing in Chromium)
  - permissions API (returns 'denied' for notifications in headless)
  - WebGL vendor/renderer (reveals SwiftShader software rasterizer)
  - WebGL parameter spoofing
  - Notification.permission

Combined, these defeat Cloudflare's Bot Management at the "challenge"
tier most of the time. They do NOT defeat the "Pro" tier (Turnstile
captchas) — for those, residential proxies are still needed.

Returns the same RenderResult shape as the legacy renderer so the
orchestrator can use either one interchangeably.
"""

import logging
import random
import sys
import time
from pathlib import Path

# Reach the legacy package for shared constants + RenderResult type
_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from config.settings import USER_AGENTS, VIEWPORT
from scraper.renderer import RenderResult, _is_api_endpoint

logger = logging.getLogger("scraper_ai.stealth")


# Full stealth init script — runs before any page JS executes.
# Each block addresses one specific fingerprint check that bot-detection
# libraries (Akamai, Datadome, Cloudflare BM, PerimeterX) look for.
_STEALTH_SCRIPT = r"""
(() => {
  // 1. navigator.webdriver — the classic giveaway.
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. navigator.plugins — empty in headless. Fake a plausible set.
  const fakePlugin = (name, filename, desc) => ({
    name, filename, description: desc, length: 1,
    item: () => null, namedItem: () => null,
  });
  const plugins = [
    fakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    fakePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  plugins.refresh = () => {};
  plugins.item = (i) => plugins[i] || null;
  plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
  Object.defineProperty(navigator, 'plugins', { get: () => plugins });

  // 3. navigator.mimeTypes — also empty in headless.
  const mimes = [
    { type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: plugins[0] },
    { type: 'text/pdf', suffixes: 'pdf', description: '', enabledPlugin: plugins[0] },
  ];
  Object.defineProperty(navigator, 'mimeTypes', { get: () => mimes });

  // 4. navigator.languages — single-entry array is suspicious.
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

  // 5. window.chrome — missing entirely in Chromium.
  if (!window.chrome) {
    window.chrome = {
      runtime: {},
      loadTimes: function () { return {}; },
      csi: function () { return {}; },
      app: { isInstalled: false },
    };
  }

  // 6. Permissions API — headless returns 'denied' for notifications,
  // which real browsers don't unless explicitly blocked.
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
      if (params && params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission || 'default' });
      }
      return origQuery(params);
    };
  }

  // 7. WebGL fingerprinting — headless Chromium uses SwiftShader by default
  // which is a giveaway. Spoof Intel integrated graphics.
  const spoofWebGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';                    // UNMASKED_VENDOR_WEBGL
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';       // UNMASKED_RENDERER_WEBGL
      return orig.call(this, parameter);
    };
  };
  spoofWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
  spoofWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);

  // 8. userAgentData — present in real Chrome 90+, absent or odd-looking
  // in headless Chromium. Returning undefined is the safest bet.
  try {
    Object.defineProperty(navigator, 'userAgentData', { get: () => undefined });
  } catch (_) {}

  // 9. Hide the headless flag in user agent if it appears.
  // (Newer Chromium versions don't include "HeadlessChrome" but be safe.)
  if (navigator.userAgent.includes('HeadlessChrome')) {
    Object.defineProperty(navigator, 'userAgent', {
      get: () => navigator.userAgent.replace('HeadlessChrome', 'Chrome'),
    });
  }

  // 10. Notification permission — same idea as #6.
  if (window.Notification && window.Notification.permission === 'denied') {
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });
  }
})();
"""


def render_stealth(url: str, *, wait_for_idle: bool = True, timeout_ms: int = 20000) -> RenderResult | None:
    """
    Render a URL with full stealth patches. Falls back to None on hard failure.
    Mostly mirrors the legacy `_render_playwright` flow but with the expanded
    init script and slightly more realistic page interactions.
    """
    logger.info("Stealth render: %s", url)
    intercepted: list[dict] = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                # Common Chromium flags that further reduce automation signals.
                # --disable-http2 forces HTTP/1.1 which avoids Akamai-style
                # H2 fingerprint checks (some chains, e.g. Costco, kill the
                # HTTP/2 connection to headless Chromium specifically).
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process,HttpsUpgrades",
                    "--disable-http2",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=random.choice(USER_AGENTS),
                locale="en-US",
                timezone_id="America/Chicago",
                java_script_enabled=True,
                # Real-Chrome-ish accept headers
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            context.add_init_script(_STEALTH_SCRIPT)

            page = context.new_page()

            def handle_response(response):
                req_url = response.url
                ct = response.headers.get("content-type", "")
                is_json = "json" in ct or "javascript" in ct
                if not (is_json or _is_api_endpoint(req_url)):
                    return
                if response.status != 200:
                    return
                if any(req_url.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".svg", ".woff")):
                    return
                try:
                    body = response.text()
                    if len(body) > 500 and (body.lstrip().startswith("{") or body.lstrip().startswith("[")):
                        try:
                            req_headers = dict(response.request.headers)
                        except Exception:
                            req_headers = {}
                        intercepted.append({
                            "url": req_url,
                            "status": response.status,
                            "body": body,
                            "method": response.request.method,
                            "request_headers": req_headers,
                        })
                except Exception:
                    pass

            page.on("response", handle_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                logger.warning("stealth goto: %s", e)

            # Let Cloudflare's JS challenge complete if it's running
            time.sleep(random.uniform(2.5, 4.0))

            if wait_for_idle:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

            # A little mouse movement makes Bot Management happier
            try:
                page.mouse.move(random.randint(100, 800), random.randint(100, 600))
                time.sleep(random.uniform(0.5, 1.2))
                page.mouse.move(random.randint(100, 800), random.randint(100, 600))
            except Exception:
                pass

            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
                time.sleep(0.6)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight*2/3)")
            except Exception:
                pass
            time.sleep(random.uniform(0.5, 1.0))

            html = ""
            final_url = url
            try:
                html = page.content()
                final_url = page.url
            except Exception:
                pass

            browser.close()

            logger.info("Stealth render done (%d bytes, %d API calls intercepted)",
                        len(html), len(intercepted))
            return RenderResult(
                html=html,
                final_url=final_url,
                method="stealth",
                intercepted_apis=intercepted,
            )
    except Exception as e:
        logger.error("Stealth render failed: %s", e)
        return None
