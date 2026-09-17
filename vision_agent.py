#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vision_agent.py (v3.2) -- teacher/memory farm agent + Cloudflare interstitial wait.

v3.2: the old bypass, spelled out. The interstitial auto-clears in seconds;
the agent waits it out at login AND in the loop before doing anything.
v3.1: CF gate (never click/teach on a wall; exit clean if truly stuck),
ghost-scene forgetting, nav-dud budget, edge-triggered teacher calls,
reload discipline, remembered-CF-scenes never match again.

Run:
    python vision_agent.py "https://runiverseidle.com/forge" --provider gemini
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
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
import warnings
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.async_api import async_playwright

warnings.filterwarnings("ignore", message=".*getdata.*",
                        category=DeprecationWarning)

__version__ = "3.2.0"

EMAIL_LOC = ('input[type="email"], input[name*="mail" i], '
             'input[autocomplete="email"], input[placeholder*="mail" i], '
             'input[id*="mail" i], input[name*="user" i], '
             'input[placeholder*="user" i], input[name*="login" i], '
             'input[name*="email" i]')


def _load_credentials():
    email = os.environ.get("GAME_EMAIL", "")
    password = os.environ.get("GAME_PASSWORD", "")
    if not email or not password:
        try:
            with open("creds.json", "r", encoding="utf-8") as f:
                d = json.load(f)
            email = email or str(d.get("email") or "")
            password = password or str(d.get("password") or "")
        except Exception:
            pass
    return email, password


EMAIL, PASSWORD = _load_credentials()

# ================ 1. CONFIG ================

try:
    _RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    _RESAMPLE = Image.BILINEAR

MAX_IMG_W = 1400
DH_SIZE = 8
STOP_FILE = ".vision_agent_stop"
ROLES = ("GATHER", "CRAFT", "SELL", "NAV", "UI")
GOAL_TEXT = {
    "GATHER": "grow resources (mine, chop, collect, harvest, claim)",
    "CRAFT": "turn resources into items (craft, forge, smelt, combine)",
    "SELL": "turn items into currency (sell, list at market)",
    "EXPLORE": "try something not yet tried to learn the game",
}
DEST_WORDS = {
    "GATHER": ("map", "world", "field", "forest", "mine", "adventure",
               "quest", "land", "expedition", "realm"),
    "CRAFT": ("forge", "craft", "smith", "smelt", "workshop", "anvil",
              "build"),
    "SELL": ("market", "shop", "trade", "sell", "merchant", "auction",
             "bazaar"),
}
CF_TITLE_RE = re.compile(r"just a moment|attention required|checking your "
                         r"browser|security verification|verify you are "
                         r"human", re.I)
JS_CF = (r"""() => !!(document.querySelector(
  '#challenge-form, .cf-turnstile, [class*="cf-chl"], #cf-challenge-running,'
  + ' iframe[src*="challenges.cloudflare.com"], #cf-spinner-verify,'
  + ' #turnstile-wrapper, [class*="cf-turnstile"]'))""")


@dataclass
class Config:
    url: str = ""
    provider: str = "auto"
    model: str = ""
    api_key: str = ""
    headless: bool = True
    viewport_w: int = 1440
    viewport_h: int = 900
    user_data_dir: str = ""
    session_file: str = "session.json"
    slowmo: float = 0.0
    interval: float = 15.0
    max_calls: int = 250
    day_cap: int = 250
    verify_every: int = 8
    cf_patience: float = 240.0
    max_minutes: float = 0.0
    memory_path: str = "vision_memory.json"
    log_path: str = "vision_agent.log"
    log_level: str = "INFO"
    start_state: str = "GATHER"
    pure_vision: bool = False
    no_require: bool = False
    save_shots: bool = True
    user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    seed: int = -1


def parse_args() -> Config:
    ap = argparse.ArgumentParser(
        description="Teacher/memory farm agent with a Cloudflare gate.")
    ap.add_argument("url", nargs="?", default=os.environ.get("GAME_URL", ""))
    ap.add_argument("--provider", choices=["auto", "gemini", "openai"],
                    default="auto")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--viewport", default="1440x900")
    ap.add_argument("--user-data-dir", default="")
    ap.add_argument("--session-file", default="session.json")
    ap.add_argument("--slowmo", type=float, default=0.0)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--max-calls", type=int, default=250)
    ap.add_argument("--day-cap", type=int, default=250)
    ap.add_argument("--verify-every", type=int, default=8)
    ap.add_argument("--cf-patience", type=float, default=240.0,
                    help="seconds to wait out a wall before exiting for "
                         "the next scheduled run")
    ap.add_argument("--max-minutes", type=float, default=0.0)
    ap.add_argument("--memory", default="vision_memory.json")
    ap.add_argument("--log-file", default="vision_agent.log")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--start-state", choices=["GATHER", "CRAFT", "SELL"],
                    default="GATHER")
    ap.add_argument("--pure-vision", action="store_true")
    ap.add_argument("--no-require", action="store_true")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--seed", type=int, default=-1)
    a = ap.parse_args()
    try:
        w, h = a.viewport.lower().split("x")
        vw, vh = int(w), int(h)
    except Exception:
        ap.error("--viewport must look like 1440x900")
    cfg = Config(url=a.url.strip(), provider=a.provider, model=a.model.strip(),
                 api_key=a.api_key.strip(), headless=not a.headed,
                 viewport_w=vw, viewport_h=vh,
                 user_data_dir=a.user_data_dir.strip(),
                 session_file=a.session_file.strip(), slowmo=a.slowmo,
                 interval=a.interval, max_calls=a.max_calls,
                 day_cap=a.day_cap, verify_every=a.verify_every,
                 cf_patience=a.cf_patience, max_minutes=a.max_minutes,
                 memory_path=a.memory, log_path=a.log_file,
                 log_level=a.log_level, start_state=a.start_state,
                 pure_vision=a.pure_vision, no_require=a.no_require,
                 save_shots=not a.no_shots, seed=a.seed)
    if not cfg.url.startswith(("http://", "https://")):
        ap.error("a game URL is required (positional arg or $GAME_URL)")
    return cfg


def build_logger(cfg: Config) -> logging.Logger:
    log = logging.getLogger("vision-agent")
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
    return ["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio",
            "--disable-blink-features=AutomationControlled",
            "--window-size=%d,%d" % (cfg.viewport_w, cfg.viewport_h)]


