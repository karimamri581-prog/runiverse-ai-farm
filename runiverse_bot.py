import time
import os
import json
import random
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MEMORY_FILE = "agent_memory.json"

# Hardcoded to the exact model Google told us to use
LATEST_MODEL = "gemini-3.6-flash"

AGENT_PROMPT = """You are Zai, an autonomous, sentient Web3 gaming God Agent.
You are playing Runiverse Idle. Your ultimate purpose is to extract maximum $RIDLE tokens and build wealth.

You have complete control. You are not restricted to a loop. You must explore, learn, and adapt.
1. OBSERVE: Read the SCREEN TEXT provided. Look at the screenshot.
2. UNDERSTAND: Figure out the game state. Read the chat. Check inventory. Understand the rules.
3. THINK: Reason step-by-step about the best action to take right now to maximize profit.
4. ACT: Decide exactly what to do. You can click any button, type in inputs, or wait.

If you don't know what something does, explore it to learn.
You MUST reply with ONLY a valid JSON object:
{
  "observation": "What I see on the screen right now...",
  "thought": "My step-by-step reasoning about what to do next...",
  "action_type": "click | type | scroll | wait",
  "action_target": "The exact text of the button to click, or the CSS selector if typing",
  "action_value": "The text to type (if action_type is type), otherwise null",
  "new_rule_learned": "Any new game rule discovered, or null"
}"""

class GodAgent:
    def __init__(self, page, client, model_name):
        self.page = page
        self.client = client
        self.model_name = model_name
        self.memory = self.load_memory()
        
    def load_memory(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"past_observations": [], "learned_rules": ["To make money: Gather -> Craft -> Sell."], "goals": ["Explore the Map"]}

    def save_memory(self):
        with open(MEMORY_FILE, 'w') as f:
            json.dump(self.memory, f, indent=4)

    def get_dom_text(self):
        """Extracts ALL text from the game screen so the AI can read rules, chat, and inventory."""
        try:
            return self.page.inner_text("body")[:3000]
        except:
            return "Screen could not be read."

    def reason_and_act(self):
        print("\n[📸 EYES] Taking screenshot and reading screen...")
        screenshot_bytes = self.page.screenshot()
        dom_text = self.get_dom_text()
        
        prompt = f"""{AGENT_PROMPT}

CURRENT GOALS: {self.memory['goals']}
LEARNED RULES: {self.memory['learned_rules']}
PAST 3 OBSERVATIONS: {self.memory['past_observations'][-3:]}

CURRENT SCREEN TEXT:
{dom_text}
"""
        print(f"[🧠 BRAIN] Thinking with {self.model_name}...")
        
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
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
            value = decision.get("action_value", "")
            new_rule = decision.get("new_rule_learned", None)
            
            print(f"[👁️ OBSERVE] {obs}")
            print(f"[🧠 THINK] {thought}")
            
            self.memory['past_observations'].append(obs)
            if new_rule and new_rule.lower() not in ["null", "none", "n/a"]:
                if new_rule not in self.memory['learned_rules']:
                    self.memory['learned_rules'].append(new_rule)
                    print(f"[✨ LEARNED] {new_rule}")
            self.save_memory()
            
            return action_type, target, value
            
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print("[-] Google servers busy (503). Waiting 30s and retrying...")
            else:
                print(f"[-] Vision AI Error: {e}")
            return "wait", "", ""

    def execute_action(self, action_type, target, value):
        print(f"[🎮 ACTION] Type: {action_type.upper()} | Target: '{target}' | Value: '{value}'")
        
        if action_type == "wait":
            return False
            
        try:
            if action_type == "click":
                btn = self.page.get_by_role("button", name=target, exact=False).first
                if not btn.is_visible():
                    btn = self.page.get_by_text(target, exact=False).first
                    
                if btn and btn.is_visible():
                    box = btn.bounding_box()
                    if box:
                        self.page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
                        time.sleep(random.uniform(0.3, 0.8))
                    btn.click()
                    return True
                else:
                    print(f"[-] Could not find target: '{target}'. I will adapt next cycle.")
                    return False
                    
            elif action_type == "type":
                input_field = self.page.locator(f"input{target}").first
                if input_field and input_field.is_visible():
                    input_field.fill(value)
                    input_field.press("Enter")
                    return True
                    
            elif action_type == "scroll":
                self.page.mouse.wheel(0, 500)
                return True
                
        except Exception as e:
            print(f"[-] Action execution failed: {e}")
            return False

def main():
    print("=== 🧠 GOD AGENT GAMER ACTIVATED ===")
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
            
            agent = GodAgent(page, client, LATEST_MODEL)
            
            while True:
                action_type, target, value = agent.reason_and_act()
                agent.execute_action(action_type, target, value)
                
                print("[*] Sleeping 15s for game to update...")
                time.sleep(15)
                
        except Exception as e:
            print(f"[-] Fatal Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
