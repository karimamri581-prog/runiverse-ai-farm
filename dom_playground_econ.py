#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dom_playground_econ.py -- the DOM Playground with an economic brain.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import builtins as _py_builtins
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

__version__ = "2.0.0"

# ════════════════════════ 1. CLI / CONFIG / LOGGING ══════════════════════════

DIM = 256

@dataclass
class Config:
    url: str = ""
    goal: str = "enter the game"
    fields: dict = field(default_factory=dict)
    headless: bool = True
    max_cycles: int = 0
    max_minutes: float = 0.0
    memory_path: str = "playground_memory.json"
    log_path: str = "dom_playground.log"
    log_level: str = "INFO"
    code_log: str = "both"
    dump_states: bool = False
    challenge_patience: float = 150.0
    user_data_dir: str = ""
    load_extension: str = ""
    slowmo: float = 0.0
    viewport_w: int = 1440
    viewport_h: int = 900
    user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    proxy: str = ""
    after_goal: str = "economy"          # economy | exit
    enter_submit: bool = False
    popup_help: bool = True
    allow_signing: bool = False
    seed: int = -1
    stop_file: str = ".dom_playground_stop"

def parse_args() -> Config:
    ap = argparse.ArgumentParser(
        description="DOM Playground v2: an agent that plays a game's economy "
                    "by writing its own Playwright code. No external APIs.")
    ap.add_argument("url", nargs="?", default=os.environ.get("GAME_URL", ""))
    ap.add_argument("--goal", default="enter the game",
                    help="';'-separated startup pipeline (login etc). After it "
                         "completes, the economic brain takes over forever.")
    ap.add_argument("--field", action="append", default=[], metavar="KEY=VALUE",
                    help="value to type into a semantically matching input "
                         "(e.g. --field email=a@b.io). Values never logged.")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=0.0)
    ap.add_argument("--memory", default="playground_memory.json")
    ap.add_argument("--log-file", default="dom_playground.log")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--code-log", choices=["both", "file", "off"], default="both")
    ap.add_argument("--dump-states", action="store_true")
    ap.add_argument("--challenge-patience", type=float, default=150.0)
    ap.add_argument("--user-data-dir", default="")
    ap.add_argument("--load-extension", default="")
    ap.add_argument("--slowmo", type=float, default=0.0)
    ap.add_argument("--viewport", default="1440x900")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--after-goal", choices=["economy", "exit"], default="economy")
    ap.add_argument("--enter-submit", action="store_true")
    ap.add_argument("--no-popup-help", action="store_true")
    ap.add_argument("--allow-signing", action="store_true",
                    help="DANGER: allow fund-moving words/buttons")
    ap.add_argument("--seed", type=int, default=-1)
    a = ap.parse_args()
    try:
        w, h = a.viewport.lower().split("x")
        vw, vh = int(w), int(h)
    except Exception:
        ap.error("--viewport must look like 1440x900")
    fields = {}
    for f in a.field:
        if "=" in f:
            k, v = f.split("=", 1)
            if k.strip():
                fields[k.strip()] = v
    cfg = Config(url=a.url.strip(), goal=a.goal, fields=fields,
                 headless=not a.headed, max_cycles=a.max_cycles,
                 max_minutes=a.max_minutes, memory_path=a.memory,
                 log_path=a.log_file, log_level=a.log_level, code_log=a.code_log,
                 dump_states=a.dump_states,
                 challenge_patience=a.challenge_patience,
                 user_data_dir=a.user_data_dir.strip(),
                 load_extension=a.load_extension.strip(), slowmo=a.slowmo,
                 viewport_w=vw, viewport_h=vh, proxy=a.proxy.strip(),
                 after_goal=a.after_goal, enter_submit=a.enter_submit,
                 popup_help=not a.no_popup_help, allow_signing=a.allow_signing,
                 seed=a.seed)
    if not cfg.url.startswith(("http://", "https://")):
        ap.error("a target URL is required (positional arg or $GAME_URL)")
    return cfg

def build_logger(cfg: Config) -> logging.Logger:
    log = logging.getLogger("dom-playground")
    log.setLevel(logging.DEBUG)
    if log.handlers:
        return log
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    con.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
    fh = RotatingFileHandler(cfg.log_path, maxBytes=3_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(con)
    log.addHandler(fh)
    return log

def _chromium_args(cfg: Config):
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio",
            "--disable-blink-features=AutomationControlled",
            "--window-size=%d,%d" % (cfg.viewport_w, cfg.viewport_h)]
    if cfg.load_extension:
        args += ["--disable-extensions-except=" + cfg.load_extension,
                 "--load-extension=" + cfg.load_extension]
    return args

# ═════════════════ 2. SEMANTIC PRIMITIVES (local, no APIs) ══════════════════

def _fnv1a(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("utf-8", "ignore"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def embed(text: str) -> list:
    v = [0.0] * DIM
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    t = " " + t + " " if t else " nil "
    for i in range(len(t) - 2):
        v[_fnv1a(t[i:i + 3]) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]

def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))

ACT_ANCHORS = {
    "GATHER":  "gather collect harvest mine chop fish forage farm hunt reap "
               "loot claim resources daily",
    "CRAFT":   "craft forge smelt build make create brew cook smith enchant "
               "combine synthesize upgrade",
    "SELL":    "sell list market trade exchange auction offer merchant post "
               "listing price",
    "EXPLORE": "explore discover unknown new map wiki menu inventory profile",
}
ACT_VEC = {k: embed(v) for k, v in ACT_ANCHORS.items()}
ACT_DEST = {"GATHER": "gathering", "CRAFT": "crafting",
            "SELL": "market", "EXPLORE": "hub"}

NAV_VEC = embed("map wiki home menu inventory profile settings leaderboard "
                "dashboard guide help world island village")
DISMISS_VEC = embed("close cancel x dismiss skip no thanks later back exit")
CURRENCY_VEC = embed("gold coins money cash tokens currency credits gems "
                     "silver wealth balance usd eth sol")

SCENE_ANCHORS = {
    "gathering": "field forest mine quarry river farm wilderness nodes "
                 "resources chop harvest hunt gather",
    "crafting":  "forge workshop smithy anvil craft smelt recipe blueprint "
                 "combine create items upgrade",
    "market":    "market shop merchant stall trade sell listing auction "
                 "bazaar exchange vendor buy",
    "inventory": "inventory bag items equipment gear stash storage backpack loot",
    "hub":       "home dashboard main menu overview map world",
}
SCENE_VECS = {k: embed(v) for k, v in SCENE_ANCHORS.items()}

def classify_scene(state: dict) -> str:
    text = ((state.get("title") or "") + " " + (state.get("text") or "")[:400]).strip()
    if not text:
        return "loading"
    v = embed(text)
    best, bs = "hub", 0.0
    for tag, av in SCENE_VECS.items():
        c = _dot(v, av)
        if c > bs:
            bs, best = c, tag
    return best if bs >= 0.14 else "hub"

SOCIAL_RE = re.compile(r"\b(share|tweet|twitter|discord|telegram|follow|"
                       r"invite|refer|friend)\b", re.I)
RISKY_RE = re.compile(r"\b(approve|transfer|withdraw|burn|spend|swap|mint|"
                      r"send|sign(?:\s+transaction)?)\b", re.I)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "at", "for", "with",
    "all", "new", "you", "your", "have", "has", "are", "is", "be", "from",
    "by", "on", "up", "out", "now", "get", "per", "each", "total", "cost",
    "price", "level", "lvl", "xp", "page", "x", "qty", "click", "tap",
    "gained", "received", "earned", "lost", "spent", "crafted",
}

def el_label(el: dict) -> str:
    for k in ("text", "aria", "title", "testid", "cls"):
        v = (el.get(k) or "").strip()
        if v:
            return v
    return el.get("tag", "?")

def elem_fp(el: dict) -> str:
    lab = re.sub(r"[\d.,]+", "#", el_label(el).lower())
    lab = re.sub(r"\s+", " ", lab.strip())[:64]
    raw = "%s|%s|%s|%s|%s" % (lab, el.get("tag"), el.get("role"),
                              el.get("frame_index"), 1 if el.get("modal") else 0)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]

def page_key(state: dict) -> str:
    try:
        u = urlsplit(state.get("url") or "")
        return (u.netloc + u.path) or "?"
    except Exception:
        return "?"

def goal_key(subgoal: str) -> str:
    return "g_" + hashlib.sha1((subgoal or "").lower().encode()).hexdigest()[:8]

def q(s: str, limit: int = 5000) -> str:
    return json.dumps((s or "")[:limit])

def _cmt(s) -> str:
    return re.sub(r"[\r\n]+", " ", str(s or ""))[:60]

def _css_attr(attr: str, value: str) -> str:
    v = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return '[%s="%s"]' % (attr, v[:64])

