#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dom_playground.py -- a self-correcting "DOM Playground" for an autonomous agent.
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

__version__ = "1.0.0"

# 1. CLI / CONFIG / LOGGING

DIM = 256

@dataclass
class Config:
    url: str = ""
    goal: str = "play the game"
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
    after_goal: str = "exit"
    enter_submit: bool = False
    popup_help: bool = True
    allow_signing: bool = False
    seed: int = -1
    stop_file: str = ".dom_playground_stop"

def parse_args() -> Config:
    ap = argparse.ArgumentParser(description="DOM Playground")
    ap.add_argument("url", nargs="?", default=os.environ.get("GAME_URL", ""))
    ap.add_argument("--goal", default="play the game")
    ap.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
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
    ap.add_argument("--after-goal", choices=["exit", "linger"], default="exit")
    ap.add_argument("--enter-submit", action="store_true")
    ap.add_argument("--no-popup-help", action="store_true")
    ap.add_argument("--allow-signing", action="store_true")
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

    cfg = Config(url=a.url.strip(), goal=a.goal, fields=fields, headless=not a.headed, 
                 max_cycles=a.max_cycles, max_minutes=a.max_minutes, memory_path=a.memory,
                 log_path=a.log_file, log_level=a.log_level, code_log=a.code_log, dump_states=a.dump_states, 
                 challenge_patience=a.challenge_patience, user_data_dir=a.user_data_dir.strip(),
                 load_extension=a.load_extension.strip(), slowmo=a.slowmo, viewport_w=vw, viewport_h=vh, 
                 proxy=a.proxy.strip(), after_goal=a.after_goal, enter_submit=a.enter_submit,
                 popup_help=not a.no_popup_help, allow_signing=a.allow_signing, seed=a.seed)
    if not cfg.url.startswith(("http://", "https://")):
        ap.error("a target URL is required")
    return cfg

def build_logger(cfg: Config) -> logging.Logger:
    log = logging.getLogger("dom-playground")
    log.setLevel(logging.DEBUG)
    if log.handlers:
        return log
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    con.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
    fh = RotatingFileHandler(cfg.log_path, maxBytes=3_000_000, backupCount=3, encoding="utf-8")
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
        args += ["--disable-extensions-except=" + cfg.load_extension, "--load-extension=" + cfg.load_extension]
    return args

# 2. SEMANTIC PRIMITIVES

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

ANCHORS = {"dismiss": ["close", "cancel", "x", "dismiss", "skip", "no thanks", "later", "back", "exit"]}
_ANCHOR_VECS = {k: embed(" ".join(v)) for k, v in ANCHORS.items()}

def anchor_sim(text: str, name: str) -> float:
    return _dot(embed(text), _ANCHOR_VECS[name])

SOCIAL_RE = re.compile(r"\b(share|tweet|twitter|discord|telegram|follow|invite|refer|friend)\b", re.I)
RISKY_RE = re.compile(r"\b(approve|transfer|withdraw|burn|spend|swap|mint|send|sign(?:\s+transaction)?)\b", re.I)

def el_label(el: dict) -> str:
    for k in ("text", "aria", "title", "testid", "cls"):
        v = (el.get(k) or "").strip()
        if v:
            return v
    return el.get("tag", "?")

def elem_fp(el: dict) -> str:
    lab = re.sub(r"[\d.,]+", "#", el_label(el).lower())
    lab = re.sub(r"\s+", " ", lab.strip())[:64]
    raw = "%s|%s|%s|%s|%s" % (lab, el.get("tag"), el.get("role"), el.get("frame_index"), 1 if el.get("modal") else 0)
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

# 3. PROBE

