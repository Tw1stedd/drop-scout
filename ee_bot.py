"""
Entertainment Earth Checkout Bot  —  v3
Fast · Accurate · Stealthy
"""

import asyncio
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

try:
    from playwright.async_api import (
        async_playwright, Page, BrowserContext, Browser, Frame,
        TimeoutError as PWTimeout,
    )
except ImportError:
    print("[ERROR] Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

# ── GUI Callbacks (set by ee_gui.py when running with UI) ─────────────────────
_LOG_CB    = None   # fn(ts, level, prefix, msg)
_STATUS_CB = None   # fn(name, status, check_num, elapsed_s)
_STOP_EVENT: "Optional[asyncio.Event]" = None   # set by run_bot, signalled by GUI stop

def set_callbacks(log_fn=None, status_fn=None):
    global _LOG_CB, _STATUS_CB
    if log_fn    is not None: _LOG_CB    = log_fn
    if status_fn is not None: _STATUS_CB = status_fn

def _set_stop():
    """Called thread-safely by BotThread.stop() to request cooperative shutdown."""
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()


# ── Paths & URLs ───────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent
CONFIG_FILE  = _ROOT / "ee_config.json"
SESSION_FILE = _ROOT / "ee_session.json"

BASE         = "https://www.entertainmentearth.com"
LOGIN_URL    = f"{BASE}/account/login"
CART_URL     = f"{BASE}/cart"
CHECKOUT_URL = f"{BASE}/checkout"

# ── Console UI ─────────────────────────────────────────────────────────────────
_C = {
    "CYAN":   "\033[96m",  "GREEN":  "\033[92m",  "YELLOW": "\033[93m",
    "RED":    "\033[91m",  "WHITE":  "\033[97m",  "PURPLE": "\033[95m",
    "DIM":    "\033[2m",   "BOLD":   "\033[1m",   "RST":    "\033[0m",
}

_LEVEL_COLOR = {
    "INFO":  _C["WHITE"],  "OK":    _C["GREEN"],   "WARN":  _C["YELLOW"],
    "ERROR": _C["RED"],    "STEP":  _C["PURPLE"],  "MON":   _C["CYAN"],
    "BOT":   _C["CYAN"],
}

def log(msg: str, level: str = "INFO", prefix: str = ""):
    ts   = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    col  = _LEVEL_COLOR.get(level, _C["WHITE"])
    tag  = f"{prefix} " if prefix else ""
    print(f"{col}[{ts}][{level:5s}] {tag}{msg}{_C['RST']}", flush=True)
    if _LOG_CB:
        try: _LOG_CB(ts, level, prefix, msg)
        except Exception: pass

def banner(targets: int, mode: str, proxies: int, interval: int):
    w = 58
    print(f"\n{_C['CYAN']}{_C['BOLD']}{'═'*w}")
    print(f"  Entertainment Earth Checkout Bot  v3".center(w))
    print(f"{'═'*w}{_C['RST']}")
    print(f"  {_C['DIM']}Targets: {targets}  │  Mode: {mode}  │  "
          f"Proxies: {proxies}  │  Interval: {interval}s{_C['RST']}\n")


# ── Config ─────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log(f"Config not found: {CONFIG_FILE}", "ERROR"); sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Discord ────────────────────────────────────────────────────────────────────
async def notify(webhook: str, msg: str, ok: bool = True):
    if not webhook or not HAS_AIOHTTP:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(webhook, json={
                "embeds": [{"title": "EE Bot", "description": msg,
                             "color": 0x00FF00 if ok else 0xFF0000,
                             "timestamp": datetime.utcnow().isoformat()}]
            }, timeout=aiohttp.ClientTimeout(total=6))
    except Exception:
        pass


# ── Proxy Helpers ──────────────────────────────────────────────────────────────
def _parse_proxy(proxy_str: str) -> Optional[dict]:
    """
    Parse any common proxy format into the dict Playwright expects.

    Supported formats (all auto-detected):
      ip:port:user:pass          ← ISP/datacenter format  e.g. 167.148.218.250:48078:user:pass
      ip:port                    ← no auth
      http://user:pass@host:port ← URL format
      http://host:port           ← URL no auth
      socks5://user:pass@host:port
    """
    if not proxy_str:
        return None

    raw = proxy_str.strip()

    # ── URL format: scheme://...  ──────────────────────────────────────────────
    if "://" in raw:
        m = re.match(r'(https?|socks[45])://([^:@/]+):([^@/]+)@(.+)', raw)
        if m:
            return {
                "server":   f"{m.group(1)}://{m.group(4)}",
                "username": m.group(2),
                "password": m.group(3),
            }
        return {"server": raw}

    # ── ip:port:user:pass  (ISP proxy format) ─────────────────────────────────
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return {
            "server":   f"http://{ip}:{port}",
            "username": user,
            "password": password,
        }

    # ── ip:port  (no auth) ────────────────────────────────────────────────────
    if len(parts) == 2:
        return {"server": f"http://{raw}"}

    # Fallback — assume it's already a server URL
    return {"server": raw}


# ── Proxy Pool ─────────────────────────────────────────────────────────────────
class ProxyPool:
    def __init__(self, proxies: List[str]):
        self._p   = [p.strip() for p in (proxies or []) if p.strip()]
        self._idx = 0

    def next(self) -> Optional[dict]:
        if not self._p:
            return None
        raw = self._p[self._idx % len(self._p)]
        self._idx += 1
        return _parse_proxy(raw)

    def __bool__(self):
        return bool(self._p)

    def __len__(self):
        return len(self._p)


# ── Stealth Init Script ────────────────────────────────────────────────────────
_STEALTH_JS = """
(function() {
// ── 1. Strip ALL Playwright / CDP automation signals ──────────────────────────
try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch(e) {}
['__playwright','__pw_manual','__pwInitScripts',
 'cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
 'cdc_adoQpoasnfa76pfcZLmcfl_Symbol','__selenium_unwrapped','__webdriverFunc',
 '__lastWatirAlert','__lastWatirConfirm','__lastWatirPrompt'
].forEach(k => { try { delete window[k]; } catch(e) {} });

// ── 2. Realistic navigator fingerprint ────────────────────────────────────────
const def = (obj, prop, val) => {
    try { Object.defineProperty(obj, prop, { get: () => val, configurable: true }); } catch(e) {}
};
def(navigator, 'platform',           'Win32');
def(navigator, 'vendor',             'Google Inc.');
def(navigator, 'languages',          ['en-US', 'en']);
def(navigator, 'hardwareConcurrency', 8);
def(navigator, 'deviceMemory',        8);
def(navigator, 'maxTouchPoints',      0);
def(navigator, 'appVersion', '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36');

// ── 3. navigator.userAgentData (UA-CH — Chrome 131) ───────────────────────────
const uaData = {
    brands: [
        { brand: 'Chromium',      version: '131' },
        { brand: 'Google Chrome', version: '131' },
        { brand: 'Not_A Brand',   version: '24'  }
    ],
    mobile: false,
    platform: 'Windows',
    getHighEntropyValues: async () => ({
        architecture: 'x86', bitness: '64', model: '',
        platform: 'Windows', platformVersion: '15.0.0',
        uaFullVersion: '131.0.6778.86',
        fullVersionList: [
            { brand: 'Chromium',      version: '131.0.6778.86' },
            { brand: 'Google Chrome', version: '131.0.6778.86' },
            { brand: 'Not_A Brand',   version: '24.0.0.0' }
        ]
    })
};
def(navigator, 'userAgentData', uaData);

// ── 4. Plugins ────────────────────────────────────────────────────────────────
const mkPlugin = name => ({ name, description: name, filename: name.toLowerCase().replace(/ /g,'')+'.dll', length: 0 });
def(navigator, 'plugins', [mkPlugin('Chrome PDF Plugin'), mkPlugin('Chrome PDF Viewer'), mkPlugin('Native Client')]);

// ── 5. Chrome runtime shim ────────────────────────────────────────────────────
window.chrome = {
    runtime: { id: undefined, onMessage: { addListener: () => {} }, sendMessage: () => {}, connect: () => ({}) },
    loadTimes: () => ({ requestTime: performance.now()/1000, startLoadTime: performance.now()/1000, commitLoadTime: performance.now()/1000, finishDocumentLoadTime: performance.now()/1000, finishLoadTime: performance.now()/1000, navigationType: 'Other', wasFetchedViaSpdy: false, wasNpnNegotiated: false, npnNegotiatedProtocol: 'unknown', wasAlternateProtocolAvailable: false, connectionInfo: 'http/1.1' }),
    csi: () => ({ startE: Date.now(), onloadT: Date.now()+200, pageT: 500, tran: 15 }),
    app: { isInstalled: false, InstallState: { DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed' }, RunningState: { CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running' }, getDetails: () => null, getIsInstalled: () => false, installState: cb => cb('not_installed'), runningState: () => 'cannot_run' },
    webstore: { onInstallStageChanged: {}, onDownloadProgress: {}, install: () => {} },
};

// ── 6. Permissions API ────────────────────────────────────────────────────────
const _origPerms = navigator.permissions?.query?.bind(navigator.permissions);
if (_origPerms) {
    navigator.permissions.query = p =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origPerms(p);
}

// ── 7. Screen + window metrics ────────────────────────────────────────────────
def(screen, 'width',       1366); def(screen, 'height',      768);
def(screen, 'availWidth',  1366); def(screen, 'availHeight', 728);
def(screen, 'colorDepth',   24); def(screen, 'pixelDepth',   24);
def(window, 'outerWidth',  1366); def(window, 'outerHeight', 768);

// ── 8. document.hasFocus — automation contexts always return false ─────────────
try { Object.defineProperty(document, 'hasFocus', { value: () => true }); } catch(e) {}

// ── 9. navigator.connection (Network Information API) ─────────────────────────
if (!navigator.connection) {
    def(navigator, 'connection', { effectiveType: '4g', downlink: 10, rtt: 50, saveData: false });
}

// ── 10. History length — headless starts at 0, real browsers don't ────────────
try { if (history.length === 0) def(history, 'length', 2); } catch(e) {}

// ── 11. WebGL spoofing (both WebGL1 and WebGL2) ───────────────────────────────
const _patchGL = (ctx) => {
    if (!ctx) return;
    const orig = ctx.prototype.getParameter;
    ctx.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return orig.call(this, p);
    };
};
_patchGL(typeof WebGLRenderingContext  !== 'undefined' ? WebGLRenderingContext  : null);
_patchGL(typeof WebGL2RenderingContext !== 'undefined' ? WebGL2RenderingContext : null);

// ── 12. Propagate webdriver=undefined into iframes ────────────────────────────
try {
    const _cw = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get() {
            const w = _cw.get.call(this);
            if (w) { try { def(w.navigator, 'webdriver', undefined); } catch(e) {} }
            return w;
        }
    });
} catch(e) {}

// ── 13. toString() trap — prevent "native code" detection on patched fns ──────
const _toString = Function.prototype.toString;
const _toStringPatched = new Map();
Function.prototype.toString = function() {
    return _toStringPatched.get(this) || _toString.call(this);
};

})();
"""


# ── Browser / Context Factory ──────────────────────────────────────────────────
async def launch_browser(playwright, cfg: dict, proxy: Optional[dict] = None) -> Browser:
    return await playwright.chromium.launch(
        headless=cfg.get("headless", False),
        slow_mo=cfg.get("slow_mo_ms", 0),
        proxy=proxy,
        args=[
            # Core anti-detection
            "--disable-blink-features=AutomationControlled",
            # Sandbox (required in most environments)
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            # Window / display
            "--window-size=1366,768",
            "--start-maximized",
            # Disable features that make headless look different
            "--disable-infobars",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-popup-blocking",
            # Renderer stability
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-ipc-flooding-protection",
            # Credentials/keychain (avoid OS dialogs)
            "--password-store=basic",
            "--use-mock-keychain",
            # Misc
            "--metrics-recording-only",
            "--no-default-browser-check",
        ],
    )


async def new_context(browser: Browser, storage_state=None) -> BrowserContext:
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept-Language":   "en-US,en;q=0.9",
            "Accept":            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-Ua":         '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile":  "?0",
            "Sec-Ch-Ua-Platform":'"Windows"',
            "Upgrade-Insecure-Requests": "1",
        },
        storage_state=storage_state,
    )
    await ctx.add_init_script(_STEALTH_JS)
    return ctx


