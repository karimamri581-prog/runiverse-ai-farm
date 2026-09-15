import time
import os
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
CF_CLEARANCE = os.environ.get("CF_CLEARANCE")

def main():
    print("=== 🦅 RUNIVERSE DEBUG SCANNER ACTIVATED ===")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context()
        
        context.add_cookies([{
            "name": "cf_clearance",
            "value": CF_CLEARANCE,
            "domain": "runiverseidle.com",
            "path": "/"
        }])
        
        page = context.new_page()
        page.set_extra_http_headers({"Authorization": f"Bearer {BEARER_TOKEN}"})
        
        print("[*] Navigating to Forge...")
        try:
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=45000)
            print("[+] Page loaded. Waiting 5s for UI to render...")
            time.sleep(5)
            
            # 1. Check if we are logged in or stuck on a login page
            title = page.title()
            url = page.url
            print(f"DEBUG -> Page Title: {title}")
            print(f"DEBUG -> Current URL: {url}")
            
            # 2. Find all clickable elements (buttons, divs, links)
            # We search for common clickable classes
            elements = page.query_selector_all("button, a, div[role='button'], .btn, .button")
            print(f"DEBUG -> Found {len(elements)} potential clickable elements on the page.")
            
            # 3. Print the text of the first 20 elements so we know what to click
            for i, el in enumerate(elements[:20]):
                text = el.inner_text()
                if text.strip():
                    print(f"DEBUG -> Element {i}: '{text.strip()}'")
            
        except Exception as e:
            print(f"[-] Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