JS_STEALTH = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
})();
"""

JS_PROBE = r"""() => {
  const OUT = {url: location.href, name: window.name || '', title: document.title || '', elements: [], text: '', flags: {}};
  const SEL = 'button, a[href], input, select, textarea, summary, [onclick], [role="button"], [role="tab"], [role="menuitem"], [role="option"], [role="switch"], [role="checkbox"], [role="link"]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity || '1') < 0.05) return null;
    if (el.getAttribute('aria-hidden') === 'true') return null;
    return r;
  };
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (n >= 300) break;
    const r = vis(el);
    if (!r) continue;
    let cls = '';
    try { cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).slice(0, 6).join(' '); } catch (e) {}
    const txt = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
    const rec = {
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', text: txt.slice(0, 120),
      aria: el.getAttribute('aria-label') || '', title: el.getAttribute('title') || '',
      testid: el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || '',
      cls: cls.slice(0, 90), href: (el.tagName === 'A' ? (el.getAttribute('href') || '') : '').slice(0, 120),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true' || /(^|[\s_-])(disabled|is-disabled)([\s_-]|$)/i.test(cls)),
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      modal: !!el.closest('[role="dialog"], [class*="modal" i], [class*="overlay" i], [class*="popup" i], [class*="drawer" i]')
    };
    const tn = el.tagName;
    if (tn === 'INPUT' || tn === 'TEXTAREA' || tn === 'SELECT') {
      rec.input = { type: (el.getAttribute('type') || tn.toLowerCase()), name: el.getAttribute('name') || '', id: el.id || '', placeholder: el.getAttribute('placeholder') || '', autocomplete: el.getAttribute('autocomplete') || '' };
    }
    OUT.elements.push(rec);
    n++;
  }
  OUT.text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 9000);
  OUT.flags = { cloudflare: !!document.querySelector('#challenge-form, #cf-challenge-running, .cf-turnstile, [class*="cf-chl"]'), turnstile: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]'), canvas: !!document.querySelector('canvas'), has_password: !!document.querySelector('input[type="password"]') };
  return OUT;
}"""

JS_CHALLENGE_CHECK = "() => ({t: (document.title || ''), cf: !!(document.querySelector('#challenge-form, .cf-turnstile, [class*=cf-chl], #cf-spinner-verify'))})"

JS_CLICK = r"""(spec) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const tTxt = norm(spec.text), tAria = norm(spec.aria), tTest = norm(spec.testid);
  let best = null, bs = 0.5;
  for (const el of document.querySelectorAll('button, a, [role="button"], [onclick], input, summary')) {
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
    state = {"cycle": cycle, "url": page.url, "title": "", "frames": [], "elements": [], "inputs": [], "flags": {"cloudflare": False, "turnstile": False, "wallet": False, "canvas": False, "password": False}, "text": "", "fingerprint": ""}
    try:
        frames = [f for f in page.frames if not (f.url or "").startswith(("chrome-extension://", "devtools://", "about:", "data:"))]
    except Exception:
        frames = [page.main_frame]
    for idx, fr in enumerate(frames[:6]):
        try:
            raw = await fr.evaluate(JS_PROBE)
        except Exception:
            continue
        state["frames"].append({"index": idx, "url": raw.get("url", ""), "name": raw.get("name", ""), "title": raw.get("title", "")})
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
    if re.search(r"just a moment|attention required|checking your browser", state["title"], re.I):
        state["flags"]["cloudflare"] = True
    try:
        w = await page.evaluate("() => ({eth: !!(window.ethereum || (window.web3 && window.web3.currentProvider)), sol: !!(window.solana || window.phantom)})")
        state["flags"]["wallet"] = bool(w.get("eth") or w.get("sol"))
    except Exception:
        pass
    fps = sorted(e["fp"] for e in state["elements"])
    state["fingerprint"] = hashlib.sha1(("|".join(fps) + "|" + state["url"] + "|" + state["title"]).encode()).hexdigest()[:16]
    return state

# 4. REASON

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

    def think(self, state: dict, history: deque, subgoal: str, memory: "Memory", stuck: int) -> Plan:
        flags = state.get("flags", {})
        pb, gb = page_key(state), goal_key(subgoal)
        last = history[-1] if history else {}
        err = last.get("error_sig")
        force = err in ("intercept", "viewport", "invisible")

        wkey = pb + "|" + gb
        win = memory.wins.get(wkey)
        if win:
            tf = win.get("target_fp") or ""
            fresh_target = (not tf or any(e.get("fp") == tf for e in state.get("elements", [])))
            not_just_failed = not (last.get("used_win") and last.get("outcome") in ("futile", "error"))
            if int(win.get("fails", 0)) < 2 and fresh_target and not_just_failed:
                el = next((e for e in state["elements"] if e.get("fp") == tf), None)
                return Plan("reuse-learned", win["code"], "replay learned snippet (%s)" % win.get("how", "?"), ctx=self._ctx_for(el) if el else {"target_fp": tf}, timeout=60.0, match=0.5)
            win["fails"] = int(win.get("fails", 0)) + 1

        if flags.get("cloudflare"):
            return self._plan_challenge()

        if self.cfg.fields:
            m = self._match_field(state, memory)
            if m:
                return self._plan_fill(m[1], m[2], m[3], state, subgoal)

        if "explore" in (subgoal or "").lower():
            el = self._pick_novel(state, memory)
            if el:
                return self._plan_click(el, state, subgoal, memory, name="explore-click", match=0.30)
            return self._plan_shake()

        modal = [e for e in state.get("elements", []) if e.get("modal")]
        if modal:
            hit = self._best(state, subgoal, memory, pool=modal)
            if hit and hit[0] >= 0.45:
                return self._plan_click(hit[1], state, subgoal, memory, name="overlay-cta", force=force, match=hit[0])
            dis = self._best_anchor(modal, "dismiss")
            if dis:
                return self._plan_click(dis, state, subgoal, memory, name="dismiss-overlay", force=force)

        if stuck >= 5 and not self._recently(history, "page:reloaded", 3):
            return self._plan_reload(subgoal)

        avoid = last.get("target_fp") if last.get("outcome") in ("futile", "error") else None
        hit = self._best(state, subgoal, memory, avoid_fp=avoid)
        if hit and hit[0] >= 0.22:
            return self._plan_click(hit[1], state, subgoal, memory, name="click-cta", force=force, match=hit[0])

        if not self._recently(history, "shake:scrolled", 2):
            return self._plan_shake()
        el = self._pick_novel(state, memory)
        if el:
            return self._plan_click(el, state, subgoal, memory, name="explore-click", match=0.25)
        return self._plan_idle(4.0)

    def _score(self, el: dict, goal_vec, memory: "Memory", avoid_fp) -> float:
        lab = el_label(el)
        s = 2.2 * _dot(goal_vec, embed(lab))
        tag, role = el.get("tag", ""), el.get("role", "")
        if tag == "button" or role == "button": s += 0.15
        elif tag == "a": s += 0.06
        if el.get("y", 0) > self.cfg.viewport_h * 1.6: s -= 0.30
        if el.get("disabled"): s -= 1.0
        if el.get("w", 0) < 24 or el.get("h", 0) < 14: s -= 0.35
        if len(lab) > 90: s -= 0.5
        if SOCIAL_RE.search(lab): s -= 0.8
        if (el.get("href") or "").startswith(("mailto:", "tel:")): s -= 1.2
        if el.get("modal"): s += 0.15
        st = memory.elem.get(el.get("fp", ""))
        if st:
            s -= 0.20 * int(st.get("fails", 0))
            s -= 0.04 * int(st.get("tries", 0))
        if avoid_fp and el.get("fp") == avoid_fp: s -= 0.5
        return s

    def _best(self, state, subgoal, memory, pool=None, avoid_fp=None):
        gv = embed(subgoal)
        items = pool if pool is not None else state.get("elements", [])
        scored = [(self._score(e, gv, memory, avoid_fp), e) for e in items if not e.get("disabled")]
        if not scored: return None
        scored.sort(key=lambda t: -t[0])
        return scored[0]

    def _best_anchor(self, els, name):
        best, bs = None, 0.30
        for e in els:
            if e.get("disabled"): continue
            s = anchor_sim(el_label(e), name)
            if s > bs: bs, best = s, e
        return best

    def _pick_novel(self, state, memory):
        cands = [e for e in state.get("elements", []) if not e.get("disabled") and e.get("w", 0) >= 24 and e.get("h", 0) >= 14 and not SOCIAL_RE.search(el_label(e))]
        fresh = [e for e in cands if e.get("fp") not in memory.elem]
        pool = fresh or cands
        return random.choice(pool[:8]) if pool else None

    def _match_field(self, state, memory):
        best = None
        pb = page_key(state)
        for key, val in self.cfg.fields.items():
            kv = embed(key)
            for inp in state.get("inputs", []):
                if inp.get("disabled"): continue
                if (pb + "|" + inp.get("fp", "")) in memory.filled: continue
                st = memory.elem.get(inp.get("fp", ""))
                if st and int(st.get("fails", 0)) >= 3: continue
                fi = inp.get("input") or {}
                purpose = " ".join(str(x) for x in (fi.get("type"), fi.get("name"), fi.get("id"), fi.get("placeholder"), fi.get("autocomplete"), inp.get("aria"), inp.get("text"), inp.get("title")) if x)
                s = _dot(kv, embed(purpose))
                if s >= 0.30 and (best is None or s > best[0]): best = (s, inp, key, val)
        return best

    def _recently(self, history, needle, n):
        return any(needle in (h.get("how") or "") for h in list(history)[-n:])

    def _ctx_for(self, el):
        if not el: return {}
        cx, cy = int(el.get("x", 0) + el.get("w", 0) / 2), int(el.get("y", 0) + el.get("h", 0) / 2)
        lab = el_label(el)
        return {"target_fp": el.get("fp", ""), "target_text": lab[:48], "cx": cx, "cy": cy, "js_spec": {"text": lab[:60], "aria": (el.get("aria") or "")[:48], "testid": el.get("testid") or "", "cx": cx, "cy": cy}}

    def _frame_prelude(self, el):
        if not el.get("frame_index"): return [], "page"
        return ["    fr = page", "    for _f in page.frames:", '        if %s in (_f.url or ""):' % q(_frame_frag(el.get("frame_url") or "")), "            fr = _f", "            break"], "fr"

    def _class_token(self, el):
        for t in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,31}", el.get("cls") or ""):
            if not t.lower().startswith(("css-", "js-", "sc-", "emotion")): return t
        return ""

    def _selector_candidates(self, el, memory, tgt, state, pb, gb):
        out = []
        name = (el.get("text") or el.get("aria") or el.get("title") or "").strip()
        role = el.get("role") or ("button" if el.get("tag") == "button" else "link" if el.get("tag") == "a" else "")
        if el.get("testid"): out.append(("testid", "%s.locator(%s).first" % (tgt, q(_css_attr("data-testid", el["testid"])))))
        if role and 2 <= len(name) <= 48: out.append(("role-name", "%s.get_by_role(%s, name=%s).first" % (tgt, q(role), q(name))))
        if 2 <= len(name) <= 48:
            out.append(("text-exact", "%s.get_by_text(%s, exact=True).first" % (tgt, q(name))))
            out.append(("has-text", "%s.locator(%s, has_text=%s).first" % (tgt, q(el.get("tag", "button")), q(name[:36]))))
        tok = self._class_token(el)
        if tok:
            same = [e for e in state.get("elements", []) if e.get("frame_index") == el.get("frame_index") and e.get("tag") == el.get("tag") and tok in (e.get("cls") or "").split()]
            try: idx = same.index(el)
            except ValueError: idx = 0
            out.append(("css-nth", "%s.locator(%s).nth(%d)" % (tgt, q(el.get("tag", "button") + "." + tok), idx)))
        pruned = []
        for how, expr in out:
            if memory.sel_fails.get("|".join((pb, gb, how)), 0) >= 2: continue
            pruned.append((how, expr))
        return pruned

    def _plan_click(self, el, state, subgoal, memory, name="click-cta", force=False, match=0.5) -> Plan:
        pb, gb = page_key(state), goal_key(subgoal)
        prelude, tgt = self._frame_prelude(el)
        L = []
        A = L.append
        A('async def strategy(page, ctx, log, S):')
        A('    # plan: %s | goal: %s | target: %s' % (name, _cmt(subgoal), _cmt(el_label(el))))
        A('    last = "no candidate"')
        A('    try: await page.wait_for_load_state("domcontentloaded", timeout=8000)')
        A('    except Exception: pass')
        for ln in prelude: A(ln)
        A('    cands = [')
        for how, expr in self._selector_candidates(el, memory, tgt, state, pb, gb):
            A('        (%s, lambda: %s),' % (q(how), expr))
        A('    ]')
        A('    for how, mk in cands:')
        A('        try:')
        A('            loc = mk()')
        if not force:
            A('            try: await loc.scroll_into_view_if_needed(timeout=2500)')
            A('            except Exception: pass')
        A('            await loc.click(timeout=4500%s)' % (", force=True" if force else ""))
        A('            log("clicked via " + how)')
        A('            await wait(0.7)')
        A('            return {"ok": True, "how": "click:" + how, "target": S.get("target_fp", "")}')
        A('        except Exception as e:')
        A('            last = how + ": " + str(e)[:120]')
        A('            log("miss " + last)')
        if not el.get("frame_index"):
            A('    try: await page.mouse.move(int(S["cx"]), int(S["cy"]), steps=6); await page.mouse.click(int(S["cx"]), int(S["cy"])); log("clicked via coords"); await wait(0.7); return {"ok": True, "how": "click:coords", "target": S.get("target_fp", "")}')
            A('    except Exception as e: last = "coords: " + str(e)[:120]; log(last)')
        A('    try:')
        # FIXED: Replaced the broken %s with actual tgt variable formatting
        A('        if await js_click(%s, S["js_spec"]): log("clicked via js-dispatch"); await wait(0.7); return {"ok": True, "how": "click:js-dispatch", "target": S.get("target_fp", "")}' % tgt)
        A('    except Exception as e: last = "js: " + str(e)[:120]; log(last)')
        A('    return {"ok": False, "why": last, "how": "click:failed"}')
        return Plan(name, "\n".join(L), el_label(el)[:48], ctx=self._ctx_for(el), timeout=60.0, match=match)

    def _plan_fill(self, inp, key, value, state, subgoal) -> Plan:
        fi = inp.get("input") or {}
        prelude, tgt = self._frame_prelude(inp)
        L = []
        A = L.append
        A('async def strategy(page, ctx, log, S):')
        A('    # plan: fill-field | goal: %s | field key: %s' % (_cmt(subgoal), _cmt(key)))
        A('    last = "no input candidate"')
        for ln in prelude: A(ln)
        A('    cands = [')
        ph = (fi.get("placeholder") or "").strip()
        if ph: A('        (%s, lambda: %s.get_by_placeholder(%s).first),' % (q("placeholder"), tgt, q(ph)))
        nm = (inp.get("aria") or inp.get("text") or "").strip()
        role = "combobox" if inp.get("tag") == "select" else "textbox"
        if nm and len(nm) <= 40: A('        (%s, lambda: %s.get_by_role(%s, name=%s).first),' % (q("role-name"), tgt, q(role), q(nm)))
        if fi.get("name"): A('        (%s, lambda: %s.locator(%s).first),' % (q("css-name"), tgt, q(_css_attr("name", fi["name"]))))
        if fi.get("id"): A('        (%s, lambda: %s.locator(%s).first),' % (q("css-id"), tgt, q(_css_attr("id", fi["id"]))))
        t = re.sub(r"[^a-z0-9_-]", "", (fi.get("type") or "").lower())[:20]
        if t and t not in ("button", "submit", "checkbox", "radio", "file", "hidden", "image", "reset"):
            same = [e for e in state.get("inputs", []) if e.get("frame_index") == inp.get("frame_index") and re.sub(r"[^a-z0-9_-]", "", (e.get("input") or {}).get("type", "").lower()) == t]
            try: idx = same.index(inp)
            except ValueError: idx = 0
            A('        (%s, lambda: %s.locator(%s).nth(%d)),' % (q("nth-type"), tgt, q('%s[type="%s"]' % (inp.get("tag", "input"), t)), idx))
        same_tag = [e for e in state.get("inputs", []) if e.get("frame_index") == inp.get("frame_index") and e.get("tag") == inp.get("tag")]
        try: idx2 = same_tag.index(inp)
        except ValueError: idx2 = 0
        A('        (%s, lambda: %s.locator(%s).nth(%d)),' % (q("nth-any"), tgt, q(inp.get("tag", "input")), idx2))
        A('    ]')
        A('    for how, mk in cands:')
        A('        try: loc = mk(); await loc.fill(S["value"], timeout=4000); log("filled field " + S["key"] + " via " + how); await wait(0.35);')
        A('            if S.get("press_enter"):')
        A('                try: await loc.press("Enter", timeout=2000)')
        A('                except Exception: pass')
        A('            return {"ok": True, "how": "fill:" + how, "target": S.get("target_fp", "")}')
        A('        except Exception as e: last = how + ": " + str(e)[:120]; log("miss " + last)')
        A('    return {"ok": False, "why": last, "how": "fill:failed"}')
        ctx = {"target_fp": inp.get("fp", ""), "value": value, "key": key, "press_enter": bool(self.cfg.enter_submit)}
        return Plan("fill-field", "\n".join(L), "input for %r" % key, ctx=ctx, timeout=45.0, match=0.5)

    def _plan_challenge(self) -> Plan:
        polls = max(8, int(self.cfg.challenge_patience // 3))
        # FIXED: Replaced q(JS_CHALLENGE_CHECK) with json.dumps to avoid truncation
        L = ['async def strategy(page, ctx, log, S):', '    # plan: wait-out-challenge', '    for i in range(%d):' % polls, '        await wait(3.0)', '        try: st = await page.evaluate(%s)' % json.dumps(JS_CHALLENGE_CHECK), '        except Exception as e: log("poll error: " + str(e)[:80]); continue', '        log("poll " + str(i) + " title=" + st["t"][:40] + " cf=" + str(st["cf"]))', '        if ("just a moment" not in st["t"].lower()) and (not st["cf"]): await wait(1.0); return {"ok": True, "how": "challenge:cleared"}', '        try: await page.mouse.move(300 + (i * 37) % 400, 260 + (i * 23) % 200, steps=4)', '        except Exception: pass', '    return {"ok": False, "why": "challenge still present", "how": "challenge:persisted"}']
        return Plan("wait-out-challenge", "\n".join(L), "cloudflare interstitial", ctx={}, timeout=float(polls * 3 + 20), match=0.0)

    def _plan_shake(self) -> Plan:
        L = ['async def strategy(page, ctx, log, S):', '    try: await page.mouse.wheel(0, 700)', '    except Exception: pass', '    await wait(1.2)', '    try: await page.mouse.wheel(0, -350)', '    except Exception: pass', '    await wait(0.8)', '    return {"ok": True, "how": "shake:scrolled"}']
        return Plan("shake-viewport", "\n".join(L), "scroll probe", ctx={}, timeout=25.0, match=0.0)

    def _plan_reload(self, subgoal) -> Plan:
        L = ['async def strategy(page, ctx, log, S):', '    await page.reload(wait_until="domcontentloaded", timeout=45000)', '    await wait(2.0)', '    return {"ok": True, "how": "page:reloaded"}']
        return Plan("hard-reset", "\n".join(L), "reload", ctx={}, timeout=60.0, match=0.0)

    def _plan_idle(self, seconds) -> Plan:
        L = ['async def strategy(page, ctx, log, S):', '    await wait(float(S.get("seconds", 4.0)))', '    return {"ok": True, "how": "idle:waited"}']
        return Plan("idle-observe", "\n".join(L), "pause", ctx={"seconds": float(seconds)}, timeout=float(seconds) + 10.0, match=0.0)

# 5. PLAYGROUND

FORBIDDEN_NAMES = frozenset(("__import__", "eval", "exec", "compile", "open", "input", "getattr", "setattr", "delattr", "vars", "globals", "locals", "breakpoint", "help", "exit", "quit", "memoryview", "type", "object", "super", "classmethod", "staticmethod", "__builtins__"))
SAFE_BUILTINS = {n: getattr(_py_builtins, n) for n in ("abs", "all", "any", "bool", "dict", "divmod", "enumerate", "Exception", "BaseException", "float", "format", "int", "isinstance", "len", "list", "map", "max", "min", "range", "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip", "ValueError", "KeyError", "TypeError", "IndexError", "AttributeError", "RuntimeError", "StopIteration", "ArithmeticError", "ZeroDivisionError")}

def _screen_code(code: str):
    tree = ast.parse(code)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.AsyncFunctionDef) or tree.body[0].name != "strategy":
        raise ValueError("code must define exactly one `async def strategy(...)`")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)): raise ValueError("forbidden construct: %s" % type(node).__name__)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES: raise ValueError("forbidden name: %r" % node.id)
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"): raise ValueError("dunder attribute access is blocked")
    return tree

