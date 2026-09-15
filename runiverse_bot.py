import time
import os
import json
import random
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Updated to the actual latest 2026 model
LATEST_MODEL = "gemini-3.8-flash"

SYSTEM_PROMPT = """You are an expert Web3 game bot playing Runiverse Idle.
Your ONLY goal is to make money by following this loop: Gather -> Craft -> Sell.

STRICT RULES:
1. You are ONLY allowed to interact with the Map (for gathering), Forge (for crafting), and Market (for selling).
2. IGNORE completely: Arena, Fishing, Guild, Dungeon, Land, Shop, Wiki, Creators, Bank.
3. If you see a button to Claim finished loot, click it immediately.
4. If you have no resources, go to the Map and click Gather/Expedition.
5. If you have resources, go to the Forge and click Craft.
6. If you have crafted items, go to the Market and click Sell/List.

Analyze the screen and decide the ONE best action to take right now.
Reply ONLY with a JSON object: {"action": "exact text of button to click", "reason": "short reason"}"""

class VisionGamer:
    def __init__(self, page, client):
        self.page = page
        self.client = client
        
    def look_and_think(self):
        print("[📸 EYES] Taking screenshot of the game...")
        screenshot_bytes = self.page.screenshot()
        
        print(f"[🧠 BRAIN] Sending image to Vision AI ({LATEST_MODEL}) for analysis...")
        
        try:
            response = self.client.models.generate_content(
                model=LATEST_MODEL,
                contents=[
                    types.Part.from_text(text=SYSTEM_PROMPT),
                    types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
                ]
            )
            ai_text = response.text.strip()
            print(f"[🧠 BRAIN] AI Raw Output: {ai_text}")
            
            ai_text = ai_text.replace("```json", "").replace("```", "").strip()
            decision = json.loads(ai_text)
            button_text = decision.get("action", "")
            reason = decision.get("reason", "")
            
            print(f"[🧠 BRAIN] Decided to click: '{button_text}' because: {reason}")
            return button_text
            
        except Exception as e:
            print(f"[-] Vision AI Error: {e}")
            return None

    def execute_click(self, button_text):
        if not button_text:
            return False
            
        try:
            btn = self.page.get_by_role("button", name=button_text, exact=False).first
            if not btn.is_visible():
                btn = self.page.get_by_text(button_text, exact=False).first
                
            if btn and btn.is_visible():
                print(f"[🎮 ACTION] Clicking '{button_text}'...")
                box = btn.bounding_box()
                if box:
                    self.page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                    time.sleep(random.uniform(0.3, 0.8))
                btn.click()
                return True
            else:
                print(f"[-] Could not find a visible button for '{button_text}'.")
                return False
        except Exception as e:
            print(f"[-] Click failed: {e}")
            return False

def main():
    print("=== 👁️ LEASHED VISION GAMER AI ACTIVATED ===")
    if not BEARER_TOKEN or not GEMINI_API_KEY:
        print("ERROR: Missing BEARER_TOKEN or GEMINI_API_KEY secrets.")
        return

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print("[+] Google GenAI Client initialized.")
        
        print(f"[*] Testing Google Gemini API Key with {LATEST_MODEL}...")
        test_response = client.models.generate_content(model=LATEST_MODEL, contents="Respond with 'OK'")
        if "OK" not in test_response.text.upper():
            raise Exception("API Key test failed.")
        print("[+] Gemini API Key is valid! Proceeding.")
        
    except Exception as e:
        print(f"[-] FATAL: Your Google Gemini API Key is invalid or model is unavailable.")
        print(f"[-] Error details: {e}")
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
                print("[+] Cloudflare bypassed!")
            except:
                print("[-] Cloudflare took too long.")
            time.sleep(5)
            
            gamer = VisionGamer(page, client)
            
            while True:
                target_button = gamer.look_and_think()
                
                if target_button:
                    gamer.execute_click(target_button)
                    wait_time = 15
                else:
                    print("[-] AI couldn't decide. Waiting 30s.")
                    wait_time = 30
                    
                time.sleep(wait_time)
                
        except Exception as e:
            print(f"[-] Fatal Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
