#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
living_agent.py -- a "living", fully autonomous Gamer AI for Web3 browser games.
"""

from __future__ import annotations

import argparse
import asyncio
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
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import (Dialog, Page, async_playwright,
                                  TimeoutError as PWTimeout)

__version__ = "1.0.0"

STOP_FILE = ".living_agent_stop"

# ═══════════════════════════════ 1. CONFIG & CLI ═══════════════════════════════

DIM = 384          # dimension of the local semantic space
OPTIMISM = 0.30    # optimistic prior for never-tried actions (curiosity fuel)
UCB_C = 0.35       # exploration bonus coefficient
NOVELTY_W = 0.45   # weight of per-element novelty in utility

# YOUR LOGIN CREDENTIALS
EMAIL = "karimamri581@gmail.com"
PASSWORD = "Taetae1997*"

@dataclass
class Config:
    url: str = ""
    headless: bool = True
    memory_path: str = "agent_memory.json"
    log_path: str = "living_agent.log"
    log_level: str = "INFO"
    max_minutes: float = 0.0
    cycle_idle: float = 3.0
    cycle_wait_cap: float = 25.0
    user_data_dir: str = ""
    load_extension: str = ""
    slowmo: float = 0.0
    user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    viewport_w: int = 1440
    viewport_h: int = 900
    wealth_label: str = ""
    allow_risky: set = field(default_factory=set)
    allow_signing: bool = False
    auto_connect_wallet: bool = True
    avoid_social: bool = True
    same_domain_only: bool = True
    seed: Optional[int] = None

def parse_args() -> Config:
    ap = argparse.ArgumentParser(
        description="Living Gamer AI Agent -- fully local cognition, no external APIs.")
    ap.add_argument("url", nargs="?", default=os.environ.get("GAME_URL", ""),
                    help="game URL (or set $GAME_URL)")
    ap.add_argument("--headed", action="store_true", help="watch it live")
    ap.add_argument("--memory", default="agent_memory.json")
    ap.add_argument("--log-file", default="living_agent.log")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--max-minutes", type=float, default=0.0, help="0 = forever")
    ap.add_argument("--user-data-dir", default="")
    ap.add_argument("--load-extension", default="")
    ap.add_argument("--slowmo", type=float, default=0.0)
    ap.add_argument("--viewport", default="1440x900")
    ap.add_argument("--wealth-label", default="")
    ap.add_argument("--allow-risky", default="")
    ap.add_argument("--allow-signing", action="store_true")
    ap.add_argument("--no-auto-connect", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()

    try:
        w, h = a.viewport.lower().split("x")
        vw, vh = int(w), int(h)
    except Exception:
        ap.error("--viewport must look like 1440x900")

    cfg = Config(
        url=a.url.strip(), headless=not a.headed, memory_path=a.memory,
        log_path=a.log_file, log_level=a.log_level, max_minutes=a.max_minutes,
        user_data_dir=a.user_data_dir.strip(), load_extension=a.load_extension.strip(),
        slowmo=a.slowmo, viewport_w=vw, viewport_h=vh,
        wealth_label=a.wealth_label.strip(),
        allow_risky={t.strip().lower() for t in a.allow_risky.split(",") if t.strip()},
        allow_signing=a.allow_signing, auto_connect_wallet=not a.no_auto_connect,
        seed=a.seed)
    if not cfg.url.startswith(("http://", "https://")):
        ap.error("a game URL is required (positional arg or $GAME_URL)")
    return cfg

def build_logger(cfg: Config) -> logging.Logger:
    log = logging.getLogger("living-agent")
    log.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    if log.handlers:
        return log
    con = logging.StreamHandler(sys.stdout)
    con.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
    fh = RotatingFileHandler(cfg.log_path, maxBytes=3_000_000, backupCount=3,
                             encoding="utf-8")
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

# ═════════════════════ 2. LOCAL SEMANTIC SPACE ═════════════════════

def _fnv1a(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("utf-8", "ignore"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def embed(text: str) -> list:
    v = [0.0] * DIM
    words = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
    t = " " + " ".join(words) + " " if words else " nil "
    for i in range(len(t) - 2):
        v[_fnv1a(t[i:i + 3]) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]

def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))

BASE_CONCEPTS = {
    "confirm":  ["ok", "okay", "confirm", "yes", "continue", "proceed", "got it",
                 "start", "play", "enter", "begin", "accept"],
    "close":    ["close", "cancel", "dismiss", "back", "no thanks", "later",
                 "exit", "skip"],
    "harvest":  ["harvest", "collect", "claim", "reap", "gather", "pick", "mine",
                 "earn", "loot", "reward", "daily"],
    "plant":    ["plant", "sow", "seed", "grow", "water", "feed", "cultivate"],
    "buy":      ["buy", "purchase", "shop", "store", "market", "hire", "recruit",
                 "upgrade", "level up", "improve", "boost", "craft"],
    "sell":     ["sell", "trade", "exchange", "list", "marketplace"],
    "stake":    ["stake", "deposit", "invest", "lock", "unstake", "vault"],
    "battle":   ["battle", "fight", "attack", "raid", "duel", "arena", "quest",
                 "mission", "adventure", "explore", "boss", "enemy"],
    "navigate": ["map", "world", "travel", "home", "dashboard", "garden",
                 "village", "inventory", "menu", "go to", "next", "island"],
    "wallet":   ["connect wallet", "connect", "wallet", "sign in", "login",
                 "log in", "metamask", "phantom", "select wallet"],
    "risky":    ["approve", "transfer", "withdraw", "burn", "spend", "pay",
                 "confirm transaction", "pay with"],
    "energy":   ["energy", "stamina", "fuel", "power", "hp", "food", "water"],
    "social":   ["share", "follow", "twitter", "discord", "telegram", "invite",
                 "refer", "friend", "community"],
    "settings": ["settings", "options", "sound", "music", "profile", "account"],
}

RISKY_RE = re.compile(
    r"\b(approve|transfer|withdraw|burn|spend|pay|swap|mint|send"
    r"|sign(?!\s*(in|up|ed)))\b", re.I)

GOOD_WORDS = ("success", "claimed", "harvest", "earned", "completed", "congrat",
              "received", "reward", "won", "purchased", "level up", "well done")
BAD_WORDS = ("error", "failed", "not enough", "insufficient", "try again",
             "cannot", "can't", "already", "denied", "rejected", "expired",
             "oops", "missing", "need more")

CURRENCY_WORDS = ("coin", "gold", "gem", "cash", "token", "sol", "eth", "bnb",
                  "matic", "usd", "usdc", "money", "credit", "diamond", "wood",
                  "steel", "crop", "balance", "bank", "$", "xp", "points")

class SemanticKernel:
    def __init__(self, learned: Optional[dict] = None):
        self._anchors = {}
        for name, phrases in BASE_CONCEPTS.items():
            self._anchors[name] = (self._phrase_vec(phrases), list(phrases))
        for name, phrases in (learned or {}).items():
            self._anchors[name] = (self._phrase_vec(list(phrases)), list(phrases))
        self._cache: dict = {}

    def _phrase_vec(self, phrases) -> list:
        acc = [0.0] * DIM
        for p in phrases:
            for i, x in enumerate(embed(p)):
                acc[i] += x
        n = math.sqrt(sum(v * v for v in acc)) or 1.0
        return [v / n for v in acc]

    def classify(self, text: str):
        t = re.sub(r"\s+", " ", (text or "").strip().lower())
        if not t:
            return "", 0.0
        cached = self._cache.get(t)
        if cached is not None:
            return cached
        v = embed(t)
        best, bs = "", 0.0
        for name, (av, _) in self._anchors.items():
            c = _dot(v, av)
            if c > bs:
                bs, best = c, name
        for name, (_, phrases) in self._anchors.items():
            if any(len(p) >= 2 and
                   re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", t)
                   for p in phrases):
                if 0.66 > bs:
                    bs, best = 0.66, name
                break
        res = (best, round(bs, 3)) if bs >= 0.34 else ("", round(bs, 3))
        if len(self._cache) < 6000:
            self._cache[t] = res
        return res

    def add_learned(self, name: str, phrases: list):
        self._anchors[name] = (self._phrase_vec(list(phrases)), list(phrases))
        self._cache.clear()

# ═════════════════════ 3. SCENE MEMORY & SIGNAL TRACKING ═════════════════════

SCENE_ANCHORS = {
    "shop":    "shop store market merchant buy purchase upgrade craft trade",
    "battle":  "battle arena fight raid combat boss quest mission enemy war troops",
    "farm":    "farm garden plant crop harvest field plot seed water grow",
    "menu":    "menu main home dashboard start play lobby select chapters",
    "gate":    "connect wallet sign login register to start your wallet",
    "loading": "loading initializing connecting please wait progress",
}
_SCENE_VECS = {tag: embed(words) for tag, words in SCENE_ANCHORS.items()}

def classify_tag(text: str) -> str:
    v = embed(text)
    best, bc = "hub", 0.0
    for tag, av in _SCENE_VECS.items():
        c = _dot(v, av)
        if c > bc:
            bc, best = c, tag
    return best if bc >= 0.20 else "hub"

class SceneMemory:
    def __init__(self, store: dict):
        self.store = store

    def resolve(self, gist: str, title: str, is_modal: bool):
        text = (title + " " + gist).strip() or "blank"
        v = embed(text)
        best_id, best_c = None, 0.0
        for sid, rec in self.store.items():
            c = _dot(v, rec.get("vec", []))
            if c > best_c:
                best_c, best_id = c, sid
        if best_id is None or best_c < 0.72:
            sid = "s%d" % (len(self.store) + 1)
            tag = "modal" if is_modal else classify_tag(text)
            self.store[sid] = {"vec": v, "tag": tag, "visits": 1}
            return sid, tag, True
        rec = self.store[best_id]
        rec["visits"] = int(rec.get("visits", 0)) + 1
        blend = [0.92 * a + 0.08 * b for a, b in zip(rec["vec"], v)]
        n = math.sqrt(sum(x * x for x in blend)) or 1.0
        rec["vec"] = [x / n for x in blend]
        return best_id, ("modal" if is_modal else rec.get("tag", "hub")), False

NUM_RE = re.compile(
    r"(?<![\w#.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*([KkMmBb])?(?![\w])")
WORD_RE = re.compile(r"[a-z$€¥₩₿◎Ξ]{2,16}")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}

def _num_value(m) -> Optional[float]:
    try:
        val = float(m.group(1).replace(",", "") +
                    ("." + m.group(2) if m.group(2) else ""))
    except ValueError:
        return None
    return val * _MULT.get((m.group(3) or "").lower(), 1.0)

def extract_signals(text: str) -> dict:
    out = {}
    for m in NUM_RE.finditer(text):
        val = _num_value(m)
        if val is None:
            continue
        pre = text[max(0, m.start() - 40):m.start()].lower()
        post = text[m.end():m.end() + 26].lower()
        words = WORD_RE.findall(pre)
        label = (" ".join(words[-2:]) if words
                 else "_ " + " ".join(WORD_RE.findall(post)[:2]))
        label = (label or "anon").strip()[:40]
        if label not in out or abs(val) > abs(out[label]):
            out[label] = val
        if len(out) >= 48:
            break
    return out

def classify_sentiment(texts):
    score = 0.0
    for t in texts:
        tl = t.lower()
        good = any(w in tl for w in GOOD_WORDS) or bool(re.search(r"\+\s*\d", t))
        bad = any(w in tl for w in BAD_WORDS) or bool(re.search(r"-\s?\d", t))
        if bad:
            score -= 0.35
        elif good:
            score += 0.22
    return max(-0.8, min(0.6, score)), list(texts)

def toast_gain(texts) -> Optional[float]:
    for t in texts:
        m = re.search(r"\+\s*([\d][\d,]*(?:\.\d+)?)", t)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None

class RunningScale:
    def __init__(self):
        self.scale = 30.0

    def update(self, d: float):
        a = abs(d)
        if a > self.scale:
            self.scale = a * 1.1
        else:
            self.scale = max(8.0, self.scale * 0.999)

class SignalTracker:
    def __init__(self, memory: "Memory"):
        self.memory = memory
        self.history: dict = {}
        self.assoc: dict = {}

    @property
    def wealth_label(self) -> str:
        return self.memory.wealth_label

    @wealth_label.setter
    def wealth_label(self, v: str):
        self.memory.wealth_label = v

    def update(self, signals: dict, cycle: int):
        for lab, val in signals.items():
            h = self.history.get(lab)
            if h is None:
                h = self.history[lab] = deque(maxlen=90)
            h.append((cycle, val))

    def bump_assoc(self, pre: dict, post: dict):
        for lab, val in post.items():
            old = pre.get(lab)
            if old is not None and val > old + 1e-9:
                self.assoc[lab] = min(6.0, self.assoc.get(lab, 0.0) + 0.25)

    def elect(self) -> str:
        best, bs = "", -1e18
        for lab, h in self.history.items():
            if len(h) < 8:
                continue
            vals = [v for _, v in h]
            lo, hi = min(vals), max(vals)
            mag = max(abs(lo), abs(hi), 1e-9)
            spread = (hi - lo) / mag
            growth = (vals[-1] - vals[0]) / mag
            presence = len(h) / 90.0
            assoc = min(4.0, self.assoc.get(lab, 0.0))
            hint = 1.0 if any(w in lab for w in CURRENCY_WORDS) else 0.0
            s = (2.0 * presence + 3.0 * assoc + 1.5 * max(0.0, growth)
                 + 0.8 * spread + 0.6 * hint)
            if s > bs:
                bs, best = s, lab
        return best

# ═══════════════════════════════ 4. DATA STRUCTURES ═════════════════════════════

@dataclass
class Element:
    tag: str
    role: str
    text: str
    aria: str
    title: str
    testid: str
    cls: str
    href: str
    disabled: bool
    x: int
    y: int
    w: int
    h: int
    in_modal: bool
    frame_url: str
    fp: str = ""
    concept: str = ""
    conf: float = 0.0
    thash: str = ""
    visits: int = 0

    def label(self) -> str:
        for s in (self.text, self.aria, self.title, self.testid, self.cls):
            if s and s.strip():
                return s.strip()[:32]
        return "icon"

    def center(self):
        return self.x + self.w / 2.0, self.y + self.h / 2.0

@dataclass
class Perception:
    cycle: int
    url: str
    title: str
    gist: str
    scene_id: str
    scene_tag: str
    scene_new: bool
    elements: list = field(default_factory=list)
    modal_elements: list = field(default_factory=list)
    toasts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    wealth: Optional[tuple] = None
    has_canvas: bool = False
    new_fps: list = field(default_factory=list)
    ripe_fps: list = field(default_factory=list)

@dataclass
class Drives:
    curiosity: float = 0.80
    avarice: float = 0.50
    boredom: float = 0.15
    caution: float = 0.10

@dataclass
class Goal:
    name: str
    focus: str
    priority: float
    ttl: int = 8

@dataclass
class Action:
    kind: str
    element: Optional[Element] = None
    seconds: float = 2.0
    reason: str = ""
    executed: bool = False

# ═══════════════════════════════ 5. PERSISTENT MEMORY ═══════════════════════════

class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.q: dict = {}
        self.elements: dict = {}
        self.learned: dict = {}
        self.macros: dict = {}
        self.scenes: dict = {}
        self.wealth_history: list = []
        self.total_actions = 0
        self.discoveries = 0
        self.sessions = 0
        self.wealth_label = ""

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
            self.q = {str(k): [int(v[0]), float(v[1])]
                      for k, v in (raw.get("q") or {}).items()
                      if isinstance(v, list) and len(v) == 2}
            self.elements = raw.get("elements") or {}
            self.learned = {str(k): [str(x) for x in v]
                             for k, v in (raw.get("learned") or {}).items()}
            self.macros = {str(k): [int(v[0]), float(v[1])]
                           for k, v in (raw.get("macros") or {}).items()
                           if isinstance(v, list) and len(v) == 2}
            self.scenes = raw.get("scenes") or {}
            self.wealth_history = [list(x) for x in raw.get("wealth_history", [])
                                   if isinstance(x, list) and len(x) == 2]
            self.total_actions = int(raw.get("total_actions", 0))
            self.discoveries = int(raw.get("discoveries", 0))
            self.sessions = int(raw.get("sessions", 0))
            self.wealth_label = str(raw.get("wealth_label", ""))
        except Exception as e:
            print("memory partially unreadable: %s" % e, file=sys.stderr)

    def trim(self):
        if len(self.q) > 4000:
            self.q = dict(sorted(self.q.items(), key=lambda kv: kv[1][0])[-4000:])
        if len(self.elements) > 3000:
            self.elements = dict(sorted(self.elements.items(),
                                       key=lambda kv: kv[1].get("last_click", 0))[-3000:])
        if len(self.macros) > 800:
            self.macros = dict(sorted(self.macros.items(),
                                      key=lambda kv: kv[1][0])[-800:])
        if len(self.scenes) > 24:
            self.scenes = dict(sorted(self.scenes.items(),
                                      key=lambda kv: kv[1].get("visits", 0))[-24:])
        del self.wealth_history[:-600]

    def save(self):
        try:
            self.trim()
            data = {"version": 1, "q": self.q, "elements": self.elements,
                    "learned": self.learned, "macros": self.macros,
                    "scenes": self.scenes, "wealth_history": self.wealth_history,
                    "total_actions": self.total_actions,
                    "discoveries": self.discoveries, "sessions": self.sessions,
                    "wealth_label": self.wealth_label}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print("memory save failed: %s" % e, file=sys.stderr)

# ═══════════════════════════════ 6. BROWSER-SIDE PROBES ════════════════════════

JS_INIT = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  window.__agentEvents = [];
  const remember = (n) => {
    try {
      const t = ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
      if (t && t.length <= 200) {
        window.__agentEvents.push({t: t.slice(0, 160), ts: Date.now()});
        if (window.__agentEvents.length > 120)
          window.__agentEvents.splice(0, window.__agentEvents.length - 120);
      }
    } catch (e) {}
  };
  try {
    new MutationObserver((muts) => {
      for (const m of muts) for (const n of m.addedNodes) if (n && n.nodeType === 1) remember(n);
    }).observe(document.documentElement, {childList: true, subtree: true});
  } catch (e) {}
})();
"""

