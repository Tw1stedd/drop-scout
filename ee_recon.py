"""
EE Selector Reconnaissance Script
Run this ONCE to map the real Entertainment Earth checkout flow.
Outputs: ee_selectors.json with all discovered selectors.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT = Path(__file__).parent / "ee_selectors.json"
BASE = "https://www.entertainmentearth.com"

PRODUCT_URL = "https://www.entertainmentearth.com/product/-/fu9s91871ee"

async def snapshot(page, label: str, data: dict):
    url = page.url
    title = await page.title()
    print(f"\n{'='*60}")
    print(f"  [{label}]  {url}")
    print(f"  Title: {title}")
    print(f"{'='*60}")

    # Dump interesting selectors
    interesting = [
        # Login / auth
        "input[type='email']", "input[type='password']",
        "button[type='submit']", "form",
        # Cart / ATC
        "button[class*='cart']", "button[class*='Cart']",
        "[class*='add-to-cart']", "[class*='addToCart']",
        "[data-testid*='cart']", "[data-action*='cart']",
        "button[class*='add']",
        # Checkout steps
        "[class*='checkout']", "[class*='guest']",
        "[class*='step']", "[class*='Step']",
        # Shipping fields
        "input[name]", "select[name]",
        # Payment fields
        "iframe",
        # Order buttons
        "button[class*='order']", "button[class*='place']",
        "button[class*='submit']", "button[class*='pay']",
    ]

    found = {}
    for sel in interesting:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                attrs = await el.evaluate("""el => {
                    const obj = {};
                    for (const a of el.attributes) obj[a.name] = a.value;
                    return obj;
                }""")
                text = ""
                try:
                    text = (await el.inner_text()).strip()[:80]
                except:
                    pass
                entry = {"tag": tag, "attrs": attrs, "text": text}
                key = f"{tag}#{attrs.get('id','')}.{attrs.get('class','')[:40]}"
                found[key] = entry
        except:
            pass

    data[label] = {"url": url, "title": title, "elements": found}

    # Print summary
    for k, v in found.items():
        if v["text"] or v["attrs"].get("name") or v["attrs"].get("id"):
            print(f"  {v['tag']:8s} | id={v['attrs'].get('id',''):20s} | name={v['attrs'].get('name',''):20s} | text={v['text'][:40]}")

async def main():
    data = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        # ── 1. Product page ──────────────────────────────────────────
        print("\n[STEP 1] Loading product page...")
        await page.goto(PRODUCT_URL, wait_until="networkidle")
        await asyncio.sleep(1)
        await snapshot(page, "product_page", data)

        # Grab specific ATC and stock status
        atc_candidates = [
            "button:has-text('Add to Cart')", "button:has-text('Add To Cart')",
            "[class*='add-to-cart']", "[class*='addtocart']",
            "button:has-text('Pre-Order')", "button:has-text('Preorder')",
            "button:has-text('Notify')", "button:has-text('Alert')",
        ]
        data["product_page"]["atc_candidates"] = {}
        for sel in atc_candidates:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    txt = (await el.inner_text()).strip()
                    attrs = await el.evaluate("el => ({id:el.id, class:el.className, disabled:el.disabled, type:el.type})")
                    data["product_page"]["atc_candidates"][sel] = {"text": txt, **attrs}
                    print(f"  ATC candidate: [{sel}] text='{txt}' disabled={attrs.get('disabled')}")
            except:
                pass

        # ── 2. Homepage to find nav/login link ───────────────────────
        print("\n[STEP 2] Checking homepage for login link...")
        await page.goto(BASE, wait_until="domcontentloaded")
        await asyncio.sleep(1)

        nav_login = await page.query_selector_all("a[href*='account'], a[href*='login'], a[href*='sign'], [class*='login'], [class*='account']")
        data["nav_links"] = []
        for el in nav_login:
            href = await el.get_attribute("href") or ""
            text = (await el.inner_text()).strip()
            data["nav_links"].append({"href": href, "text": text})
            print(f"  Nav link: text='{text}' href='{href}'")

        # ── 3. Find and navigate to login page ───────────────────────
        login_urls = [
            f"{BASE}/account/login",
            f"{BASE}/login",
            f"{BASE}/sign-in",
            f"{BASE}/customer/account/login",
        ]
        login_found = None
        for url in login_urls:
            try:
                r = await page.goto(url, wait_until="domcontentloaded")
                if r and r.status == 200:
                    login_found = url
                    print(f"\n[STEP 3] Login page found at: {url}")
                    await asyncio.sleep(1)
                    await snapshot(page, "login_page", data)
                    break
            except:
                continue

        if not login_found:
            print("\n[STEP 3] Login page not found via direct URL — checking for login modal trigger...")
            # Try clicking account icon
            for sel in ["a[href*='account']", "[class*='account-icon']", "[aria-label*='account' i]", "[aria-label*='login' i]"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await asyncio.sleep(1)
                        await snapshot(page, "login_modal", data)
                        break
                except:
                    continue

        # ── 4. Cart page ──────────────────────────────────────────────
        print("\n[STEP 4] Loading cart page...")
        await page.goto(f"{BASE}/cart", wait_until="networkidle")
        await asyncio.sleep(1)
        await snapshot(page, "cart_page", data)

        # Look for checkout buttons
        checkout_btns = await page.query_selector_all("a[href*='checkout'], button:has-text('Checkout'), button:has-text('checkout')")
        data["cart_page"]["checkout_buttons"] = []
        for el in checkout_btns:
            href = await el.get_attribute("href") or ""
            text = (await el.inner_text()).strip()
            tag = await el.evaluate("el => el.tagName")
            data["cart_page"]["checkout_buttons"].append({"tag": tag, "href": href, "text": text})
            print(f"  Checkout btn: text='{text}' href='{href}'")

        # ── 5. Checkout page ──────────────────────────────────────────
        print("\n[STEP 5] Loading checkout page...")
        await page.goto(f"{BASE}/checkout", wait_until="networkidle")
        await asyncio.sleep(2)
        await snapshot(page, "checkout_page", data)

        # Look for guest checkout option
        guest_candidates = [
            "button:has-text('Guest')", "a:has-text('Guest')",
            "button:has-text('Continue as Guest')", "[value*='guest' i]",
            "[class*='guest']", "input[value*='guest' i]",
        ]
        data["checkout_page"]["guest_options"] = {}
        for sel in guest_candidates:
            try:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    attrs = await el.evaluate("el => ({id:el.id, class:el.className, href:el.href||''})")
                    data["checkout_page"]["guest_options"][sel] = {"text": txt, **attrs}
                    print(f"  Guest option: [{sel}] text='{txt}'")
            except:
                pass

        # ── 6. Pause for manual navigation ───────────────────────────
        print("\n" + "="*60)
        print("  MANUAL STEPS — Please do the following in the browser:")
        print("  1. Add any in-stock item to your cart")
        print("  2. Click Checkout / Proceed to Checkout")
        print("  3. Go through each checkout step")
        print("  When done, press ENTER here to capture final state.")
        print("="*60)
        input("  Press ENTER after completing checkout steps > ")

        await snapshot(page, "post_manual_checkout", data)

        # Save all discovered data
        with open(OUTPUT, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n[DONE] Selectors saved to: {OUTPUT}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