async def _sandbox_wait(seconds):
    try: s = float(seconds)
    except Exception: s = 1.0
    await asyncio.sleep(max(0.0, min(s, 30.0)))

async def _sandbox_js_click(target, spec):
    try: return bool(await target.evaluate(JS_CLICK, dict(spec or {})))
    except Exception: return False

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
        try: tree = _screen_code(plan.code)
        except Exception as e:
            return ExecutionResult(False, plan.name + ":compile", "compile/screen: %s" % str(e)[:160], logs, "CompileError", time.time() - t0, "")
        ns = {"__builtins__": SAFE_BUILTINS, "wait": _sandbox_wait, "js_click": _sandbox_js_click}
        try:
            exec(compile(tree, "<generated>", "exec"), ns)
            fn = ns["strategy"]
        except Exception as e:
            return ExecutionResult(False, plan.name + ":define", "define: %s" % str(e)[:160], logs, type(e).__name__, time.time() - t0, traceback.format_exc()[-400:])
        try:
            raw = await asyncio.wait_for(fn(self.page, self.context, log, dict(plan.ctx)), timeout=plan.timeout)
        except asyncio.TimeoutError:
            return ExecutionResult(False, plan.name + ":timeout", "exceeded %.0fs sandbox budget" % plan.timeout, logs, "TimeoutError", time.time() - t0, "")
        except Exception as e:
            return ExecutionResult(False, plan.name + ":error", "%s: %s" % (type(e).__name__, str(e)[:180]), logs, type(e).__name__, time.time() - t0, traceback.format_exc()[-400:])
        res = raw if isinstance(raw, dict) else {"ok": bool(raw)}
        return ExecutionResult(bool(res.get("ok")), str(res.get("how") or plan.name), str(res.get("why", "")), logs, None, time.time() - t0, "")