JS_CENSUS = r"""() => {
  const SEL = 'button, a, [role="button"], [role="tab"], [role="menuitem"], [role="option"], [onclick], input[type="button"], input[type="submit"], [class*="btn" i], [class*="button" i], [class*="cta" i]';
  const out = {elements: [], toasts: [], events: [], meta: {}};
  const seen = new Set();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none'
        || parseFloat(s.opacity || '1') < 0.05) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  };
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el) || out.elements.length >= 350) continue;
    seen.add(el);
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    let cls = '';
    try { cls = (typeof el.className === 'string' ? el.className : '')
                 .trim().split(/\s+/).slice(0, 6).join(' '); } catch (e) {}
    let txt = '';
    try { txt = (el.innerText || el.textContent || '')
                 .replace(/\s+/g, ' ').trim(); } catch (e) {}
    const aria = el.getAttribute('aria-label') || '';
    if (!txt && aria) txt = aria;
    out.elements.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: txt.slice(0, 140),
      aria: aria,
      title: el.getAttribute('title') || '',
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id')
              || el.getAttribute('data-qa') || el.getAttribute('data-test') || '',
      cls: cls,
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'
              || /(^|[\s_-])(disabled|is-disabled)([\s_-]|$)/i.test(cls)),
      href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '',
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      in_modal: !!el.closest('[role="dialog"], [class*="modal" i], [class*="overlay" i], [class*="popup" i], [class*="drawer" i]')
    });
  }
  for (const el of document.querySelectorAll('[role="alert"], [role="status"], [class*="toast" i], [class*="notif" i], [class*="snack" i]')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t) out.toasts.push(t.slice(0, 160));
  }
  const heads = [];
  for (const el of document.querySelectorAll('h1, h2, h3')) {
    const t = (el.innerText || '').trim();
    if (t) heads.push(t.slice(0, 60));
  }
  out.meta = {
    title: (document.title || '').slice(0, 120),
    headings: heads.slice(0, 6),
    has_canvas: !!document.querySelector('canvas'),
    body_text: ((document.body && document.body.innerText) || '')
                .replace(/\s+/g, ' ').slice(0, 20000)
  };
  if (Array.isArray(window.__agentEvents)) out.events = window.__agentEvents.splice(0, 40);
  return out;
}"""