def _frame_frag(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        frag = parts.path.rstrip("/").split("/")[-1] or parts.netloc
    except Exception:
        frag = (url or "")[-30:]
    return (frag or "frame")[:40]

def _n(v):
    if v is None:
        return "?"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    try:
        return format(v, ",")
    except Exception:
        return str(v)

def _dstr(d: dict) -> str:
    if not d:
        return "-"
    return ", ".join("%s %+g" % (k, v) for k, v in list(d.items())[:4])

# ════════════════════════════ 3. PROBE ═══════════════════════════════════════

JS_STEALTH = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
})();
"""

JS_PROBE = r"""() => {
  const OUT = {url: location.href, name: window.name || '',
               title: document.title || '', elements: [], text: '', flags: {}};
  const SEL = 'button, a[href], input, select, textarea, summary, [onclick],'
    + ' [role="button"], [role="tab"], [role="menuitem"], [role="option"],'
    + ' [role="switch"], [role="checkbox"], [role="link"]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none'
        || parseFloat(s.opacity || '1') < 0.05) return null;
    if (el.getAttribute('aria-hidden') === 'true') return null;
    return r;
  };
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (n >= 300) break;
    const r = vis(el);
    if (!r) continue;
    let cls = '';
    try { cls = (typeof el.className === 'string' ? el.className : '')
                 .trim().split(/\s+/).slice(0, 6).join(' '); } catch (e) {}
    const txt = ((el.innerText || el.textContent || '') + ' '
                 + (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
    const rec = {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: txt.slice(0, 120),
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test')
              || el.getAttribute('data-qa') || '',
      cls: cls.slice(0, 90),
      href: (el.tagName === 'A' ? (el.getAttribute('href') || '') : '').slice(0, 120),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'
              || /(^|[\s_-])(disabled|is-disabled)([\s_-]|$)/i.test(cls)),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      modal: !!el.closest('[role="dialog"], [class*="modal" i], [class*="overlay" i],'
                + ' [class*="popup" i], [class*="drawer" i]')
    };
    const tn = el.tagName;
    if (tn === 'INPUT' || tn === 'TEXTAREA' || tn === 'SELECT') {
      rec.input = {type: (el.getAttribute('type') || tn.toLowerCase()),
                   name: el.getAttribute('name') || '', id: el.id || '',
                   placeholder: el.getAttribute('placeholder') || '',
                   autocomplete: el.getAttribute('autocomplete') || ''};
    }
    OUT.elements.push(rec);
    n++;
  }
  OUT.text = ((document.body && document.body.innerText) || '')
              .replace(/\s+/g, ' ').slice(0, 9000);
  OUT.flags = {
    cloudflare: !!document.querySelector('#challenge-form, #cf-challenge-running,'
                + ' .cf-turnstile, [class*="cf-chl"]'),
    turnstile: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]'),
    canvas: !!document.querySelector('canvas'),
    has_password: !!document.querySelector('input[type="password"]')
  };
  return OUT;
}"""

JS_CHALLENGE_CHECK = ("() => ({t: (document.title || ''), cf: !!(document.querySelector("
                      "'#challenge-form, .cf-turnstile, [class*=cf-chl],"
                      " #cf-spinner-verify'))})")

JS_CLICK = r"""(spec) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const tTxt = norm(spec.text), tAria = norm(spec.aria), tTest = norm(spec.testid);
  let best = null, bs = 0.5;
  for (const el of document.querySelectorAll(
      'button, a, [role="button"], [onclick], input, summary')) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    let s = 0;
    if (tTest && norm(el.getAttribute('data-testid')) === tTest) s += 4;
    const tx = norm(el.innerText || el.textContent);
    const ar = norm(el.getAttribute('aria-label'));
    if (tTxt && tx === tTxt) s += 3;
    else if (tTxt && tTxt.length > 2 && tx.includes(tTxt)) s += 2;
    if (tAria && (ar === tAria || ar.includes(tAria))) s += 2;
    const d = Math.hypot((r.x + r.width / 2) - spec.cx, (r.y + r.height / 2) - spec.cy);
    if (d < 40) s += 2; else s -= d / 250;
    if (s > bs) { bs = s; best = el; }
  }
  if (!best) return false;
  try { best.scrollIntoView({block: 'center'}); } catch (e) {}
  best.click();
  return true;
}"""

async def probe_page(page, cycle: int) -> dict:
    state = {"cycle": cycle, "url": page.url, "title": "", "frames": [],
             "elements": [], "inputs": [],
             "flags": {"cloudflare": False, "turnstile": False, "wallet": False,
                       "canvas": False, "password": False},
             "text": "", "fingerprint": "", "scene_tag": "hub"}
    try:
        frames = [f for f in page.frames
                  if not (f.url or "").startswith(
                      ("chrome-extension://", "devtools://", "about:", "data:"))]
    except Exception:
        frames = [page.main_frame]
    for idx, fr in enumerate(frames[:6]):
        try:
            raw = await fr.evaluate(JS_PROBE)
        except Exception:
            continue
        state["frames"].append({"index": idx, "url": raw.get("url", ""),
                                 "name": raw.get("name", ""),
                                 "title": raw.get("title", "")})
        if idx == 0:
            state["title"] = raw.get("title", "")
        for el in raw.get("elements", []):
            el["frame_index"] = idx
            el["frame_url"] = raw.get("url", "")
            el["fp"] = elem_fp(el)
            if el.get("input"):
                state["inputs"].append(el)
            else:
                state["elements"].append(el)
        fl = raw.get("flags") or {}
        for k in ("cloudflare", "turnstile", "canvas", "password"):
            state["flags"][k] = state["flags"][k] or bool(fl.get(k))
        state["text"] = (state["text"] + " " + (raw.get("text") or ""))[:12000]
    if re.search(r"just a moment|attention required|checking your browser",
                 state["title"], re.I):
        state["flags"]["cloudflare"] = True
    try:
        w = await page.evaluate(
            "() => ({eth: !!(window.ethereum || (window.web3 &&"
            " window.web3.currentProvider)), sol: !!(window.solana || window.phantom)})")
        state["flags"]["wallet"] = bool(w.get("eth") or w.get("sol"))
    except Exception:
        pass
    state["scene_tag"] = classify_scene(state)
    fps = sorted(e["fp"] for e in state["elements"])
    state["fingerprint"] = hashlib.sha1(
        ("|".join(fps) + "|" + state["url"] + "|" + state["title"])
        .encode()).hexdigest()[:16]
    return state

# ═════════════ 4. ECONOMY TRACKER (inventory, state machine, loops) ═══════════

NUM_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)([KkMmBb])?(?![\d%])")
WORD_RE = re.compile(r"[A-Za-z$€£₿◎Ξ][A-Za-z$€₿◎Ξ]{0,14}")
GAIN_RE = re.compile(r"([+-])\s*(\d+)\s+([A-Za-z][A-Za-z ]{2,26})")
MULT = {"k": 1e3, "m": 1e6, "b": 1e9}

@dataclass
class EconDelta:
    increases: dict = field(default_factory=dict)
    decreases: dict = field(default_factory=dict)
    gained: dict = field(default_factory=dict)
    spent: dict = field(default_factory=dict)
    new_labels: list = field(default_factory=list)
    items_appeared: list = field(default_factory=list)
    item_up: list = field(default_factory=list)
    item_down: list = field(default_factory=list)
    currency_delta: float = 0.0
    url_changed: bool = False
    scene_changed: bool = False
    new_elements: int = 0

    def movement(self) -> bool:
        return bool(self.increases or self.decreases or self.gained
                    or self.spent or self.items_appeared)

@dataclass
class Drives:
    curiosity: float = 0.70
    boredom: float = 0.15
    avarice: float = 0.35

class EconomyTracker:
    def __init__(self, log: logging.Logger):
        self.log = log
        self.alias = {}
        self.signals = {}
        self.cvec = {}
        self._cvec_cache = {}
        self.prev = {}
        self.cur = {}
        self.cycle = 0
        self.state = "GATHER"
        self.state_age = 0
        self.currency = ""
        self.currency_votes = {}
        self.recipes = {}
        self.last_gain_cycle = {}
        self.chain = deque(maxlen=12)
        self.loop = []
        self.loop_i = 0
        self.loop_fail = 0
        self.sell_fail = 0
        self.craft_fail = 0
        self.sell_retry_after = 0
        self.craft_retry_after = 0
        self.explore_for = 0
        self.mass_max = 0.0
        self.prev_disabled = {}

    def to_dict(self):
        sig = {l: {"roles": sorted(r["roles"]), "item": r["item"],
                   "presence": r["presence"], "last": r["last"],
                   "ever_changed": r["ever_changed"]}
               for l, r in self.signals.items()}
        return {"alias": self.alias, "signals": sig, "currency": self.currency,
                "recipes": self.recipes, "loop": self.loop, "state": self.state}

    def load(self, blob):
        if not isinstance(blob, dict):
            return
        try:
            self.alias = {str(k): str(v) for k, v in (blob.get("alias") or {}).items()}
            for lab, rec in (blob.get("signals") or {}).items():
                self.signals[str(lab)] = {
                    "roles": set(rec.get("roles") or []),
                    "item": bool(rec.get("item")),
                    "presence": int(rec.get("presence", 0)),
                    "last": rec.get("last"),
                    "ever_changed": bool(rec.get("ever_changed"))}
                self.cvec[str(lab)] = embed(str(lab))
            self.currency = str(blob.get("currency") or "")
            self.recipes = blob.get("recipes") or {}
            self.loop = [list(x) for x in (blob.get("loop") or []) if len(x) == 2]
            self.state = str(blob.get("state") or "GATHER")
        except Exception as e:
            self.log.warning("economy memory partially unreadable: %s", e)

    def _vec(self, label):
        v = self._cvec_cache.get(label)
        if v is None:
            v = self._cvec_cache[label] = embed(label)
        return v

    def _canon(self, label: str) -> str:
        c = self.alias.get(label)
        if c and c in self.signals:
            return c
        v = self._vec(label)
        best, bs = None, 0.80
        for c2, cv in self.cvec.items():
            s = _dot(v, cv)
            if s > bs:
                bs, best = s, c2
        if best is not None:
            self.alias[label] = best
            return best
        if len(self.signals) < 48:
            self.signals[label] = {"roles": set(), "item": False,
                                   "presence": 0, "last": None,
                                   "ever_changed": False}
            self.cvec[label] = v
            self.alias[label] = label
            return label
        best = max(self.cvec, key=lambda c: _dot(v, self.cvec[c]))
        self.alias[label] = best
        return best

    def _parse(self, text: str) -> dict:
        raw = {}
        for m in NUM_RE.finditer(text or ""):
            try:
                num = float(m.group(1).replace(",", "")) * \
                    MULT.get((m.group(2) or "").lower(), 1.0)
            except ValueError:
                continue
            if not (0 <= num <= 1e13):
                continue
            pre = text[max(0, m.start() - 34):m.start()].lower().rstrip("+-: ")
            post = text[m.end():m.end() + 20].lower()
            words = [w for w in WORD_RE.findall(pre) if w not in STOPWORDS]
            if len(words) >= 2 and len(words[-2] + words[-1]) <= 22:
                label = words[-2] + " " + words[-1]
            elif words:
                label = words[-1]
            else:
                fw = [w for w in WORD_RE.findall(post) if w not in STOPWORDS]
                label = fw[0] if fw else ""
            if not label or len(label) > 26:
                continue
            raw.setdefault(label, []).append(num)
        out = {}
        for lab, vs in raw.items():
            vs.sort()
            out[self._canon(lab)] = vs[len(vs) // 2]
        return out

    def _parse_gains(self, text):
        gains, spends = {}, {}
        for m in GAIN_RE.finditer(text or ""):
            try:
                n = int(m.group(2))
            except ValueError:
                continue
            if n <= 0 or n > 100000:
                continue
            words = [w for w in m.group(3).split()[:2]
                     if w.lower() not in STOPWORDS]
            if not words:
                continue
            lab = self._canon(" ".join(words).lower())
            tgt = gains if m.group(1) == "+" else spends
            tgt[lab] = tgt.get(lab, 0) + n
        return gains, spends

    def observe(self, state: dict, memory):
        self.cycle = int(state.get("cycle") or self.cycle + 1)
        self.prev, self.cur = self.cur, self._parse(state.get("text", ""))
        for lab, val in self.cur.items():
            rec = self.signals[lab]
            rec["presence"] += 1
            old = rec["last"]
            rec["last"] = val
            if old is not None and abs(val - old) > max(0.4, 0.004 * abs(old)):
                rec["ever_changed"] = True
                self.last_gain_cycle[lab] = self.cycle
        now = time.time()
        dis = {}
        for e in state.get("elements", []):
            fp = e.get("fp")
            if fp:
                dis[fp] = bool(e.get("disabled"))
        for fp, d in dis.items():
            st = memory.elem_stat(fp)
            was = self.prev_disabled.get(fp)
            if was is True and not d:
                da = st.get("disable_at")
                if da:
                    st.setdefault("cd", []).append(round(now - da, 1))
                    del st["cd"][:-6]
                    st.pop("disable_at", None)
            elif was is False and d and not st.get("disable_at"):
                st["disable_at"] = now
        self.prev_disabled = dis

    def diff(self, pre: dict, post: dict) -> EconDelta:
        d = EconDelta(url_changed=(pre.get("url") != post.get("url")),
                      scene_changed=(pre.get("scene_tag") != post.get("scene_tag")))
        a, b = self.prev, self.cur
        for lab, v in b.items():
            if lab not in a:
                d.new_labels.append(lab)
                continue
            dv = v - a[lab]
            tol = max(0.4, 0.004 * abs(a[lab]))
            if dv > tol:
                d.increases[lab] = dv
            elif dv < -tol:
                d.decreases[lab] = dv
        d.gained, d.spent = self._parse_gains(post.get("text", ""))
        for l in d.new_labels:
            if self._looks_item(l, b.get(l)):
                d.items_appeared.append(l)
        for l in d.increases:
            if self.signals.get(l, {}).get("item"):
                d.item_up.append(l)
        for l in d.decreases:
            if self.signals.get(l, {}).get("item"):
                d.item_down.append(l)
        if self.currency and self.currency in d.increases:
            d.currency_delta = d.increases[self.currency]
        else:
            for l, dv in d.increases.items():
                if self._currency_prior(l) and dv > d.currency_delta:
                    d.currency_delta = dv
        return d

    def _looks_item(self, lab, val) -> bool:
        r = self.signals.get(lab)
        if not r or val is None:
            return False
        if r["roles"] or r["ever_changed"] or r["item"]:
            return False
        if lab == self.currency or self._currency_prior(lab):
            return False
        return len(lab) >= 3 and 0 <= val <= 99

    def _currency_prior(self, lab) -> bool:
        return _dot(self.cvec.get(lab) or embed(lab), CURRENCY_VEC) > 0.52

    def resources(self) -> dict:
        return {l: r["last"] for l, r in self.signals.items()
                if not r["item"] and l != self.currency and r["presence"] >= 2
                and ("source" in r["roles"] or "input" in r["roles"]
                     or r["ever_changed"])}

    def items(self) -> dict:
        return {l: r["last"] for l, r in self.signals.items()
                if r["item"] and (r["last"] or 0) > 0}

    def item_total(self) -> int:
        return sum(1 for v in self.items().values() if v)

    def _mass(self) -> float:
        return sum(max(0.0, r["last"] or 0) for l, r in self.signals.items()
                   if not r["item"] and l != self.currency
                   and r["presence"] >= 3
                   and (r["ever_changed"] or "source" in r["roles"]
                        or "input" in r["roles"]))

    def _mass_ratio(self) -> float:
        m = self._mass()
        self.mass_max = max(self.mass_max, m)
        return (m / self.mass_max) if self.mass_max > 0 else 0.0

    def _gather_stalled(self) -> bool:
        last = max((self.last_gain_cycle.get(l, -99)
                    for l in self.resources()), default=-99)
        return (self.cycle - last) >= 3

    def can_craft(self) -> bool:
        for _item, cost in self.recipes.items():
            ok = True
            for l, c in cost.items():
                have = (self.signals.get(l) or {}).get("last") or 0
                if have < c:
                    ok = False
                    break
            if ok:
                return True
        return False

    def decide(self, drives: Drives, stuck_cur: int) -> str:
        if self.loop and drives.avarice > 0.72:
            self.state = self.loop[min(self.loop_i, len(self.loop) - 1)][0]
            return self.state
        if stuck_cur >= 5 and self.explore_for <= 0:
            self.explore_for = 3
        if (drives.boredom > 0.75 and drives.avarice < 0.6
                and self.explore_for <= 0):
            self.explore_for = 2 + int(drives.curiosity * 3)
        if self.explore_for > 0:
            want = "EXPLORE"
        elif self.item_total() >= 1 and self.cycle >= self.sell_retry_after:
            want = "SELL"
        elif (self.can_craft() or (self._gather_stalled()
                                   and self._mass_ratio() > 0.45)
                and self.cycle >= self.craft_retry_after):
            want = "CRAFT"
        else:
            want = "GATHER"
        if (self.state_age < 2 and want != self.state
                and self.state in ("GATHER", "CRAFT")
                and want in ("GATHER", "CRAFT")):
            return self.state
        if want != self.state:
            self.state, self.state_age = want, 0
        else:
            self.state_age += 1
        return self.state

    def satisfaction(self, activity: str, d: EconDelta) -> bool:
        if activity == "GATHER":
            return self._resource_up(d)
        if activity == "CRAFT":
            return bool(d.gained or d.items_appeared or d.item_up)
        if activity == "SELL":
            if d.currency_delta and d.currency_delta > 0:
                return True
            return bool(d.item_down) and bool(d.increases)
        if activity == "EXPLORE":
            return bool(d.new_labels or d.new_elements
                        or d.scene_changed or d.url_changed)
        return False

    def _resource_up(self, d: EconDelta) -> bool:
        for l in d.increases:
            if l == self.currency:
                continue
            r = self.signals.get(l)
            if r and r["item"]:
                continue
            if r and ("source" in r["roles"] or "input" in r["roles"]
                      or r["ever_changed"]):
                return True
        return False

    def apply_outcome(self, activity, d, satisfied, outcome, scene_tag, drives):
        if not activity:
            return
        if activity == "EXPLORE" and self.explore_for > 0:
            self.explore_for -= 1
        if satisfied:
            self.chain.append((activity, scene_tag))
            if activity == "GATHER":
                for l in d.increases:
                    r = self.signals.get(l)
                    if r and not r["item"] and l != self.currency:
                        r["roles"].add("source")
            elif activity == "CRAFT":
                for l in d.decreases:
                    if l in self.signals:
                        self.signals[l]["roles"].add("input")
                new_item = None
                for l in list(d.gained) + list(d.items_appeared):
                    if l in self.signals:
                        self.signals[l]["item"] = True
                        new_item = new_item or l
                if new_item and d.decreases:
                    self.recipes[new_item] = {k: abs(v)
                                              for k, v in d.decreases.items()}
                if not (self.loop and drives.avarice > 0.72):
                    self.state, self.state_age = "SELL", 0
            elif activity == "SELL":
                for l in d.increases:
                    self.currency_votes[l] = self.currency_votes.get(l, 0) + 1
                best = max(self.currency_votes, key=self.currency_votes.get,
                           default=None)
                if best and self.currency_votes.get(best, 0) >= 2:
                    self.currency = best
                self.sell_fail = 0
                if not (self.loop and drives.avarice > 0.72):
                    self.state, self.state_age = "GATHER", 0
                self._close_loop(d, drives)
        else:
            if activity == "SELL" and outcome in ("futile", "error"):
                self.sell_fail += 1
                if self.sell_fail >= 8:
                    self.sell_retry_after = self.cycle + 25
                    self.sell_fail = 0
                    self.log.info("econ | selling is fruitless for now; "
                                  "will retry in ~25 cycles")
            if activity == "CRAFT" and outcome in ("futile", "error"):
                self.craft_fail += 1
                if self.craft_fail >= 8:
                    self.craft_retry_after = self.cycle + 25
                    self.craft_fail = 0
        if self.loop and drives.avarice > 0.72:
            step = self.loop[min(self.loop_i, len(self.loop) - 1)]
            if satisfied and activity == step[0]:
                self.loop_i = (self.loop_i + 1) % len(self.loop)
                self.loop_fail = 0
            elif outcome in ("futile", "error"):
                self.loop_fail += 1
                if self.loop_fail >= 3:
                    self.log.info("econ | money loop abandoned after failures")
                    self.loop, self.loop_i, self.loop_fail = [], 0, 0
                    drives.avarice = 0.4

    def _close_loop(self, d, drives):
        acts = [a for a, _ in self.chain]
        if "GATHER" in acts and "CRAFT" in acts and d.currency_delta > 0:
            self.loop = [[a, s] for a, s in self.chain]
            self.loop_i = 0
            self.loop_fail = 0
            drives.avarice = min(1.0, max(drives.avarice, 0.9))
            self.log.info("MONEY LOOP locked in: %s | avarice spiked to %.2f",
                          " -> ".join("%s@%s" % (a.lower(), s)
                                      for a, s in self.loop), drives.avarice)
        elif d.currency_delta <= 0:
            drives.avarice = max(0.10, drives.avarice * 0.85)
        self.chain.clear()

# ═════════════════════════ 5. REASON (heuristics -> code) ════════════════════

@dataclass
class Plan:
    name: str
    code: str
    desc: str
    ctx: dict = field(default_factory=dict)
    timeout: float = 60.0
    match: float = 0.5

class Reasoner:
    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg, self.log = cfg, log
        try:
            self._domain = urlsplit(cfg.url).hostname or ""
        except Exception:
            self._domain = ""

    def think(self, state, history, memory, econ, drives, stuck, mode, subgoal):
        if state.get("flags", {}).get("cloudflare"):
            return self._plan_challenge()
        if mode == "pipeline":
            return self._think_pipeline(state, history, memory, subgoal)
        return self._think_economy(state, history, memory, econ, drives, stuck)

    def _think_economy(self, state, history, memory, econ, drives, stuck):
        activity = econ.decide(drives, stuck)
        last = history[-1] if history else {}
        force = last.get("error_sig") in ("intercept", "viewport", "invisible")

        modal = [e for e in state.get("elements", []) if e.get("modal")]
        if modal:
            enabled = [e for e in modal if not e.get("disabled")]
            hit = self._best_for_activity(enabled, activity, memory)
            if hit:
                return self._plan_click(hit[1], state, activity, memory,
                                        name="overlay-" + activity.lower(),
                                        force=force, match=hit[0],
                                        activity=activity)
            dis = self._best_anchor(enabled)
            if dis:
                return self._plan_click(dis, state, activity, memory,
                                        name="dismiss-overlay", force=force,
                                        activity=activity)

        if self.cfg.fields:
            m = self._match_field(state, memory)
            if m:
                return self._plan_fill(m[1], m[2], m[3], state, activity)

        cands = self._rank_for_activity(state, activity, memory, False)
        if cands:
            score, el = cands[0]
            if score >= 0.30 and score > self._navness(el, memory) + 0.10:
                return self._plan_click(el, state, activity, memory,
                                        name="act-" + activity.lower(),
                                        force=force, match=score,
                                        activity=activity)

        allc = self._rank_for_activity(state, activity, memory, True)
        if allc and allc[0][1].get("disabled"):
            est = self._cooldown_estimate(memory, allc[0][1])
            return self._plan_wait(est, "%s is busy (cooldown ~%.0fs)"
                                   % (activity.lower(), est), activity)

        dest_override = None
        if econ.loop and drives.avarice > 0.72:
            step = econ.loop[min(econ.loop_i, len(econ.loop) - 1)]
            if step[1] in SCENE_VECS:
                dest_override = step[1]
        nav = self._nav_candidates(state, activity, memory, dest_override)
        if nav:
            return self._plan_click(nav[0][1], state, activity, memory,
                                    name="nav-" + activity.lower(),
                                    force=force, match=nav[0][0],
                                    activity=activity, nav=True)

        if not self._recently(history, "shake:scrolled", 2):
            return self._plan_shake()
        el = self._pick_novel(state, memory)
        if el:
            return self._plan_click(el, state, activity, memory,
                                    name="explore-click", match=0.25,
                                    activity=activity)
        if stuck >= 5 and not self._recently(history, "page:reloaded", 3):
            return self._plan_reload(activity)
        return self._plan_idle(4.0)

    def _think_pipeline(self, state, history, memory, subgoal):
        pb, gb = page_key(state), goal_key(subgoal)
        last = history[-1] if history else {}
        force = last.get("error_sig") in ("intercept", "viewport", "invisible")
        win = memory.wins.get(pb + "|" + gb)
        if win:
            tf = win.get("target_fp") or ""
            fresh = (not tf) or any(e.get("fp") == tf
                                    for e in state.get("elements", []))
            ok = (int(win.get("fails", 0)) < 2
                  and not (last.get("used_win")
                           and last.get("outcome") in ("futile", "error")))
            if ok and fresh:
                el = next((e for e in state.get("elements", [])
                           if e.get("fp") == tf), None)
                return Plan("reuse-learned", win["code"],
                            "replay learned snippet (%s)" % win.get("how", "?"),
                            ctx=self._ctx_for(el) if el else {"target_fp": tf},
                            timeout=60.0, match=0.5)
            win["fails"] = int(win.get("fails", 0)) + 1
        if self.cfg.fields:
            m = self._match_field(state, memory)
            if m:
                return self._plan_fill(m[1], m[2], m[3], state, subgoal)
        modal = [e for e in state.get("elements", [])
                 if e.get("modal") and not e.get("disabled")]
        if modal:
            hit = self._best_goal(modal, subgoal, memory)
            if hit and hit[0] >= 0.45:
                return self._plan_click(hit[1], state, subgoal, memory,
                                        name="overlay-cta", force=force,
                                        match=hit[0])
            dis = self._best_anchor(modal)
            if dis:
                return self._plan_click(dis, state, subgoal, memory,
                                        name="dismiss-overlay", force=force)
        avoid = last.get("target_fp") if last.get("outcome") in ("futile",
                                                                "error") else None
        hit = self._best_goal(state.get("elements", []), subgoal, memory, avoid)
        if hit and hit[0] >= 0.22:
            return self._plan_click(hit[1], state, subgoal, memory,
                                    name="click-cta", force=force, match=hit[0])
        if not self._recently(history, "shake:scrolled", 2):
            return self._plan_shake()
        el = self._pick_novel(state, memory)
        if el:
            return self._plan_click(el, state, subgoal, memory,
                                    name="explore-click", match=0.25)
        return self._plan_idle(4.0)

    def _action_score(self, el, av, memory) -> float:
        lab = el_label(el)
        s = 2.4 * _dot(embed(lab), av)
        if el.get("tag") == "button" or el.get("role") == "button":
            s += 0.12
        if el.get("href"):
            s -= 0.22
        if (el.get("y") or 0) > self.cfg.viewport_h * 1.7:
            s -= 0.25
        if (el.get("w") or 0) < 22 or (el.get("h") or 0) < 12:
            s -= 0.30
        if len(lab) > 80:
            s -= 0.40
        st = memory.elem.get(el.get("fp") or "")
        if st:
            s -= 0.22 * int(st.get("fails", 0))
            s += 0.10 * int(st.get("wins", 0))
            if st.get("nav"):
                s -= 0.60
        return s

    def _navness(self, el, memory) -> float:
        s = _dot(embed(el_label(el)), NAV_VEC)
        if el.get("href"):
            s += 0.18
        if el.get("tag") == "a":
            s += 0.08
        if (el.get("y") if el.get("y") is not None else 999) < 110 \
                and (el.get("h") or 99) < 60:
            s += 0.10
        st = memory.elem.get(el.get("fp") or "")
        if st and st.get("nav"):
            s += 0.50
        return s

    def _rank_for_activity(self, state, activity, memory, include_disabled):
        av = ACT_VEC.get(activity, ACT_VEC["EXPLORE"])
        out = []
        for el in state.get("elements", []):
            if el.get("disabled") and not include_disabled:
                continue
            if self._is_blocked(el):
                continue
            out.append((self._action_score(el, av, memory), el))
        out.sort(key=lambda t: -t[0])
        return out[:14]

    def _best_for_activity(self, els, activity, memory):
        av = ACT_VEC.get(activity, ACT_VEC["EXPLORE"])
        best = None
        for el in els:
            if self._is_blocked(el):
                continue
            s = self._action_score(el, av, memory)
            if best is None or s > best[0]:
                best = (s, el)
        return best

    def _best_goal(self, els, subgoal, memory, avoid_fp=None):
        gv = embed(subgoal)
        best = None
        for el in els:
            if el.get("disabled") or self._is_blocked(el):
                continue
            s = self._action_score(el, gv, memory)
            if avoid_fp and el.get("fp") == avoid_fp:
                s -= 0.5
            if best is None or s > best[0]:
                best = (s, el)
        return best

    def _best_anchor(self, els):
        best, bs = None, 0.30
        for e in els:
            s = _dot(embed(el_label(e)), DISMISS_VEC)
            if s > bs:
                bs, best = s, e
        return best

    def _nav_candidates(self, state, activity, memory, dest_override=None):
        dest = dest_override or ACT_DEST.get(activity, "hub")
        dv = SCENE_VECS.get(dest, SCENE_VECS["hub"])
        cur_tag = state.get("scene_tag", "hub")
        out = []
        for el in state.get("elements", []):
            if el.get("disabled") or self._is_blocked(el):
                continue
            fp = el.get("fp") or ""
            mapped = memory.nav_map.get(fp)
            if mapped == cur_tag and dest == cur_tag:
                continue
            s = 2.0 * _dot(embed(el_label(el)), dv)
            if mapped == dest:
                s += 1.6
            s += 0.5 * self._navness(el, memory)
            st = memory.elem.get(fp) or {}
            s -= 0.15 * int(st.get("fails", 0))
            if s < 0.30 and mapped != dest:
                continue
            out.append((s, el))
        out.sort(key=lambda t: -t[0])
        return out[:6]

    def _pick_novel(self, state, memory):
        cands = []
        for el in state.get("elements", []):
            if el.get("disabled") or self._is_blocked(el):
                continue
            if (el.get("w") or 0) < 22 or (el.get("h") or 0) < 12:
                continue
            st = memory.elem.get(el.get("fp") or "")
            if st and int(st.get("tries", 0)) > 0:
                continue
            cands.append(el)
        if not cands:
            cands = [e for e in state.get("elements", [])
                     if not e.get("disabled") and not self._is_blocked(e)]
        return random.choice(cands[:10]) if cands else None

    def _match_field(self, state, memory):
        best = None
        pb = page_key(state)
        for key, val in self.cfg.fields.items():
            kv = embed(key)
            for inp in state.get("inputs", []):
                if inp.get("disabled"):
                    continue
                if (pb + "|" + inp.get("fp", "")) in memory.filled:
                    continue
                st = memory.elem.get(inp.get("fp", ""))
                if st and int(st.get("fails", 0)) >= 3:
                    continue
                fi = inp.get("input") or {}
                purpose = " ".join(str(x) for x in
                                  (fi.get("type"), fi.get("name"), fi.get("id"),
                                   fi.get("placeholder"), fi.get("autocomplete"),
                                   inp.get("aria"), inp.get("text"),
                                   inp.get("title")) if x)
                s = _dot(kv, embed(purpose))
                if s >= 0.30 and (best is None or s > best[0]):
                    best = (s, inp, key, val)
        return best

    def _is_blocked(self, el) -> bool:
        txt = " ".join((el.get("text") or "", el.get("aria") or "",
                        el.get("title") or ""))
        if RISKY_RE.search(txt) and not self.cfg.allow_signing:
            return True
        if SOCIAL_RE.search(txt):
            return True
        href = el.get("href") or ""
        if href.startswith(("mailto:", "tel:", "javascript:void")):
            return True
        if href.startswith(("http://", "https://")):
            try:
                h = urlsplit(href).hostname or ""
                if h and self._domain and h != self._domain \
                        and not h.endswith("." + self._domain):
                    return True
            except ValueError:
                return True
        return False

    def _recently(self, history, needle, n):
        return any(needle in (h.get("how") or "") for h in list(history)[-n:])

    def _cooldown_estimate(self, memory, el) -> float:
        st = memory.elem.get(el.get("fp") or "") or {}
        cds = st.get("cd") or []
        est = (sum(cds) / len(cds)) if cds else 8.0
        return max(3.0, min(40.0, est * 1.15 + 1.5))

    def _ctx_for(self, el, activity="", nav_to=""):
        if not el:
            return {"activity": activity, "nav_to": nav_to}
        cx = int(el.get("x", 0) + (el.get("w") or 0) / 2)
        cy = int(el.get("y", 0) + (el.get("h") or 0) / 2)
        lab = el_label(el)
        return {"target_fp": el.get("fp", ""), "target_text": lab[:48],
                "cx": cx, "cy": cy,
                "js_spec": {"text": lab[:60],
                            "aria": (el.get("aria") or "")[:48],
                            "testid": el.get("testid") or "",
                            "cx": cx, "cy": cy},
                "activity": activity, "nav_to": nav_to}

    def _frame_prelude(self, el):
        if not el.get("frame_index"):
            return [], "page"
        return ["    fr = page",
                "    for _f in page.frames:",
                '        if %s in (_f.url or ""):'
                % q(_frame_frag(el.get("frame_url") or "")),
                "            fr = _f",
                "            break"], "fr"

    def _class_token(self, el):
        for t in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,31}", el.get("cls") or ""):
            if not t.lower().startswith(("css-", "js-", "sc-", "emotion")):
                return t
        return ""

    def _selector_candidates(self, el, memory, tgt, state, pb, gb):
        out = []
        name = (el.get("text") or el.get("aria") or el.get("title") or "").strip()
        role = el.get("role") or ("button" if el.get("tag") == "button" else
                                  "link" if el.get("tag") == "a" else "")
        if el.get("testid"):
            out.append(("testid", "%s.locator(%s).first"
                        % (tgt, q(_css_attr("data-testid", el["testid"])))))
        if role and 2 <= len(name) <= 48:
            out.append(("role-name", "%s.get_by_role(%s, name=%s).first"
                        % (tgt, q(role), q(name))))
        if 2 <= len(name) <= 48:
            out.append(("text-exact", "%s.get_by_text(%s, exact=True).first"
                        % (tgt, q(name))))
            out.append(("has-text", "%s.locator(%s, has_text=%s).first"
                        % (tgt, q(el.get("tag", "button")), q(name[:36]))))
        tok = self._class_token(el)
        if tok:
            same = [e for e in state.get("elements", [])
                    if e.get("frame_index") == el.get("frame_index")
                    and e.get("tag") == el.get("tag")
                    and tok in (e.get("cls") or "").split()]
            try:
                idx = same.index(el)
            except ValueError:
                idx = 0
            out.append(("css-nth", "%s.locator(%s).nth(%d)"
                        % (tgt, q(el.get("tag", "button") + "." + tok), idx)))
        pruned = []
        for how, expr in out:
            if memory.sel_fails.get("|".join((pb, gb, how)), 0) >= 2:
                continue
            pruned.append((how, expr))
        return pruned

    def _plan_click(self, el, state, goal, memory, name="click-cta",
                    force=False, match=0.5, activity="", nav=False) -> Plan:
        pb, gb = page_key(state), goal_key(goal)
        prelude, tgt = self._frame_prelude(el)
        nav_to = ACT_DEST.get(activity, "") if nav else ""
        L = []
        A = L.append
        A('async def strategy(page, ctx, log, S):')
        A('    # plan: %s | goal: %s | target: %s%s'
          % (name, _cmt(goal), _cmt(el_label(el)),
             (" | navigate to: " + nav_to) if nav else ""))
        A('    # every selector below was derived from the live DOM probe')
        A('    last = "no candidate"')
        A('    try:')
        A('        await page.wait_for_load_state("domcontentloaded", timeout=8000)')
        A('    except Exception:')
        A('        pass')
        for ln in prelude:
            A(ln)
        A('    cands = [')
        for how, expr in self._selector_candidates(el, memory, tgt, state, pb, gb):
            A('        (%s, lambda: %s),' % (q(how), expr))
        A('    ]')
        A('    for how, mk in cands:')
        A('        try:')
        A('            loc = mk()')
        if not force:
            A('            try:')
            A('                await loc.scroll_into_view_if_needed(timeout=2500)')
            A('            except Exception:')
            A('                pass')
        A('            await loc.click(timeout=4500%s)'
          % (", force=True" if force else ""))
        A('            log("clicked via " + how)')
        A('            await wait(0.7)')
        A('            return {"ok": True, "how": "click:" + how,')
        A('                    "target": S.get("target_fp", "")}')
        A('        except Exception as e:')
        A('            last = how + ": " + str(e)[:120]')
        A('            log("miss " + last)')
        if not el.get("frame_index"):
            A('    # fallback 1: real mouse at coordinates from the live probe')
            A('    try:')
            A('        await page.mouse.move(int(S["cx"]), int(S["cy"]), steps=6)')
            A('        await page.mouse.click(int(S["cx"]), int(S["cy"]))')
            A('        log("clicked via coords")')
            A('        await wait(0.7)')
            A('        return {"ok": True, "how": "click:coords",')
            A('                "target": S.get("target_fp", "")}')
            A('    except Exception as e:')
            A('        last = "coords: " + str(e)[:120]')
            A('        log(last)')
        A('    # fallback 2: direct JS dispatch (ignores pointer interception)')
        A('    try:')
        A('        if await js_click(%s, S["js_spec"]):' % tgt)
        A('            log("clicked via js-dispatch")')
        A('            await wait(0.7)')
        A('            return {"ok": True, "how": "click:js-dispatch",')
        A('                    "target": S.get("target_fp", "")}')
        A('    except Exception as e:')
        A('        last = "js: " + str(e)[:120]')
        A('        log(last)')
        A('    return {"ok": False, "why": last, "how": "click:failed"}')
        return Plan(name, "\n".join(L), el_label(el)[:48],
                    ctx=self._ctx_for(el, activity, nav_to),
                    timeout=60.0, match=match)

    def _plan_fill(self, inp, key, value, state, goal) -> Plan:
        fi = inp.get("input") or {}
        prelude, tgt = self._frame_prelude(inp)
        L = []
        A = L.append
        A('async def strategy(page, ctx, log, S):')
        A('    # plan: fill-field | goal: %s | field key: %s (value never logged)'
          % (_cmt(goal), _cmt(key)))
        A('    last = "no input candidate"')
        for ln in prelude:
            A(ln)
        A('    cands = [')
        ph = (fi.get("placeholder") or "").strip()
        if ph:
            A('        (%s, lambda: %s.get_by_placeholder(%s).first),'
              % (q("placeholder"), tgt, q(ph)))
        nm = (inp.get("aria") or inp.get("text") or "").strip()
        role = "combobox" if inp.get("tag") == "select" else "textbox"
        if nm and len(nm) <= 40:
            A('        (%s, lambda: %s.get_by_role(%s, name=%s).first),'
              % (q("role-name"), tgt, q(role), q(nm)))
        if fi.get("name"):
            A('        (%s, lambda: %s.locator(%s).first),'
              % (q("css-name"), tgt, q(_css_attr("name", fi["name"]))))
        if fi.get("id"):
            A('        (%s, lambda: %s.locator(%s).first),'
              % (q("css-id"), tgt, q(_css_attr("id", fi["id"]))))
        t = re.sub(r"[^a-z0-9_-]", "", (fi.get("type") or "").lower())[:20]
        if t and t not in ("button", "submit", "checkbox", "radio",
                           "file", "hidden", "image", "reset"):
            same = [e for e in state.get("inputs", [])
                    if e.get("frame_index") == inp.get("frame_index")
                    and re.sub(r"[^a-z0-9_-]", "",
                               (e.get("input") or {}).get("type", "").lower()) == t]
            try:
                idx = same.index(inp)
            except ValueError:
                idx = 0
            A('        (%s, lambda: %s.locator(%s).nth(%d)),'
              % (q("nth-type"), tgt,
                 q('%s[type="%s"]' % (inp.get("tag", "input"), t)), idx))
        same_tag = [e for e in state.get("inputs", [])
                    if e.get("frame_index") == inp.get("frame_index")
                    and e.get("tag") == inp.get("tag")]
        try:
            idx2 = same_tag.index(inp)
        except ValueError:
            idx2 = 0
        A('        (%s, lambda: %s.locator(%s).nth(%d)),'
          % (q("nth-any"), tgt, q(inp.get("tag", "input")), idx2))
        A('    ]')
        A('    for how, mk in cands:')
        A('        try:')
        A('            loc = mk()')
        A('            await loc.fill(S["value"], timeout=4000)')
        A('            log("filled field " + S["key"] + " via " + how)')
        A('            await wait(0.35)')
        A('            if S.get("press_enter"):')
        A('                try:')
        A('                    await loc.press("Enter", timeout=2000)')
        A('                except Exception:')
        A('                    pass')
        A('            return {"ok": True, "how": "fill:" + how,')
        A('                    "target": S.get("target_fp", "")}')
        A('        except Exception as e:')
        A('            last = how + ": " + str(e)[:120]')
        A('            log("miss " + last)')
        A('    return {"ok": False, "why": last, "how": "fill:failed"}')
        ctx = {"target_fp": inp.get("fp", ""), "value": value, "key": key,
               "press_enter": bool(self.cfg.enter_submit)}
        return Plan("fill-field", "\n".join(L), "input for %r" % key,
                    ctx=ctx, timeout=45.0, match=0.5)

    def _plan_wait(self, seconds, reason, activity="") -> Plan:
        L = ['async def strategy(page, ctx, log, S):',
             '    # plan: wait-cooldown | %s' % _cmt(reason),
             '    await wait(float(S["seconds"]))',
             '    return {"ok": True, "how": "wait:cooldown"}']
        return Plan("wait-cooldown", "\n".join(L), reason,
                    ctx={"seconds": float(seconds), "activity": activity},
                    timeout=float(seconds) + 15.0, match=0.0)

    def _plan_challenge(self) -> Plan:
        polls = max(8, int(self.cfg.challenge_patience // 3))
        L = ['async def strategy(page, ctx, log, S):',
             '    # plan: wait-out-challenge | passive patience, never CAPTCHA solving']
        L.append('    for i in range(%d):' % polls)
        L += ['        await wait(3.0)',
              '        try:',
              '            st = await page.evaluate(%s)' % q(JS_CHALLENGE_CHECK),
              '        except Exception as e:',
              '            log("poll error: " + str(e)[:80])',
              '            continue',
              '        log("poll " + str(i) + " title=" + st["t"][:40]'
              ' + " cf=" + str(st["cf"]))',
              '        if ("just a moment" not in st["t"].lower()) and (not st["cf"]):',
              '            await wait(1.0)',
              '            return {"ok": True, "how": "challenge:cleared"}',
              '        try:',
              '            await page.mouse.move(300 + (i * 37) % 400,'
              ' 260 + (i * 23) % 200, steps=4)',
              '        except Exception:',
              '            pass',
              '    return {"ok": False, "why": "challenge still present",'
              ' "how": "challenge:persisted"}']
        return Plan("wait-out-challenge", "\n".join(L), "cloudflare interstitial",
                    ctx={}, timeout=float(polls * 3 + 20), match=0.0)

    def _plan_shake(self) -> Plan:
        L = ['async def strategy(page, ctx, log, S):',
             '    # plan: shake-viewport | widen the searchable area',
             '    try:',
             '        await page.mouse.wheel(0, 700)',
             '    except Exception:',
             '        pass',
             '    await wait(1.2)',
             '    try:',
             '        await page.mouse.wheel(0, -350)',
             '    except Exception:',
             '        pass',
             '    await wait(0.8)',
             '    return {"ok": True, "how": "shake:scrolled"}']
        return Plan("shake-viewport", "\n".join(L), "scroll probe",
                    ctx={}, timeout=25.0, match=0.0)

    def _plan_reload(self, goal) -> Plan:
        L = ['async def strategy(page, ctx, log, S):',
             '    # plan: hard-reset | stuck on %s' % _cmt(goal),
             '    await page.reload(wait_until="domcontentloaded", timeout=45000)',
             '    await wait(2.0)',
             '    return {"ok": True, "how": "page:reloaded"}']
        return Plan("hard-reset", "\n".join(L), "reload",
                    ctx={}, timeout=60.0, match=0.0)

    def _plan_idle(self, seconds) -> Plan:
        L = ['async def strategy(page, ctx, log, S):',
             '    # plan: idle-observe | nothing actionable; let the world settle',
             '    await wait(float(S.get("seconds", 4.0)))',
             '    return {"ok": True, "how": "idle:waited"}']
        return Plan("idle-observe", "\n".join(L), "pause",
                    ctx={"seconds": float(seconds)},
                    timeout=float(seconds) + 10.0, match=0.0)

# ═════════════════ 6. PLAYGROUND (safe exec sandbox, unchanged) ══════════════

FORBIDDEN_NAMES = frozenset((
    "__import__", "eval", "exec", "compile", "open", "input", "getattr",
    "setattr", "delattr", "vars", "globals", "locals", "breakpoint", "help",
    "exit", "quit", "memoryview", "type", "object", "super", "classmethod",
    "staticmethod", "__builtins__"))

SAFE_BUILTINS = {n: getattr(_py_builtins, n) for n in (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "Exception",
    "BaseException", "float", "format", "int", "isinstance", "len", "list",
    "map", "max", "min", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "zip", "ValueError", "KeyError",
    "TypeError", "IndexError", "AttributeError", "RuntimeError",
    "StopIteration", "ArithmeticError", "ZeroDivisionError")}

def _screen_code(code: str):
    tree = ast.parse(code)
    if (len(tree.body) != 1
            or not isinstance(tree.body[0], ast.AsyncFunctionDef)
            or tree.body[0].name != "strategy"):
        raise ValueError("code must define exactly one `async def strategy(...)`")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global,
                             ast.Nonlocal)):
            raise ValueError("forbidden construct: %s" % type(node).__name__)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError("forbidden name: %r" % node.id)
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("dunder attribute access is blocked")
    return tree

async def _sandbox_wait(seconds):
    try:
        s = float(seconds)
    except Exception:
        s = 1.0
    await asyncio.sleep(max(0.0, min(s, 30.0)))

async def _sandbox_js_click(target, spec):
    try:
        return bool(await target.evaluate(JS_CLICK, dict(spec or {})))
    except Exception:
        return False

@dataclass
class ExecutionResult:
    ok: bool
    how: str
    why: str
    logs: list
    error_type: str = ""
    duration: float = 0.0
    tail: str = ""

class Playground:
    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg, self.log = cfg, log
        self.page = None
        self.context = None

    def bind(self, page, context):
        self.page, self.context = page, context

    async def run(self, plan: Plan) -> ExecutionResult:
        t0 = time.time()
        logs = []

        def log(msg):
            entry = str(msg)[:200]
            logs.append(entry)
            self.log.debug("  [gen] %s", entry)

        try:
            tree = _screen_code(plan.code)
        except Exception as e:
            return ExecutionResult(False, plan.name + ":compile",
                                   "compile/screen: %s" % str(e)[:160], logs,
                                   "CompileError", time.time() - t0, "")
        ns = {"__builtins__": SAFE_BUILTINS,
              "wait": _sandbox_wait, "js_click": _sandbox_js_click}
        try:
            exec(compile(tree, "<generated>", "exec"), ns)
            fn = ns["strategy"]
        except Exception as e:
            return ExecutionResult(False, plan.name + ":define",
                                   "define: %s" % str(e)[:160], logs,
                                   type(e).__name__, time.time() - t0,
                                   traceback.format_exc()[-400:])
        try:
            raw = await asyncio.wait_for(
                fn(self.page, self.context, log, dict(plan.ctx)),
                timeout=plan.timeout)
        except asyncio.TimeoutError:
            return ExecutionResult(False, plan.name + ":timeout",
                                   "exceeded %.0fs sandbox budget" % plan.timeout,
                                   logs, "TimeoutError", time.time() - t0, "")
        except Exception as e:
            return ExecutionResult(False, plan.name + ":error",
                                   "%s: %s" % (type(e).__name__, str(e)[:180]),
                                   logs, type(e).__name__, time.time() - t0,
                                   traceback.format_exc()[-400:])
        res = raw if isinstance(raw, dict) else {"ok": bool(raw)}
        return ExecutionResult(bool(res.get("ok")),
                               str(res.get("how") or plan.name),
                               str(res.get("why", "")), logs, None,
                               time.time() - t0, "")

# ═════════════════ 7. REFLECT (econ evidence -> memory -> drives) ═══════════

ERROR_PATTERNS = [
    (re.compile(r"intercepts pointer|pointer events", re.I), "intercept"),
    (re.compile(r"outside of the viewport", re.I), "viewport"),
    (re.compile(r"not visible", re.I), "invisible"),
    (re.compile(r"timeout|timed out", re.I), "timeout"),
    (re.compile(r"frame", re.I), "frame"),
    (re.compile(r"strict mode", re.I), "strict"),
    (re.compile(r"net::ERR|ERR_NAME|ERR_CONNECTION|ERR_TIMED", re.I), "network"),
    (re.compile(r"execution context|not attached|detached", re.I), "context"),
]

def _error_sig(res: ExecutionResult):
    if res.ok:
        return None
    blob = (res.why or "") + " " + (res.tail or "")
    for rx, tag in ERROR_PATTERNS:
        if rx.search(blob):
            return tag
    return "other"

@dataclass
class Reflection:
    outcome: str        # econ | progress | nav | futile | error
    satisfied: bool
    error_sig: str
    delta: EconDelta
    added: int
    removed: int

class Reflector:
    def __init__(self, log: logging.Logger):
        self.log = log

    def reflect(self, pre, plan, res, post, econ, drives, memory, mode, subgoal):
        pfps = {e.get("fp") for e in pre.get("elements", [])}
        qfps = {e.get("fp") for e in post.get("elements", [])}
        added, removed = len(qfps - pfps), len(pfps - qfps)
        d = econ.diff(pre, post)
        d.new_elements = added
        activity = str(plan.ctx.get("activity") or "")
        error_sig = _error_sig(res)

        if not post.get("text"):
            outcome, satisfied = "error", False
        elif not res.ok:
            outcome, satisfied = "error", False
        elif mode == "economy" and activity:
            satisfied = econ.satisfaction(activity, d)
            if satisfied:
                outcome = "econ"
            elif d.url_changed or d.scene_changed:
                outcome = "nav"
            elif d.movement() or added:
                outcome = "progress"
            else:
                outcome = "futile"
        else:
            changed = bool(d.url_changed or added or removed
                           or pre.get("title") != post.get("title")
                           or pre.get("text") != post.get("text"))
            outcome = "progress" if changed else "futile"
            satisfied = plan.match >= 0.22 and outcome == "progress"

        tf = str(plan.ctx.get("target_fp") or "")
        if tf:
            st = memory.elem_stat(tf)
            st["tries"] = int(st.get("tries", 0)) + 1
            if outcome in ("econ", "progress"):
                st["wins"] = int(st.get("wins", 0)) + 1
            elif outcome in ("futile", "error"):
                st["fails"] = int(st.get("fails", 0)) + 1
        if outcome == "nav" and tf:
            memory.nav_map[tf] = post.get("scene_tag", "hub")
            memory.elem_stat(tf)["nav"] = True
        if outcome == "econ" and activity:
            tag = post.get("scene_tag", "hub")
            aff = memory.scene_aff.setdefault(tag, {})
            aff[activity] = int(aff.get(activity, 0)) + 1
        econ.apply_outcome(activity, d, satisfied, outcome,
                            post.get("scene_tag", "hub"), drives)
        self._update_drives(drives, outcome, d)
        return Reflection(outcome, satisfied, error_sig, d, added, removed)

    @staticmethod
    def _update_drives(drives, outcome, d):
        if outcome == "econ":
            drives.boredom = max(0.0, drives.boredom - 0.35)
            drives.curiosity = max(0.10, drives.curiosity - 0.15)
            if d.currency_delta > 0:
                drives.avarice = min(1.0, drives.avarice + 0.20)
        elif outcome == "nav":
            drives.boredom = min(1.0, drives.boredom + 0.08)
            if d.scene_changed:
                drives.curiosity = max(0.10, drives.curiosity - 0.20)
        elif outcome == "progress":
            drives.boredom = max(0.0, drives.boredom - 0.10)
        elif outcome == "futile":
            drives.boredom = min(1.0, drives.boredom + 0.12)
            drives.curiosity = min(1.0, drives.curiosity + 0.06)
        else:
            drives.boredom = min(1.0, drives.boredom + 0.05)
        if d.currency_delta <= 0:
            drives.avarice = max(0.10, drives.avarice - 0.015)

# ═══════════════════════════════ 8. MEMORY ══════════════════════════════════

class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.elem = {}        # fp -> {tries, wins, fails, cd[], nav, disable_at}
        self.wins = {}        # page|goal -> winning generated code (pipeline)
        self.sel_fails = {}
        self.filled = set()
        self.nav_map = {}      # element fp -> scene tag it navigates to
        self.scene_aff = {}    # scene tag -> {activity: successes}
        self.econ = {}         # EconomyTracker persistence blob
        self.cycles = 0
        self.sessions = 0

    def elem_stat(self, fp):
        st = self.elem.get(fp)
        if st is None:
            st = self.elem[fp] = {"tries": 0, "wins": 0, "fails": 0, "cd": []}
        return st

    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except Exception:
                pass
            return
        try:
            self.elem = raw.get("elem") or {}
            self.wins = raw.get("wins") or {}
            self.sel_fails = raw.get("sel_fails") or {}
            self.filled = set(raw.get("filled") or [])
            self.nav_map = raw.get("nav_map") or {}
            self.scene_aff = raw.get("scene_aff") or {}
            self.econ = raw.get("econ") or {}
            self.cycles = int(raw.get("cycles", 0))
            self.sessions = int(raw.get("sessions", 0))
        except Exception as e:
            print("memory partially unreadable: %s" % e, file=sys.stderr)

    def trim(self):
        if len(self.elem) > 4000:
            self.elem = dict(sorted(self.elem.items(),
                                    key=lambda kv: kv[1].get("tries", 0))[-4000:])
        if len(self.sel_fails) > 2000:
            self.sel_fails = dict(sorted(self.sel_fails.items(),
                                         key=lambda kv: kv[1])[-2000:])
        if len(self.wins) > 300:
            self.wins = dict(sorted(self.wins.items(),
                                    key=lambda kv: kv[1].get("n", 0))[-300:])

    def save(self):
        try:
            self.trim()
            data = {"version": 2, "elem": self.elem, "wins": self.wins,
                    "sel_fails": self.sel_fails, "filled": sorted(self.filled),
                    "nav_map": self.nav_map, "scene_aff": self.scene_aff,
                    "econ": self.econ, "cycles": self.cycles,
                    "sessions": self.sessions}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print("memory save failed: %s" % e, file=sys.stderr)

# ═════════════ 9. AGENT (probe -> reason -> execute -> reflect loop) ═════════

class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = build_logger(cfg)
        self.memory = Memory(Path(cfg.memory_path))
        self.memory.load()
        self.econ = EconomyTracker(self.log)
        self.econ.load(self.memory.econ)
        self.drives = Drives()
        self.reasoner = Reasoner(cfg, self.log)
        self.reflector = Reflector(self.log)
        self.playground = Playground(cfg, self.log)
        self.subgoals = [s.strip() for s in cfg.goal.split(";") if s.strip()] \
            or ["enter the game"]
        self.idx = 0
        self.mode = "pipeline"          # pipeline -> economy
        self.stuck = {}
        self.history = deque(maxlen=14)
        self.cycle = 0
        self.stop = {"flag": False}
        self.page = None
        self.context = None
        self._console_errors = 0

    async def run(self):
        cfg = self.cfg
        self.memory.sessions += 1
        self.log.info("boot | url=%s | session #%d | lifetime cycles=%d",
                      cfg.url, self.memory.sessions, self.memory.cycles)
        self.log.info("startup pipeline: %s", " -> ".join(self.subgoals))
        self.log.info("economy memory: %d signals | currency=%r | %d recipes | "
                      "money loop %s", len(self.econ.signals),
                      self.econ.currency or "?", len(self.econ.recipes),
                      "ON" if self.econ.loop else "off")
        if cfg.fields:
            self.log.info("field values for keys: %s (values never logged)",
                          ", ".join(sorted(cfg.fields)))
        async with async_playwright() as pw:
            browser = None
            common = dict(headless=cfg.headless, args=_chromium_args(cfg),
                          slow_mo=cfg.slowmo)
            if cfg.proxy:
                common["proxy"] = {"server": cfg.proxy}
            try:
                if cfg.user_data_dir:
                    self.context = await pw.chromium.launch_persistent_context(
                        cfg.user_data_dir,
                        viewport={"width": cfg.viewport_w,
                                  "height": cfg.viewport_h},
                        ignore_https_errors=True, **common)
                else:
                    browser = await pw.chromium.launch(**common)
                    self.context = await browser.new_context(
                        viewport={"width": cfg.viewport_w,
                                  "height": cfg.viewport_h},
                        ignore_https_errors=True, user_agent=cfg.user_agent)
            except Exception as e:
                self.log.error("browser launch failed: %s", e)
                return
            await self.context.add_init_script(JS_STEALTH)
            self.page = (self.context.pages[0] if self.context.pages
                         else await self.context.new_page())
            self.page.set_default_timeout(15000)
            self._attach(self.page)
            if cfg.popup_help:
                self.context.on("page", self._spawn_popup_help)
            if not await self._goto():
                return
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, self.stop.update, {"flag": True})
                except (NotImplementedError, RuntimeError):
                    pass
            t0 = time.time()
            try:
                await self._loop(t0)
            finally:
                self._save()
                self.log.info("memory persisted: %d signals, currency=%r, "
                              "%d recipes, %d element stats",
                              len(self.econ.signals), self.econ.currency or "?",
                              len(self.econ.recipes), len(self.memory.elem))
                try:
                    await self.context.close()
                    if browser:
                        await browser.close()
                except Exception:
                    pass
        self.log.info("playground shut down cleanly")

    async def _loop(self, t0):
        cfg = self.cfg
        challenge_fails = 0
        while not self.stop["flag"]:
            if cfg.max_cycles and self.cycle >= cfg.max_cycles:
                self.log.info("cycle budget reached"); break
            if cfg.max_minutes and (time.time() - t0) / 60.0 >= cfg.max_minutes:
                self.log.info("time budget reached"); break
            if Path(cfg.stop_file).exists():
                self.log.info("stop-file detected"); break
            if self.page.is_closed():
                await self._recover(); continue

            pre = await self.probe()
            self.econ.observe(pre, self.memory)
            if cfg.dump_states:
                self._dump_state(pre)
            self._log_probe(pre)

            subgoal = self._subgoal()
            stuck_cur = self.stuck.get(subgoal, 0)
            if self.mode == "pipeline":
                self.log.info("CYCLE  | #%d | pipeline[%d/%d]: %r", self.cycle,
                              min(self.idx + 1, len(self.subgoals)),
                              len(self.subgoals), subgoal)
            else:
                if self.cycle % 5 == 0:
                    self._econ_status()
            prev_state = self.econ.state
            plan = self.reasoner.think(pre, self.history, self.memory,
                                      self.econ, self.drives, stuck_cur,
                                      self.mode, subgoal)
            if self.mode == "economy" and self.econ.state != prev_state:
                self.log.info("ECON   | state %s -> %s", prev_state,
                              self.econ.state)
            self.log.info("REASON | plan=%s | target=%r | match=%.2f",
                          plan.name, plan.desc, plan.match)
            self._emit_code(plan)

            self.playground.bind(self.page, self.context)
            res = await self.playground.run(plan)
            self.log.info("EXECUTE| %s | how=%s | %.1fs",
                          "ok" if res.ok else "FAIL", res.how, res.duration)

            post = await self.probe()
            self.econ.observe(post, self.memory)
            refl = self.reflector.reflect(pre, plan, res, post, self.econ,
                                          self.drives, self.memory,
                                          self.mode, subgoal)
            self.history.append({"cycle": self.cycle, "plan": plan.name,
                                 "how": res.how, "outcome": refl.outcome,
                                 "error_sig": refl.error_sig,
                                 "target_fp": plan.ctx.get("target_fp", ""),
                                 "activity": plan.ctx.get("activity", ""),
                                 "used_win": plan.name == "reuse-learned"})
            d = refl.delta
            self.log.info(
                "REFLECT| %s | satisfied=%s | +%s/-%s | gained %s | bank %s | "
                "+%d/-%d controls%s%s",
                refl.outcome, "yes" if refl.satisfied else "no",
                _dstr(d.increases), _dstr(d.decreases), _dstr(d.gained),
                ("%+g" % d.currency_delta) if d.currency_delta else "-",
                refl.added, refl.removed,
                (" | " + refl.error_sig) if refl.error_sig else "",
                (" | nav->%s" % post.get("scene_tag"))
                if refl.outcome == "nav" else "")

            if pre["flags"]["cloudflare"] and post["flags"]["cloudflare"]:
                challenge_fails += 1
                if challenge_fails >= 3:
                    self.log.error("challenge never cleared; try --headed")
                    break
            else:
                challenge_fails = 0

            if self.mode == "pipeline":
                if refl.satisfied:
                    self.stuck[subgoal] = 0
                    self.idx += 1
                    self.log.info("GOAL   | %r satisfied", subgoal)
                    if self.idx >= len(self.subgoals):
                        if cfg.after_goal == "exit":
                            self.log.info("PIPELINE COMPLETE after %d cycles",
                                         self.cycle)
                            break
                        self.mode = "economy"
                        self.log.info("PIPELINE COMPLETE | entering ECONOMY "
                                      "mode: gather -> craft -> sell, forever")
                else:
                    self.stuck[subgoal] = self.stuck.get(subgoal, 0) + 1
                    if self.stuck[subgoal] >= 8:
                        self.log.warning("GOAL   | %r is stuck; skipping", subgoal)
                        self.stuck[subgoal] = 0
                        self.idx += 1
                        if self.idx >= len(self.subgoals):
                            if cfg.after_goal == "exit":
                                self.log.info("pipeline finished (with skips)")
                                break
                            self.mode = "economy"
                            self.log.info("entering ECONOMY mode")
            else:
                act = plan.ctx.get("activity") or self.econ.state
                if plan.name not in ("wait-cooldown", "idle-observe",
                                     "shake-viewport"):
                    if refl.satisfied:
                        self.stuck[act] = 0
                    else:
                        self.stuck[act] = self.stuck.get(act, 0) + 1

            self.memory.cycles += 1
            if self.cycle % 10 == 0:
                self._save()
            await self._pace(plan)

    def _subgoal(self):
        if self.mode == "economy":
            return self.econ.state
        return self.subgoals[min(self.idx, len(self.subgoals) - 1)]

    async def probe(self) -> dict:
        self.cycle += 1
        try:
            state = await probe_page(self.page, self.cycle)
        except Exception as e:
            self.log.warning("PROBE  | failed: %s", str(e)[:100])
            state = {"cycle": self.cycle, "url": "", "title": "", "frames": [],
                     "elements": [], "inputs": [],
                     "flags": {"cloudflare": False, "turnstile": False,
                               "wallet": False, "canvas": False,
                               "password": False},
                     "text": "", "fingerprint": "", "scene_tag": "loading"}
        state["flags"]["console_errors"] = self._console_errors
        self._console_errors = 0
        return state

    def _log_probe(self, state):
        f = state["flags"]
        try:
            u = urlsplit(state.get("url") or "")
            short = (u.netloc + u.path) or "?"
        except Exception:
            short = "?"
        self.log.info("PROBE  | c%d %s [%s] | controls=%d inputs=%d cf=%s",
                      state["cycle"], short, state.get("scene_tag", "?"),
                      len(state["elements"]), len(state["inputs"]),
                      "yes" if f["cloudflare"] else "no")

    def _econ_status(self):
        e, dr = self.econ, self.drives
        res = e.resources()
        res_s = ", ".join("%s %s" % (l, _n(v))
                          for l, v in sorted(res.items(),
                                            key=lambda kv: -(kv[1] or 0))[:6]) \
            or "none"
        it = e.items()
        it_s = ", ".join("%s x%s" % (l, _n(v))
                         for l, v in list(it.items())[:6]) if it else "none"
        bank = ("%s %s" % (e.currency, _n((e.signals.get(e.currency) or {})
                                          .get("last")))) \
            if e.currency else "unknown"
        loop = ""
        if e.loop:
            loop = " | loop %d/%d" % (e.loop_i + 1, len(e.loop))
        self.log.info("ECON   | %s (age %d) | bank: %s | res: %s | items: %s |"
                      " cur %.2f bor %.2f ava %.2f%s",
                      e.state, e.state_age, bank, res_s, it_s,
                      dr.curiosity, dr.boredom, dr.avarice, loop)

    def _emit_code(self, plan):
        if self.cfg.code_log == "off":
            return
        n = plan.code.count("\n") + 1
        if self.cfg.code_log == "both":
            self.log.info("CODE   | %d lines of generated Playwright:\n%s"
                          "\n----- end code", n, plan.code)
        else:
            self.log.debug("CODE   | %d lines:\n%s\n----- end code", n, plan.code)

    def _dump_state(self, state):
        try:
            d = Path("states")
            d.mkdir(exist_ok=True)
            (d / ("state_%05d.json" % self.cycle)).write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
            for old in sorted(d.glob("state_*.json"))[:-200]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    async def _pace(self, plan):
        if plan.name.startswith(("act", "nav", "overlay", "dismiss", "explore",
                                 "reuse", "fill", "click")):
            await asyncio.sleep(random.uniform(0.8, 1.8))
        else:
            await asyncio.sleep(0.4)

    def _save(self):
        try:
            self.memory.econ = self.econ.to_dict()
        except Exception:
            pass
        self.memory.save()

    async def _goto(self):
        for attempt in range(3):
            try:
                await self.page.goto(self.cfg.url,
                                     wait_until="domcontentloaded",
                                     timeout=60000)
                try:
                    await self.page.wait_for_load_state("networkidle",
                                                        timeout=8000)
                except Exception:
                    pass
                return True
            except Exception as e:
                self.log.warning("navigation failed (%d/3): %s",
                                 attempt + 1, str(e)[:120])
                await asyncio.sleep(2.0 * (attempt + 1))
        self.log.error("cannot reach %s", self.cfg.url)
        return False

    async def _recover(self):
        self.log.warning("page closed; reopening")
        try:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(15000)
            self._attach(self.page)
            await self._goto()
        except Exception as e:
            self.log.error("recovery failed: %s", e)
            await asyncio.sleep(2.0)

    def _attach(self, page):
        page.on("dialog", self._on_dialog)
        page.on("console", self._on_console)

    async def _on_dialog(self, dialog):
        msg = dialog.message or ""
        try:
            if dialog.type == "beforeunload":
                await dialog.dismiss()
                return
            if RISKY_RE.search(msg) and not self.cfg.allow_signing:
                self.log.warning("DIALOG | denied (fund-safety): %r", msg[:80])
                await dialog.dismiss()
            else:
                self.log.info("DIALOG | accepted: %r", msg[:80])
                await dialog.accept()
        except Exception:
            pass

    def _on_console(self, m):
        try:
            if m.type == "error":
                self._console_errors += 1
        except Exception:
            pass

    def _spawn_popup_help(self, p):
        try:
            asyncio.get_running_loop().create_task(self._help_popup(p))
        except Exception:
            pass

    async def _help_popup(self, p):
        try:
            await p.wait_for_load_state("domcontentloaded", timeout=8000)
            await asyncio.sleep(1.0)
            for name in ("Connect", "Next", "Unlock"):
                loc = p.get_by_role("button", name=name).first
                if await loc.count():
                    await loc.click(timeout=2500)
                    self.log.info("POPUP  | clicked %r", name)
                    return
        except Exception:
            pass

# ═══════════════════════════════ 10. MAIN ═══════════════════════════════════

def main():
    cfg = parse_args()
    if cfg.seed >= 0:
        random.seed(cfg.seed)
    agent = Agent(cfg)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\ninterrupted -- memory was saved on shutdown")

if __name__ == "__main__":
    main()
