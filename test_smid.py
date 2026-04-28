import sys
from curl_cffi import requests
from bs4 import BeautifulSoup

def main():
    url = "https://www.amazon.com/dp/B0G4XJPN8Q/?smid=ATVPDKIKX0DER"
    session = requests.Session(impersonate="chrome120", headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    
    resp = session.get(url)
    soup = BeautifulSoup(resp.content, "lxml")
    
    add_to_cart = soup.find("input", {"id": "add-to-cart-button"})
    buy_now = soup.find("input", {"id": "buy-now-button"})
    
    price_element = soup.find("span", {"class": "a-offscreen"})
    price = price_element.text.strip() if price_element else "No price"
    
    merchant = soup.find("div", {"id": "merchant-info"})
    merchant_text = merchant.text.strip() if merchant else "No merchant info"
    
    print(f"Price: {price}")
    print(f"Merchant: {merchant_text}")
    print(f"Add to cart: {bool(add_to_cart)}")
    print(f"Buy now: {bool(buy_now)}")

if __name__ == "__main__":
    main()
