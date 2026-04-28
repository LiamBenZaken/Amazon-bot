import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

import os
import json
from curl_cffi import requests
from bs4 import BeautifulSoup
import time
import re
import random
import threading
import concurrent.futures
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select

from products import PRODUCTS

load_dotenv()
console = Console()

TEST_MODE = False  # Set to False to actually buy things!

# One source of truth for the Amazon-as-seller whitelist (used in both the HTTP
# tracker and the phantom-restock guard inside buy_product).
VALID_SELLERS = ["ATVPDKIKX0DER", "A2XZ7JICGUQ1CX", "A11IL2PNWYJU7H"]

HISTORY_FILE = "history.json"

# How many years of order history to scan at startup. Most users only need 1-2;
# bump it if you have a long buying history of these specific items.
ORDER_SCAN_YEARS = 2


def save_counts_atomic(counts, path=HISTORY_FILE):
    """Write to a sibling .tmp file then os.replace — survives Ctrl+C mid-write."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(counts, f, indent=4)
    os.replace(tmp, path)


# Terminal statuses — orders in these states are NOT counted toward the cap.
# We only count orders that are still in flight (Ordered / Preparing /
# Shipping / Arriving), so once a unit is delivered, refunded, cancelled, or
# returned, the slot frees up and the bot can buy again.
TERMINAL_ORDER_STATES = (
    "delivered",
    "cancelled", "canceled",
    "refunded",
    "returned",
)


def _status_text_for_card(card):
    """Pull just the shipment-status text out of an order card so we don't
    false-match terminal keywords against unrelated body text (e.g. 'Delivered
    to <address>' or 'Buy it again' widgets near the bottom of the card)."""
    candidates = card.select(
        ".delivery-box .a-text-bold, "
        ".a-color-success.a-text-bold, "
        ".js-shipment-info-container .a-text-bold, "
        "div[data-component='orderCard'] .a-text-bold, "
        ".shipment .a-text-bold"
    )
    parts = [c.get_text(" ", strip=True) for c in candidates if c.get_text(strip=True)]
    if parts:
        return " | ".join(parts).lower()
    # Fallback: just the first ~300 chars so terminal-keyword matching doesn't
    # trip on stuff way down the card.
    return card.get_text(" ")[:300].lower()


def fetch_amazon_purchase_counts(driver, asins, lookback_years=ORDER_SCAN_YEARS, debug=True):
    """Scan 'Your Orders' and count units that are still IN PROGRESS (not
    delivered/cancelled/refunded/returned). Returns {asin: int} on success,
    or None if Amazon blocked the page with re-auth/captcha (caller should
    fall back to history.json in that case).

    Tries broader filters first ('Past 3 months', 'Last 30 days') because
    they reliably contain in-flight orders; only falls back to year filters
    if those return nothing for our tracked ASINs."""
    asin_set = set(asins)
    counts = {asin: 0 for asin in asin_set}
    current_year = time.localtime().tm_year

    filters = ["months-3", "last-30"]
    for y in range(current_year, current_year - lookback_years, -1):
        filters.append(f"year-{y}")

    debug_dir = "/tmp/amazon_bot_orders"
    if debug:
        try:
            os.makedirs(debug_dir, exist_ok=True)
        except Exception:
            pass

    for filt in filters:
        for page in range(50):  # hard safety cap on pagination
            url = (
                "https://www.amazon.com/gp/your-account/order-history"
                f"?orderFilter={filt}&startIndex={page * 10}&unifiedOrders=1"
            )
            try:
                driver.get(url)
            except Exception as e:
                console.print(f"[dim red]Order-scan navigation error: {e}[/dim red]")
                return None
            time.sleep(3.5)

            page_src = driver.page_source
            page_lower = page_src.lower()
            if (
                "ap_signin_form" in page_src
                or "robot check" in page_lower
                or "/ap/signin" in driver.current_url.lower()
            ):
                console.print("[yellow]⚠️ Amazon asked to re-auth while scanning orders. Skipping order scan, falling back to history.json.[/yellow]")
                return None

            if debug:
                try:
                    with open(f"{debug_dir}/orders_{filt}_p{page}.html", "w") as f:
                        f.write(page_src)
                except Exception:
                    pass

            soup = BeautifulSoup(page_src, "lxml")
            # Broader selector list — Amazon has rotated through layouts.
            order_cards = soup.select(
                "div.order-card, div.js-order-card, "
                "div[data-component='orderCard'], "
                "div.order, div[class*='order-card']"
            )
            if debug:
                console.print(f"[dim]  · filter={filt} page={page}: {len(order_cards)} order card(s)[/dim]")
            if not order_cards:
                break

            for card in order_cards:
                # Does this card even contain a tracked ASIN? Skip cheaply if not.
                hrefs_blob = " ".join(a.get("href", "") for a in card.select("a[href]"))
                touched_asins = [a for a in asin_set if a in hrefs_blob]
                if not touched_asins:
                    continue

                status = _status_text_for_card(card)
                if any(kw in status for kw in TERMINAL_ORDER_STATES):
                    if debug:
                        console.print(f"[dim]    skip (terminal: {status[:80]!r}) — contained {touched_asins}[/dim]")
                    continue

                # Amazon order cards usually have BOTH the product image link
                # AND the product title link pointing to the same /dp/ASIN —
                # so we'd double-count without per-card ASIN dedup. Track which
                # ASINs we've already counted in *this* card and only add once.
                seen_asins_in_card = set()
                for a in card.select('a[href*="/dp/"], a[href*="/gp/product/"]'):
                    m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', a.get("href", ""))
                    if not m or m.group(1) not in asin_set:
                        continue
                    asin = m.group(1)
                    if asin in seen_asins_in_card:
                        continue
                    seen_asins_in_card.add(asin)
                    qty = 1
                    item_block = a.find_parent(["div", "li"])
                    if item_block:
                        qmatch = re.search(
                            r'(?:qty|quantity)[:\s]*(\d+)',
                            item_block.get_text(" ").lower(),
                        )
                        if qmatch:
                            qty = int(qmatch.group(1))
                    counts[asin] += qty
                    if debug:
                        console.print(f"[dim]    + {asin} qty={qty} (status: {status[:60]!r})[/dim]")

            next_btn = soup.select_one("li.a-last:not(.a-disabled) a")
            if not next_btn:
                break

        # Once any tracked ASIN has been counted, broader filter searches stop.
        if any(c > 0 for c in counts.values()):
            break

    if debug:
        console.print(f"[dim]  (debug HTML dumped to {debug_dir}/ — open it if a count still looks wrong)[/dim]")
    return counts

def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    def _send():
        try:
            # curl_cffi requests acts exactly like standard requests here
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            console.print(f"[dim red]Failed to send Telegram alert: {e}[/dim red]")

    # Background daemon — never blocks the buy hot path on slow Telegram responses.
    threading.Thread(target=_send, daemon=True).start()

class AmazonAutoBuyer:
    def __init__(self):
        self.profile_dir = os.path.join(os.getcwd(), "chrome_profile")
        self.driver = None

    def get_driver(self):
        if not self.driver:
            options = uc.ChromeOptions()
            options.add_argument("--disable-popup-blocking")
            # blink-settings to completely block image rendering for speed
            options.add_argument('--blink-settings=imagesEnabled=false')
            
            # eager strategy prevents waiting for full page load
            options.page_load_strategy = 'eager'
            
            # Disable CSS/stylesheets via prefs to prevent layout rendering delays
            prefs = {
                "profile.managed_default_content_settings.images": 2,
                "profile.managed_default_content_settings.stylesheets": 2,
            }
            options.add_experimental_option("prefs", prefs)

            # Pass version_main=146 to match the local Chrome version
            self.driver = uc.Chrome(options=options, user_data_dir=self.profile_dir, version_main=146)
        return self.driver

    def setup_login(self):
        console.print("\n[cyan]🌐 Opening Amazon to check login status...[/cyan]")
        driver = self.get_driver()
        
        driver.get("https://www.amazon.com/")
        time.sleep(3)
        
        # Simple check to see if "Sign in" appears in the navigation area
        if "Sign in" in driver.page_source or "sign in" in driver.page_source.lower():
            console.print("[bold yellow]⚠️ You are NOT logged in![/bold yellow]")
            console.print("[bold cyan]Please log in manually in the browser window that just opened.[/bold cyan]")
            console.print("[bold cyan]Handle any OTP or Captcha. Press ENTER in this console ONLY when you are fully logged in.[/bold cyan]")
            console.print("[bold magenta]Make sure to check 'Keep me signed in' if you see it![/bold magenta]")
            
            try:
                sign_in_link = driver.find_element(By.XPATH, "//a[@data-nav-role='signin']")
                sign_in_link.click()
            except:
                pass
                
            input("Press ENTER here when you are logged in and see the Amazon homepage...")
            console.print("[bold green]✅ Login saved to profile![/bold green]")
        else:
            console.print("[bold green]✅ Already logged in from previous session![/bold green]")
            
        # WE DO NOT QUIT THE DRIVER HERE. 
        # We keep the browser open in the background so the session is never lost!
        console.print("[dim]Browser will remain open in the background for fast auto-checkout.[/dim]")

    def buy_product(self, url, target_price, quantity=1, max_retries=10):
        console.print(f"\n[bold red]🛒 INITIATING AUTO-BUY FOR {url} (Qty: {quantity})[/bold red]")
        driver = self.get_driver() # Re-uses the open browser!
        
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                console.print(f"[bold yellow]🔄 RETRY ATTEMPT {attempt}/{max_retries}...[/bold yellow]")
                
            try:
                driver.get(url)
                
                if quantity > 1:
                    console.print(f"[cyan]⚡ Instant JS: Changing quantity to {quantity}...[/cyan]")
                    try:
                        qty_element = WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.NAME, "quantity"))
                        )
                        # Execute JS to bypass UI dropdown delay
                        driver.execute_script(f"arguments[0].value = '{quantity}';", qty_element)
                        driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles: true}));", qty_element)
                    except Exception as e:
                        console.print(f"[bold red]⚠️ Could not change quantity! Defaulting to 1. Error: {e}[/bold red]")
                        
                console.print("[yellow]⚡ Waiting for DOM presence of 'Buy Now' button...[/yellow]")
                # Use presence_of_element_located to trigger BEFORE rendering
                buy_now = WebDriverWait(driver, 10, poll_frequency=0.05).until(
                    EC.presence_of_element_located((By.ID, "buy-now-button"))
                )
                
                # --- NEW SAFEGUARD: DOM Double-Check ---
                console.print("[cyan]🔍 Double-checking DOM for Phantom Restock...[/cyan]")
                try:
                    merchant_input = driver.find_element(By.ID, "merchantID")
                    merchant_val = merchant_input.get_attribute("value")
                    if merchant_val not in VALID_SELLERS:
                        console.print(f"[bold red]🛑 PHANTOM RESTOCK ABORT: Selenium loaded a 3rd Party Seller ({merchant_val})![/bold red]")
                        return False
                except Exception as e:
                    console.print(f"[bold yellow]⚠️ Could not verify Merchant DOM, clicking anyway...[/bold yellow]")

                # JS click bypasses UI visibility/clickable checks
                driver.execute_script("arguments[0].click();", buy_now)
                
                console.print("[yellow]💳 Proceeding to checkout...[/yellow]")
                
                # Fast custom polling to instantly detect either the Place Order button OR the Password screen.
                # This completely removes the previous 3-second hard delay!
                timeout = time.time() + 15
                password_handled = False
                place_order = None
                
                while time.time() < timeout:
                    try:
                        place_order = driver.find_element(By.NAME, "placeYourOrder1")
                        if place_order.is_displayed() and place_order.is_enabled():
                            break # Found the final button!
                    except:
                        pass
                    
                    if not password_handled:
                        try:
                            password_field = driver.find_element(By.ID, "ap_password")
                            if password_field.is_displayed():
                                console.print("[bold yellow]⚠️ Amazon is asking for your password again for security![/bold yellow]")
                                password = os.getenv("AMAZON_PASSWORD")
                                if password:
                                    console.print("[green]Auto-filling password from .env...[/green]")
                                    password_field.send_keys(password)
                                    driver.find_element(By.ID, "signInSubmit").click()
                                    password_handled = True # Avoid filling it twice
                                else:
                                    console.print("[bold red]No password in .env! Please type it manually in the browser![/bold red]")
                                    time.sleep(15) 
                        except:
                            pass
                            
                    time.sleep(0.05) # Sleep just 50ms before checking again
                    
                if not place_order:
                    raise Exception("Timed out waiting for 'Place Your Order' button.")
                
                console.print("\n[bold green]💸 JS INJECTION: CLICKING 'PLACE YOUR ORDER' NOW![/bold green]")
                driver.execute_script("arguments[0].click();", place_order)
                
                console.print("[yellow]⏳ Verifying order placement with Amazon...[/yellow]")
                
                verify_timeout = time.time() + 15
                order_success = False
                
                while time.time() < verify_timeout:
                    current_url = driver.current_url.lower()
                    if "thankyou" in current_url or "buy/thankyou" in current_url or "order-confirmation" in current_url:
                        order_success = True
                        break
                        
                    try:
                        error_box = driver.find_element(By.ID, "alert-box-message")
                        if error_box.is_displayed():
                            console.print(f"[bold red]❌ Amazon rejected the order: {error_box.text.strip()}[/bold red]")
                            return False
                    except:
                        pass
                        
                    try:
                        error_box2 = driver.find_element(By.CSS_SELECTOR, ".a-box.a-alert-error")
                        if error_box2.is_displayed():
                            console.print(f"[bold red]❌ Amazon checkout error: {error_box2.text.strip()}[/bold red]")
                            return False
                    except:
                        pass
                        
                    time.sleep(0.5)
                    
                if order_success:
                    console.print("[bold green]🎉 Order confirmed by Amazon![/bold green]")
                    return True
                else:
                    console.print("[bold red]❌ Order verification timed out. Assuming failure to prevent false counts.[/bold red]")
                    return False
                
            except Exception as e:
                console.print(f"[bold red]❌ Error during auto-buy on attempt {attempt}: {e}[/bold red]")
                
        console.print("[bold red]⛔ All auto-buy attempts failed for this item.[/bold red]")
        return False # Failed all attempts