# 6. REFLECT

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
    if res.ok: return None
    blob = (res.why or "") + " " + (res.tail or "")
    for rx, tag in ERROR_PATTERNS:
        if rx.search(blob): return tag
    return "other"

@dataclass
class Reflection:
    outcome: str
    changed: bool
    delta: dict
    satisfied: bool
    error_sig: str

class Reflector:
    def __init__(self, log: logging.Logger):
        self.log = log

    def reflect(self, pre, plan, res, post, subgoal, memory) -> Reflection:
        d = self._diff(pre, post)
        if res.ok and d["changed"]: outcome = "progress"
        elif res.ok: outcome = "futile"
        else: outcome = "error"
        esig = _error_sig(res)
        tf = plan.ctx.get("target_fp", "")
        pb, gb = page_key(pre), goal_key(subgoal)

        if tf:
            st = memory.elem.setdefault(tf, {"tries": 0, "wins": 0, "fails": 0})
            st["tries"] += 1
            if outcome == "progress": st["wins"] += 1
            else: st["fails"] += 1

        how = res.how or ""
        tier = how.split(":", 1)[1] if ":" in how else how
        if tier and outcome != "progress":
            k = "|".join((pb, gb, tier))
            memory.sel_fails[k] = int(memory.sel_fails.get(k, 0)) + 1

        wkey = pb + "|" + gb
        if outcome == "progress" and plan.name.startswith(("click", "fill", "overlay", "dismiss", "explore")):
            prev_n = int(memory.wins.get(wkey, {}).get("n", 0))
            memory.wins[wkey] = {"code": plan.code, "how": how, "target_fp": tf, "n": prev_n + 1, "fails": 0}
            if plan.name.startswith("fill") and tf: memory.filled.add(pb + "|" + tf)
        elif plan.name == "reuse-learned" and wkey in memory.wins:
            w = memory.wins[wkey]
            if outcome == "progress": w["n"] = int(w.get("n", 0)) + 1
            else: w["fails"] = int(w.get("fails", 0)) + 1

        return Reflection(outcome, d["changed"], d, self._satisfied(plan, outcome, post), esig)

    def _diff(self, pre, post):
        pfps = {e.get("fp") for e in pre.get("elements", [])}
        qfps = {e.get("fp") for e in post.get("elements", [])}
        d = {"url": pre.get("url") != post.get("url"), "title": pre.get("title") != post.get("title"), "added": len(qfps - pfps), "removed": len(pfps - qfps), "text": pre.get("text") != post.get("text")}
        d["changed"] = bool(d["url"] or d["title"] or d["added"] or d["removed"] or d["text"])
        return d

    def _satisfied(self, plan, outcome, post):
        if "challenge" in plan.name: return not post.get("flags", {}).get("cloudflare", False)
        if plan.match < 0.22 or outcome != "progress": return False
        return True

