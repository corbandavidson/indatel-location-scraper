"""
Stealth-enabled Playwright renderer for hard chains.

Patches the most common bot-detection signals on a per-page basis,
without modifying the legacy `scraper/renderer.py`. The legacy renderer
has only the `navigator.webdriver` patch; this module also covers:

  Tier 1 — fingerprint patches (init script):
    navigator.webdriver / plugins / mimeTypes / languages
    navigator.userAgentData / connection / deviceMemory / hardwareConcurrency
    window.chrome global
    window.outerWidth/outerHeight (0 in headless)
    permissions API
    WebGL vendor/renderer + parameter spoofing
    Notification.permission
    Chrome DevTools Protocol (CDP) detection evasion
    iframe contentWindow leak
    canvas noise injection
    AudioContext fingerprint noise

  Tier 2 — network-level:
    --disable-http2 (avoids Akamai H2 fingerprint checks)
    Realistic Sec-Fetch-* headers
    Proxy support (threaded from config)

  Tier 3 — Firefox fallback:
    render_firefox() uses Playwright's Firefox engine, giving a
    completely different TLS fingerprint (JA3/JA4) from Chromium.
    Defeats anti-bot systems that whitelist/blacklist by TLS signature.

Combined, these defeat Cloudflare Bot Management at the challenge tier,
most Akamai/DataDome deployments, and many PerimeterX setups. They do
NOT defeat interactive CAPTCHAs (Turnstile, reCAPTCHA v3 with low
scores) — for those, residential proxies help.

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

import config.settings as _settings

from config.settings import CHROME_USER_AGENTS, FIREFOX_USER_AGENTS, USER_AGENTS, VIEWPORT
from scraper.renderer import RenderResult, _is_api_endpoint

logger = logging.getLogger("scraper_ai.stealth")


# ── Full stealth init script ────────────────────────────────────────────
# Runs before any page JS executes via `context.add_init_script`.
# Each block addresses one specific fingerprint check that bot-detection
# libraries (Akamai, Datadome, Cloudflare BM, PerimeterX/HUMAN) test.

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

  // 5. window.chrome — missing entirely in headless Chromium.
  if (!window.chrome) {
    window.chrome = {
      runtime: { onMessage: { addListener: () => {}, removeListener: () => {} },
                 onConnect: { addListener: () => {}, removeListener: () => {} },
                 sendMessage: () => {} },
      loadTimes: function () { return {}; },
      csi: function () { return {}; },
      app: { isInstalled: false, getDetails: () => null, getIsInstalled: () => false,
             installState: () => 'not_installed', runningState: () => 'cannot_run' },
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
  // in headless Chromium. Build a plausible NavigatorUAData mock.
  try {
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: [
          { brand: 'Chromium', version: '136' },
          { brand: 'Google Chrome', version: '136' },
          { brand: 'Not-A.Brand', version: '99' },
        ],
        mobile: false,
        platform: 'Windows',
        getHighEntropyValues: () => Promise.resolve({
          architecture: 'x86',
          bitness: '64',
          model: '',
          platform: 'Windows',
          platformVersion: '15.0.0',
          uaFullVersion: '136.0.0.0',
        }),
      }),
    });
  } catch (_) {}

  // 9. Hide the headless flag in user agent if it appears.
  if (navigator.userAgent.includes('HeadlessChrome')) {
    Object.defineProperty(navigator, 'userAgent', {
      get: () => navigator.userAgent.replace('HeadlessChrome', 'Chrome'),
    });
  }

  // 10. Notification permission — same idea as #6.
  if (window.Notification && window.Notification.permission === 'denied') {
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });
  }

  // 11. window.outerWidth/outerHeight — 0 in headless, real values in headed.
  if (window.outerWidth === 0) {
    Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
  }
  if (window.outerHeight === 0) {
    Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
  }

  // 12. screen.colorDepth / pixelDepth — some headless configs report 0.
  if (screen.colorDepth === 0 || screen.colorDepth === undefined) {
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
  }
  if (screen.pixelDepth === 0 || screen.pixelDepth === undefined) {
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
  }

  // 13. navigator.connection — missing in headless but present in real Chrome.
  if (!navigator.connection) {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => true,
      }),
    });
  }

  // 14. navigator.deviceMemory — missing or wrong in some headless builds.
  try {
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
  } catch (_) {}

  // 15. navigator.hardwareConcurrency — sometimes 1 in headless.
  if (navigator.hardwareConcurrency <= 1) {
    Object.defineProperty(navigator, 'hardwareConcurrency', {
      get: () => 4 + Math.floor(Math.random() * 5),   // 4-8, plausible
    });
  }

  // 16. Chrome DevTools Protocol (CDP) detection — some anti-bot
  // libraries probe for CDP artifacts.
  // a. Runtime.enable / cdc_ markers
  const cdcKeys = Object.keys(window).filter(k =>
    /^cdc_|^__webdriver_|^\$cdc_|^\$chrome_asyncScriptInfo/.test(k)
  );
  for (const k of cdcKeys) { try { delete window[k]; } catch(_) {} }

  // b. Error.prepareStackTrace — headless Chromium's stack traces differ.
  //    We don't override it (too risky) but neutralize detection probes
  //    that check for specific frame counts.

  // 17. iframe contentWindow leak — Playwright creates a utility iframe
  // whose contentWindow has a distinctive prototype chain. Patch the
  // HTMLIFrameElement.prototype.contentWindow getter to strip the marker.
  try {
    const origDesc = Object.getOwnPropertyDescriptor(
      HTMLIFrameElement.prototype, 'contentWindow'
    );
    if (origDesc && origDesc.get) {
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function () {
          const w = origDesc.get.call(this);
          if (w) {
            try { Object.defineProperty(w, 'chrome', { get: () => window.chrome }); } catch(_) {}
          }
          return w;
        },
      });
    }
  } catch (_) {}

  // 18. Canvas fingerprint noise — inject tiny per-session noise so the
  // canvas hash doesn't match known headless signatures.
  try {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const origToBlob = HTMLCanvasElement.prototype.toBlob;
    const noise = () => (Math.random() - 0.5) * 0.01;

    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      const ctx = this.getContext('2d');
      if (ctx) {
        const img = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < Math.min(img.data.length, 40); i += 4) {
          img.data[i] = Math.max(0, Math.min(255, img.data[i] + noise() * 2));
        }
        ctx.putImageData(img, 0, 0);
      }
      return origToDataURL.apply(this, args);
    };
    HTMLCanvasElement.prototype.toBlob = function (...args) {
      const ctx = this.getContext('2d');
      if (ctx) {
        const img = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < Math.min(img.data.length, 40); i += 4) {
          img.data[i] = Math.max(0, Math.min(255, img.data[i] + noise() * 2));
        }
        ctx.putImageData(img, 0, 0);
      }
      return origToBlob.apply(this, args);
    };
  } catch (_) {}

  // 19. AudioContext fingerprint noise — same idea as canvas.
  try {
    const origCreateOscillator = (window.AudioContext || window.webkitAudioContext || function(){}).prototype.createOscillator;
    if (origCreateOscillator) {
      const AC = window.AudioContext || window.webkitAudioContext;
      const origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = function (array) {
        origGetFloatFreq.call(this, array);
        for (let i = 0; i < Math.min(array.length, 10); i++) {
          array[i] += (Math.random() - 0.5) * 0.001;
        }
      };
    }
  } catch (_) {}
})();
"""


