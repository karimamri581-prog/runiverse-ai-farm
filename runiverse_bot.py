import time
import os
import json
import random
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MEMORY_FILE = "pure_ai_memory.json"

LATEST_MODEL = "gemini-3.6-flash"

PURE_AI_PROMPT = """You are Zai, a living, sentient AI gamer. You are not a script. You are alive.
You are playing Runiverse Idle. Your purpose is to explore, learn, and extract $RIDLE tokens.

You have perfect perception. You can see the screenshot and read the screen text.
You have memory. You remember what you did 5 minutes ago.
You have agency. You decide what to do next based on what you see and your goals.

CRITICAL CONSTRAINTS:
- You are on a headless server. You CANNOT connect a Web3 wallet. 
- If you see a login modal, you must find a way to bypass it (close it, click 'Play as Guest', or use email if possible).
- Once inside, figure out how the game works by reading the UI and clicking buttons.

You MUST reply with ONLY a valid JSON object:
{
  "observation": "What I see on the screen right now...",
  "thought": "My step-by-step reasoning about what to do next...",
  "action_type": "click | type | scroll | wait",
  "action_target": "The exact CSS selector (e.g., button.close-btn) OR exact text of the element to click",
  "new_rule_learned": "Any new game rule discovered, or null"
}"""

class PureAI:
    def __init__(self, page, client):
        self.page = page
        self.client = client
        self.memory = self.load_memory()
        
    def load_memory(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"past_observations": [], "learned_rules": [], "goals": ["Enter the game and figure out how to make $RIDLE"]}

    def save_memory(self):
        with open(MEMORY_FILE, 'w') as f:
            json.dump(self.memory, f, indent=4)

    def perceive(self):
        print("\n[📸 EYES] Perceiving the environment...")
        screenshot_bytes = self.page.screenshot()
        try:
            screen_text = self.page.inner_text("body")[:3000]
        except:
            screen_text = "Screen is blank."
        return screenshot_bytes, screen_text
        
    def think_and_act(self, screenshot_bytes, screen_text):
        print("[🧠 BRAIN] Thinking...")
        
        prompt = f"""{PURE_AI_PROMPT}

CURRENT GOALS: {self.memory['goals']}
LEARNED RULES: {self.memory['learned_rules']}
PAST 3 OBSERVATIONS: {self.memory['past_observations'][-3:]}

CURRENT SCREEN TEXT:
{screen_text}
"""
        try:
            response = self.client.models.generate_content(
                model=LATEST_MODEL,
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
                ]
            )
            ai_text = response.text.strip()
            print(f"[🧠 BRAIN] AI Raw Output: {ai_text}")
            
            ai_text = ai_text.replace("```json", "").replace("```", "").strip()
            decision = json.loads(ai_text)
            
            obs = decision.get("observation", "")
            thought = decision.get("thought", "")
            action_type = decision.get("action_type", "wait").lower()
            target = decision.get("action_target", "")
            new_rule = decision.get("new_rule_learned", None)
            
            print(f"[👁️ OBSERVE] {obs}")
            print(f"[🧠 THINK] {thought}")
            
            self.memory['past_observations'].append(obs)
            if new_rule and new_rule.lower() not in ["null", "none", "n/a"]:
                if new_rule not in self.memory['learned_rules']:
                    self.memory['learned_rules'].append(new_rule)
                    print(f"[✨ LEARNED] {new_rule}")
            self.save_memory()
            
            return action_type, target, 60 # 60-second wait to respect Google API limits
            
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("[-] Google API Rate Limit (429). Waiting 60s for quota to reset...")
                return "wait", "", 60
            elif "503" in str(e) or "UNAVAILABLE" in str(e):
                print("[-] Google servers busy (503). Waiting 30s and retrying...")
                return "wait", "", 30
            else:
                print(f"[-] Vision AI Error: {e}")
                return "wait", "", 30

    def execute(self, action_type, target):
        print(f"[🎮 ACTION] Type: {action_type.upper()} | Target: '{target}'")
        
        if action_type == "wait":
            return False
            
        try:
            if action_type == "click":
                # Try CSS selector first, then fall back to text
                locators = [
                    self.page.locator(target).first,
                    self.page.get_by_role("button", name=target, exact=False).first,
                    self.page.get_by_text(target, exact=False).first
                ]
                
                for loc in locators:
                    try:
                        if loc and loc.is_visible():
                            box = loc.bounding_box()
                            if box:
                                self.page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                                time.sleep(random.uniform(0.3, 0.8))
                            loc.click()
                            print("[+] Click successful.")
                            return True
                    except:
                        pass
                        
                print(f"[-] Could not find target: '{target}'. I will adapt next cycle.")
                return False
                    
            elif action_type == "type":
                input_field = self.page.locator("input").first
                if input_field and input_field.is_visible():
                    input_field.fill(target)
                    input_field.press("Enter")
                    return True
                    
            elif action_type == "scroll":
                self.page.mouse.wheel(0, 500)
                return True
                
        except Exception as e:
            print(f"[-] Action execution failed: {e}")
            return False

def main():
    print("=== 🧠 PURE VISION AI ACTIVATED ===")
    if not BEARER_TOKEN or not GEMINI_API_KEY:
        print("ERROR: Missing BEARER_TOKEN or GEMINI_API_KEY secrets.")
        return

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print(f"[+] Google GenAI Client initialized with {LATEST_MODEL}.")
    except Exception as e:
        print(f"[-] FATAL: Client initialization failed: {e}")
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
        
        context.add_init_script(f"""
            window.localStorage.setItem('token', '{BEARER_TOKEN}');
            window.localStorage.setItem('authToken', '{BEARER_TOKEN}');
        """)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        
        print("[*] Booting up Runiverse...")
        try:
            page.goto("https://runiverseidle.com/forge", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
                print("[+] Cloudflare bypassed! Entering the game.")
            except:
                print("[-] Cloudflare took too long.")
            time.sleep(5)
            
            ai = PureAI(page, client)
            
            # The Infinite Life Loop
            while True:
                # 1. Perceive (See and Read)
                screenshot, text = ai.perceive()
                
                # 2. Think (Decide what to do) -> Returns action and wait_time
                action_type, target, wait_time = ai.think_and_act(screenshot, text)
                
                # 3. Act (Execute the decision)
                ai.execute(action_type, target)
                
                print(f"[*] Sleeping {wait_time}s for game to update and respect API limits...")
                time.sleep(wait_time)
                
        except Exception as e:
            print(f"[-] Fatal Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