# 7. MEMORY

class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.elem = {}
        self.wins = {}
        self.sel_fails = {}
        self.filled = set()
        self.cycles = 0
        self.sessions = 0

    def load(self):
        if not self.path.exists(): return
        try: raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            try: self.path.replace(self.path.with_suffix(".corrupt"))
            except Exception: pass
            return
        try:
            self.elem = raw.get("elem") or {}
            self.wins = raw.get("wins") or {}
            self.sel_fails = raw.get("sel_fails") or {}
            self.filled = set(raw.get("filled") or [])
            self.cycles = int(raw.get("cycles", 0))
            self.sessions = int(raw.get("sessions", 0))
        except Exception as e: print("memory partially unreadable: %s" % e, file=sys.stderr)

    def trim(self):
        if len(self.elem) > 4000: self.elem = dict(sorted(self.elem.items(), key=lambda kv: kv[1].get("tries", 0))[-4000:])
        if len(self.sel_fails) > 2000: self.sel_fails = dict(sorted(self.sel_fails.items(), key=lambda kv: kv[1])[-2000:])
        if len(self.wins) > 300: self.wins = dict(sorted(self.wins.items(), key=lambda kv: kv[1].get("n", 0))[-300:])

    def save(self):
        try:
            self.trim()
            data = {"elem": self.elem, "wins": self.wins, "sel_fails": self.sel_fails, "filled": sorted(self.filled), "cycles": self.cycles, "sessions": self.sessions}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e: print("memory save failed: %s" % e, file=sys.stderr)

