import time
import os
import random
from playwright.sync_api import sync_playwright

# Pull tokens from GitHub Secrets
BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
CF_CLEARANCE = os.environ.get("CF_CLEARANCE")

def human_delay():
    delay = random.uniform(1.5, 3.5)
    time.sleep(delay)

def main():
    print("=== 🦅 RUNIVERSE SMART BOT ACTIVATED ===")
    
    if not BEARER_TOKEN or not CF_CLEARANCE:
        print("ERROR: Missing GitHub Secrets.")
        return

    with sync_playwright() as p:
        # Launch headless Chrome with --no-sandbox (Required for GitHub Actions)
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context()
        
        # Inject Cloudflare cookie
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
            # FIX: Use domcontentloaded instead of networkidle so the chat doesn't hang the bot
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=45000)
            print("[+] Page loaded. Waiting 5s for UI to render...")
            time.sleep(5)
            
            # Simulate human mouse movement
            page.mouse.move(random.randint(100, 800), random.randint(100, 600))
            human_delay()
            
            keywords = ["Craft", "Forge", "Gather", "Mine", "Start", "Claim"]
            action_taken = False
            
            for word in keywords:
                try:
                    button = page.get_by_role("button", name=word, exact=False).first
                    if button.is_visible() and button.is_enabled():
                        print(f"[+] Found actionable button: '{word}'. Clicking...")
                        button.click()
                        action_taken = True
                        human_delay()
                        break
                except Exception:
                    pass # Button not found, try next
                    
            if not action_taken:
                print("[-] No actionable buttons found. Might be on cooldown.")
                
        except Exception as e:
            print(f"[-] Navigation/Action error: {e}")
        finally:
            browser.close()
            print("[*] Bot cycle complete.")

if __name__ == "__main__":
    main()