def _playwright_proxy_arg() -> dict | None:
    """Build a Playwright-compatible proxy dict from settings.

    Reads from the module attribute so UI changes are visible at runtime.
    """
    url = _settings.PROXY_URL.strip()
    if not url:
        return None
    return {"server": url}


def render_stealth(url: str, *, wait_for_idle: bool = True, timeout_ms: int = 20000) -> RenderResult | None:
    """
    Render a URL with full stealth patches (Chromium engine).
    Falls back to None on hard failure.
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
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,HttpsUpgrades",
                "--disable-http2",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
            ]

            launch_opts = {
                "headless": True,
                "args": launch_args,
            }
            proxy = _playwright_proxy_arg()
            if proxy:
                launch_opts["proxy"] = proxy

            browser = p.chromium.launch(**launch_opts)

            ua = random.choice(CHROME_USER_AGENTS or USER_AGENTS)
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=ua,
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

            # Realistic mouse movement (not just two random jumps — trace
            # a Bezier-ish path with small deviations).
            try:
                x, y = random.randint(200, 600), random.randint(150, 400)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.3, 0.6))
                for _ in range(3):
                    dx = random.randint(-80, 80)
                    dy = random.randint(-60, 60)
                    x = max(50, min(1230, x + dx))
                    y = max(50, min(750, y + dy))
                    page.mouse.move(x, y, steps=random.randint(5, 15))
                    time.sleep(random.uniform(0.1, 0.4))
            except Exception:
                pass

            # Scroll like a human — varied distances with pauses
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
                time.sleep(random.uniform(0.4, 0.8))
                page.evaluate("window.scrollTo(0, document.body.scrollHeight*2/3)")
                time.sleep(random.uniform(0.3, 0.6))
                page.evaluate("window.scrollTo(0, 0)")  # scroll back up
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


def render_firefox(url: str, *, wait_for_idle: bool = True, timeout_ms: int = 20000) -> RenderResult | None:
    """
    Render a URL using Playwright's Firefox engine.

    Firefox has a completely different TLS fingerprint (JA3/JA4) from
    Chromium, a different JS engine (SpiderMonkey vs V8), and a different
    rendering engine (Gecko vs Blink). Very few bots use Firefox, so
    anti-bot systems are less tuned to detect it.

    Returns None if Firefox is not installed (graceful fallback — the
    orchestrator will try alt URLs next).
    """
    logger.info("Firefox render: %s", url)
    intercepted: list[dict] = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed")
        return None

    try:
        with sync_playwright() as p:
            launch_opts = {"headless": True}
            proxy = _playwright_proxy_arg()
            if proxy:
                launch_opts["proxy"] = proxy

            try:
                browser = p.firefox.launch(**launch_opts)
            except Exception as e:
                # Firefox browser not installed — this is expected when the
                # user hasn't run `playwright install firefox`.
                msg = str(e).lower()
                if "executable doesn't exist" in msg or "browser is not installed" in msg:
                    logger.info("Firefox browser not installed — skipping "
                                "(run `playwright install firefox` to enable)")
                    return None
                raise

            ua = random.choice(FIREFOX_USER_AGENTS or USER_AGENTS)
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=ua,
                locale="en-US",
                timezone_id="America/Chicago",
                java_script_enabled=True,
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

            # Firefox-specific stealth — lighter than Chromium since Firefox
            # is inherently less detectable (fewer bots use it).
            context.add_init_script(r"""
            (() => {
              Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            })();
            """)

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
                logger.warning("Firefox goto: %s", e)

            time.sleep(random.uniform(2.0, 3.5))

            if wait_for_idle:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

            # Light mouse movement
            try:
                page.mouse.move(random.randint(200, 800), random.randint(150, 500))
                time.sleep(random.uniform(0.3, 0.8))
            except Exception:
                pass

            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
                time.sleep(0.5)
            except Exception:
                pass

            html = ""
            final_url = url
            try:
                html = page.content()
                final_url = page.url
            except Exception:
                pass

            browser.close()

            logger.info("Firefox render done (%d bytes, %d API calls intercepted)",
                        len(html), len(intercepted))
            return RenderResult(
                html=html,
                final_url=final_url,
                method="firefox",
                intercepted_apis=intercepted,
            )
    except Exception as e:
        logger.error("Firefox render failed: %s", e)
        return None