# 8. AGENT

class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = build_logger(cfg)
        self.memory = Memory(Path(cfg.memory_path))
        self.memory.load()
        self.reasoner = Reasoner(cfg, self.log)
        self.reflector = Reflector(self.log)
        self.playground = Playground(cfg, self.log)
        self.subgoals = [s.strip() for s in cfg.goal.split(";") if s.strip()] or ["interact with the page"]
        self.idx = 0
        self.done = False
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
        self.log.info("boot | url=%s", cfg.url)
        self.log.info("goal pipeline: %s", " -> ".join(self.subgoals))
        if cfg.fields: self.log.info("field values provided for keys: %s (values never logged)", ", ".join(sorted(cfg.fields)))
        async with async_playwright() as pw:
            browser = None
            common = dict(headless=cfg.headless, args=_chromium_args(cfg), slow_mo=cfg.slowmo)
            if cfg.proxy: common["proxy"] = {"server": cfg.proxy}
            try:
                if cfg.user_data_dir:
                    self.context = await pw.chromium.launch_persistent_context(cfg.user_data_dir, viewport={"width": cfg.viewport_w, "height": cfg.viewport_h}, ignore_https_errors=True, **common)
                else:
                    browser = await pw.chromium.launch(**common)
                    self.context = await browser.new_context(viewport={"width": cfg.viewport_w, "height": cfg.viewport_h}, ignore_https_errors=True, user_agent=cfg.user_agent)
            except Exception as e:
                self.log.error("browser launch failed: %s", e)
                return
            await self.context.add_init_script(JS_STEALTH)
            self.page = (self.context.pages[0] if self.context.pages else await self.context.new_page())
            self.page.set_default_timeout(15000)
            self._attach(self.page)
            if cfg.popup_help: self.context.on("page", self._spawn_popup_help)
            if not await self._goto(): return
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try: loop.add_signal_handler(s, self.stop.update, {"flag": True})
                except (NotImplementedError, RuntimeError): pass
            t0 = time.time()
            try: await self._loop(t0)
            finally:
                self.memory.save()
                self.log.info("memory persisted: %d winning snippets, %d element stats, %d cycles lifetime", len(self.memory.wins), len(self.memory.elem), self.memory.cycles)
                try:
                    await self.context.close()
                    if browser: await browser.close()
                except Exception: pass
        self.log.info("playground shut down cleanly")

    async def _loop(self, t0):
        cfg = self.cfg
        challenge_fails = 0
        while not self.stop["flag"]:
            if cfg.max_cycles and self.cycle >= cfg.max_cycles: self.log.info("cycle budget reached"); break
            if cfg.max_minutes and (time.time() - t0) / 60.0 >= cfg.max_minutes: self.log.info("time budget reached"); break
            if Path(cfg.stop_file).exists(): self.log.info("stop-file detected"); break
            if self.page.is_closed(): await self._recover(); continue

            pre = await self.probe()
            if cfg.dump_states: self._dump_state(pre)
            self._log_probe(pre)

            subgoal = self._subgoal()
            self.log.info("CYCLE  | #%d | goal[%d/%d]: %r", self.cycle, min(self.idx + 1, len(self.subgoals)), len(self.subgoals), subgoal)
            plan = self.reasoner.think(pre, self.history, subgoal, self.memory, self.stuck.get(subgoal, 0))
            self.log.info("REASON | plan=%s | target=%r | match=%.2f", plan.name, plan.desc, plan.match)
            self._emit_code(plan)

            self.playground.bind(self.page, self.context)
            res = await self.playground.run(plan)
            self.log.info("EXECUTE| %s | how=%s | %.1fs", "ok" if res.ok else "FAIL", res.how, res.duration)

            post = await self.probe()
            refl = self.reflector.reflect(pre, plan, res, post, subgoal, self.memory)
            self.history.append({"cycle": self.cycle, "plan": plan.name, "how": res.how, "outcome": refl.outcome, "error_sig": refl.error_sig, "changed": refl.changed, "used_win": plan.name == "reuse-learned", "target_fp": plan.ctx.get("target_fp", "")})
            d = refl.delta
            self.log.info("REFLECT| outcome=%s | +%d/-%d controls | url_changed=%s | satisfied=%s%s", refl.outcome, d["added"], d["removed"], "yes" if d["url"] else "no", "yes" if refl.satisfied else "no", (" | error=" + refl.error_sig) if refl.error_sig else "")

            if pre["flags"]["cloudflare"] and post["flags"]["cloudflare"]:
                challenge_fails += 1
                if challenge_fails >= 3:
                    self.log.error("challenge never cleared; try --headed or a persistent profile")
                    break
            else: challenge_fails = 0

            if refl.satisfied:
                self.stuck[subgoal] = 0
                self.log.info("GOAL   | %r satisfied", subgoal)
                self.idx += 1
                if self.idx >= len(self.subgoals):
                    if cfg.after_goal == "exit":
                        self.log.info("PIPELINE COMPLETE after %d cycles", self.cycle)
                        break
                    self.done = True
                    self.log.info("PIPELINE COMPLETE | free exploration mode")
            else:
                self.stuck[subgoal] = self.stuck.get(subgoal, 0) + 1
                if self.stuck[subgoal] >= 8:
                    self.log.warning("GOAL   | %r is stuck; skipping it", subgoal)
                    self.stuck[subgoal] = 0
                    self.idx += 1
                    if self.idx >= len(self.subgoals):
                        if cfg.after_goal == "exit": self.log.info("pipeline finished (with skips) after %d cycles", self.cycle); break
                        self.done = True

            self.memory.cycles += 1
            if self.cycle % 10 == 0: self.memory.save()
            await self._pace(plan)

    def _subgoal(self):
        if self.done: return "explore something new"
        return self.subgoals[min(self.idx, len(self.subgoals) - 1)]

    async def probe(self) -> dict:
        self.cycle += 1
        try: state = await probe_page(self.page, self.cycle)
        except Exception as e:
            self.log.warning("PROBE  | failed: %s", str(e)[:100])
            state = {"cycle": self.cycle, "url": "", "title": "", "frames": [], "elements": [], "inputs": [], "flags": {"cloudflare": False, "turnstile": False, "wallet": False, "canvas": False, "password": False}, "text": "", "fingerprint": ""}
        state["flags"]["console_errors"] = self._console_errors
        self._console_errors = 0
        return state

    def _log_probe(self, state):
        f = state["flags"]
        try:
            u = urlsplit(state.get("url") or "")
            short = (u.netloc + u.path) or "?"
        except Exception: short = "?"
        self.log.info("PROBE  | c%d %s | frames=%d controls=%d inputs=%d cf=%s wallet=%s", state["cycle"], short, len(state["frames"]), len(state["elements"]), len(state["inputs"]), "yes" if f["cloudflare"] else "no", "yes" if f["wallet"] else "no")

    def _emit_code(self, plan):
        if self.cfg.code_log == "off": return
        n = plan.code.count("\n") + 1
        if self.cfg.code_log == "both": self.log.info("CODE   | %d lines of generated Playwright:\n%s\n----- end code", n, plan.code)
        else: self.log.debug("CODE   | %d lines:\n%s\n----- end code", n, plan.code)

    def _dump_state(self, state):
        try:
            d = Path("states")
            d.mkdir(exist_ok=True)
            (d / ("state_%05d.json" % self.cycle)).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            for old in sorted(d.glob("state_*.json"))[:-200]:
                try: old.unlink()
                except Exception: pass
        except Exception: pass

    async def _pace(self, plan):
        if plan.name.startswith(("click", "fill", "reuse", "overlay", "dismiss", "explore")): await asyncio.sleep(random.uniform(0.8, 1.8))
        else: await asyncio.sleep(0.4)

    async def _goto(self):
        for attempt in range(3):
            try:
                await self.page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=60000)
                try: await self.page.wait_for_load_state("networkidle", timeout=8000)
                except Exception: pass
                return True
            except Exception as e:
                self.log.warning("navigation failed (%d/3): %s", attempt + 1, str(e)[:120])
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
            if dialog.type == "beforeunload": await dialog.dismiss(); return
            if RISKY_RE.search(msg) and not self.cfg.allow_signing:
                self.log.warning("DIALOG | denied (fund-safety): %r", msg[:80])
                await dialog.dismiss()
            else:
                self.log.info("DIALOG | accepted: %r", msg[:80])
                await dialog.accept()
        except Exception: pass

    def _on_console(self, m):
        try:
            if m.type == "error": self._console_errors += 1
        except Exception: pass

    def _spawn_popup_help(self, p):
        try: asyncio.get_running_loop().create_task(self._help_popup(p))
        except Exception: pass

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
        except Exception: pass

# 9. MAIN

def main():
    cfg = parse_args()
    if cfg.seed >= 0: random.seed(cfg.seed)
    agent = Agent(cfg)
    try: asyncio.run(agent.run())
    except KeyboardInterrupt: print("\ninterrupted -- memory was saved on shutdown")

if __name__ == "__main__":
    main()
