import time
import os
import random
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")

# Game Rules Knowledge Base
STATES = {
    "GATHERING": ["EXPEDITION", "GATHER", "SEARCH", "SEND", "SCOUT"],
    "CRAFTING": ["CRAFT", "FORGE", "SMELT", "CREATE"],
    "SELLING": ["SELL", "LIST", "CONFIRM"],
    "CLAIMING": ["CLAIM", "COLLECT", "FINISHED", "COMPLETE"]
}

class StateAwareGamer:
    def __init__(self, page):
        self.page = page
        self.current_state = "INIT"
        self.cycle = 0
        
    def log(self, msg):
        print(f"[🧠 AI | State: {self.current_state}] {msg}")

    def read_screen(self):
        """Reads all visible text on the screen to understand the game state."""
        try:
            # Get all text content from the main game area
            body_text = self.page.inner_text("body").upper()
            return body_text
        except:
            return ""

    def find_and_click(self, keywords):
        """Finds a visible, enabled button matching keywords and clicks it."""
        elements = self.page.query_selector_all("button, a, div[role='button'], .btn, [class*='raft']")
        
        for el in elements:
            try:
                text = el.inner_text().strip().upper()
                if not text or text in ["MAP", "BAG", "FORGE", "MARKET", "ARENA", "FISHING", "CREATORS", "LAND", "WIKI", "‹", "›", "✕", "X"]:
                    continue
                    
                for word in keywords:
                    if word in text:
                        if el.is_visible() and el.is_enabled():
                            self.log(f"Found action: '{text}'. Clicking...")
                            box = el.bounding_box()
                            if box:
                                self.page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                                time.sleep(random.uniform(0.3, 0.8))
                            el.click()
                            return True
            except:
                pass
        return False

    def navigate_to(self, tab_name):
        """Navigates to a specific tab in the game."""
        self.log(f"Navigating to {tab_name}...")
        try:
            btn = self.page.get_by_text(tab_name, exact=False).first
            if btn.is_visible():
                btn.click()
                time.sleep(3)
                return True
        except:
            return False

    def play(self):
        """The main game loop driven by game rules."""
        self.cycle += 1
        screen_text = self.read_screen()
        
        # Rule 1: If there is finished loot, claim it immediately.
        if any(word in screen_text for word in STATES["CLAIMING"]):
            self.current_state = "CLAIMING"
            self.log("I see finished loot! Claiming it.")
            if self.find_and_click(STATES["CLAIMING"]):
                return 5

        # Rule 2: If we are on the Forge page and can craft, do it.
        if "FORGE" in self.page.url.upper() or self.current_state == "CRAFTING":
            self.current_state = "CRAFTING"
            self.log("Checking if I can craft items...")
            if self.find_and_click(STATES["CRAFTING"]):
                return 10 # Wait for crafting animation
            
            # If craft fails, maybe we lack resources. Go gather.
            self.log("Cannot craft. Need materials. Going to Map.")
            self.navigate_to("MAP")
            return 3

        # Rule 3: If we are on the Map, send character to gather.
        if "MAP" in self.page.url.upper() or self.current_state == "GATHERING":
            self.current_state = "GATHERING"
            self.log("Looking for resources to gather...")
            if self.find_and_click(STATES["GATHERING"]):
                return 15 # Wait for expedition
            
            # If nothing to gather, inventory might be full. Go sell.
            self.log("No resources left to gather. Going to Market to sell.")
            self.navigate_to("MARKET")
            return 3

        # Rule 4: If we are on the Market, list items for sale.
        if "MARKET" in self.page.url.upper() or self.current_state == "SELLING":
            self.current_state = "SELLING"
            self.log("Listing items for sale on the marketplace...")
            if self.find_and_click(STATES["SELLING"]):
                return 5
            
            # Done selling. Go back to Forge to craft.
            self.log("Done selling. Going back to Forge.")
            self.navigate_to("FORGE")
            return 3

        # Fallback: If lost, go to Forge
        self.current_state = "LOST"
        self.log("I am lost. Going to Forge.")
        self.navigate_to("FORGE")
        return 10

def main():
    print("=== 🧠 STATE-AWARE GAMER AI ACTIVATED ===")
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
        
        print("[*] Booting up the game...")
        try:
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
                print("[+] Cloudflare bypassed! I am in the game.")
            except:
                print("[-] Cloudflare took too long.")
                
            time.sleep(5)
            
            gamer = StateAwareGamer(page)
            
            # The Infinite Life Loop
            while True:
                wait_time = gamer.play()
                time.sleep(wait_time)
                
        except Exception as e:
            print(f"[-] Fatal Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