async def new_page(ctx: BrowserContext, timeout_ms: int,
                   block_resources: bool = False, headless: bool = True) -> Page:
    page = await ctx.new_page()
    page.set_default_timeout(timeout_ms)

    # Tracking/analytics hosts to always block regardless of mode
    _BLOCKED_HOSTS = {
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.com", "connect.facebook.net", "hotjar.com",
        "segment.com", "amplitude.com", "mixpanel.com", "newrelic.com",
    }

    if block_resources:
        if headless:
            # Headless: block images/media/fonts/CSS — 50-80% faster page loads
            _BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}
            async def _route(route, req):
                if req.resource_type in _BLOCKED_TYPES:
                    await route.abort()
                elif any(h in req.url for h in _BLOCKED_HOSTS):
                    await route.abort()
                else:
                    await route.continue_()
        else:
            # Visible browser: keep images/CSS so the page looks normal;
            # only kill analytics/tracking noise
            async def _route(route, req):
                if any(h in req.url for h in _BLOCKED_HOSTS):
                    await route.abort()
                else:
                    await route.continue_()
        await page.route("**/*", _route)

    return page


async def save_session(ctx: BrowserContext):
    state = await ctx.storage_state()
    with open(SESSION_FILE, "w") as f:
        json.dump(state, f)


def load_session() -> Optional[dict]:
    if SESSION_FILE.exists():
        with open(SESSION_FILE) as f:
            return json.load(f)
    return None


# ── Fast Selector Helpers ──────────────────────────────────────────────────────
# KEY PRINCIPLE: use query_selector (instant, no wait) as first pass,
# then wait_for_selector with a COMBINED css selector as timed fallback.
# This avoids N×timeout wasted time when elements don't exist.