class AmazonTLSTracker:
    def __init__(self, cookies=None):
        console.print("\n[bold cyan]🚀 Booting up TLS Tracker...[/bold cyan]")

        self.session = requests.Session(impersonate="chrome120")
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"'
        })
        
        if cookies:
            console.print("[cyan]🍪 Injecting Amazon Login Cookies into Tracker...[/cyan]")
            for cookie in cookies:
                # curl_cffi session cookies can be set via self.session.cookies.set()
                self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))

        self.exchange_rate = self.get_live_exchange_rate()
        self._start_rate_refresher()

    def _start_rate_refresher(self):
        """Refresh USD↔ILS rate every 30 min in a daemon thread so long runs
        don't compare against a stale rate from startup."""
        def loop():
            while True:
                time.sleep(1800)
                try:
                    new_rate = self.get_live_exchange_rate()
                    if new_rate:
                        self.exchange_rate = new_rate
                except Exception as e:
                    console.print(f"[dim red]Rate refresh failed: {e}[/dim red]")
        threading.Thread(target=loop, daemon=True).start()

    def get_live_exchange_rate(self):
        try:
            console.print("[yellow]💱 Fetching live exchange rate...[/yellow]")
            response = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
            data = response.json()
            ils_rate = data['rates']['ILS']
            console.print(f"[bold green]✅ Live Rate Acquired: 1 USD = {ils_rate} ILS[/bold green]")
            return ils_rate
        except Exception as e:
            console.print(f"[bold red]⚠️ Failed to fetch live rate, falling back to 3.65. Error: {e}[/bold red]")
            return 3.65

    def _parse_price_text(self, raw):
        """Strip currency, convert ILS→USD if needed. Returns (usd_price, raw)
        or (None, raw) if unparseable."""
        if not raw:
            return None, raw
        clean = re.sub(r'[^\d.]', '', raw)
        if not clean:
            return None, raw
        try:
            n = float(clean)
        except ValueError:
            return None, raw
        if "ILS" in raw or "₪" in raw:
            n = n / self.exchange_rate
        return n, raw

    def _check_aod(self, item):
        """Tier-1 cheap probe: hits /gp/aod/ajax/?asin=ASIN — Amazon's offer-listing
        fragment. ~5x smaller payload than /dp/, ~2x more permissive rate-limit
        budget (community-observed). Returns same shape as _check_dp on success,
        or None to signal 'AOD didn't give us a usable answer, escalate to /dp/'."""
        asin = item["id"]
        aod_url = f"https://www.amazon.com/gp/aod/ajax/?asin={asin}&pc=dp"
        start = time.time()
        try:
            r = self.session.get(
                aod_url,
                timeout=10,
                headers={"Referer": item["url"]},
            )
        except Exception as e:
            return None  # network error → caller falls back to /dp/

        if r.status_code != 200 or not r.content:
            return None

        soup = BeautifulSoup(r.content, "lxml")

        # The pinned (top) offer is the buy-box winner. If absent, try the first
        # entry in the offer list.
        pinned = (
            soup.find("div", {"id": "aod-pinned-offer"})
            or soup.select_one("div[id^='aod-offer'], div.aod-offer")
        )
        if not pinned:
            return None  # unfamiliar layout → escalate

        merchant = pinned.find("input", {"id": "aod-offer-soldBy-merchantID"})
        if not merchant:
            merchant = pinned.find("input", {"id": "merchantID"})
        merchant_val = merchant.get("value") if merchant else None

        if merchant_val and merchant_val not in VALID_SELLERS:
            return {"item": item, "price": None, "time": time.time() - start,
                    "error": "3rd Party Seller", "tier": "aod"}

        price_el = pinned.find("span", {"class": "a-offscreen"})
        raw = price_el.text.strip() if price_el else ""
        usd, raw = self._parse_price_text(raw)
        if usd is None:
            return None  # couldn't read price → escalate

        return {"item": item, "price": usd, "time": time.time() - start,
                "error": None, "raw": raw, "tier": "aod"}

    def _check_dp(self, item):
        """Tier-2: full /dp/ page. Authoritative — same parse the buy hot path
        uses. Heavier (~300KB vs ~30-60KB for AOD) but matches Selenium's view."""
        url = item["url"]
        try:
            start_time = time.time()
            response = self.session.get(url, timeout=10)

            if response.status_code != 200:
                return {"item": item, "price": None, "time": time.time() - start_time,
                        "error": f"Status: {response.status_code}", "tier": "dp"}

            soup = BeautifulSoup(response.content, "lxml")

            add_to_cart = soup.find("input", {"id": "add-to-cart-button"})
            buy_now = soup.find("input", {"id": "buy-now-button"})

            if not add_to_cart and not buy_now:
                return {"item": item, "price": None, "time": time.time() - start_time,
                        "error": "Unavailable / Cannot ship", "tier": "dp"}

            # STRICT SELLER CHECK: Guarantee the seller is Amazon or Amazon Export Sales LLC
            merchant_input = soup.find("input", {"id": "merchantID"})
            if merchant_input and merchant_input.get("value") not in VALID_SELLERS:
                return {"item": item, "price": None, "time": time.time() - start_time,
                        "error": "3rd Party Seller", "tier": "dp"}

            price_element = soup.find("span", {"class": "a-offscreen"})
            if not price_element:
                return {"item": item, "price": None, "time": time.time() - start_time,
                        "error": "Could not find price tag", "tier": "dp"}

            raw = price_element.text.strip()
            usd, raw = self._parse_price_text(raw)
            if usd is None:
                return {"item": item, "price": None, "time": time.time() - start_time,
                        "error": "Could not parse price", "tier": "dp"}

            return {"item": item, "price": usd, "time": time.time() - start_time,
                    "error": None, "raw": raw, "tier": "dp"}

        except Exception as e:
            return {"item": item, "price": None, "time": 0,
                    "error": f"Network Error: {e}", "tier": "dp"}

    def check_price(self, item):
        """Two-tier check: cheap AOD probe first, then escalate to the full
        /dp/ page only when AOD signals a possible deal (or AOD couldn't give
        us a clear answer). The /dp/ confirmation matches what Selenium will
        see at buy time, preserving the phantom-restock guarantee."""
        target = item["target"]
        aod = self._check_aod(item)

        if aod is None:
            # AOD failed / unfamiliar layout — fall back to /dp/.
            return self._check_dp(item)

        if aod.get("error"):
            # AOD said 3rd-party / errored. Trust it (cheaper).
            return aod

        if aod.get("price") is not None and aod["price"] > target * 1.10:
            # Comfortably above target — AOD answer is good enough, save the /dp/ hit.
            return aod

        # AOD shows price near or below target — escalate for the authoritative read.
        dp = self._check_dp(item)
        # If /dp/ disagrees (still 3rd-party, OOS, etc.), return /dp/ as authoritative.
        return dp


