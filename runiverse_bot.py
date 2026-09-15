import time
import os
import random
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")

def main():
    print("=== 🦅 RUNIVERSE STEALTH GITHUB BOT ACTIVATED ===")
    
    if not BEARER_TOKEN:
        print("ERROR: Missing BEARER_TOKEN secret.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        
        # 1. Use standard Windows Chrome headers
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            extra_http_headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
        )
        
        # 2. Inject JS to hide the bot flag (Bypasses Cloudflare)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        
        print("[*] Navigating to Forge (Stealth Mode)...")
        try:
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=60000)
            
            print("[*] Waiting for Cloudflare JS Challenge to solve (up to 20s)...")
            try:
                page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
                print("[+] Cloudflare bypassed!")
            except:
                print("[-] Cloudflare took too long. Proceeding anyway...")
                
            print("[*] Waiting 5s for game UI to render...")
            time.sleep(5)
            
            # 3. Find all clickable elements and log them
            elements = page.query_selector_all("button, a, div[role='button'], .btn, .button")
            print(f"DEBUG -> Found {len(elements)} potential clickable elements.")
            
            for i, el in enumerate(elements[:20]):
                text = el.inner_text()
                if text.strip():
                    print(f"DEBUG -> Element {i}: '{text.strip()}'")
            
            # 4. Smart DOM scan for action buttons
            keywords = ["Craft", "Forge", "Gather", "Mine", "Start", "Claim"]
            action_taken = False
            
            for word in keywords:
                try:
                    button = page.get_by_role("button", name=word, exact=False).first
                    if button.is_visible() and button.is_enabled():
                        print(f"[+] Found actionable button: '{word}'. Clicking...")
                        button.click()
                        action_taken = True
                        time.sleep(random.uniform(1.5, 3.0))
                        break
                except Exception:
                    pass
                    
            if not action_taken:
                print("[-] No actionable buttons found right now. Might be on cooldown.")
                
        except Exception as e:
            print(f"[-] Navigation/Action error: {e}")
        finally:
            browser.close()
            print("[*] Bot cycle complete.")

if __name__ == "__main__":
    main()