JS_CLICK = r"""(spec) => {
  const SEL = 'button, a, [role="button"], [onclick], [class*="btn" i]';
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const tTxt = norm(spec.text), tAria = norm(spec.aria), tTest = norm(spec.testid);
  let best = null, bestScore = 0.5;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    let s = 0;
    if (tTest && norm(el.getAttribute('data-testid')) === tTest) s += 4;
    const tx = norm(el.innerText || el.textContent), a = norm(el.getAttribute('aria-label'));
    if (tTxt && tx === tTxt) s += 3;
    else if (tTxt && tTxt.length > 2 && tx.includes(tTxt)) s += 2;
    if (tAria && (a === tAria || a.includes(tAria))) s += 2;
    const d = Math.hypot((r.x + r.width / 2) - spec.cx, (r.y + r.height / 2) - spec.cy);
    if (d < 40) s += 2; else s -= d / 250;
    if (s > bestScore) { bestScore = s; best = el; }
  }
  if (best) { try { best.scrollIntoView({block: 'center'}); } catch (e) {} best.click(); return true; }
  return false;
}"""

# ═══════════════════════════ 7. THE COGNITIVE CORE ═════════════════════════════

class CognitiveCore:
    def __init__(self, page: Page, context, cfg: Config, memory: Memory,
                 kernel: SemanticKernel, log: logging.Logger, stop: dict):
        self.page, self.context = page, context
        self.cfg, self.memory, self.kernel, self.log, self.stop = \
            cfg, memory, kernel, log, stop
        self.scenes = SceneMemory(memory.scenes)
        self.signals = SignalTracker(memory)
        self.drives = Drives()
        self.cycle = 0
        self.goal: Optional[Goal] = None
        self.last_click_concept = ""
        self._prev_disabled: dict = {}
        self._dialog_events = deque(maxlen=10)
        self._last_reload = 0.0
        self._last_scroll = 0.0
        self._last_wallet_try = 0.0
        self._wallet_connected = False
        self._gate_cycles = 0
        self.error_streak = 0
        self._console_errors = 0
        self._domain = (urlsplit(cfg.url).hostname or "") if cfg.url else ""

    async def run(self):
        cfg = self.cfg
        self.log.info(
            "boot | game=%s | session #%d | prior_actions=%d | "
            "forged_concepts=%d | known_scenes=%d",
            cfg.url, self.memory.sessions, self.memory.total_actions,
            len(self.memory.learned), len(self.memory.scenes))
        try:
            await self.page.goto(cfg.url, wait_until="domcontentloaded",
                                 timeout=60000)
        except Exception as e:
            self.log.error("initial navigation failed: %s", e)
            return
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        self._attach_page(self.page)
        self.context.on("page", self._on_popup)

        per = await self.perceive()
        self.log.info("awake | scene %s[%s] | %d controls | %d numeric signals | "
                      "wealth signal=%r", per.scene_id, per.scene_tag,
                      len(per.elements), len(per.signals),
                      per.wealth[0] if per.wealth else None)

        t0 = time.time()
        while not self.stop["flag"]:
            try:
                if cfg.max_minutes and (time.time() - t0) / 60.0 >= cfg.max_minutes:
                    self.log.info("time budget reached")
                    break
                if Path(STOP_FILE).exists():
                    self.log.info("stop-file detected")
                    break
                if self.page.is_closed():
                    await self._recover()
                    per = await self.perceive()
                    continue

                # INJECTED LOGIN LOGIC
                if await self.auto_login():
                    per = await self.perceive()
                    continue

                action = self.think(per)
                act_start = time.time()
                await self.act(action)
                per = await self.reflect(per, action, act_start)

                if self.cycle % 25 == 0:
                    self.memory.save()
                if self.cycle % 100 == 0:
                    self._stats()
                self.error_streak = 0
                await asyncio.sleep(self._cycle_delay(action))
            except Exception:
                self.error_streak += 1
                self.log.error("cycle %d crashed:\n%s", self.cycle,
                               traceback.format_exc()[-900:])
                if self.error_streak in (4, 8, 14):
                    await self._recover()
                await asyncio.sleep(1.5)
        self.log.info("going dormant after %d cycles", self.cycle)

    async def auto_login(self):
        """Instantly logs in using hardcoded credentials. Opens the wallet modal first if needed."""
        try:
            # 1. Check if we are already seeing the Email input directly
            email_input = self.page.locator("input[type='email'], input[name='email']").first
            if await email_input.is_visible():
                self.log.info("Login modal visible. Typing credentials...")
                await email_input.fill(EMAIL)
                await asyncio.sleep(0.5)
                
                pass_input = self.page.locator("input[type='password'], input[name='password']").first
                await pass_input.fill(PASSWORD)
                await asyncio.sleep(0.5)
                
                await pass_input.press("Enter")
                self.log.info(f"[✅ SUCCESS] Logged in as {EMAIL}.")
                await asyncio.sleep(5) # Wait for game to load
                return True

            # 2. If not, look for an "Email" button to click (sometimes it's hidden in the modal)
            email_btn = self.page.locator("text=/Email/i").first
            try:
                if await email_btn.is_visible():
                    self.log.info("Found 'Email' login option. Clicking it...")
                    await email_btn.click()
                    await asyncio.sleep(2)
                    
                    # Now try to fill the inputs
                    email_input = self.page.locator("input[type='email'], input[name='email']").first
                    if await email_input.is_visible():
                        await email_input.fill(EMAIL)
                        await asyncio.sleep(0.5)
                        
                        pass_input = self.page.locator("input[type='password'], input[name='password']").first
                        await pass_input.fill(PASSWORD)
                        await asyncio.sleep(0.5)
                        
                        await pass_input.press("Enter")
                        self.log.info(f"[✅ SUCCESS] Logged in as {EMAIL}.")
                        await asyncio.sleep(5)
                        return True
                    else:
                        self.log.warning("Clicked Email, but no input appeared.")
            except Exception:
                pass # Email button not visible, move to step 3

            # 3. If no Email button, click the "Wallet" or "Connect" button to open the modal
            self.log.info("Looking for Connect/Wallet button to open login modal...")
            connect_btn = self.page.locator("text=/Connect|Log in|Wallet|Sign in/i").first
            try:
                if await connect_btn.is_visible():
                    await connect_btn.click()
                    await asyncio.sleep(2)
                    # Now that the modal is open, look for the Email button again
                    email_btn = self.page.locator("text=/Email/i").first
                    if await email_btn.is_visible():
                        self.log.info("Login modal opened. Clicking Email option...")
                        await email_btn.click()
                        await asyncio.sleep(2)
                        
                        email_input = self.page.locator("input[type='email'], input[name='email']").first
                        if await email_input.is_visible():
                            await email_input.fill(EMAIL)
                            await asyncio.sleep(0.5)
                            
                            pass_input = self.page.locator("input[type='password'], input[name='password']").first
                            await pass_input.fill(PASSWORD)
                            await asyncio.sleep(0.5)
                            
                            await pass_input.press("Enter")
                            self.log.info(f"[✅ SUCCESS] Logged in as {EMAIL}.")
                            await asyncio.sleep(5)
                            return True
            except Exception:
                pass

        except Exception as e:
            # Don't spam logs if it's just a timeout
            # self.log.debug(f"auto_login skipped: {e}")
            pass
        return False

    async def perceive(self) -> Perception:
        self.cycle += 1
        try:
            return await self._perceive_inner()
        except Exception as e:
            self.log.warning("perception hiccup: %s", str(e)[:120])
            return self._empty_perception()

    async def _perceive_inner(self) -> Perception:
        elements, toasts, events, bodies = [], [], [], []
        title, headings, has_canvas = "", [], False
        try:
            frames = [f for f in self.page.frames
                      if not (f.url or "").startswith(
                          ("chrome-extension://", "devtools://", "about:"))]
        except Exception:
            frames = []
        for i, fr in enumerate(frames[:5]):
            try:
                raw = await fr.evaluate(JS_CENSUS)
            except Exception:
                continue
            fu = fr.url or ""
            for d in raw.get("elements", []):
                d["frame_url"] = fu
                elements.append(self._make_element(d))
            toasts.extend(raw.get("toasts", []))
            events.extend(raw.get("events", []))
            meta = raw.get("meta") or {}
            bodies.append(meta.get("body_text", ""))
            has_canvas = has_canvas or bool(meta.get("has_canvas"))
            if i == 0:
                title = meta.get("title", "")
                headings = meta.get("headings", [])
        if not bodies:
            return self._empty_perception()
        body_text = " || ".join(b for b in bodies if b)

        new_fps, ripe_fps = [], []
        for el in elements:
            st = self.memory.elements.get(el.fp)
            if st is None:
                st = self.memory.elements[el.fp] = {
                    "visits": 0, "success": 0.0, "cooldowns": [],
                    "dead_until": 0.0, "futile": 0}
                if self.cycle > 1:
                    new_fps.append(el.fp)
                    self.memory.discoveries += 1
            el.visits = int(st.get("visits", 0))
            was = self._prev_disabled.get(el.fp)
            if was is True and not el.disabled:
                da = st.get("disable_at")
                if da:
                    cds = st.setdefault("cooldowns", [])
                    cds.append(round(time.time() - da, 1))
                    del cds[:-6]
                    st.pop("disable_at", None)
                    ripe_fps.append(el.fp)
            elif was is False and el.disabled and st.get("disable_at") is None:
                st["disable_at"] = time.time()
            self._prev_disabled[el.fp] = el.disabled
        if len(self._prev_disabled) > 12000:
            self._prev_disabled = dict(list(self._prev_disabled.items())[6000:])

        modal_elements = [e for e in elements if e.in_modal]
        gist = " | ".join(h for h in headings if h) or title
        scene_id, scene_tag, scene_new = self.scenes.resolve(
            gist, title, bool(modal_elements))
        if scene_new:
            self.log.info("perception | discovered scene %s [%s]: %r",
                          scene_id, scene_tag, (gist or title)[:60])
        if (scene_tag in ("hub", "unknown")
                and any(e.concept == "wallet" for e in elements)
                and len(elements) <= 10):
            scene_tag = "gate"

        signals = extract_signals(body_text)
        self.signals.update(signals, self.cycle)
        label = self.cfg.wealth_label or self.signals.wealth_label
        if not label and self.cycle % 30 == 0:
            elected = self.signals.elect()
            if elected:
                self.signals.wealth_label = elected
                label = elected
                self.log.info("economy | elected primary resource: %r", elected)
        wealth = (label, signals[label]) if (label and label in signals) else None
        if wealth:
            self.memory.wealth_history.append(
                [self.cycle, round(wealth[1], 4)])
            del self.memory.wealth_history[:-600]
        if ripe_fps:
            self.log.info("perception | %d control(s) finished cooling down",
                          len(ripe_fps))

        if scene_tag == "gate":
            self._gate_cycles += 1
            if (self._gate_cycles >= 6
                    and time.time() - self._last_wallet_try > 90):
                self._wallet_connected = False
                self._gate_cycles = 0
        else:
            self._gate_cycles = 0
        if re.search(r"0x[0-9a-fA-F]{8,}", body_text):
            self._wallet_connected = True

        now_ms = time.time() * 1000.0
        events = [e for e in events
                  if now_ms - float(e.get("ts") or 0) < 90000]

        return Perception(cycle=self.cycle, url=self.page.url, title=title,
                          gist=gist, scene_id=scene_id, scene_tag=scene_tag,
                          scene_new=scene_new, elements=elements,
                          modal_elements=modal_elements, toasts=toasts,
                          events=events, signals=signals, wealth=wealth,
                          has_canvas=has_canvas, new_fps=new_fps,
                          ripe_fps=ripe_fps)

    def _empty_perception(self) -> Perception:
        return Perception(cycle=self.cycle, url="", title="", gist="",
                          scene_id="loading", scene_tag="loading",
                          scene_new=False)

    def _make_element(self, d: dict) -> Element:
        text = (d.get("text") or "").strip()
        aria = (d.get("aria") or "").strip()
        title_a = (d.get("title") or "").strip()
        testid = (d.get("testid") or "").strip()
        cls = (d.get("cls") or "").strip()
        label_src = text or aria or title_a or testid or cls or "icon"
        label_low = re.sub(r"\s+", " ", label_src.lower())[:90]
        stable = re.sub(r"[\d.,]+", "#", label_low)
        fp = hashlib.sha1(("%s|%s|%s|%d" % (
            stable, d.get("tag", ""), d.get("role", ""),
            1 if d.get("in_modal") else 0)).encode()).hexdigest()[:12]
        thash = hashlib.sha1(stable.encode()).hexdigest()[:8]
        concept, conf = self.kernel.classify(
            label_src if (text or aria or title_a) else cls)
        return Element(tag=d.get("tag", ""), role=d.get("role", ""),
                       text=text[:140], aria=aria, title=title_a,
                       testid=testid, cls=cls, href=d.get("href", ""),
                       disabled=bool(d.get("disabled")),
                       x=int(d.get("x", 0)), y=int(d.get("y", 0)),
                       w=int(d.get("w", 0)), h=int(d.get("h", 0)),
                       in_modal=bool(d.get("in_modal")),
                       frame_url=d.get("frame_url", ""),
                       fp=fp, concept=concept, conf=conf, thash=thash)

    def think(self, per: Perception) -> Action:
        goal = self._choose_goal(per)
        w = per.wealth
        self.log.info(
            "c%04d %s[%s] wealth=%s | cur %.2f ava %.2f bor %.2f cau %.2f "
            "| goal: %s",
            per.cycle, per.scene_id, per.scene_tag,
            format(w[1], ",.0f") if w else "-",
            self.drives.curiosity, self.drives.avarice,
            self.drives.boredom, self.drives.caution, goal.name)

        focus = goal.focus
        if focus == "wait":
            s = self._wait_seconds()
            return Action("wait", seconds=s, reason="cooling (%.0fs)" % s)
        if focus == "recover":
            cands = self._candidates(per, focus)
            if cands:
                _, el = self._pick(cands)
                self.log.info("  -> retreating through %r", el.label())
                return Action("click", element=el, reason="retreat")
            self.log.info("  -> withdrawing to a fresh page")
            return Action("reload", reason="retreat")
        if focus == "idle":
            if per.elements and time.time() - self._last_scroll > 20:
                return Action("scroll", reason="peek below the fold")
            return Action("idle", seconds=random.uniform(2.5, 5.0),
                          reason="watching")

        cands = self._candidates(per, focus)
        if not cands:
            if per.has_canvas and not per.elements:
                return Action("idle", seconds=random.uniform(3.0, 6.0),
                              reason="painted canvas; waiting for DOM")
            if time.time() - self._last_scroll > 12:
                return Action("scroll", reason="searching the fold")
            return Action("wait", seconds=random.uniform(3.0, 7.0),
                          reason="nothing offered")

        u, el = self._pick(cands)
        _, (n, q) = self._q_lookup(per, el)
        nov = 1.0 / (1.0 + el.visits)
        self.log.info("  -> %r [%s q%+.2f n%d nov%.2f u%+.2f]",
                      el.label(), el.concept or "unknown", q, n, nov, u)
        return Action("click", element=el)

    def _choose_goal(self, per: Perception) -> Goal:
        proposals = []
        if per.scene_tag == "loading":
            proposals.append(Goal("wait for the world to render", "wait", 0.7, 2))
        if per.modal_elements:
            proposals.append(Goal("resolve the overlay", "modal", 0.93, 3))
        wallety = (per.scene_tag == "gate"
                   or any(e.concept == "wallet" for e in per.elements))
        if (self.cfg.auto_connect_wallet and wallety
                and not self._wallet_connected
                and time.time() - self._last_wallet_try > 45):
            proposals.append(Goal("plug in the wallet", "wallet", 0.88, 3))
        if per.ripe_fps:
            proposals.append(Goal("claim what ripened", "ripe", 0.85, 3))
        if self.error_streak >= 4 or self.drives.caution > 0.66:
            proposals.append(Goal("recover composure", "recover", 0.80, 4))
        if self._best_q(per) > 0.12:
            proposals.append(Goal("grow the hoard", "wealth",
                                  0.50 + 0.30 * self.drives.avarice, 10))
        unvisited = sum(1 for e in per.elements
                        if e.visits == 0 and not e.disabled
                        and not self._is_blocked(e, per))
        if unvisited or per.scene_new:
            proposals.append(Goal("chase the unknown", "explore",
                                  0.34 + 0.40 * self.drives.curiosity, 8))
        if self._cooling_count() and not any(g.focus == "wealth"
                                              for g in proposals):
            proposals.append(Goal("let the world turn", "wait", 0.42, 2))
        if not proposals:
            proposals.append(Goal("simply exist", "idle", 0.2, 3))

        proposals.sort(key=lambda g: -g.priority)
        best = proposals[0]
        cur = self.goal
        if cur and cur.focus == best.focus:
            cur.ttl = max(cur.ttl, best.ttl)
            return cur
        if cur and cur.ttl > 0 and best.priority < cur.priority + 0.07:
            cur.ttl -= 1
            return cur
        self.goal = best
        return best

    def _candidates(self, per: Perception, focus: str):
        pool = per.elements
        if focus == "modal" and per.modal_elements:
            pool = per.modal_elements
        out = []
        for el in pool:
            if el.disabled or self._is_blocked(el, per):
                continue
            if focus == "recover" and el.concept not in ("close", "navigate"):
                continue
            out.append((self._utility(el, per, focus), el))
        out.sort(key=lambda t: -t[0])
        if not out and pool is not per.elements and per.elements:
            for el in per.elements:
                if el.disabled or self._is_blocked(el, per):
                    continue
                out.append((self._utility(el, per, focus), el))
            out.sort(key=lambda t: -t[0])
        return out[:14]

    def _utility(self, el: Element, per: Perception, focus: str) -> float:
        _, (n, q) = self._q_lookup(per, el)
        total = max(10, self.memory.total_actions)
        ucb = UCB_C * math.sqrt(math.log(total) / (1.0 + n))
        nov = 1.0 / (1.0 + el.visits)
        u = q + ucb * (0.6 + 0.8 * self.drives.curiosity) + NOVELTY_W * nov
        if el.in_modal:
            u += 0.35
        if len(el.text) > 90:
            u -= 0.5
        if focus == "wealth":
            u += 0.45 * q
        elif focus == "explore":
            u += 0.55 * nov + (0.35 if el.concept == "navigate" else 0.0)
        elif focus == "ripe" and el.fp in per.ripe_fps:
            u += 1.5
        elif focus == "wallet" and el.concept == "wallet":
            u += 2.2
        if self.last_click_concept and random.random() > 0.15:
            mk = self.last_click_concept + ">>" + (
                el.concept or ("u:" + el.thash))
            n2, avg = self.memory.macros.get(mk, [0, 0.0])
            if n2 >= 3 and avg > 0.12:
                u += min(0.9, avg)
        return u

    def _pick(self, cands):
        top = cands[:10]
        T = max(0.12, min(1.8, 0.15 + 0.9 * self.drives.boredom
                         + 0.6 * self.drives.curiosity))
        mx = top[0][0]
        ws = [math.exp((u - mx) / T) for u, _ in top]
        r = random.uniform(0.0, sum(ws))
        acc = 0.0
        for w, cand in zip(ws, top):
            acc += w
            if r <= acc:
                return cand
        return top[-1]

    def _qkey(self, per: Perception, el: Element) -> str:
        c = el.concept or ("u:" + el.thash)
        return "%s|%s|%s" % (per.scene_id, c, "m" if el.in_modal else "p")

    def _q_lookup(self, per: Perception, el: Element):
        key = self._qkey(per, el)
        hit = self.memory.q.get(key)
        if hit:
            return key, hit
        c = el.concept or ("u:" + el.thash)
        m = "m" if el.in_modal else "p"
        for alt in ("*|%s|%s" % (c, m), "%s|%s|*" % (per.scene_tag, c)):
            hit = self.memory.q.get(alt)
            if hit:
                return alt, hit
        return key, [0, OPTIMISM]

    def _best_q(self, per: Perception) -> float:
        best = -1.0
        for el in per.elements:
            if el.disabled or self._is_blocked(el, per):
                continue
            _, (_, q) = self._q_lookup(per, el)
            if q > best:
                best = q
        return best

    def _is_blocked(self, el: Element, per: Perception) -> bool:
        st = self.memory.elements.get(el.fp) or {}
        if float(st.get("dead_until", 0) or 0) > time.time():
            return True
        txt = (el.text + " " + el.aria + " " + el.title).lower()
        matched = {m.group(0).lower() for m in RISKY_RE.finditer(txt)}
        if matched - self.cfg.allow_risky:
            return True
        if self.cfg.avoid_social and el.concept == "social":
            return True
        if el.href:
            if el.href.startswith(("mailto:", "tel:")):
                return True
            if self.cfg.same_domain_only:
                try:
                    h = urlsplit(el.href).hostname or ""
                    if (h and self._domain and h != self._domain
                            and not h.endswith("." + self._domain)):
                        return True
                except ValueError:
                    return True
        return False

    def _wait_seconds(self) -> float:
        now = time.time()
        remains = []
        for fp, st in self.memory.elements.items():
            da = st.get("disable_at")
            if not da or not self._prev_disabled.get(fp):
                continue
            cds = st.get("cooldowns") or []
            mean_cd = (sum(cds) / len(cds)) if cds else 8.0
            r = da + mean_cd * 1.05 + 1.5 - now
            if 0 < r < self.cfg.cycle_wait_cap:
                remains.append(r)
        return min(remains) if remains else min(5.0, self.cfg.cycle_wait_cap)

    def _cooling_count(self) -> int:
        return sum(1 for fp, st in self.memory.elements.items()
                   if st.get("disable_at") and self._prev_disabled.get(fp))

    async def act(self, action: Action):
        if action.kind == "click" and action.element is not None:
            el = action.element
            if random.random() < 0.06 and el.concept not in ("confirm", "close",
                                                             "wallet"):
                await self._hover(el)
                self.drives.curiosity = max(0.05, self.drives.curiosity - 0.03)
                self.log.info("  .. hesitates over %r, just watching", el.label())
                return
            ok = await self._click(el)
            action.executed = ok
            self.memory.total_actions += 1
            st = self.memory.elements.setdefault(el.fp, {
                "visits": 0, "success": 0.0, "cooldowns": [],
                "dead_until": 0.0, "futile": 0})
            st["visits"] = int(st.get("visits", 0)) + 1
            st["last_click"] = time.time()
            if ok:
                if el.concept == "wallet":
                    self._last_wallet_try = time.time()
                    self._wallet_connected = True
                    self.log.info("  .. wallet handshake attempted")
                await asyncio.sleep(random.uniform(0.45, 1.0))
                try:
                    await self.page.wait_for_load_state("networkidle",
                                                       timeout=2500)
                except Exception:
                    pass
            else:
                self.log.info("  .. could not touch %r", el.label())
        elif action.kind == "wait":
            await asyncio.sleep(max(0.5, min(action.seconds,
                                            self.cfg.cycle_wait_cap)))
        elif action.kind == "scroll":
            self._last_scroll = time.time()
            try:
                await self.page.mouse.wheel(
                    0, random.choice((500, -500, 800, -800, 1100)))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.6, 1.2))
        elif action.kind == "reload":
            if time.time() - self._last_reload > 25:
                self._last_reload = time.time()
                try:
                    await self.page.reload(wait_until="domcontentloaded",
                                          timeout=45000)
                except Exception as e:
                    self.log.warning("reload failed: %s", str(e)[:100])
        elif action.kind == "idle":
            try:
                await self.page.mouse.move(
                    random.uniform(80, self.cfg.viewport_w - 80),
                    random.uniform(80, self.cfg.viewport_h - 80), steps=10)
            except Exception:
                pass
            await asyncio.sleep(max(1.0, action.seconds))

    async def _click(self, el: Element) -> bool:
        frame = self._frame_for(el)
        loc = self._locator_for(frame, el)
        if loc is not None:
            try:
                await loc.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.15, 0.5))
            try:
                await loc.click(timeout=5000)
                return True
            except Exception as e:
                self.log.debug("locator click failed on %r: %s",
                               el.label(), str(e)[:120])
        cx, cy = el.center()
        main = (not el.frame_url) or (el.frame_url == self.page.url)
        if (main and 3 <= cx <= self.cfg.viewport_w - 3
                and 3 <= cy <= self.cfg.viewport_h - 3):
            try:
                await self.page.mouse.move(cx + random.uniform(-3, 3),
                                           cy + random.uniform(-3, 3), steps=6)
                await self.page.mouse.click(int(cx), int(cy))
                return True
            except Exception as e:
                self.log.debug("mouse click failed: %s", str(e)[:120])
        try:
            spec = {"text": el.text[:80], "aria": el.aria[:60],
                    "testid": el.testid, "cx": cx, "cy": cy}
            return bool(await frame.evaluate(JS_CLICK, spec))
        except Exception as e:
            self.log.debug("js click failed: %s", str(e)[:120])
            return False

    def _frame_for(self, el: Element):
        fu = el.frame_url or ""
        if not fu or fu == self.page.url:
            return self.page
        for f in self.page.frames:
            if (f.url or "") == fu:
                return f
        return self.page

    def _locator_for(self, frame, el: Element):
        if el.testid:
            t = el.testid.replace("\\", "\\\\").replace('"', '\\"')
            return frame.locator('[data-testid="%s"]' % t).first
        name = el.aria or el.text
        if name and 2 <= len(name) <= 60:
            role = el.role
            if not role:
                if el.tag == "button":
                    role = "button"
                elif el.tag == "a":
                    role = "link"
            if role in ("button", "link", "tab", "menuitem", "option",
                        "switch", "checkbox", "radio"):
                try:
                    return frame.get_by_role(role, name=name).first
                except Exception:
                    pass
            if el.tag in ("button", "a"):
                return frame.locator(el.tag, has_text=name[:40]).first
        return None

    async def _hover(self, el: Element):
        try:
            cx, cy = el.center()
            await self.page.mouse.move(cx + random.uniform(-2, 2),
                                       cy + random.uniform(-2, 2), steps=5)
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    async def reflect(self, pre: Perception, action: Action,
                      act_start: float) -> Perception:
        post = await self.perceive()

        fresh = [t for t in post.toasts if t not in pre.toasts]
        fresh += [e.get("t", "") for e in post.events
                  if float(e.get("ts") or 0) / 1000.0 >= act_start - 0.5]
        while self._dialog_events:
            fresh.append(self._dialog_events.popleft())
        fresh = [t for t in dict.fromkeys(
            x.strip() for x in fresh if x and x.strip())][:6]
        sent, sent_texts = classify_sentiment(fresh)

        wdelta = None
        if pre.wealth and post.wealth and pre.wealth[0] == post.wealth[0]:
            wdelta = post.wealth[1] - pre.wealth[1]
        if wdelta is None:
            g = toast_gain(fresh)
            if g is not None:
                wdelta = g

        reward = 0.0
        if wdelta is not None and abs(wdelta) > 1e-9:
            self.scale.update(wdelta)
            reward += max(-1.2, min(1.2, wdelta / max(1.0, self.scale.scale)))
        reward += sent
        reward += 0.10 * min(3, len(post.new_fps))
        if post.scene_new:
            reward += 0.06
        reward -= 0.03

        el = action.element
        if action.kind == "click" and el is not None:
            st = self.memory.elements.setdefault(el.fp, {
                "visits": 0, "success": 0.0, "cooldowns": [],
                "dead_until": 0.0, "futile": 0})
            if action.executed:
                key, (n, q) = self._q_lookup(pre, el)
                alpha = max(0.06, 1.0 / (2.0 + n))
                self.memory.q[key] = [n + 1, q + alpha * (reward - q)]
                concept = el.concept or ("u:" + el.thash)
                if self.last_click_concept:
                    mk = self.last_click_concept + ">>" + concept
                    n2, avg = self.memory.macros.get(mk, [0, 0.0])
                    self.memory.macros[mk] = [n2 + 1,
                                               (avg * n2 + reward) / (n2 + 1)]
                self.last_click_concept = concept
                if reward > 0.02:
                    st["success"] = float(st.get("success", 0.0)) + reward
                    self.signals.bump_assoc(pre.signals, post.signals)
                if (not el.concept and 3 <= len(el.text) <= 60
                        and reward >= 0.45):
                    name = "lc_" + el.thash
                    if name not in self.memory.learned:
                        self.memory.learned[name] = [el.text]
                        self.kernel.add_learned(name, [el.text])
                        self.log.info("mind | forged concept %s from %r "
                                      "(it paid off)", name, el.label())
                no_effect = ((wdelta is None or abs(wdelta) < 1e-9)
                             and not sent_texts and not post.new_fps
                             and not post.scene_new
                             and post.scene_id == pre.scene_id)
                if no_effect:
                    st["futile"] = int(st.get("futile", 0)) + 1
                    if st["futile"] >= 4:
                        st["dead_until"] = time.time() + 900.0
                        st["futile"] = 0
                        self.log.info("mind | %r feels lifeless; ignoring it "
                                      "for a while", el.label())
                else:
                    st["futile"] = 0

        d = self.drives
        if reward >= 0.08:
            d.boredom = max(0.0, d.boredom - 0.45)
            d.caution = max(0.0, d.caution - 0.10)
        else:
            d.boredom = min(1.0, d.boredom + 0.045)
        if post.new_fps or post.scene_new:
            d.curiosity = max(0.12, d.curiosity - 0.30)
        else:
            d.curiosity = min(1.0, d.curiosity + 0.02)
        if sent < 0 or (wdelta is not None and wdelta < 0):
            d.caution = min(1.0, d.caution + 0.18)
        d.caution = max(0.0, d.caution - 0.025)
        d.avarice = 0.2 + (0.6 if self._best_q(post) > 0.12 else 0.0)
        if self._console_errors:
            d.caution = min(1.0, d.caution + 0.04 * min(3,
                                                        self._console_errors))
            self._console_errors = 0

        wds = ("%+g" % wdelta) if wdelta is not None else "n/a"
        self.log.info("  <- reward %+.2f | wealth %s | new %d | %s",
                      reward, wds, len(post.new_fps),
                      ("; ".join(sent_texts)[:110] or "silence"))
        return post

    def _attach_page(self, page: Page):
        page.on("dialog", self._on_dialog)
        page.on("console", self._on_console)

    async def _on_dialog(self, dialog: Dialog):
        text = dialog.message or ""
        self._dialog_events.append(text[:160])
        try:
            if dialog.type == "beforeunload":
                await dialog.dismiss()
                return
            low = text.lower()
            risky = bool(RISKY_RE.search(low))
            if (not risky and re.search(r"\d", low)
                    and re.search(r"\b(cost|fee|gas|price)\b", low)):
                risky = True
            if risky and not self.cfg.allow_signing:
                self.log.warning("  !! dialog DENIED (protects funds): %r",
                                 text[:90])
                await dialog.dismiss()
            else:
                self.log.info("  dialog accepted: %r", text[:90])
                await dialog.accept()
        except Exception as e:
            self.log.debug("dialog handling error: %s", e)

    def _on_console(self, msg):
        try:
            if msg.type == "error":
                self._console_errors += 1
        except Exception:
            pass

    async def _on_popup(self, p: Page):
        self.log.info("  popup: %s", (p.url or "")[:80])
        try:
            await p.wait_for_load_state("domcontentloaded", timeout=8000)
            await asyncio.sleep(1.2)
            for name in ("Connect", "Next", "Unlock"):
                try:
                    loc = p.get_by_role("button", name=name).first
                    if await loc.count():
                        await loc.click(timeout=2500)
                        self.log.info("  popup: clicked %r", name)
                        await asyncio.sleep(0.8)
                        return
                except Exception:
                    continue
        except Exception as e:
            self.log.debug("popup handled: %s", str(e)[:100])

    async def _recover(self):
        self.log.warning("recovering (error streak %d)", self.error_streak)
        try:
            if self.page.is_closed():
                self.page = await self.context.new_page()
                self._attach_page(self.page)
                await self.page.goto(self.cfg.url,
                                     wait_until="domcontentloaded",
                                     timeout=45000)
            elif time.time() - self._last_reload > 20:
                self._last_reload = time.time()
                await self.page.reload(wait_until="domcontentloaded",
                                       timeout=45000)
        except Exception as e:
            self.log.error("recovery failed: %s", e)

    def _cycle_delay(self, action: Action) -> float:
        if action.kind == "click":
            return random.uniform(0.5, 1.4)
        if action.kind == "wait":
            return 0.4
        return random.uniform(self.cfg.cycle_idle * 0.5,
                              self.cfg.cycle_idle * 1.2)

    def _stats(self):
        top = sorted(self.memory.q.items(),
                     key=lambda kv: kv[1][1])[-6:][::-1]
        mind = ", ".join("%s:%+.2f(n%d)" % (k.split("|")[1], v[1], v[0])
                         for k, v in top) or "nothing yet"
        wh = self.memory.wealth_history
        wtxt = "-"
        if wh:
            wtxt = "%s (from %s)" % (format(wh[-1][1], ",.2f"),
                                     format(wh[0][1], ",.2f"))
        self.log.info("STATUS c%d | wealth %s | actions %d | discoveries %d | "
                     "scenes %d | mind[%s]", self.cycle, wtxt,
                     self.memory.total_actions, self.memory.discoveries,
                     len(self.memory.scenes), mind)

