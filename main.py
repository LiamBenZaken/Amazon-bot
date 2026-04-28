import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

import os
import json
from curl_cffi import requests
from bs4 import BeautifulSoup
import time
import re
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
        "disable_web_page_preview": False
    }
    try:
        # curl_cffi requests acts exactly like standard requests here
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        console.print(f"[dim red]Failed to send Telegram alert: {e}[/dim red]")

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
                    valid_sellers = ["ATVPDKIKX0DER", "A2XZ7JICGUQ1CX", "A11IL2PNWYJU7H"]
                    if merchant_val not in valid_sellers:
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

    def check_price(self, item):
        url = item["url"]
        try:
            start_time = time.time()
            response = self.session.get(url, timeout=10)

            if response.status_code != 200:
                return {"item": item, "price": None, "time": time.time() - start_time, "error": f"Status: {response.status_code}"}

            soup = BeautifulSoup(response.content, "lxml")

            add_to_cart = soup.find("input", {"id": "add-to-cart-button"})
            buy_now = soup.find("input", {"id": "buy-now-button"})

            if not add_to_cart and not buy_now:
                return {"item": item, "price": None, "time": time.time() - start_time, "error": "Unavailable / Cannot ship"}

            # STRICT SELLER CHECK: Guarantee the seller is Amazon or Amazon Export Sales LLC
            merchant_input = soup.find("input", {"id": "merchantID"})
            valid_amazon_sellers = ["ATVPDKIKX0DER", "A2XZ7JICGUQ1CX", "A11IL2PNWYJU7H"]
            if merchant_input and merchant_input.get("value") not in valid_amazon_sellers:
                return {"item": item, "price": None, "time": time.time() - start_time, "error": "3rd Party Seller"}

            price_element = soup.find("span", {"class": "a-offscreen"})

            if price_element:
                raw_price_text = price_element.text.strip()
                clean_number_str = re.sub(r'[^\d.]', '', raw_price_text)

                if not clean_number_str:
                    return {"item": item, "price": None, "time": time.time() - start_time, "error": "Could not parse price"}

                numeric_price = float(clean_number_str)
                final_usd_price = numeric_price

                if "ILS" in raw_price_text or "₪" in raw_price_text:
                    final_usd_price = numeric_price / self.exchange_rate

                return {"item": item, "price": final_usd_price, "time": time.time() - start_time, "error": None, "raw": raw_price_text}
            else:
                return {"item": item, "price": None, "time": time.time() - start_time, "error": "Could not find price tag"}

        except Exception as e:
            return {"item": item, "price": None, "time": 0, "error": f"Network Error: {e}"}


if __name__ == "__main__":
    console.print("\n[bold cyan]🔧 Setting up Amazon AutoBuyer...[/bold cyan]")
    buyer = AmazonAutoBuyer()
    buyer.setup_login()
    
    console.print("\n[dim]Extracting session cookies from AutoBuyer...[/dim]")
    session_cookies = buyer.get_driver().get_cookies()

    tracker = AmazonTLSTracker(cookies=session_cookies)

    HISTORY_FILE = "history.json"
    purchased_counts = {}
    
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                purchased_counts = json.load(f)
            console.print(f"[green]📂 Loaded purchase history from {HISTORY_FILE}[/green]")
        except Exception as e:
            console.print(f"[bold red]⚠️ Failed to load history.json: {e}[/bold red]")
            
    # Ensure all products have a key in case new ones were added
    for item_id in PRODUCTS.keys():
        if item_id not in purchased_counts:
            purchased_counts[item_id] = 0

    loop_count = 1
    
    # State tracker to prevent Telegram spam
    active_deals = {item_id: False for item_id in PRODUCTS.keys()}
    
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
            table.add_row(name, target, current_price, status, time_taken)

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
                        
                    # Only buy if we haven't maxed out yet
                    if purchased_counts[item_id] < item["max_limit"]:
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
            else:
                # Item is out of stock / unavailable. Reset deal state.
                active_deals[item_id] = False
        
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
                    purchased_counts[item_id] += qty
                    # Save the new counts to local db
                    try:
                        with open(HISTORY_FILE, "w") as f:
                            json.dump(purchased_counts, f, indent=4)
                        console.print(f"[dim]💾 Saved updated purchase state to {HISTORY_FILE}[/dim]")
                    except Exception as e:
                        console.print(f"[bold red]⚠️ Failed to save state to {HISTORY_FILE}: {e}[/bold red]")
                else:
                    console.print(f"[bold red]⚠️ Skipped adding to purchase count because auto-buy failed.[/bold red]")
                
            console.print("\n[bold magenta]🎉 Deals processed. Checking prices IMMEDIATELY to fill remaining limits![/bold magenta]")
        else:
            if all(count >= PRODUCTS[item_id]["max_limit"] for item_id, count in purchased_counts.items()):
                console.print(Panel("All products have reached their max purchase limit of 5!", title="✅ Finished", border_style="magenta"))
                break # We can safely stop if EVERYTHING is bought.
            else:
                console.print(Panel("No new products are below their target prices right now.", title="💤 Nothing to buy", border_style="yellow"))
                console.print("\n[dim]Waiting 15 seconds before checking again...[/dim]")
                time.sleep(15)
            
        loop_count += 1