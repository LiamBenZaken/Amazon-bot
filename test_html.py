import sys
from curl_cffi import requests
from bs4 import BeautifulSoup

def main():
    url = "https://www.amazon.com/dp/B0G4XJPN8Q/"
    session = requests.Session(impersonate="chrome120", headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    })
    
    resp = session.get(url)
    with open("amazon_dump.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
        
    soup = BeautifulSoup(resp.content, "lxml")
    
    # Let's find sellers
    merchant_info = soup.find("div", {"id": "merchant-info"})
    if merchant_info:
        print("Main Merchant info:", merchant_info.text.strip())
        
    print("Looking for other sellers...")
    # There is usually a link to open the offer listing
    for a in soup.find_all("a", href=True):
        if "offer-listing" in a["href"] or "condition=new" in a["href"]:
            print("Found offer link:", a["href"])
            
    # Look for mbc (more buying choices)
    mbc = soup.find("div", {"id": "moreBuyingChoices_feature_div"})
    if mbc:
        print("MBC found! Length:", len(mbc.text))

if __name__ == "__main__":
    main()