# ═══════════════════════════════ 8. LIFECYCLE ══════════════════════════════════

async def main(cfg: Config):
    log = build_logger(cfg)
    memory = Memory(Path(cfg.memory_path))
    memory.load()
    memory.sessions += 1
    kernel = SemanticKernel(memory.learned)
    stop = {"flag": False}

    async with async_playwright() as pw:
        browser = None
        common = dict(headless=cfg.headless, args=_chromium_args(cfg),
                      slow_mo=cfg.slowmo)
        if cfg.user_data_dir:
            context = await pw.chromium.launch_persistent_context(
                cfg.user_data_dir,
                viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
                ignore_https_errors=True, user_agent=cfg.user_agent, **common)
        else:
            browser = await pw.chromium.launch(**common)
            context = await browser.new_context(
                viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
                ignore_https_errors=True, locale="en-US", user_agent=cfg.user_agent)
        await context.add_init_script(JS_INIT)
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(15000)

        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, stop.update, {"flag": True})
            except (NotImplementedError, RuntimeError):
                pass

        agent = CognitiveCore(page, context, cfg, memory, kernel, log, stop)
        try:
            await agent.run()
        finally:
            try:
                memory.save()
                log.info("memory persisted to %s", cfg.memory_path)
            except Exception:
                pass
            try:
                await context.close()
                if browser:
                    await browser.close()
            except Exception:
                pass
    log.info("agent shut down cleanly")

if __name__ == "__main__":
    cfg = parse_args()
    if cfg.seed is not None:
        random.seed(cfg.seed)
    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        print("\ninterrupted -- memory was saved on shutdown")