async def fast_click(page: Page, selectors: list, timeout: int = 5000) -> bool:
    """Try all selectors instantly, then wait on combined if none found."""
    # Pass 1: instant check (0 timeout)
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                await el.click()
                return True
        except Exception:
            pass
    # Pass 2: wait on the combined CSS selector (one single timeout)
    combined = ", ".join(selectors)
    try:
        el = await page.wait_for_selector(combined, state="visible", timeout=timeout)
        if el and await el.is_enabled():
            await el.click()
            return True
    except Exception:
        pass
    return False


async def fast_fill(page: Page, selectors: list, value: str, timeout: int = 4000) -> bool:
    """Try all selectors instantly, then wait on combined."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(value)
                return True
        except Exception:
            pass
    combined = ", ".join(selectors)
    try:
        el = await page.wait_for_selector(combined, state="visible", timeout=timeout)
        if el:
            await el.fill(value)
            return True
    except Exception:
        pass
    return False


async def fast_select(page: Page, selectors: list, value: str) -> bool:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await page.select_option(sel, value=value)
                return True
        except Exception:
            pass
    return False


async def el_exists(page: Page, selector: str, timeout: int = 1500) -> bool:
    try:
        el = await page.wait_for_selector(selector, state="visible", timeout=timeout)
        return el is not None
    except Exception:
        return False


async def check_captcha(page: Page) -> bool:
    """Returns True if a captcha / bot-challenge is visible on the page."""
    for sel in [
        "iframe[src*='recaptcha']",   "iframe[src*='hcaptcha']",
        "iframe[src*='captcha']",      "iframe[src*='cloudflare-static']",
        "[class*='captcha']",          "[id*='captcha']",
        "[class*='cf-challenge']",     "[id*='cf-challenge']",
        "[class*='hcaptcha']",         "[id*='hcaptcha']",
        "input[name='h-captcha-response']",
        "input[name='g-recaptcha-response']",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            pass
    # URL-based check (Cloudflare challenge page)
    url = page.url.lower()
    if "challenge" in url or "captcha" in url:
        return True
    return False


async def dismiss_popups(page: Page):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Got it')",
        "[aria-label='Close dialog']",
        "button:has-text('No Thanks')",
        ".modal-close",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
        except Exception:
            pass


# ── JS-Based Checkout Step Detection ──────────────────────────────────────────
# Runs as a single page.evaluate() call — instant vs 4×2s sequential timeouts.
# IMPORTANT: only standard CSS selectors here — NO Playwright extensions like
# :has-text().  Those only work in Playwright's own selector engine, not inside
# document.querySelector which is what page.evaluate() uses.

_STEP_DETECT_JS = """() => {
    const has = s => {
        try { return !!document.querySelector(s); }
        catch(e) { return false; }
    };
    const url = location.href.toLowerCase();

    // ── Confirmed ──────────────────────────────────────────────────────────
    if (url.includes('confirmation') || url.includes('thank-you') ||
        url.includes('thankyou')     || url.includes('order-success') ||
        url.includes('order-complete'))
        return 'confirmed';

    // ── Review / Place-order step ──────────────────────────────────────────
    // Scan button text directly — :has-text is NOT valid browser CSS
    const btns = document.querySelectorAll(
        'button:not([disabled]), input[type=submit]:not([disabled])');
    for (const b of btns) {
        const t = (b.textContent || b.value || '').trim().toLowerCase();
        if (t === 'place order'    || t === 'place my order' ||
            t === 'submit order'   || t === 'complete order' ||
            t === 'confirm order'  || t === 'pay now'        ||
            t.includes('place order') || t.includes('submit order'))
            return 'review';
    }

    // ── Payment ────────────────────────────────────────────────────────────
    // Note: NO trailing comma inside the selector string (causes SyntaxError)
    if (has("input[name='cardNumber'], input[name='card_number'], input[autocomplete='cc-number']") ||
        has("input[autocomplete='cc-exp'], input[autocomplete='cc-csc']") ||
        has("iframe[src*='stripe'], iframe[src*='braintree'], iframe[src*='paypal']") ||
        has("[class*='payment-form'], [id*='payment-form'], [class*='PaymentForm']"))
        return 'payment';

    // ── Shipping method ────────────────────────────────────────────────────
    if (has("input[type='radio'][name*='ship'], input[type='radio'][name*='deliver']") ||
        has("[class*='shipping-method'], [id*='shipping-method'], [class*='ShippingMethod']"))
        return 'shipping_method';

    // ── Shipping address ───────────────────────────────────────────────────
    // Cover name= variants, autocomplete, and EE's "Recipient First Name" placeholders
    if (has("input[name='firstName'], input[name='first_name'], input[name='address1']") ||
        has("input[autocomplete='given-name'], input[autocomplete='address-line1']") ||
        has("input[placeholder*='First Name'], input[placeholder*='first name']") ||
        has("input[placeholder*='Street'], input[placeholder*='P.O'], input[placeholder*='street']"))
        return 'shipping_address';

    // ── Email / contact info step (guest checkout — email only, no password) ──
    // Guard: only fire when no shipping fields are present (EE combines email+address
    // on one page, which is handled by shipping_address above).
    if (has("input[type='email']") && !has("input[type='password']") &&
        !has("input[placeholder*='First Name'], input[placeholder*='Street'], input[name='firstName'], input[name='address1']"))
        return 'email_step';

    // ── Login gate ─────────────────────────────────────────────────────────
    // No :has-text — check class names instead
    if (has("input[type='email']") && has("input[type='password']") &&
        has("[class*='login'], [class*='Login'], form[action*='login'], form[id*='login']"))
        return 'login_gate';

    // ── Guest / checkout gate ──────────────────────────────────────────────
    const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
    if (has("[class*='guest'], [class*='Guest']") ||
        bodyText.includes('guest checkout') ||
        bodyText.includes('continue as guest') ||
        bodyText.includes('checkout as guest'))
        return 'checkout_gate';

    return 'unknown';
}"""

async def detect_step(page: Page) -> str:
    try:
        return await page.evaluate(_STEP_DETECT_JS)
    except Exception:
        return "unknown"


# ── Login ──────────────────────────────────────────────────────────────────────
async def login(page: Page, email: str, password: str, cfg: dict) -> bool:
    log(f"Logging in: {email}", "STEP")
    t0 = time.time()
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded",
                        timeout=cfg["bot"].get("timeout_ms", 15000))
        await dismiss_popups(page)

        ok_e = await fast_fill(page, [
            "input[name='email']", "input[type='email']",
            "#email", "#CustomerEmail", "input[autocomplete='email']",
        ], email)

        ok_p = await fast_fill(page, [
            "input[name='password']", "input[type='password']",
            "#password", "#CustomerPassword", "input[autocomplete='current-password']",
        ], password)

        if not ok_e or not ok_p:
            log("Login fields not found — check LOGIN_URL", "ERROR")
            return False

        clicked = await fast_click(page, [
            "button[type='submit']",
            "button:has-text('Sign In')",
            "button:has-text('Log In')",
            "button:has-text('Login')",
            "input[type='submit']",
        ])
        if not clicked:
            await page.keyboard.press("Enter")

        await page.wait_for_load_state("domcontentloaded",
                                       timeout=cfg["bot"].get("timeout_ms", 15000))

        url = page.url
        if "login" in url or "sign-in" in url:
            for sel in [".alert-danger", ".error-message", "[class*='error']", ".flash-error"]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    log(f"Login error: {(await el.inner_text()).strip()}", "ERROR")
                    return False
            log("Still on login page — check credentials", "WARN")
            return False

        log(f"Login OK  ({time.time()-t0:.2f}s)", "OK")
        return True

    except Exception as e:
        log(f"Login exception: {e}", "ERROR")
        return False


async def session_valid(page: Page, cfg: dict) -> bool:
    """Quick auth check without a full page load."""
    try:
        await page.goto(BASE, wait_until="domcontentloaded",
                        timeout=cfg["bot"].get("timeout_ms", 15000))
        for sel in [
            "a[href*='logout']", "a[href*='/account/logout']",
            "[class*='account-name']", "[class*='user-name']",
            "a[href*='/account']:has-text('Hi')",
        ]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
    except Exception:
        pass
    return False


# ── Stock Detection ────────────────────────────────────────────────────────────
async def get_stock_status(page: Page) -> str:
    """
    Check ATC/Pre-order FIRST (the hot path), then OOS.
    This is faster when most checks are on items that ARE in stock.
    """
    # 1. Active Add-to-Cart (most important path — check first)
    for sel in [
        "button:has-text('Add to Cart')",
        "button:has-text('Add To Cart')",
        "button:has-text('Pre-Order')",
        "button:has-text('Pre-order')",
        "[data-action='add-to-cart']",
        "button[class*='AddToCart']:not([disabled])",
        "button[class*='add-to-cart']:not([disabled])",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                t = (await el.inner_text()).strip().lower()
                if "pre-order" in t or "preorder" in t:
                    return "pre_order"
                return "in_stock"
        except Exception:
            pass

    # 2. Out of stock signals
    for sel in [
        "text=Sold Out",
        "text=This item is sold out",
        "text=Out of Stock",
        "[class*='sold-out']",
        "[class*='out-of-stock']",
        "button[disabled]:has-text('Add to Cart')",
        "button[disabled]:has-text('Sold Out')",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return "out_of_stock"
        except Exception:
            pass

    return "unknown"


async def get_price(page: Page) -> Optional[float]:
    for sel in [
        "span[itemprop='price']",
        "[class*='sale-price']", "[class*='salePrice']",
        "[class*='product-price']", "[class*='productPrice']",
        "[class*='price']:not([class*='old']):not([class*='was']):not([class*='compare'])",
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                m = re.search(r"\$?([\d,]+\.?\d*)", text.replace(",", ""))
                if m:
                    return float(m.group(1))
        except Exception:
            pass
    return None


# ── Add to Cart ────────────────────────────────────────────────────────────────
async def add_to_cart(page: Page, quantity: int = 1) -> bool:
    log(f"ATC qty={quantity}...", "STEP")
    t0 = time.time()

    # Always try to set quantity — even for 1 (page default may differ)
    await fast_fill(page, [
        "input[name='quantity']", "input[name='qty']",
        "#quantity", "#Quantity", "input[type='number'][min]",
        "input[type='number']",
    ], str(quantity), timeout=2000)

    clicked = await fast_click(page, [
        "button:has-text('Add to Cart')",
        "button:has-text('Add To Cart')",
        "button:has-text('Pre-Order')",
        "button:has-text('Pre-order')",
        "[data-action='add-to-cart']",
        "button[class*='AddToCart']:not([disabled])",
        "button[class*='add-to-cart']:not([disabled])",
    ], timeout=5000)

    if not clicked:
        log("ATC button not found/disabled", "ERROR")
        return False

    # ── Wait for the ATC XHR/fetch to complete ─────────────────────────────
    # IMPORTANT: do NOT check for cart-count/badge existence — those elements
    # already live in the nav on every EE page (count=0), so a querySelector
    # check fires immediately and does NOT confirm the cart add succeeded.
    # networkidle is the definitive signal that all cart API calls are done.
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        await asyncio.sleep(1.5)   # fallback: flat wait if network never idles

    # Dismiss any "Continue Shopping" / "View Cart" / "Checkout" modal
    for sel in [
        "button:has-text('Continue Shopping')",
        "button:has-text('Keep Shopping')",
        "a:has-text('Continue Shopping')",
        "button:has-text('Continue Browsing')",
        "[class*='cart-modal'] button:has-text('Close')",
        "[class*='CartModal'] button:has-text('Close')",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                break
        except Exception:
            pass

    log(f"Added to cart  ({time.time()-t0:.2f}s)", "OK")
    return True


# ── Cart → Checkout  (skip the cart page, go direct) ──────────────────────────
async def go_to_checkout(page: Page, cfg: dict) -> bool:
    """
    Fastest path: navigate directly to /checkout.
    EE will redirect if cart is empty (we detect that below).
    """
    log("→ Checkout", "STEP")
    t0 = time.time()
    try:
        await page.goto(CHECKOUT_URL, wait_until="domcontentloaded",
                        timeout=cfg["bot"].get("timeout_ms", 15000))
    except Exception as e:
        log(f"Checkout nav error: {e}", "ERROR")
        return False

    url = page.url
    # Redirected back to cart/home = cart empty
    if "/cart" in url or url == BASE + "/" or "empty" in url:
        log("Cart appears empty after ATC!", "ERROR")
        return False

    log(f"At checkout  ({time.time()-t0:.2f}s) → {url}", "OK")
    return True


# ── Checkout Gate (login / guest selection) ────────────────────────────────────
async def handle_gate(page: Page, mode: str, cfg: dict) -> bool:
    step = await detect_step(page)
    if step not in ("login_gate", "checkout_gate"):
        return True  # already past the gate

    if mode == "guest":
        log("Guest checkout...", "STEP")
        clicked = await fast_click(page, [
            "button:has-text('Continue as Guest')",
            "button:has-text('Checkout as Guest')",
            "button:has-text('Guest Checkout')",
            "button:has-text('Continue as a Guest')",
            "button:has-text('Continue Without Account')",
            "a:has-text('Continue as Guest')",
            "a:has-text('Guest Checkout')",
            "a:has-text('Checkout as Guest')",
            "[class*='guest-checkout']",
            "[class*='guestCheckout']",
            "[id*='guest-checkout']",
            "[id*='guestCheckout']",
            "button:has-text('Guest')",
            "input[type='submit'][value*='guest' i]",
            "input[type='radio'][value*='guest' i]",
        ], timeout=6000)
        if clicked:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            log("Guest selected", "OK")
        else:
            log("Guest button not found — inspect checkout gate page", "WARN")
        return True

    # Account mode — fill inline login form
    email    = cfg["account"]["email"]
    password = cfg["account"]["password"]

    filled = await fast_fill(page, [
        "input[name='email']", "input[type='email']",
        "#email", "#checkout-email",
    ], email, timeout=3000)

    if not filled:
        return True  # not a login gate, proceed

    await fast_fill(page, [
        "input[name='password']", "input[type='password']",
        "#password", "#checkout-password",
    ], password)

    clicked = await fast_click(page, [
        "button:has-text('Sign In')",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "button:has-text('Sign in')",
        "button:has-text('Continue')",
        "[class*='login'] button[type='submit']",
        "[id*='login'] button[type='submit']",
        "form[action*='login'] button[type='submit']",
    ], timeout=5000)
    if not clicked:
        # Last resort — press Enter in the password field
        try:
            el = await page.query_selector("input[type='password']")
            if el:
                await el.press("Enter")
        except Exception:
            pass
    await page.wait_for_load_state("domcontentloaded",
                                   timeout=cfg["bot"].get("timeout_ms", 15000))
    return True


# ── Shipping Address Step ──────────────────────────────────────────────────────
async def fill_shipping(page: Page, s: dict, email: str = "") -> bool:
    """
    Fill EE's shipping address form.
    EE layout (from live site):
      - Your Email  (confirmation will be sent here)
      - Recipient First Name  /  City
      - Recipient Last Name   /  State dropdown
      - Company (Optional)    /  Zip / Postal Code
      - Street and number, P.O.box, c/o.  /  Country dropdown  /  Phone
      - Apartment, suite, unit, etc. (Optional)
    """
    log("Filling shipping...", "STEP")
    t0 = time.time()

    # Email — EE puts this at the very top of the shipping form for guest checkout
    if email:
        await fast_fill(page, [
            "input[type='email']",
            "input[name='email']", "input[name='Email']",
            "input[autocomplete='email']",
            "#email",
            "input[placeholder*='Email']", "input[placeholder*='email']",
        ], email, timeout=2000)

    fields = [
        # First name — EE uses "Recipient First Name" as placeholder
        (["input[name='firstName']",  "input[name='first_name']",
          "input[name='recipientFirstName']",
          "input[autocomplete='given-name']",
          "#firstName", "#first-name",
          "input[placeholder*='First Name']", "input[placeholder*='first name']"],
         s["first_name"]),

        # Last name — EE uses "Recipient Last Name" as placeholder
        (["input[name='lastName']",   "input[name='last_name']",
          "input[name='recipientLastName']",
          "input[autocomplete='family-name']",
          "#lastName", "#last-name",
          "input[placeholder*='Last Name']", "input[placeholder*='last name']"],
         s["last_name"]),

        # Company (optional)
        (["input[name='company']", "input[name='Company']",
          "input[autocomplete='organization']",
          "#company",
          "input[placeholder*='Company']", "input[placeholder*='company']"],
         s.get("company", "")),

        # Street — EE: "Street and number, P.O.box, c/o."
        (["input[name='address1']",  "input[name='address_1']",
          "input[name='street']",    "input[name='streetAddress']",
          "input[autocomplete='address-line1']",
          "#address1", "#street",
          "input[placeholder*='Street']", "input[placeholder*='P.O']",
          "input[placeholder*='street']", "input[placeholder*='address']"],
         s["address1"]),

        # Apt / Suite — EE: "Apartment, suite, unit, etc. (Optional)"
        (["input[name='address2']",  "input[name='address_2']",
          "input[name='apartment']", "input[name='suite']",
          "input[autocomplete='address-line2']",
          "#address2", "#apartment",
          "input[placeholder*='Apartment']", "input[placeholder*='suite']",
          "input[placeholder*='apt']",       "input[placeholder*='unit']"],
         s.get("address2", "")),

        # City
        (["input[name='city']",
          "input[autocomplete='address-level2']",
          "#city",
          "input[placeholder*='City']", "input[placeholder*='city']"],
         s["city"]),

        # ZIP — EE: "Zip / Postal Code"
        (["input[name='zip']",       "input[name='postalCode']",
          "input[name='postal_code']","input[name='zipCode']",
          "input[autocomplete='postal-code']",
          "#zip", "#zipcode", "#postalCode",
          "input[placeholder*='Zip']",    "input[placeholder*='Postal']",
          "input[placeholder*='zip']"],
         s["zip"]),

        # Phone
        (["input[name='phone']",    "input[name='Phone']",
          "input[type='tel']",
          "input[autocomplete='tel']",
          "#phone",
          "input[placeholder*='Phone']", "input[placeholder*='phone']"],
         s.get("phone", "")),
    ]

    for selectors, value in fields:
        if value:
            await fast_fill(page, selectors, value, timeout=3000)

    # State dropdown — value is usually the 2-letter code ("CA") even when
    # the label reads "CA - California"
    state = s.get("state", "CA")
    state_sels = [
        "select[name='state']",     "select[name='State']",
        "select[name='province']",  "select[name='stateCode']",
        "select[autocomplete='address-level1']", "#state",
    ]
    if not await fast_select(page, state_sels, state):
        # Try label format "CA - California"
        for sel in state_sels:
            try:
                el = await page.query_selector(sel)
                if el:
                    await page.select_option(sel, label=state)
                    break
            except Exception:
                pass
        else:
            await fast_fill(page, ["input[name='state']", "#state"], state, timeout=2000)

    # Country dropdown — default "United States" / "US"
    country = s.get("country", "US")
    country_sels = [
        "select[name='country']", "select[name='Country']",
        "select[name='countryCode']",
        "select[autocomplete='country']", "#country",
    ]
    if not await fast_select(page, country_sels, country):
        # Try label e.g. "United States"
        for sel in country_sels:
            try:
                el = await page.query_selector(sel)
                if el:
                    await page.select_option(sel, label="United States")
                    break
            except Exception:
                pass

    log(f"Shipping filled  ({time.time()-t0:.2f}s)", "OK")
    return True


async def submit_step(page: Page, cfg: dict) -> bool:
    clicked = await fast_click(page, [
        # Explicit text — most reliable
        "button:has-text('Continue to Payment')",
        "button:has-text('Continue to Shipping Methods')",
        "button:has-text('Continue to Shipping')",
        "button:has-text('Continue to Review')",
        "button:has-text('Save & Continue')",
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Proceed')",
        # Scoped submit — only inside checkout wrapper elements
        "[class*='checkout'] button[type='submit']",
        "[id*='checkout'] button[type='submit']",
        "[class*='shipping-form'] button[type='submit']",
        "[class*='payment-form'] button[type='submit']",
        # Absolute last resort — intentionally last to avoid newsletter/coupon forms
        "form:not([id*='newsletter']):not([class*='subscribe']) button[type='submit']",
    ], timeout=5000)
    if clicked:
        await page.wait_for_load_state("domcontentloaded",
                                       timeout=cfg["bot"].get("timeout_ms", 15000))
    return clicked


# ── Shipping Method Step ───────────────────────────────────────────────────────
async def select_shipping_method(page: Page, method: str = "standard") -> bool:
    log(f"Shipping method: {method}", "STEP")
    methods = {
        "standard":  ["input[value*='standard' i]", "input[id*='standard' i]",
                      "label:has-text('Standard')", "label:has-text('Economy')"],
        "ground":    ["input[value*='ground' i]",   "label:has-text('Ground')"],
        "expedited": ["input[value*='expedit' i]",  "input[value*='express' i]",
                      "label:has-text('Expedited')", "label:has-text('Priority')"],
    }
    for sel in methods.get(method, methods["standard"]):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                log(f"Method '{method}' selected", "OK")
                return True
        except Exception:
            pass
    # Fallback: first radio
    for sel in ["input[type='radio']:not([disabled])", "input[type='radio']"]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                log("Shipping: picked first option", "WARN")
                return True
        except Exception:
            pass
    return True


# ── Payment Step ───────────────────────────────────────────────────────────────
async def _fill_frame(frame: Frame, p: dict) -> bool:
    """Fill card details inside a payment iframe."""
    mm = p["expiry_month"].zfill(2)
    yy = p["expiry_year"][-2:]
    exp = f"{mm}/{yy}"

    pairs = [
        (["input[name='cardnumber']", "input[autocomplete='cc-number']",
          "input[name='number']", "input[placeholder*='card' i]"], p["card_number"]),
        (["input[name='exp-date']", "input[autocomplete='cc-exp']",
          "input[name='expiry']", "input[placeholder*='MM' i]"], exp),
        (["input[name='cvc']", "input[autocomplete='cc-csc']",
          "input[name='cvv']", "input[placeholder*='CVC' i]", "input[placeholder*='CVV' i]"], p["cvv"]),
    ]
    filled = 0
    for selectors, value in pairs:
        for sel in selectors:
            try:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    await el.fill(value)
                    filled += 1
                    break
            except Exception:
                pass
    return filled > 0


async def fill_payment(page: Page, p: dict, use_saved: bool) -> bool:
    log("Filling payment...", "STEP")
    t0 = time.time()

    # Try saved card first (fastest)
    if use_saved:
        for sel in [
            "[class*='saved-card']", "[class*='savedCard']",
            "[class*='saved-payment']",
            "input[type='radio'][value*='saved' i]",
            "label:has-text('Use saved card')",
            "label:has-text('Saved card')",
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    log(f"Saved payment selected  ({time.time()-t0:.2f}s)", "OK")
                    return True
            except Exception:
                pass

    mm   = p["expiry_month"].zfill(2)
    yyyy = p["expiry_year"]
    yy   = yyyy[-2:]
    exp  = f"{mm}/{yy}"

    # ── Iframes (Stripe, Braintree, etc.) ──
    # Check page.frames (already loaded iframes)
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if any(x in (frame.url or "") for x in
               ["stripe", "braintree", "paypal", "cardconnect", "authorize.net", "recurly"]):
            log(f"Payment iframe: {frame.url[:60]}", "INFO")
            if await _fill_frame(frame, p):
                log(f"Payment filled (iframe)  ({time.time()-t0:.2f}s)", "OK")
                return True

    # Check iframe elements by selector
    for iframe_sel in [
        "iframe[name*='card' i]", "iframe[src*='stripe' i]",
        "iframe[src*='braintree' i]", "iframe[title*='card' i]",
        "iframe[title*='number' i]", "iframe[title*='Credit' i]",
        "iframe[title*='Payment' i]",
    ]:
        try:
            el = await page.query_selector(iframe_sel)
            if el:
                frame = await el.content_frame()
                if frame and await _fill_frame(frame, p):
                    log(f"Payment filled (iframe element)  ({time.time()-t0:.2f}s)", "OK")
                    return True
        except Exception:
            pass

    # ── Direct page fields ──
    field_groups = [
        (["input[name='cardNumber']", "input[name='card_number']",
          "input[autocomplete='cc-number']", "#cardNumber", "#card-number",
          "input[name='number']"], p["card_number"]),
        (["input[name='expiry']", "input[autocomplete='cc-exp']",
          "input[name='expirationDate']", "#expiry", "#expirationDate"], exp),
        (["input[name='cvv']", "input[name='cvc']", "input[name='securityCode']",
          "input[autocomplete='cc-csc']", "#cvv", "#cvc", "#securityCode"], p["cvv"]),
        (["input[name='nameOnCard']", "input[name='name_on_card']",
          "input[name='cardholderName']", "input[autocomplete='cc-name']",
          "#nameOnCard", "#cardholder-name"], p["name_on_card"]),
    ]
    for selectors, value in field_groups:
        if value:
            await fast_fill(page, selectors, value, timeout=3000)

    # Separate month/year dropdowns
    await fast_select(page,
        ["select[name='expMonth']", "select[name='exp_month']",
         "select[name='expirationMonth']"], mm)
    await fast_select(page,
        ["select[name='expYear']", "select[name='exp_year']",
         "select[name='expirationYear']"], yyyy)

    log(f"Payment filled  ({time.time()-t0:.2f}s)", "OK")
    return True


# ── Place Order ────────────────────────────────────────────────────────────────
async def place_order(page: Page, cfg: dict) -> bool:
    log("Placing order...", "STEP")
    t0 = time.time()

    clicked = await fast_click(page, [
        "button:has-text('Place Order')",
        "button:has-text('Place My Order')",
        "button:has-text('Submit Order')",
        "button:has-text('Complete Order')",
        "button:has-text('Confirm Order')",
        "button:has-text('Review and Place Order')",
        "button:has-text('Pay Now')",
        "input[value='Place Order']",
        "#place-order", "#submit-order",
        "[class*='place-order']:not([disabled])",
        "[class*='placeOrder']:not([disabled])",
    ], timeout=8000)

    if not clicked:
        log("Place Order button not found!", "ERROR")
        return False

    # Wait for navigation to confirmation page
    try:
        await page.wait_for_url(
            re.compile(r"confirmation|thank-you|thankyou|order-success|order-complete"),
            timeout=25000,
        )
        log(f"Order placed — confirmation page reached  ({time.time()-t0:.2f}s)", "OK")
        return True
    except Exception:
        # Didn't redirect — settle the page and check where we actually are
        try:
            await page.wait_for_load_state("domcontentloaded",
                                           timeout=cfg["bot"].get("timeout_ms", 15000))
        except Exception:
            pass
        ok = await confirm_success(page)
        if not ok:
            log(f"No confirmation page detected after placing order — may have failed  (url={page.url})",
                "ERROR")
        return ok


async def confirm_success(page: Page) -> bool:
    url = page.url
    for kw in ["confirmation", "thank-you", "thankyou", "order-success",
               "order-complete", "success"]:
        if kw in url.lower():
            return True
    for sel in [
        "h1:has-text('Thank You')", "h1:has-text('Order Confirmed')",
        "h1:has-text('Order Placed')", "[class*='order-confirm']",
        "[class*='order-success']", "[class*='thank-you']",
        "text=Your order has been placed",
        "text=Order #",
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            pass
    log(f"Could not confirm success — check browser  (url={url})", "WARN")
    return False


# ── Full Checkout Orchestrator ─────────────────────────────────────────────────
async def run_checkout(page: Page, cfg: dict, target: dict) -> bool:
    co   = cfg["checkout"]
    bot  = cfg["bot"]
    mode = "guest" if bot.get("checkout_mode") == "guest" else "account"
    name = target["name"]
    t0   = time.time()

    def elapsed(): return f"{time.time()-t0:.2f}s"

    # 1 ── Product page + ATC
    url = target.get("url") or f"{BASE}/product/-/{target['item_number'].lower()}"
    await page.goto(url, wait_until="domcontentloaded",
                    timeout=bot.get("timeout_ms", 15000))
    await dismiss_popups(page)

    # EE renders the ATC button via JavaScript after domcontentloaded — wait for it
    # before attempting to click, so we don't race against the JS hydration.
    atc_ready_sels = [
        "button:has-text('Add to Cart')",
        "button:has-text('Add To Cart')",
        "button:has-text('Pre-Order')",
        "[data-action='add-to-cart']",
        "button[class*='AddToCart']:not([disabled])",
        "button[class*='add-to-cart']:not([disabled])",
    ]
    for sel in atc_ready_sels:
        try:
            await page.wait_for_selector(sel, state="visible",
                                         timeout=bot.get("timeout_ms", 15000))
            break
        except Exception:
            pass

    qty = target.get("quantity", 1)
    if not await add_to_cart(page, qty):
        return False

    # 2 ── Direct to checkout (skip cart page entirely)
    if not await go_to_checkout(page, cfg):
        return False

    # Let any JS-driven checkout shell fully render before the step detector runs
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # Resolve the guest contact email once (account email is the fallback)
    guest_email = (co.get("guest_email") or cfg["account"].get("email", ""))

    # 3 ── Step loop — JS detection + targeted handlers (max 10 iterations)
    await dismiss_popups(page)
    for iteration in range(10):
        # Let the page settle on first check and after gate-handled navigations
        if iteration == 0:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        step = await detect_step(page)
        log(f"Step {iteration+1}: {step}  [{elapsed()}]", "INFO", f"[{name}]")

        if step == "confirmed":
            success = await confirm_success(page)
            log(f"{'✅ ORDER PLACED' if success else '⚠ Status unclear'}  [{elapsed()}]",
                "OK" if success else "WARN", f"[{name}]")
            return success

        elif step in ("checkout_gate", "login_gate"):
            await handle_gate(page, mode, cfg)
            # Wait for the gate navigation to complete before re-detecting
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

        elif step == "email_step":
            # Guest contact info — EE asks for email before shipping
            log("Email/contact step...", "STEP")
            if guest_email:
                await fast_fill(page, [
                    "input[type='email']", "input[name='email']",
                    "#email", "#guest-email", "input[autocomplete='email']",
                ], guest_email)
            await submit_step(page, cfg)

        elif step == "shipping_address":
            # Pass email so fill_shipping can fill the "Your Email" field at the
            # top of EE's combined address form (guest mode always needs this;
            # account mode can skip it if EE pre-fills from the account)
            email_for_shipping = guest_email if mode == "guest" else (
                cfg["account"].get("email", "") if mode == "account" else ""
            )
            await fill_shipping(page, co["shipping"], email=email_for_shipping)
            await submit_step(page, cfg)

        elif step == "shipping_method":
            await select_shipping_method(page, co.get("shipping_method", "standard"))
            await submit_step(page, cfg)

        elif step == "payment":
            await fill_payment(page, co["payment"], co.get("use_saved_payment", False))
            await submit_step(page, cfg)

        elif step == "review":
            if not bot.get("auto_checkout", True):
                log(f"[DRY RUN] Cart ready — auto_checkout=false  [{elapsed()}]", "WARN")
                await asyncio.sleep(60)
                return True
            # Return immediately — never loop back to review (would double-order)
            return await place_order(page, cfg)

        else:  # unknown — wait for page to settle then re-detect
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

    log("Checkout loop hit max iterations", "ERROR")
    return False


# ── Monitor Task ───────────────────────────────────────────────────────────────
class MonitorTask:
    def __init__(self, target: dict, cfg: dict, pool: ProxyPool,
                 playwright, acquired: set, lock: asyncio.Lock):
        self.target  = target
        self.cfg     = cfg
        self.pool    = pool
        self.pw      = playwright
        self.acquired = acquired
        self.lock    = lock
        self.bot     = cfg["bot"]
        self.webhook = self.bot.get("discord_webhook_url", "")

    async def run(self):
        name          = self.target["name"]
        interval      = self.bot.get("monitor_interval_seconds", 5)
        retries       = self.bot.get("max_retries", 3)
        timeout       = self.bot.get("timeout_ms", 15000)
        rotate_every  = self.bot.get("proxy_rotate_interval", 15)
        jitter_pct    = self.bot.get("interval_jitter_pct", 20)
        is_guest      = self.bot.get("checkout_mode", "account") == "guest"
        is_headless   = self.bot.get("headless", True)

        storage = None if is_guest else (
            load_session() if self.bot.get("save_session") else None
        )

        proxy = self.pool.next() if self.pool else None
        if proxy:
            log(f"Proxy: {proxy['server']}", "INFO", f"[{name}]")

        # ── Helper: (re)create browser + context + page ───────────────────────
        async def _spawn():
            b   = await launch_browser(self.pw, self.bot, proxy)
            c   = await new_context(b, storage_state=storage)
            p   = await new_page(c, timeout, block_resources=True,
                                 headless=is_headless)
            return b, c, p

        browser, ctx, page = await _spawn()

        # ── Helper: gracefully close a browser (with timeout) ─────────────────
        async def _close(b):
            try:
                await asyncio.wait_for(b.close(), timeout=6.0)
            except Exception:
                pass

        # ── Helper: rotate to the next proxy and relaunch ─────────────────────
        async def _rotate(reason: str):
            nonlocal proxy, browser, ctx, page
            if not self.pool:
                return
            new_proxy = self.pool.next()
            if not new_proxy:
                return
            log(f"Proxy rotate ({reason}) → {new_proxy['server']}", "WARN", f"[{name}]")
            proxy = new_proxy
            old_b = browser
            browser, ctx, page = await _spawn()
            await _close(old_b)

        check_num        = 0
        consecutive_errs = 0

        try:
            # Login only in account mode when no valid session exists
            if not is_guest and not storage:
                ok = await login(page,
                                 self.cfg["account"]["email"],
                                 self.cfg["account"]["password"],
                                 self.cfg)
                if ok and self.bot.get("save_session"):
                    await save_session(ctx)
                    storage = load_session()

            product_url = (self.target.get("url") or
                           f"{BASE}/product/-/{self.target['item_number'].lower()}")

            # ── Monitor loop ──────────────────────────────────────────────────
            while True:
                async with self.lock:
                    if name in self.acquired:
                        break

                # ── Periodic proxy rotation ───────────────────────────────────
                if self.pool and proxy and rotate_every > 0 and check_num > 0 \
                        and check_num % rotate_every == 0:
                    await _rotate(f"scheduled every {rotate_every} checks")

                check_num += 1
                t_check = time.time()
                try:
                    # Small random pre-navigation pause — real users don't reload
                    # the instant a page finishes; this also breaks any fixed-
                    # cadence fingerprint that bot detectors look for.
                    await asyncio.sleep(random.uniform(0.4, 1.2))

                    await page.goto(product_url, wait_until="domcontentloaded",
                                    timeout=timeout)

                    # ── Captcha / bot-challenge detection ─────────────────────
                    if await check_captcha(page):
                        log(f"#{check_num:04d}  CAPTCHA detected — rotating proxy + backing off 30s",
                            "WARN", f"[{name}]")
                        await _rotate("captcha")
                        # Backoff to avoid hammering the challenge endpoint
                        if _STOP_EVENT is not None:
                            try:
                                await asyncio.wait_for(_STOP_EVENT.wait(), timeout=30)
                                break
                            except asyncio.TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(30)
                        consecutive_errs = 0
                        continue

                    status = await get_stock_status(page)
                    elapsed_s = time.time() - t_check
                    log(f"#{check_num:04d}  {status:<15s}  ({elapsed_s:.2f}s)",
                        "MON", f"[{name}]")
                    if _STATUS_CB:
                        try: _STATUS_CB(name, status, check_num, elapsed_s)
                        except Exception: pass

                    consecutive_errs = 0   # successful check — reset error counter

                    if status in ("in_stock", "pre_order"):
                        # Price guard
                        max_p = self.target.get("max_price", 0)
                        if max_p > 0:
                            price = await get_price(page)
                            if price and price > max_p:
                                log(f"Price ${price:.2f} > max ${max_p:.2f} — skip",
                                    "WARN", f"[{name}]")
                                continue

                        # Claim the target
                        async with self.lock:
                            if name in self.acquired:
                                break
                            self.acquired.add(name)

                        log(f"IN STOCK! → checkout (attempt 1/{retries})", "OK", f"[{name}]")
                        await notify(self.webhook,
                                     f"🔔 **{name}** in stock! Checking out now...", True)

                        # Fresh proxy for checkout (different IP from monitor)
                        chk_proxy = self.pool.next() if self.pool else proxy
                        if chk_proxy and chk_proxy.get("server") != (proxy or {}).get("server"):
                            log(f"Checkout proxy: {chk_proxy['server']}", "INFO", f"[{name}]")
                            chk_browser = await launch_browser(self.pw, self.bot, chk_proxy)
                            chk_ctx     = await new_context(chk_browser, storage_state=storage)
                        else:
                            chk_browser = browser
                            chk_ctx     = ctx

                        _CHK_HOSTS = {
                            "google-analytics.com", "googletagmanager.com",
                            "doubleclick.net", "facebook.com", "hotjar.com",
                        }
                        chk_page = await chk_ctx.new_page()
                        chk_page.set_default_timeout(timeout)
                        if is_headless:
                            async def _chk_route(route, req):
                                if req.resource_type in {"image", "media"}:
                                    await route.abort()
                                elif any(h in req.url for h in _CHK_HOSTS):
                                    await route.abort()
                                else:
                                    await route.continue_()
                        else:
                            async def _chk_route(route, req):
                                if any(h in req.url for h in _CHK_HOSTS):
                                    await route.abort()
                                else:
                                    await route.continue_()
                        await chk_page.route("**/*", _chk_route)

                        success = False
                        for attempt in range(1, retries + 1):
                            if attempt > 1:
                                log(f"Retry {attempt}/{retries}…", "WARN", f"[{name}]")
                            try:
                                success = await run_checkout(chk_page, self.cfg, self.target)
                            except Exception as e:
                                log(f"Checkout error attempt {attempt}: {e}",
                                    "ERROR", f"[{name}]")
                            if success:
                                break
                            await asyncio.sleep(0.5)

                        msg   = (f"✅ **{name}** ordered!" if success
                                 else f"❌ **{name}** failed after {retries} attempts")
                        log(msg, "OK" if success else "ERROR", f"[{name}]")
                        if (success and self.bot.get("notify_on_success")) or \
                           (not success and self.bot.get("notify_on_failure")):
                            await notify(self.webhook, msg, success)

                        if chk_browser is not browser:
                            await _close(chk_browser)
                        break

                except PWTimeout:
                    consecutive_errs += 1
                    log(f"#{check_num:04d}  timeout  (streak={consecutive_errs})",
                        "WARN", f"[{name}]")
                    if self.pool and consecutive_errs >= 2:
                        await _rotate(f"{consecutive_errs} consecutive timeouts")
                        consecutive_errs = 0

                except Exception as e:
                    consecutive_errs += 1
                    log(f"#{check_num:04d}  error: {e}  (streak={consecutive_errs})",
                        "WARN", f"[{name}]")
                    if self.pool and consecutive_errs >= 3:
                        await _rotate(f"{consecutive_errs} consecutive errors")
                        consecutive_errs = 0

                # ── Interval sleep with ±jitter, interruptible by stop event ──
                jitter    = interval * (jitter_pct / 100.0)
                sleep_for = max(1.0, interval + random.uniform(-jitter, jitter))
                if _STOP_EVENT is not None:
                    try:
                        await asyncio.wait_for(_STOP_EVENT.wait(), timeout=sleep_for)
                        log("Stop received — exiting monitor", "WARN", f"[{name}]")
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(sleep_for)

        finally:
            await _close(browser)


# ── Bot Entry ──────────────────────────────────────────────────────────────────
async def run_bot(cfg: dict):
    global _STOP_EVENT
    _STOP_EVENT = asyncio.Event()   # fresh event each run

    targets = [t for t in cfg["targets"] if t.get("enabled", True)]
    if not targets:
        log("No enabled targets!", "ERROR"); return

    bot     = cfg["bot"]
    proxies = bot.get("proxies", [])
    if isinstance(proxies, str) and proxies:
        proxies = [proxies]
    pool    = ProxyPool(proxies)

    banner(len(targets), bot.get("checkout_mode", "account"),
           len(pool), bot.get("monitor_interval_seconds", 5))

    acquired = set()
    lock     = asyncio.Lock()

    async with async_playwright() as pw:
        tasks = [
            MonitorTask(t, cfg, pool, pw, acquired, lock).run()
            for t in targets
        ]
        await asyncio.gather(*tasks)

    log("All tasks complete.", "OK")


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Entertainment Earth Checkout Bot v3")
    ap.add_argument("--url",              type=str,  help="Quick-checkout one URL")
    ap.add_argument("--headless",         action="store_true")
    ap.add_argument("--no-auto-checkout", action="store_true", help="Fill cart, don't place order")
    ap.add_argument("--guest",            action="store_true", help="Guest checkout mode")
    ap.add_argument("--proxy",            type=str,  help="Proxy: http://user:pass@host:port")
    ap.add_argument("--interval",         type=int,  help="Monitor interval seconds")
    ap.add_argument("--recon",            action="store_true", help="Run selector recon then exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.headless:           cfg["bot"]["headless"]                  = True
    if args.no_auto_checkout:   cfg["bot"]["auto_checkout"]             = False
    if args.guest:              cfg["bot"]["checkout_mode"]             = "guest"
    if args.proxy:              cfg["bot"]["proxies"]                   = [args.proxy]
    if args.interval:           cfg["bot"]["monitor_interval_seconds"]  = args.interval

    if args.url:
        cfg["targets"] = [{
            "name": args.url.rstrip("/").split("/")[-1],
            "url": args.url,
            "item_number": "",
            "max_price": 0,
            "quantity": 1,
            "enabled": True,
        }]

    if args.recon:
        import subprocess
        subprocess.run([sys.executable, str(_ROOT / "ee_recon.py")])
    else:
        asyncio.run(run_bot(cfg))