JS_STEALTH = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
})();
"""

JS_RECTS = r"""() => {
  const out = [];
  const SEL = 'button, a[href], [role="button"], [role="tab"], [role="menuitem"],'
    + ' [role="option"], [role="switch"], input[type="button"], input[type="submit"],'
    + ' summary, [onclick]';
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 18) continue;
    if (r.x < -4 || r.y < -4 || r.x + r.width > innerWidth + 4
        || r.y + r.height > innerHeight + 4) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none'
        || parseFloat(s.opacity || '1') < 0.05) continue;
    let cls = '';
    try { cls = (typeof el.className === 'string' ? el.className : ''); } catch (e) {}
    out.push({x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              disabled: !!(el.disabled
                  || el.getAttribute('aria-disabled') === 'true'
                  || /(^|[\s_-])(disabled|is-disabled)([\s_-]|$)/i.test(cls))});
    if (out.length >= 80) break;
  }
  return out;
}"""

JS_TEXT = r"""() => {
  try {
    return ((document.body && document.body.innerText) || '')
      .replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').slice(0, 14000);
  } catch (e) { return ''; }
}"""

RISKY_RE = re.compile(r"\b(approve|transfer|withdraw|burn|spend|swap|mint|"
                      r"send|sign(?:\s+transaction)?)\b", re.I)

# ================ 2. LOCAL VISION (free, per-cycle) ================

def dhash(img, size=DH_SIZE):
    g = img.convert("L").resize((size + 1, size), _RESAMPLE)
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        row = px[r * (size + 1):(r + 1) * (size + 1)]
        for c in range(size):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


_FONT_CACHE = {}


def _font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    paths = ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
             "/System/Library/Fonts/Helvetica.ttc",
             "C:\\Windows\\Fonts\\arialbd.ttf")
    f = None
    for p in paths:
        try:
            f = ImageFont.truetype(p, size)
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size)
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _components(mask, sw, sh):
    seen = bytearray(sw * sh)
    comps = []
    for i in range(sw * sh):
        if not mask[i] or seen[i]:
            continue
        stack, seen[i] = [i], 1
        minx = maxx = i % sw
        miny = maxy = i // sw
        area = 0
        while stack:
            p = stack.pop()
            area += 1
            x, y = p % sw, p // sw
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < sw and 0 <= ny < sh:
                    q = ny * sw + nx
                    if mask[q] and not seen[q]:
                        seen[q] = 1
                        stack.append(q)
        comps.append((minx, miny, maxx, maxy, area))
    return comps


def detect_chips(img, max_chips=18):
    W, H = img.size
    sw, sh = max(64, W // 8), max(40, H // 8)
    small = img.convert("RGB").resize((sw, sh), _RESAMPLE)
    hsv = small.convert("HSV")
    _h, s, v = hsv.split()
    edges = v.filter(ImageFilter.FIND_EDGES)
    sd, ed = list(s.getdata()), list(edges.getdata())
    scores = [(0.55 * sd[i] + 0.45 * ed[i]) / 255.0 for i in range(sw * sh)]
    ordered = sorted(scores)
    thr = max(0.16, ordered[int(0.84 * len(ordered))])
    mask = [1 if (sc >= thr and sc > 0.16) else 0 for sc in scores]
    grown = bytearray(mask)
    for y in range(sh):
        base = y * sw
        for x in range(sw):
            if mask[base + x]:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1),
                               (-1, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < sw and 0 <= ny < sh:
                        grown[ny * sw + nx] = 1
    mask = list(grown)
    sx, sy = W / sw, H / sh
    boxes = []
    for (minx, miny, maxx, maxy, area) in _components(mask, sw, sh):
        if area < 10:
            continue
        x, y = int(minx * sx), int(miny * sy)
        w, h = int((maxx - minx + 1) * sx), int((maxy - miny + 1) * sy)
        if w < 30 or h < 22 or w > W * 0.9 or h > H * 0.6:
            continue
        if not (0.15 < w / max(1, h) < 10):
            continue
        msum = 0.0
        for yy in range(miny, maxy + 1):
            for xx in range(minx, maxx + 1):
                msum += scores[yy * sw + xx]
        boxes.append({"x": x, "y": y, "w": w, "h": h,
                      "score": min(1.0, msum / max(1, area))})
    return _merge_boxes(boxes, max_chips)


def _iou(a, b):
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return ((x2 - x1) * (y2 - y1)) / max(1, min(a["w"] * a["h"],
                                                b["w"] * b["h"]))


def _merge_boxes(boxes, limit):
    boxes = sorted(boxes, key=lambda b: -(b["score"] * (b["w"] * b["h"]) ** 0.5))
    out = []
    for b in boxes:
        if any(_iou(b, o) > 0.55 for o in out):
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def prepare(img):
    W, H = img.size
    if W <= MAX_IMG_W:
        return img, 1.0
    f = MAX_IMG_W / float(W)
    return img.resize((MAX_IMG_W, int(H * f)), _RESAMPLE), f


def annotate(img, chips):
    W, H = img.size
    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    top, left = int(H * 0.14), int(W * 0.15)
    bottom = int(H * 0.88)
    f_big, f_small = _font(20), _font(13)
    d.rectangle([0, 0, W, top], fill=(255, 40, 40, 26))
    d.rectangle([0, 0, left, H], fill=(255, 40, 40, 22))
    d.rectangle([0, bottom, W, H], fill=(255, 40, 40, 22))
    d.text((8, 6), "NAV ZONE", font=f_small, fill=(255, 60, 60, 255))
    d.text((8, top + 6), "ACTION ZONE", font=f_small, fill=(60, 255, 120, 255))
    for x in range(0, W, 100):
        major = (x % 200 == 0)
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, 90 if major else 40))
        if major and x:
            d.text((x + 3, top + 26), str(x), font=f_small,
                   fill=(255, 255, 255, 200))
    for y in range(0, H, 100):
        major = (y % 200 == 0)
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 90 if major else 40))
        if major and y:
            d.text((left + 6, y + 3), str(y), font=f_small,
                   fill=(255, 255, 255, 200))
    for i, c in enumerate(chips, 1):
        x, y, w, h = c["x"], c["y"], c["w"], c["h"]
        d.rectangle([x, y, x + w, y + h], outline=(50, 255, 50, 255), width=3)
        cx, cy = x + w // 2, y + h // 2
        d.line([(cx - 5, cy), (cx + 5, cy)], fill=(50, 255, 50, 255), width=2)
        d.line([(cx, cy - 5), (cx, cy + 5)], fill=(50, 255, 50, 255), width=2)
        try:
            d.rounded_rectangle([x + 2, y + 2, x + 34, y + 24], radius=6,
                                fill=(50, 255, 50, 235))
        except Exception:
            d.rectangle([x + 2, y + 2, x + 34, y + 24], fill=(50, 255, 50, 235))
        d.text((x + 9, y + 3), str(i), font=f_big, fill=(0, 0, 0, 255))
    return Image.alpha_composite(base, ov).convert("RGB")


def pixel_diff(a, b):
    size = (160, 100)
    da = list(a.convert("L").resize(size, _RESAMPLE).getdata())
    db = list(b.convert("L").resize(size, _RESAMPLE).getdata())
    return sum(abs(x - y) for x, y in zip(da, db)) / float(len(da))


def region_diff(a, b, box):
    W, H = a.size
    x, y, w, h = box
    x0, y0 = max(0, x - 90), max(0, y - 90)
    x1, y1 = min(W, x + w + 90), min(H, y + h + 90)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    ca = a.convert("L").crop((x0, y0, x1, y1)).resize((80, 50), _RESAMPLE)
    cb = b.convert("L").crop((x0, y0, x1, y1)).resize((80, 50), _RESAMPLE)
    la, lb = list(ca.getdata()), list(cb.getdata())
    return sum(abs(p - q) for p, q in zip(la, lb)) / float(len(la))


def dead_key(scene_hex, x, y):
    if scene_hex:
        return "%s:%d:%d" % (scene_hex, x // 30, y // 30)
    return "*:%d:%d" % (x // 30, y // 30)

# ================ 3. VLM CLIENT ================

class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class Transient(Exception):
    pass


class BadConfig(Exception):
    pass


class GeminiVision:
    NAME = "gemini"
    STATIC_CHAIN = ("gemini-2.5-flash", "gemini-2.0-flash",
                    "gemini-1.5-flash", "gemini-1.5-pro")
    _RECOMMEND_RE = re.compile(r"use\s+models/([A-Za-z0-9._\-]+)", re.I)
    _EXCLUDE = ("embedding", "aqa", "imagen", "veo", "tts",
                "image-generation", "learnlm", "-live", "audio", "gemma")

    def __init__(self, key, model):
        self.key = key
        self._explicit = bool(model)
        self.models = [model] if model else list(self.STATIC_CHAIN)
        self.model = self.models[0]
        self._log = logging.getLogger("vision-agent")
        self._think_ok = {}

    def discover_models(self):
        url = ("https://generativelanguage.googleapis.com/v1beta/models"
               "?pageSize=100")
        names, token, pages = [], None, 0
        try:
            while pages < 3:
                pages += 1
                req = urllib.request.Request(
                    url + (("&pageToken=" + token) if token else ""),
                    headers={"x-goog-api-key": self.key}, method="GET")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                for m in data.get("models") or []:
                    name = (m.get("name") or "").replace("models/", "")
                    if "generateContent" not in (m.get(
                            "supportedGenerationMethods") or []):
                        continue
                    if any(b in name.lower() for bad in self._EXCLUDE):
                        continue
                    if name:
                        names.append(name)
                token = data.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            self._log.warning("discovery failed: %s", str(e)[:120])
            return []
        names = list(dict.fromkeys(names))
        names.sort(key=self._pref)
        return names[:6]

    @staticmethod
    def _ver(name):
        m = re.search(r"gemini-(\d+)(?:[.-](\d+))?", name)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

    @staticmethod
    def _pref(n):
        nl = n.lower()
        v = GeminiVision._ver(n)
        pen = 0
        if "flash" in nl:
            pen -= 100
        elif "pro" in nl:
            pen += 20
        if "lite" in nl:
            pen += 30
        if "exp" in nl or "preview" in nl:
            pen += 40
        if "thinking" in nl:
            pen += 50
        if "latest" in nl:
            pen -= 10
        return (pen, -v[0], -v[1])

    def install_discovered(self, found):
        if not found:
            return
        head = self.models[:1] if self._explicit else []
        seen, merged = set(), []
        for m in head + found + self.models:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)
        self.models = merged
        self.model = merged[0]

    def _post(self, b64, prompt, model, think=True):
        gc = {"temperature": 0.2, "max_output_tokens": 2048,
              "response_mime_type": "application/json"}
        if think:
            gc["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
            "generationConfig": gc}
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "%s:generateContent" % model,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": self.key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:220]
            except Exception:
                detail = ""
            if e.code == 429:
                ra = e.headers.get("retry-after")
                try:
                    ra = float(ra) if ra else None
                except ValueError:
                    ra = None
                raise RateLimited(ra)
            if e.code >= 500:
                raise Transient("http %d" % e.code)
            low = detail.lower()
            if think and e.code == 400 and ("thinking" in low
                                            or "unknown name" in low):
                self._think_ok[model] = False
                return self._post(b64, prompt, model, think=False)
            raise BadConfig("http %d: %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Transient(str(e)[:160])
        cands = data.get("candidates") or []
        if not cands:
            raise Transient("no candidates")
        for p in (cands[0].get("content") or {}).get("parts") or []:
            if p.get("text"):
                return p["text"]
        raise Transient("empty response")

    def _ask_sync(self, jpeg_bytes, prompt):
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        errors, tried = [], set()
        queue = list(self.models)
        i = 0
        while i < len(queue) and len(tried) < 8:
            model = queue[i]
            i += 1
            if not model or model in tried:
                continue
            tried.add(model)
            for attempt in (1, 2):
                try:
                    out = self._post(b64, prompt, model,
                                     self._think_ok.get(model, True))
                    if self.model != model:
                        self.model = model
                    return out
                except BadConfig as e:
                    msg = str(e)
                    errors.append("%s -> %s" % (model, msg[:140]))
                    m = self._RECOMMEND_RE.search(msg)
                    if m and m.group(1) not in tried \
                            and m.group(1) not in queue:
                        queue.insert(i, m.group(1))
                    break
                except Transient as e:
                    errors.append("%s -> %s" % (model, str(e)[:140]))
                    if attempt == 1:
                        time.sleep(2.5)
        raise Transient("; ".join(errors[-3:]) or "no models tried")

    async def ask(self, jpeg_bytes, prompt):
        return await asyncio.to_thread(self._ask_sync, jpeg_bytes, prompt)


class OpenAIVision:
    NAME = "openai"

    def __init__(self, key, model):
        self.key = key
        chain = ("gpt-4o-mini", "gpt-4o")
        self.models = ([model] + [m for m in chain if m != model]) if model \
            else list(chain)
        self.model = self.models[0]

    def _post(self, b64, prompt, model):
        body = {"model": model, "max_tokens": 2048, "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + b64,
                        "detail": "high"}}]}]}
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:220]
            except Exception:
                detail = ""
            if e.code == 429:
                ra = e.headers.get("retry-after")
                try:
                    ra = float(ra) if ra else None
                except ValueError:
 ra = None
                raise RateLimited(ra)
            if e.code >= 500:
                raise Transient("http %d" % e.code)
            raise BadConfig("http %d: %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Transient(str(e)[:160])
        try:
            return data["choices"][0]["message"]["content"] or ""
        except Exception:
            raise Transient("malformed response")

    def _ask_sync(self, jpeg_bytes, prompt):
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        errors = []
        for model in self.models:
            for attempt in (1, 2):
                try:
                    out = self._post(b64, prompt, model)
                    if self.model != model:
                        self.model = model
                    return out
                except BadConfig as e:
                    errors.append("%s -> %s" % (model, e))
                    break
                except Transient as e:
                    errors.append("%s -> %s" % (model, e))
                    if attempt == 1:
                        time.sleep(2.5)
        raise Transient("; ".join(errors[-3:]))

    async def ask(self, jpeg_bytes, prompt):
        return await asyncio.to_thread(self._ask_sync, jpeg_bytes, prompt)


def make_vlm(cfg, log):
    key = cfg.api_key
    provider = cfg.provider
    if provider == "auto":
        gk = key or os.environ.get("GEMINI_API_KEY") or \
            os.environ.get("GOOGLE_API_KEY")
        if gk:
            provider, key = "gemini", gk
        elif key or os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
            key = key or os.environ.get("OPENAI_API_KEY")
        else:
            raise SystemExit("no API key: set GEMINI_API_KEY or OPENAI_API_KEY")
    else:
        env = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
        key = key or os.environ.get(env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("missing $%s" % env)
    v = GeminiVision(key, cfg.model) if provider == "gemini" \
        else OpenAIVision(key, cfg.model)
    log.info("provider: %s | chain: %s", v.NAME, " -> ".join(v.models[:4]))
    if isinstance(v, GeminiVision):
        found = v.discover_models()
        if found:
            v.install_discovered(found)
            log.info("live discovery: %d models | chain now: %s",
                     len(found), " -> ".join(v.models[:4]))
    return v


class Governor:
    def __init__(self, log, interval, run_cap, day_cap, day_count=0):
        self.log = log
        self.interval = interval
        self.run_cap = run_cap
        self.day_cap = day_cap
        self.day = time.strftime("%Y-%m-%d")
        self.day_count = day_count
        self.run_count = 0
        self.last_call = 0.0
        self.backoff_until = 0.0
        self._warned = 0.0

    def allow(self, force=False):
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_count = today, 0
        if now < self.backoff_until:
            return False
        if self.day_count >= self.day_cap:
            if now - self._warned > 300:
                self._warned = now
                self.log.warning("GOV    | daily cap %d reached; running on "
                                 "memory only", self.day_cap)
            return False
        if self.run_count >= self.run_cap:
            return False
        if not force and now - self.last_call < self.interval:
            return False
        return True

    def called(self):
        now = time.time()
        self.last_call = now
        self.run_count += 1
        self.day_count += 1

    def rate_limited(self, retry_after):
        base = retry_after if retry_after else 45.0
        self.backoff_until = time.time() + base
        self.log.warning("GOV    | 429; backing off %.0fs", base)

    def transient(self):
        self.backoff_until = time.time() + 20.0
        self.log.warning("GOV    | transient; 20s pause")

# ================ 4. DECISIONS & MEMORY ================

def _extract_json(text):
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if m:
        t = m.group(1).strip()
    i = t.find("{")
    if i < 0:
        return None
    t = t[i:]
    depth, instr, esc, end = 0, False, False, -1
    for idx, ch in enumerate(t):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
    if end < 0:
        return None
    try:
        return json.loads(t[:end + 1])
    except Exception:
        return None


@dataclass
class Decision:
    kind: str
    x: Optional[int] = None
    y: Optional[int] = None
    label: str = ""
    source: str = "memory"
    chip: Optional[dict] = None
    seconds: float = 3.0


class SceneMemory:
    def __init__(self, path: Path):
        self.path = path
        self.scenes = {}
        self.manual = []
        self.dead = {}
        self.stats = {"day": time.strftime("%Y-%m-%d"), "day_calls": 0,
                      "teacher_calls": 0, "clicks": 0, "ok_clicks": 0}
        self.econ = {}

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
            self.scenes = raw.get("scenes") or {}
            self.manual = raw.get("manual") or []
            self.dead = raw.get("dead") or {}
            st = raw.get("stats") or {}
            today = time.strftime("%Y-%m-%d")
            keep = {k: int(v) for k, v in st.items()
                    if k in self.stats and k != "day_calls"}
            self.stats.update(keep)
            self.stats["day_calls"] = int(st.get("day_calls", 0)) \
                if st.get("day") == today else 0
            self.econ = raw.get("econ") or {}
        except Exception as e:
            print("memory partially unreadable: %s" % e, file=sys.stderr)

    def save(self, econ_blob=None):
        try:
            dead = {k: v for k, v in self.dead.items()
                    if v.get("until", 0) > time.time() - 3600}
            scenes = dict(sorted(self.scenes.items(),
                                 key=lambda kv: kv[1].get("ts", 0))[-150:])
            data = {"version": 7, "scenes": scenes,
                    "manual": self.manual[-4:], "dead": dead,
                    "stats": self.stats, "econ": econ_blob or self.econ}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print("memory save failed: %s" % e, file=sys.stderr)

    def find(self, h: int):
        """Tolerant lookup, but NEVER match a remembered Cloudflare wall."""
        best, bd = None, 7
        for hexkey, entry in self.scenes.items():
            if CF_TITLE_RE.search(entry.get("scene") or ""):
                continue
            try:
                d = hamming(int(hexkey, 16), h)
            except ValueError:
                continue
            if d < bd:
                bd, best = d, hexkey
        return (best, self.scenes.get(best)) if best else (None, None)

    def learn(self, hexkey, scene_desc, chips, labels, roles):
        entry = self.scenes.get(hexkey) or {"scene": "", "chips": {},
                                            "ts": 0, "visits": 0}
        entry["scene"] = scene_desc or entry.get("scene", "")
        entry["ts"] = time.time()
        entry["visits"] = int(entry.get("visits", 0)) + 1
        for i, c in enumerate(chips, 1):
            cx, cy = c["x"] + c["w"] // 2, c["y"] + c["h"] // 2
            rec = self.chip_rec(entry, cx, cy)
            if rec is None:
                rec = {"x": cx, "y": cy, "label": "", "role": "UI",
                       "tries": 0, "ok": 0, "busy_until": 0}
                entry["chips"]["%d,%d" % (cx, cy)] = rec
            if labels.get(i):
                rec["label"] = labels[i]
            if roles.get(i) in ROLES:
                rec["role"] = roles[i]
        self.scenes[hexkey] = entry
        return entry

    @staticmethod
    def chip_rec(entry, cx, cy):
        best, bd = None, 60
        for rec in (entry.get("chips") or {}).values():
            d = math.hypot(rec.get("x", 0) - cx, rec.get("y", 0) - cy)
            if d < bd:
                bd, best = d, rec
        return best

# ================ 5. ECONOMY ================

class Economy:
    VOLATILE_HINTS = ("player", "online", "users", "member", "version",
                      "ping", "latency", "server", "clock", "viewer", "time")

    def __init__(self, log):
        self.log = log
        self.state = "GATHER"
        self.state_age = 0
        self.obs = {}
        self.prev = {}
        self.currency = ""
        self.votes = {}
        self.items = set()
        self.volatile = set()
        self.mass_max = 0.0
        self.idx = 0
        self.last_gain = {}
        self.sell_fail = self.craft_fail = 0
        self.sell_block = self.craft_block = 0

    def to_dict(self):
        return {"state": self.state, "obs": self.obs,
                "currency": self.currency, "items": sorted(self.items),
                "volatile": sorted(self.volatile)}

    def load(self, blob):
        if not isinstance(blob, dict):
            return
        try:
            self.state = str(blob.get("state") or self.state)
            self.obs = {str(k): float(v) for k, v in
                        (blob.get("obs") or {}).items()}
            self.currency = str(blob.get("currency") or "")
            self.items = set(blob.get("items") or [])
            self.volatile = set(blob.get("volatile") or [])
        except Exception as e:
            self.log.warning("econ memory unreadable: %s", e)

    def ingest(self, obs):
        clean = {}
        for k, v in obs.items():
            if isinstance(v, (int, float)) and v == v and 0 <= v < 1e12:
                clean[str(k).strip().lower()[:24]] = float(v)
        for k in clean:
            if any(h in k for h in self.VOLATILE_HINTS):
                self.volatile.add(k)
        self.prev, self.obs = self.obs, clean
        self.idx += 1
        deltas = {}
        for k, v in clean.items():
            old = self.prev.get(k)
            if old is None:
                deltas[k] = v
                self.last_gain[k] = self.idx
            elif abs(v - old) > max(0.4, 0.004 * abs(old)):
                deltas[k] = v - old
                if v > old:
                    self.last_gain[k] = self.idx
        return deltas

    def live(self, d):
        return {k: v for k, v in d.items() if k not in self.volatile}

    def satisfied(self, state, deltas):
        deltas = self.live(deltas)
        if not deltas:
            return False
        if state == "GATHER":
            return any(v > 0 for k, v in deltas.items()
                       if k != self.currency and k not in self.items)
        if state == "CRAFT":
            return (any(k not in self.items and v > 0
                        for k, v in deltas.items())
                    or any(v < 0 for k, v in deltas.items()
                           if k != self.currency))
        if state == "SELL":
            if self.currency and deltas.get(self.currency, 0) > 0:
                return True
            return (any(v < 0 for k, v in deltas.items() if k in self.items)
                    and any(v > 0 for k, v in deltas.items()))
        return False

    def _mass(self):
        return sum(max(0.0, v) for k, v in self.obs.items()
                   if k != self.currency and k not in self.items
                   and k not in self.volatile)

    def _ratio(self):
        m = self._mass()
        self.mass_max = max(self.mass_max, m)
        return (m / self.mass_max) if self.mass_max > 0 else 0.0

    def _stalled(self):
        tracked = [k for k in self.obs if k != self.currency
                   and k not in self.items and k not in self.volatile]
        last = max((self.last_gain.get(k, -99) for k in tracked),
                   default=-99)
        return (self.idx - last) >= 2

    def decide(self, stuck):
        if stuck >= 12:
            return "EXPLORE"
        have_items = any(v > 0 for k, v in self.obs.items()
                         if k in self.items)
        if have_items and self.idx >= self.sell_block:
            want = "SELL"
        elif ((self._ratio() > 0.45 and self.idx >= self.craft_block)
              or (self._stalled() and self._ratio() > 0.45)):
            want = "CRAFT"
        else:
            want = "GATHER"
        if (want != self.state and self.state in ("GATHER", "CRAFT")
                and want in ("GATHER", "CRAFT") and self.state_age < 2):
            return self.state
        if want != self.state:
            self.state, self.state_age = want, 0
        else:
            self.state_age += 1
        return self.state

    def apply(self, state, deltas, satisfied):
        if not satisfied or not deltas:
            if state == "SELL":
                self.sell_fail += 1
                if self.sell_fail >= 6:
                    self.sell_block = self.idx + 15
                    self.sell_fail = 0
            if state == "CRAFT":
                self.craft_fail += 1
                if self.craft_fail >= 6:
                    self.craft_block = self.idx + 15
                    self.craft_fail = 0
            return
        if state == "CRAFT":
            for k, v in deltas.items():
                if (v > 0 and k not in self.items and k != self.currency
                        and k not in self.volatile):
                    self.items.add(k)
            if self.state == "CRAFT":
                self.state, self.state_age = "SELL", 0
        elif state == "SELL":
            for k, v in deltas.items():
                if v > 0 and k not in self.volatile:
                    self.votes[k] = self.votes.get(k, 0) + 1
            best = max(self.votes, key=self.votes.get, default=None)
            if best and self.votes.get(best, 0) >= 2:
                if best != self.currency:
                    self.log.info("ECON   | currency identified: %r", best)
                self.currency = best
            if self.state == "SELL":
                self.state, self.state_age = "GATHER", 0

# ================ 6. THE AGENT ================

TEACHER_PROMPT = (
    "You are teaching a game-playing agent. The screenshot shows a browser "
    "game; numbered LIME boxes mark clickable regions, red zones are "
    "navigation chrome, the center is the action area. The agent wants to "
    "play the game's economy: gather resources -> craft items -> sell for "
    "currency.\n\n"
    "ANSWER WITH ONE JSON OBJECT ONLY:\n"
    '{"scene":"<short description of this screen>",\n'
    ' "labels":{"<chip number>":"<exact visible text or short purpose>"},\n'
    ' "roles":{"<chip number>":"GATHER|CRAFT|SELL|NAV|UI"},\n'
    ' "kind":"primary|nav|login|dialog_confirm|dialog_decline|wait",\n'
    ' "chip":<best chip number for the objective, or null>,\n'
    ' "observations":{"<resource/currency/item>":<number>},\n'
    ' "notes":"<popups, errors, cooldowns, anything important>"}\n\n'
    "RULES:\n"
    "1. Label EVERY numbered chip (use its visible text; 'icon: <guess>' if "
    "no text).\n"
    "2. roles: GATHER = produces/collects resources; CRAFT = turns "
    "resources into items; SELL = market/selling; NAV = navigation tab or "
    "menu; UI = close/settings/misc.\n"
    "3. kind+chip: the best next click for the OBJECTIVE. If a real login "
    "form (email/password fields) is visible, kind \"login\" -- the agent "
    "has saved credentials. A Cloudflare/captcha/robot check is NOT a "
    "login; if you see one, answer kind \"wait\" and say so in notes.\n"
    "4. Report visible numbers (resources, currency, items, energy) in "
    "observations; skip live counters like online players.\n")


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = build_logger(cfg)
        self.vlm = make_vlm(cfg, self.log)
        self.mem = SceneMemory(Path(cfg.memory_path))
        self.mem.load()
        today = time.strftime("%Y-%m-%d")
        day_calls = self.mem.stats.get("day_calls", 0) \
            if self.mem.stats.get("day") == today else 0
        self.gov = Governor(self.log, cfg.interval, cfg.max_calls,
                            cfg.day_cap, day_count=day_calls)
        self.econ = Economy(self.log)
        self.econ.load(self.mem.econ)
        self.dead = self.mem.dead
        self.page = None
        self.context = None
        self.stop = {"flag": False}
        self.cycle = 0
        self.stuck = 0
        self.confusion = 0
        self.actions_since_verify = 0
        self.scene_hex = ""
        self.entry = None
        self.chips: list = []
        self.inv_scale = 1.0
        self.pending = None
        self._acted_since_obs = False
        self.recent: deque = deque(maxlen=10)
        self.shots_dir = Path("shots")
        self._last_reload = 0.0
        self.nav_duds = 0
        self.scene_duds = 0
        self._forced_teacher = False

    # ---------------- Cloudflare (local, free) ----------------

    async def cf_check(self) -> bool:
        try:
            if await self.page.evaluate(JS_CF):
                return True
        except Exception:
            pass
        try:
            if CF_TITLE_RE.search((await self.page.title()) or ""):
                return True
        except Exception:
            pass
        return False

    async def wait_out_cf(self) -> bool:
        """Patient, passive waiting. Never solves the challenge -- just
        polls (interstitials usually clear on their own) and, if the wall
        truly holds, tells the caller to end the run."""
        deadline = time.time() + self.cfg.cf_patience
        n = 0
        while time.time() < deadline and not self.stop["flag"]:
            n += 1
            remain = max(3.0, deadline - time.time())
            self.log.info("CF     | interstitial (poll %d); %.0fs of "
                          "patience left", n, remain)
            try:
                await self.page.mouse.move(random.uniform(200, 1200),
                                           random.uniform(150, 700), steps=6)
            except Exception:
                pass
            await asyncio.sleep(min(15.0, remain))
            if not await self.cf_check():
                self.log.info("CF     | cleared after %d polls", n)
                self.stuck = 0
                return True
        if self.stop["flag"]:
            return True
        return not await self.cf_check()

    # ---------------- plumbing ----------------

    def _shot(self, name, img):
        if not self.cfg.save_shots:
            return
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(self.shots_dir / name, "JPEG", quality=78)
            files = sorted(self.shots_dir.glob("*.jpg"))
            for old in files[:-250]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _save_all(self):
        self.mem.econ = self.econ.to_dict()
        self.mem.stats["day"] = time.strftime("%Y-%m-%d")
        self.mem.stats["day_calls"] = self.gov.day_count
        self.mem.save()

    async def capture(self):
        png = await self.page.screenshot(full_page=False)
        return Image.open(io.BytesIO(png)).convert("RGB")

    async def dom_chips(self, scale):
        if self.cfg.pure_vision:
            return []
        try:
            rects = await self.page.evaluate(JS_RECTS)
        except Exception:
            return []
        out = [{"x": int(r["x"] * scale), "y": int(r["y"] * scale),
                "w": int(r["w"] * scale), "h": int(r["h"] * scale),
                "score": 1.0, "dom": True,
                "disabled": bool(r.get("disabled"))} for r in rects or []]
        return _merge_boxes(out, 22)

    async def dom_text(self):
        try:
            return await self.page.evaluate(JS_TEXT) or ""
        except Exception:
            return ""

    async def selftest(self):
        img = Image.new("RGB", (64, 64), (24, 24, 24))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        try:
            txt = await self.vlm.ask(buf.getvalue(),
                                     'Reply with exactly {"ok":true}')
            self.log.info("VISION | selftest OK on %s: %s", self.vlm.model,
                          (txt or "")[:60])
            return True
        except RateLimited as e:
            self.log.warning("VISION | rate-limited but key valid "
                             "(retry_after=%s)", e.retry_after)
            return True
        except Exception as e:
            self.log.error("VISION | SELFTEST FAILED -> %s", str(e)[:300])
            if "401" in str(e) or "403" in str(e):
                self.log.error("VISION | rotate the key in repo secrets")
            return False

    # ---------------- login ----------------

    async def _try_click(self, patterns):
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for getter in (lambda: self.page.get_by_role("button", name=rx),
                           lambda: self.page.get_by_role("link", name=rx),
                           lambda: self.page.get_by_text(rx, exact=False)):
                try:
                    loc = getter().first
                    if await loc.count():
                        await loc.click(timeout=2500)
                        return True
                except Exception:
                    continue
        return False

    async def login(self):
        page, log = self.page, self.log
        log.info("LOGIN  | navigating to %s", self.cfg.url)
        if not EMAIL or not PASSWORD:
            log.warning("LOGIN  | no credentials configured "
                        "(GAME_EMAIL/GAME_PASSWORD or creds.json)")
        try:
            await page.goto(self.cfg.url, wait_until="domcontentloaded",
                            timeout=60000)
        except Exception as e:
            log.error("LOGIN  | navigation failed: %s", str(e)[:120])
            return False
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        # ---- THE OLD BYPASS, SPELLED OUT ---------------------------
        # the interstitial auto-clears in seconds; wait it out BEFORE the
        # login form is touched, so the gate never eats the login attempt
        for _ in range(6):
            if await self.cf_check():
                self.log.info("LOGIN  | interstitial up; waiting it out")
                await asyncio.sleep(8)
            else:
                break
        # -------------------------------------------------------------
        await self._try_click((r"^accept( all)?$", r"^agree", r"^got it$",
                               r"^ok$", r"^i understand$"))
        await page.wait_for_timeout(1200)
        stages, filled, last_open = 0, False, 0.0
        deadline = time.time() + 45
        while time.time() < deadline and not filled:
            try:
                n_pw = await page.locator(
                    'input[type="password"]').count()
            except Exception:
                n_pw = 0
            if n_pw:
                try:
                    pw = page.locator('input[type="password"]').first
                    await pw.click(timeout=4000)
                    await pw.fill(PASSWORD, timeout=4000)
                    em = page.locator(EMAIL_LOC).first
                    if await em.count():
                        await em.fill(EMAIL, timeout=4000)
                    log.info("LOGIN  | credentials entered; submitting")
                    try:
                        await pw.press("Enter")
                    except Exception:
                        await self._try_click((r"sign\s*in", r"log\s*in",
                                               r"^continue$", r"^submit$"))
                    filled = True
                    break
                except Exception as e:
                    log.warning("LOGIN  | password stage failed: %s",
                                str(e)[:120])
                    break
            try:
                n_em = await page.locator(EMAIL_LOC).count()
            except Exception:
                n_em = 0
            if n_em and stages < 3:
                stages += 1
                try:
                    em = page.locator(EMAIL_LOC).first
                    await em.click(timeout=4000)
                    await em.fill(EMAIL, timeout=4000)
                    log.info("LOGIN  | email entered (stage %d)", stages)
                    await self._try_click((r"^continue$", r"^next$",
                                           r"submit", r"sign\s*in",
                                           r"log\s*in"))
                    await page.wait_for_timeout(2500)
                    continue
                except Exception:
                    pass
            elif time.time() - last_open > 8:
                last_open = time.time()
                if await self._try_click((r"^enter\b", r"^log\s*in$",
                                          r"^sign\s*in$", r"^login$",
                                          r"play now", r"enter.*game")):
                    log.info("LOGIN  | opened the login UI")
                    await page.wait_for_timeout(2500)
                    continue
            await page.wait_for_timeout(1500)
        t0 = time.time()
        while time.time() - t0 < 45:
            try:
                if await page.locator(
                        'input[type="password"]').count() == 0:
                    break
            except Exception:
                break
            await page.wait_for_timeout(2000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        if self.cfg.session_file:
            try:
                await self.context.storage_state(path=self.cfg.session_file)
                log.info("LOGIN  | session saved to %s", self.cfg.session_file)
            except Exception:
                pass
        self._shot("login_end.jpg", await self.capture())
        log.info("LOGIN  | done | url=%s | title=%r", page.url[:80],
                 (await page.title())[:50])
        return True

    async def _fill_credentials(self, dec: Decision = None):
        page, log = self.page, self.log
        await page.wait_for_timeout(600)
        try:
            n_em = await page.locator(EMAIL_LOC).count()
        except Exception:
            n_em = 0
        try:
            n_pw = await page.locator('input[type="password"]').count()
        except Exception:
            n_pw = 0
        if n_em or n_pw:
            try:
                if n_em:
                    em = page.locator(EMAIL_LOC).first
                    await em.click(timeout=3000)
                    await em.fill(EMAIL, timeout=3000)
                    log.info("LOGIN  | typed email")
                if n_pw:
                    pw = page.locator('input[type="password"]').first
                    await pw.click(timeout=3000)
                    await pw.fill(PASSWORD, timeout=3000)
                    log.info("LOGIN  | typed password")
                    try:
                        await pw.press("Enter")
                    except Exception:
                        pass
                else:
                    await self._try_click((r"^(continue|next|log\s*in|"
                                           r"sign\s*in|submit|send)$",))
                await page.wait_for_timeout(1500)
                self._shot("login_fill.jpg", await self.capture())
                return True
            except Exception as e:
                log.warning("LOGIN  | form fill failed: %s", str(e)[:120])
        if dec is not None and dec.x is not None:
            try:
                await self._human_click(int(dec.x * self.inv_scale),
                                         int(dec.y * self.inv_scale))
                await page.keyboard.type(EMAIL,
                                          delay=random.uniform(30, 80))
                await page.keyboard.press("Tab")
                await page.keyboard.type(PASSWORD,
                                         delay=random.uniform(30, 80))
                await page.keyboard.press("Enter")
                log.info("LOGIN  | blind keyboard sequence")
                await page.wait_for_timeout(1500)
                self._shot("login_fill_blind.jpg", await self.capture())
                return True
            except Exception as e:
                log.warning("LOGIN  | blind fill failed: %s", str(e)[:120])
        return False

    # ---------------- the teacher call ----------------

    async def teacher(self, img, state, with_text=False):
        chips = self.chips
        table = "\n".join(
            "%d:(x=%d,y=%d,w=%d,h=%d)%s" % (
                i, c["x"], c["y"], c["w"], c["h"],
                " [disabled]" if c.get("disabled") else "")
            for i, c in enumerate(chips, 1)) or "(none detected)"
        prompt = TEACHER_PROMPT
        prompt += "\nOBJECTIVE: " + GOAL_TEXT.get(state, state)
        manual = "; ".join(self.mem.manual[-2:])
        if manual:
            prompt += "\nGAME NOTES SO FAR: " + manual[:600]
        prompt += "\nCLICKABLE REGIONS:\n" + table
        if with_text:
            text = await self.dom_text()
            prompt += ("\n\nPAGE TEXT (tutorials, tooltips, menus -- "
                       "digest it):\n" + (text[:7000] or "(no text)"))
            prompt += ("\nAlso add to your JSON: \"guidance\":\"<one short "
                       "paragraph of strategy advice for this game>\"")
        ann = annotate(img, chips)
        self._shot("teacher_c%04d_%s.jpg" % (self.cycle, state.lower()), ann)
        buf = io.BytesIO()
        ann.save(buf, "JPEG", quality=72)
        # edge-triggered force: one forced call per stuck episode
        force = ((self.stuck >= 3 or self.confusion >= 4)
                 and not self._forced_teacher)
        if not self.gov.allow(force=force):
            return None
        try:
            txt = await self.vlm.ask(buf.getvalue(), prompt)
        except RateLimited as e:
            self.gov.rate_limited(e.retry_after)
            return None
        except Transient as e:
            self.gov.transient()
            self.log.warning("TEACH  | transient: %s", str(e)[:180])
            return None
        self.gov.called()
        if force:
            self._forced_teacher = True
        self.mem.stats["teacher_calls"] += 1
        self.actions_since_verify = 0
        raw = _extract_json(txt)
        if not isinstance(raw, dict):
            self.log.warning("TEACH  | unparseable response")
            return None
        labels, roles = {}, {}
        for k, v in (raw.get("labels") or {}).items():
            try:
                labels[int(k)] = str(v)[:40]
            except (TypeError, ValueError):
                continue
        for k, v in (raw.get("roles") or {}).items():
            try:
                r = str(v).upper().strip()
                if r in ROLES:
                    roles[int(k)] = r
            except (TypeError, ValueError):
                continue
        self.entry = self.mem.learn(self.scene_hex,
                                    str(raw.get("scene") or ""), chips,
                                    labels, roles)
        self._save_all()
        if with_text and raw.get("guidance"):
            note = str(raw["guidance"])[:400]
            if note not in self.mem.manual:
                self.mem.manual.append(note)
            self.log.info("TEACH  | guidance: %s", note[:220])
        self.confusion = 0
        obs = {}
        if isinstance(raw.get("observations"), dict):
            for k, v in raw["observations"].items():
                try:
                    val = float(v)
                except (TypeError, ValueError):
                    continue
                if val == val and 0 <= val < 1e12:
                    obs[str(k).strip().lower()[:24]] = val
        if obs:
            deltas = self.econ.ingest(obs)
            if not self._acted_since_obs:
                for k, v in deltas.items():
                    if abs(v) > 0.5 and k not in self.econ.volatile:
                        self.econ.volatile.add(k)
                        self.log.info("ECON   | %r marked volatile (moved "
                                      "while idle)", k)
            self._judge_pending(deltas)
            live = self.econ.live(deltas)
            if live:
                self.log.info("ECON   | observed: %s",
                              ", ".join("%s %+g" % kv for kv in live.items()))
            self._acted_since_obs = False
        lab_str = ", ".join("#%d=%s[%s]" % (i, labels.get(i, "?"),
                                            roles.get(i, "?"))
                            for i in sorted(set(list(labels) +
                                                list(roles)))[:10])
        self.log.info("TEACH  | learned scene %s: %s", self.scene_hex[:8],
                      lab_str[:220])
        if raw.get("notes"):
            self.log.info("NOTES  | %s", str(raw["notes"])[:160])
        kind = str(raw.get("kind") or "wait").lower()
        if kind not in ("primary", "nav", "login", "dialog_confirm",
                        "dialog_decline", "wait"):
            kind = "wait"
        try:
            chip_no = int(raw.get("chip")) if raw.get("chip") else None
        except (TypeError, ValueError):
            chip_no = None
        if kind in ("primary", "nav", "dialog_confirm", "dialog_decline"):
            if chip_no and 1 <= chip_no <= len(chips):
                c = chips[chip_no - 1]
                if kind == "primary":
                    cy = c["y"] + c["h"] / 2
                    if cy < img.size[1] * 0.14:
                        center = [x for x in chips
                                  if img.size[1] * 0.14 < x["y"] + x["h"] / 2
                                  < img.size[1] * 0.88]
                        if center:
                            kind = "nav"
                rec = SceneMemory.chip_rec(self.entry,
                                           c["x"] + c["w"] // 2,
                                           c["y"] + c["h"] // 2)
                return Decision(kind=kind, x=c["x"] + c["w"] // 2,
                                y=c["y"] + c["h"] // 2,
                                label=labels.get(chip_no, ""),
                                source="teacher", chip=rec)
            return None
        return Decision(kind=kind, source="teacher")

    # ---------------- local play (free) ----------------

    def _local_pick(self, state) -> Decision:
        entry = self.entry
        now = time.time()
        recs = []
        for rec in (entry.get("chips") or {}).values():
            if rec.get("busy_until", 0) > now:
                continue
            if self.dead.get(dead_key(self.scene_hex, rec["x"], rec["y"]),
                             {}).get("until", 0) > now:
                continue
            if self.dead.get(dead_key(None, rec["x"], rec["y"]),
                             {}).get("until", 0) > now:
                continue
            recs.append(rec)
        if state == "EXPLORE":
            allrecs = list((entry.get("chips") or {}).values()) or recs
            if not allrecs:
                return Decision(kind="wait", source="memory",
                                seconds=6.0, label="nothing to explore")
            allrecs.sort(key=lambda r: (r.get("tries", 0), -r.get("ok", 0)))
            r = allrecs[0]
            return Decision(kind=("nav" if r.get("role") == "NAV"
                                  else "primary"), x=r["x"], y=r["y"],
                            label=r.get("label", ""), chip=r)
        if not recs:
            allrole = [r for r in (entry.get("chips") or {}).values()
                       if r.get("role") == state]
            if allrole and all(r.get("busy_until", 0) > now for r in allrole):
                wait = min(r["busy_until"] for r in allrole) - now + 1
                return Decision(kind="wait", source="memory", seconds=wait,
                                label="cooldown %.0fs" % wait)
            return Decision(kind="scroll" if self.cycle % 3 == 0 else "wait",
                            source="memory", label="nothing live")
        role = [r for r in recs if r.get("role") == state]
        if role:
            role.sort(key=lambda r: (-r.get("ok", 0), r.get("tries", 0)))
            r = role[0]
            return Decision(kind="primary", x=r["x"], y=r["y"],
                            label=r.get("label", ""), chip=r)
        if self.nav_duds >= 4:
            return Decision(kind="wait", source="memory", seconds=30.0,
                            label="nav dud budget spent")
        dest = DEST_WORDS.get(state, ())
        navs = [r for r in recs if r.get("role") == "NAV"]
        match = []
        for r in navs:
            lab = (r.get("label") or "").lower()
            if any(re.search(r"(?<![a-z])%s" % re.escape(w), lab)
                   for w in dest):
                match.append(r)
        pool = match or navs
        if pool:
            pool.sort(key=lambda r: r.get("tries", 0))
            r = pool[0]
            for (px, py, ts, kind) in self.recent:
                if (kind == "nav" and abs(px - r["x"]) < 30
                        and abs(py - r["y"]) < 30 and now - ts < 30):
                    return Decision(kind="wait", source="memory",
                                    seconds=4.0, label="nav cooldown")
            return Decision(kind="nav", x=r["x"], y=r["y"],
                            label=r.get("label", ""), chip=r)
        return Decision(kind="scroll" if self.cycle % 2 == 0 else "wait",
                        source="memory", label="no %s chip here" % state)

    # ---------------- economy judge ----------------

    def _judge_pending(self, deltas):
        if not self.pending or not deltas:
            return
        state, x, y, scene_hex, ts = self.pending
        if time.time() - ts > 600:
            self.pending = None
            return
        sat = self.econ.satisfied(state, deltas)
        self.econ.apply(state, deltas, sat)
        if sat:
            self.stuck = 0
            self.log.info("ECON   | %s CONFIRMED by deltas", state)
        self.pending = None

    def _bump_dead(self, x, y):
        hit = False
        for k in (dead_key(self.scene_hex, x, y), dead_key(None, x, y)):
            rec = self.dead.setdefault(k, {"fails": 0, "until": 0})
            rec["fails"] += 1
            if rec["fails"] >= 2 and rec.get("until", 0) <= time.time():
                rec["until"] = time.time() + 150
                rec["fails"] = 0
                hit = True
        if hit:
            self.log.info("POLICY | chip (%d,%d) dead for 150s", x, y)

    # ---------------- act + reflect ----------------

    async def act(self, dec: Decision):
        page = self.page
        if dec.kind in ("wait", "none"):
            await asyncio.sleep(max(1.0, min(dec.seconds, 30.0)))
            return
        if dec.kind == "scroll":
            try:
                await page.mouse.wheel(0, random.choice((600, 900, -600)))
            except Exception:
                pass
            self._acted_since_obs = True
            await asyncio.sleep(random.uniform(1.0, 1.8))
            return
        if dec.kind == "login":
            self.log.info("ACT    | login form; typing credentials")
            await self._fill_credentials(dec)
            self._acted_since_obs = True
            await asyncio.sleep(random.uniform(2.0, 3.0))
            return
        x = int((dec.x or 0) * self.inv_scale)
        y = int((dec.y or 0) * self.inv_scale)
        x = max(3, min(self.cfg.viewport_w - 4, x))
        y = max(3, min(self.cfg.viewport_h - 4, y))
        self.log.info("ACT    | %s click page(%d,%d) [%s] %r",
                      "nav" if dec.kind == "nav" else "mouse", x, y,
                      dec.source, dec.label)
        await self._human_click(x, y)
        self.mem.stats["clicks"] += 1
        self.actions_since_verify += 1
        self._acted_since_obs = True
        self.recent.append((x, y, time.time(), dec.kind))
        rec = dec.chip
        if rec:
            rec["tries"] = int(rec.get("tries", 0)) + 1
            if dec.kind == "primary":
                rec["busy_until"] = time.time() + 12.0
        self.pending = (self.econ.state, dec.x or 0, dec.y or 0,
                        self.scene_hex, time.time())
        await asyncio.sleep(random.uniform(1.6, 2.8))

    async def _human_click(self, x, y):
        jx, jy = x + random.uniform(-3, 3), y + random.uniform(-3, 3)
        try:
            await self.page.mouse.move(jx, jy, steps=random.randint(5, 9))
            await asyncio.sleep(random.uniform(0.05, 0.16))
            await self.page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.11))
            await self.page.mouse.up()
        except Exception as e:
            self.log.debug("click failed: %s", str(e)[:100])

    async def _reflect(self, pre, post, dec: Decision) -> bool:
        changed = False
        if dec.kind in ("primary", "nav", "dialog_confirm",
                        "dialog_decline") and dec.x is not None:
            px = int((dec.x or 0) * self.inv_scale)
            py = int((dec.y or 0) * self.inv_scale)
            rd = region_diff(pre, post, (px, py, 4, 4))
            gd = pixel_diff(pre, post)
            changed = (rd > 4.5) or hamming(dhash(pre), dhash(post)) >= 5
            self.log.info("CHECK  | %s | region %.1f | global %.1f",
                          "world changed" if changed else "nothing happened",
                          rd, gd)
        if changed:
            self.stuck = 0
            self.confusion = max(0, self.confusion - 1)
            self.nav_duds = 0
            self.scene_duds = 0
            self.mem.stats["ok_clicks"] += 1
            if dec.chip:
                dec.chip["ok"] = int(dec.chip.get("ok", 0)) + 1
                self.dead.pop(dead_key(self.scene_hex, dec.chip["x"],
                                       dec.chip["y"]), None)
        else:
            if dec.source == "teacher" and dec.kind in ("wait", "login"):
                self.stuck = max(0, self.stuck - 1)
            else:
                self.stuck += 1
                if dec.kind == "nav":
                    self.nav_duds += 1
                if dec.source == "memory":
                    self.scene_duds += 1
                if dec.kind in ("primary", "nav") and dec.x is not None:
                    self._bump_dead(dec.x, dec.y)
        return changed

    # ---------------- run ----------------

    async def run(self):
        cfg = self.cfg
        self.log.info("boot | %s | scenes known: %d | notes: %d | "
                      "currency=%r | creds=%s | session=%s", cfg.url,
                      len(self.mem.scenes), len(self.mem.manual),
                      self.econ.currency or "?",
                      "yes" if (EMAIL and PASSWORD) else "MISSING",
                      "yes" if Path(cfg.session_file).exists() else "no")
        async with async_playwright() as pw:
            browser = None
            common = dict(headless=cfg.headless, args=_chromium_args(cfg),
                          slow_mo=cfg.slowmo)
            storage = None
            if cfg.session_file and Path(cfg.session_file).exists():
                try:
                    storage = json.loads(
                        Path(cfg.session_file).read_text(encoding="utf-8"))
                    self.log.info("using saved session state")
                except Exception:
                    storage = None
            try:
                if cfg.user_data_dir:
                    self.context = await pw.chromium.launch_persistent_context(
                        cfg.user_data_dir,
                        viewport={"width": cfg.viewport_w,
                                  "height": cfg.viewport_h},
                        device_scale_factor=1, ignore_https_errors=True,
                        **common)
                else:
                    browser = await pw.chromium.launch(**common)
                    kw = dict(viewport={"width": cfg.viewport_w,
                                       "height": cfg.viewport_h},
                              device_scale_factor=1,
                              ignore_https_errors=True,
                              user_agent=cfg.user_agent)
                    if storage:
                        kw["storage_state"] = storage
                    self.context = await browser.new_context(**kw)
            except Exception as e:
                self.log.error("browser launch failed: %s", e)
                return
            await self.context.add_init_script(JS_STEALTH)
            self.page = (self.context.pages[0] if self.context.pages
                         else await self.context.new_page())
            self.page.set_default_timeout(15000)
            self.page.on("dialog", self._on_dialog)
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, self.stop.update,
                                            {"flag": True})
                except (NotImplementedError, RuntimeError):
                    pass
            if not await self.selftest():
                self._save_all()
                if not cfg.no_require:
                    self.log.error("VISION | selftest failed; exiting (use "
                                   "--no-require to override)")
                    try:
                        await self.context.close()
                        if browser:
                            await browser.close()
                    except Exception:
                        pass
                    sys.exit(2)
            if not await self.login():
                self._save_all()
                return
            t0 = time.time()
            try:
                await self._loop(t0)
            finally:
                self._save_all()
                st = self.mem.stats
                self.log.info("saved | teacher calls %d (today %d) | clicks "
                              "%d | good clicks %d | scenes %d",
                              st["teacher_calls"], self.gov.day_count,
                              st["clicks"], st["ok_clicks"],
                              len(self.mem.scenes))
                try:
                    await self.context.close()
                    if browser:
                        await browser.close()
                except Exception:
                    pass
        self.log.info("agent shut down cleanly")

    async def _loop(self, t0):
        cfg = self.cfg
        while not self.stop["flag"]:
            try:
                if Path(STOP_FILE).exists():
                    self.log.info("stop-file detected")
                    break
                if cfg.max_minutes and (time.time() - t0) / 60 >= \
                        cfg.max_minutes:
                    self.log.info("time budget reached")
                    break
                if self.page.is_closed():
                    await self._recover()
                    continue
                self.cycle += 1
                if self.stuck < 3 and self.confusion < 4:
                    self._forced_teacher = False
                # ---- CF gate: never spend anything on a wall ------------
                if await self.cf_check():
                    self._shot("cf_wall.jpg", await self.capture())
                    cleared = await self.wait_out_cf()
                    if not cleared:
                        self.log.warning("CF     | wall persisted past "
                                         "%.0fs; exiting cleanly -- the "
                                         "next hourly run starts on a "
                                         "fresh IP", cfg.cf_patience)
                        break
                    continue
                pre = await self.capture()
                img, scale = prepare(pre)
                self.inv_scale = 1.0 / scale
                self.chips = detect_chips(img)
                dom = await self.dom_chips(scale)
                if dom:
                    self.chips = _merge_boxes(dom + self.chips, 22)
                h = dhash(img)
                hexkey, entry = self.mem.find(h)
                known = entry is not None and entry.get("chips")
                if known and self.scene_duds >= 6:
                    self.log.warning("POLICY | scene %s matches badly "
                                     "(%d dead clicks); forgetting it",
                                     hexkey[:8], self.scene_duds)
                    self.mem.scenes.pop(hexkey, None)
                    known = False
                    entry = None
                    self.scene_duds = 0
                if known:
                    self.scene_hex = hexkey
                    self.entry = entry
                else:
                    self.scene_hex = "%016x" % h
                    self.entry = None
                state = self.econ.decide(self.stuck)
                verify_due = self.actions_since_verify >= cfg.verify_every
                need_teacher = (not known) or verify_due or self.stuck >= 3 \
                    or self.confusion >= 4
                dec = None
                if need_teacher:
                    dec = await self.teacher(
                        img, state, with_text=(self.confusion >= 4))
                if dec is None and known:
                    dec = self._local_pick(state)
                if dec is None:
                    dec = Decision(kind="wait", source="none",
                                   seconds=6.0)
                await self.act(dec)
                post = await self.capture()
                changed = await self._reflect(pre, post, dec)
                if not changed and dec.source == "memory" \
                        and dec.kind == "primary":
                    self.confusion += 1
                if (self.stuck >= 25
                        and time.time() - self._last_reload > 1200
                        and not await self.cf_check()):
                    self._last_reload = time.time()
                    self.log.warning("RECOVER | hard reload (stuck %d)",
                                     self.stuck)
                    try:
                        await self.page.reload(
                            wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                if self.cycle % 15 == 0:
                    self._status()
                if self.cycle % 10 == 0:
                    self._save_all()
                await asyncio.sleep(random.uniform(0.5, 1.2))
            except Exception:
                self.log.error("cycle %d crashed:\n%s", self.cycle,
                               traceback.format_exc()[-600:])
                await asyncio.sleep(2.0)

    def _status(self):
        st = self.mem.stats
        bank = ("%s %s" % (self.econ.currency,
                           int(self.econ.obs.get(self.econ.currency, 0)))
                if self.econ.currency else "?")
        scene = (self.entry or {}).get("scene", "?")
        self.log.info("STATUS | c%d | %s | bank %s | scene %s | stuck %d | "
                      "conf %d | teacher today %d/%d | clicks %d",
                      self.cycle, self.econ.state, bank, scene[:40],
                      self.stuck, self.confusion,
                      self.gov.day_count, self.cfg.day_cap, st["clicks"])

    async def _recover(self):
        self.log.warning("page closed; reopening")
        try:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(15000)
            self.page.on("dialog", self._on_dialog)
            await self.page.goto(self.cfg.url,
                                 wait_until="domcontentloaded",
                                 timeout=60000)
            await self.login()
        except Exception as e:
            self.log.error("recovery failed: %s", e)
            await asyncio.sleep(2.0)

    async def _on_dialog(self, dialog):
        msg = dialog.message or ""
        try:
            if dialog.type == "beforeunload":
                await dialog.dismiss()
                return
            if RISKY_RE.search(msg):
                self.log.warning("DIALOG | denied (fund-safety): %r", msg[:80])
                await dialog.dismiss()
            else:
                self.log.info("DIALOG | accepted: %r", msg[:80])
                await dialog.accept()
        except Exception:
            pass


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