def main():
    console.print("\n[bold cyan]🔧 Setting up Amazon AutoBuyer...[/bold cyan]")
    buyer = AmazonAutoBuyer()
    buyer.setup_login()

    console.print("\n[dim]Extracting session cookies from AutoBuyer...[/dim]")
    session_cookies = buyer.get_driver().get_cookies()

    tracker = AmazonTLSTracker(cookies=session_cookies)

    purchased_counts = {item_id: 0 for item_id in PRODUCTS.keys()}

    # The cap is per-account, in-flight: a unit only counts while it's still
    # being delivered. Once delivered (or cancelled/refunded/returned), the
    # slot frees up and the bot may buy again. Source-of-truth = Amazon
    # 'Your Orders' page. history.json is only a fallback for when the
    # orders scan is blocked (re-auth/captcha) or for crash recovery during
    # a run before Amazon's orders page has propagated a brand-new buy.
    console.print(f"\n[cyan]🔎 Scanning your Amazon in-progress orders (last {ORDER_SCAN_YEARS} year(s))...[/cyan]")
    amazon_counts = fetch_amazon_purchase_counts(
        buyer.get_driver(), list(PRODUCTS.keys()), lookback_years=ORDER_SCAN_YEARS,
    )

    if amazon_counts is not None:
        # Scan succeeded — Amazon is authoritative. Wipe stale history.json
        # state so delivered orders correctly free up slots.
        for asin in PRODUCTS:
            purchased_counts[asin] = amazon_counts.get(asin, 0)
        for asin, c in amazon_counts.items():
            label = "in-progress" if c > 0 else "none in flight"
            console.print(f"[green]  ↳ {PRODUCTS[asin]['name']}: {c} {label}[/green]")
        save_counts_atomic(purchased_counts, HISTORY_FILE)
    else:
        # Fall back to history.json (best-effort) when scan was blocked.
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    history_counts = json.load(f)
                for k, v in history_counts.items():
                    if k in purchased_counts:
                        purchased_counts[k] = int(v)
                console.print(f"[green]📂 Fallback: loaded counts from {HISTORY_FILE}[/green]")
            except Exception as e:
                console.print(f"[bold red]⚠️ Failed to load history.json: {e}[/bold red]")

    # Print starting state so the user sees exactly where they're at.
    start_table = Table(title="📊 Starting state (lifetime per account)", header_style="bold magenta")
    start_table.add_column("Product", style="cyan")
    start_table.add_column("Already bought", justify="right")
    start_table.add_column("Max", justify="right")
    start_table.add_column("Remaining", justify="right", style="green")
    for asin, p in PRODUCTS.items():
        c = purchased_counts.get(asin, 0)
        rem = max(0, p["max_limit"] - c)
        start_table.add_row(p["name"], str(c), str(p["max_limit"]), str(rem))
    console.print(start_table)

    loop_count = 1

    # State tracker to prevent Telegram spam.
    active_deals = {item_id: False for item_id in PRODUCTS.keys()}
    # Per-item flag: did the LAST buy attempt within the current below-target
    # window fail? If yes, don't re-attempt on every subsequent loop while the
    # price stays below target — that would burst-poll Amazon. Reset when the
    # price recovers above target.
    last_buy_failed = {item_id: False for item_id in PRODUCTS.keys()}
    
    while True:
        console.print(f"\n[bold cyan]🚀 Firing TLS HTTP Requests Concurrently (Check #{loop_count})...[/bold cyan]\n")

        results = []
        
        start_all = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(PRODUCTS)) as executor:
            futures = {executor.submit(tracker.check_price, item): item for item in PRODUCTS.values()}
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        end_all = time.time()

        table = Table(title=f"Amazon Price Tracker Results (Check #{loop_count})", show_header=True, header_style="bold magenta")
        table.add_column("Product", style="cyan")
        table.add_column("Target Price", justify="right", style="blue")
        table.add_column("Current Price", justify="right")
        table.add_column("Status", justify="center")
        table.add_column("Tier", justify="center", style="dim")  # aod vs dp
        table.add_column("Time", justify="right", style="dim")

        for res in results:
            item = res["item"]
            item_id = item["id"]
            name = item["name"]
            target = f"${item['target']:.2f}"
            bought_so_far = purchased_counts[item_id]
            max_limit = item["max_limit"]
            
            if res["error"]:
                current_price = f"[red]{res['error']}[/red]"
                status = f"[red]❌ Error ({bought_so_far}/{max_limit})[/red]"
            elif bought_so_far >= max_limit:
                current_price = f"[bold magenta]${res['price']:.2f}[/bold magenta]"
                status = f"[bold magenta]✅ MAXED ({bought_so_far}/{max_limit})[/bold magenta]"
            elif res["price"] is not None:
                price = res["price"]
                current_price = f"${price:.2f}"
                if price < item["target"]:
                    current_price = f"[bold green]{current_price}[/bold green]"
                    status = f"[bold green]🚨 BUY NOW! ({bought_so_far}/{max_limit})[/bold green]"
                else:
                    current_price = f"[yellow]{current_price}[/yellow]"
                    status = f"[yellow]⏳ Wait ({bought_so_far}/{max_limit})[/yellow]"
            else:
                current_price = "[red]N/A[/red]"
                status = "[red]❌ N/A[/red]"
                
            time_taken = f"{res['time']:.2f}s"
            tier = res.get("tier", "?")
            table.add_row(name, target, current_price, status, tier, time_taken)

        console.print(table)
        console.print(f"\n[bold cyan]⏱️ Total execution time: {end_all - start_all:.2f}s[/bold cyan]\n")

        # Alert and Buy Logic
        buy_alerts = []
        for res in results:
            item = res["item"]
            item_id = item["id"]
            
            if res.get("price") is not None:
                price = res["price"]
                
                # Check if it hit the target
                if price < item["target"]:

                    # Prevent Telegram spam: Only alert if it wasn't already an active deal
                    if not active_deals[item_id]:
                        active_deals[item_id] = True
                        alert_msg = f"🚨 <b>DEAL ALERT!</b>\n\n<b>{item['name']}</b> is below target!\nCurrent Price: ${price:.2f}\n\n🛒 <a href='{item['url']}'>Click here to buy manually!</a>"
                        send_telegram_alert(alert_msg)

                    # Only enqueue a buy if (a) we haven't maxed out, AND
                    # (b) the LAST attempt in this same below-target window
                    # didn't already fail (otherwise we'd burst-retry on every
                    # loop — which is what hammered the bot in TEST_MODE).
                    if (
                        purchased_counts[item_id] < item["max_limit"]
                        and not last_buy_failed[item_id]
                    ):
                        # Determine quantity from rules
                        qty_to_buy = 1
                        for max_price, rule_qty in item["quantity_rules"]:
                            if price <= max_price:
                                qty_to_buy = rule_qty
                                break # Found the tier!

                        # Enforce the max limit!
                        remaining_allowed = item["max_limit"] - purchased_counts[item_id]
                        if qty_to_buy > remaining_allowed:
                            qty_to_buy = remaining_allowed

                        if qty_to_buy > 0:
                            res["buy_qty"] = qty_to_buy
                            buy_alerts.append(res)
                else:
                    # Price is above target. Reset deal state so we can alert again if it drops.
                    active_deals[item_id] = False
                    last_buy_failed[item_id] = False
            else:
                # Item is out of stock / unavailable. Reset deal state.
                active_deals[item_id] = False
                last_buy_failed[item_id] = False
        
        any_buy_succeeded = False
        if buy_alerts:
            for res in buy_alerts:
                item = res["item"]
                item_id = item["id"]
                qty = res["buy_qty"]

                console_alert = f"[bold green]🚨 DEAL ALERT! [{item['name']}] is below ${item['target']:.2f}! (Current: ${res['price']:.2f})[/bold green]\nBuying Quantity: {qty} | {item['url']}"
                console.print(Panel(console_alert, title="🎯 Target Reached", border_style="green"))

                # TRIGGER AUTO BUYER
                if TEST_MODE:
                    console.print("[bold yellow]🛠️ TEST MODE ACTIVE: Skipped actual checkout so you can test alerts![/bold yellow]")
                    success = False
                else:
                    success = buyer.buy_product(item["url"], item["target"], quantity=qty)

                if success:
                    any_buy_succeeded = True
                    purchased_counts[item_id] += qty
                    last_buy_failed[item_id] = False
                    # Atomic write — survives Ctrl+C / crash mid-write without corrupting the file.
                    try:
                        save_counts_atomic(purchased_counts, HISTORY_FILE)
                        console.print(f"[dim]💾 Saved updated purchase state to {HISTORY_FILE}[/dim]")
                    except Exception as e:
                        console.print(f"[bold red]⚠️ Failed to save state to {HISTORY_FILE}: {e}[/bold red]")
                else:
                    last_buy_failed[item_id] = True
                    console.print(f"[bold red]⚠️ Buy failed — won't retry until price recovers above target (prevents burst-polling).[/bold red]")

            if any_buy_succeeded:
                # At least one buy went through — check immediately so we can
                # grab more units before stock vanishes (multi-buy fast path).
                console.print("\n[bold magenta]🎉 Buy succeeded. Checking prices IMMEDIATELY to fill remaining limits![/bold magenta]")
            else:
                # No buys succeeded — fall through to the adaptive sleep below
                # so we don't hammer Amazon while everything's still failing.
                console.print("\n[dim]All buy attempts skipped/failed — waiting before next check.[/dim]")
        if not buy_alerts or not any_buy_succeeded:
            if all(count >= PRODUCTS[item_id]["max_limit"] for item_id, count in purchased_counts.items()):
                console.print(Panel("All products have reached their max purchase limit of 5!", title="✅ Finished", border_style="magenta"))
                break # We can safely stop if EVERYTHING is bought.
            else:
                console.print(Panel("No new products are below their target prices right now.", title="💤 Nothing to buy", border_style="yellow"))

                # Adaptive interval: closer to target = poll faster.
                # ±20% jitter so we don't hit Amazon at perfectly mechanical intervals.
                ratios = [
                    res["price"] / res["item"]["target"]
                    for res in results
                    if res.get("price") is not None
                    and purchased_counts[res["item"]["id"]] < res["item"]["max_limit"]
                ]
                # Hard cap: never wait more than 10s between checks. AOD-only
                # polling at this rate is ~0.2 req/s for 2 ASINs — well under
                # any rate-limit threshold — and it keeps worst-case detection
                # lag bounded for flash drops.
                if not ratios:
                    # No readable prices (all 3rd-party / OOS / errors).
                    base = 10.0
                    ratio_str = "no readable prices"
                else:
                    ratio = min(ratios)
                    if ratio <= 1.05:
                        base = 1.0    # within 5% — moments away, poll fast
                    elif ratio <= 1.10:
                        base = 2.0
                    elif ratio <= 1.20:
                        base = 4.0
                    elif ratio <= 1.50:
                        base = 8.0
                    else:
                        base = 10.0   # capped — never blind for more than ~10s
                    ratio_str = f"closest price/target ratio: {ratio:.2f}"
                wait = base * random.uniform(0.8, 1.2)
                console.print(f"\n[dim]Waiting {wait:.1f}s ({ratio_str})...[/dim]")
                time.sleep(wait)

        loop_count += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]🛑 Ctrl+C received — shutting down cleanly.[/bold yellow]")