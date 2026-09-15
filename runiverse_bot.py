import time
import os
import random
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")

# Words to ignore (Navigation menu, we don't want to click these by accident)
IGNORE_WORDS = ["MAP", "BAG", "MARKET", "ARENA", "FISHING", "CREATORS", "LAND", "WIKI", "‹", "›", "✕", "OK", "X"]

def scan_and_click(page, keywords):
    """Scans the screen for specific game actions and clicks them."""
    elements = page.query_selector_all("button, a, div[role='button'], .btn, .button, [class*='raft']")
    
    for el in elements:
        try:
            text = el.inner_text().strip().upper()
            if text and text not in IGNORE_WORDS:
                # If the text contains any of our keywords, click it
                for word in keywords:
                    if word in text:
                        if el.is_visible() and el.is_enabled():
                            print(f"[🎮 GAMER] Found action: '{text}'. Clicking...")
                            el.click()
                            time.sleep(random.uniform(2.0, 4.0)) # Human delay after click
                            return True
        except:
            pass
    return False

def play_game(page):
    """The main gamer loop"""
    print("=== 🎮 AUTONOMOUS GAMER AI ACTIVATED ===")
    cycle = 1
    
    while True:
        print(f"\n--- Cycle {cycle} ---")
        
        # 1. Try to gather/mine/craft
        action_found = scan_and_click(page, ["CRAFT", "FORGE", "GATHER", "MINE", "START", "SMELT"])
        
        # 2. Try to claim finished resources
        if not action_found:
            action_found = scan_and_click(page, ["CLAIM", "REWARD", "FINISHED", "COMPLETE"])
            
        # 3. Every 5 cycles, go to the Market to sell loot
        if cycle % 5 == 0:
            print("[🎮 GAMER] Inventory check. Going to Market to sell loot...")
            try:
                market_btn = page.get_by_text("MARKET", exact=False).first
                if market_btn.is_visible():
                    market_btn.click()
                    time.sleep(3)
                    
                    # Try to list/sell items
                    scan_and_click(page, ["SELL", "LIST", "CONFIRM"])
                    
                    # Go back to Forge
                    forge_btn = page.get_by_text("FORGE", exact=False).first
                    if forge_btn.is_visible():
                        forge_btn.click()
                        time.sleep(3)
            except:
                print("[-] Could not navigate to market.")
                
        # 4. Wait for game cooldowns (1 minute)
        if not action_found:
            print("[-] No actions available. Waiting 60s for game cooldowns...")
            time.sleep(60)
        else:
            print("[+] Action successful. Waiting 30s before next scan...")
            time.sleep(30)
            
        cycle += 1

def main():
    if not BEARER_TOKEN:
        print("ERROR: Missing BEARER_TOKEN secret.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            extra_http_headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        
        print("[*] Navigating to Forge...")
        try:
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=60000)
            
            print("[*] Waiting for Cloudflare to clear...")
            try:
                page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
                print("[+] Cloudflare bypassed!")
            except:
                print("[-] Cloudflare took too long. Proceeding anyway...")
                
            print("[*] Waiting 7s for game UI to fully render...")
            time.sleep(7)
            
            # Start the infinite gamer loop
            play_game(page)
            
        except Exception as e:
            print(f"[-] Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
