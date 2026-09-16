#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vision_agent.py (v1.1) -- a Spatial Vision-Agent that plays a Web3 game by
LOOKING at it. Full rewrite of the DOM-scanner approach: decisions come from
pixels, not from HTML text.

    ┌─────────────────┐   ┌──────────────────────────────┐   ┌────────────────┐
    │  CAPTURE        │──>│  SEE (local, free)           │──>│  ASK (VLM,     │
    │  viewport PNG   │   │  saliency chips + grid +      │   │  rate-limited)  │
    │  DSF=1 so px==  │   │  NAV/ACTION zone overlay      │   │  JSON decision: │
    │  CSS coords     │   └──────────────────────────────┘   │  kind+point     │
    └─────────────────┘                                     └───────┬────────┘
            │                                                        │
            │   ┌────────────────────────────────────────────────────▼───────┐
            │   │  POLICY (local): zone veto, chip snap, dead-chip & repeat  │
            │   │  guards  ->  final (x, y)                                  │
            │   └────────────────────────────────────────┬───────────────────┘
            │                                            │
    ┌───────▼────────┐   ┌──────────────────────────────▼───┐
    │  ACT            │──>│  REFLECT (local, free)           │
    │  humanized      │   │  pixel-diff did-it-work, VLM     │
    │  mouse.click    │   │  observation deltas -> economy   │
    └─────────────────┘   │  -> drives -> next goal          │
                          └──────────────────────────────────┘

WHAT v1.1 FIXED (driven by the first GitHub Actions run):
  * Gemini client: real error surfaces (no more "all models failed" mystery),
    automatic model fallback chain (2.5-flash -> 2.0-flash -> 1.5-flash ->
    1.5-pro), sticky on whichever model works. 4xx = BadConfig (switch model,
    don't retry); 429 = RateLimited (honor Retry-After); 5xx = Transient.
  * Boot selftest: a 5-second, 1-cent call that proves the API pipeline before
    the game even loads. Fails loud with the actual provider error text.
  * Blind mode no longer spams VETO: local picks are center-zone only, and
    out-of-center exploration picks are typed as "nav" (they pass the policy
    as navigation, not as fake primaries).
  * Login rewritten: first tries to OPEN the login UI (a "Log in"/"Sign in"
    button), then waits up to 45s for the form, fills email+password,
    multi-stage (email -> continue -> password) aware. Always screenshots
    shots/login_end.jpg so you can SEE what state it reached.
  * Every VLM request saves its annotated screenshot to shots/ask_cNNNN.jpg
    -> upload as a CI artifact and you can watch the agent's eyes.
  * Dead-chip logic deduped (one log line, both scene-keyed and global keys
    actually enforced in approve()).
  * REFLECT region-diff now compares in page coordinates (was off by the
    downscale factor on wide viewports).
  * CI fonts (Liberation) added to the annotate font search; Pillow getdata
    deprecation warnings silenced.
  * --require-vlm flag: exit non-zero if the selftest fails (CI fail-fast).

GITHUB ACTIONS WORKFLOW (save as .github/workflows/farm.yml):

    name: farm
    on:
      workflow_dispatch:
      schedule:
        - cron: "0 */3 * * *"          # every 3 hours
    jobs:
      farm:
        runs-on: ubuntu-latest
        timeout-minutes: 35
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with: { python-version: "3.11" }
          - uses: actions/cache@v4
            with:
              path: vision_memory.json
              key: vision-memory-${{ github.run_id }}
              restore-keys: vision-memory-
          - run: pip install playwright pillow
          - run: playwright install --with-deps chromium
          - name: Farm
            env:
              GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
              GAME_EMAIL: ${{ secrets.GAME_EMAIL }}
              GAME_PASSWORD: ${{ secrets.GAME_PASSWORD }}
            run: |
              python vision_agent.py "https://runiverseidle.com/forge" \
                  --max-minutes 25 --require-vlm
          - name: Upload screenshots & memory
            if: always()
            uses: actions/upload-artifact@v4
            with:
              name: vision-run
              path: |
                shots/
                vision_memory.json
              if-no-files-found: ignore

WHY THE TAB-LOOP IS STRUCTURALLY DEAD (unchanged, five layers):
  1. The VLM literally sees red NAV zones / green ACTION zone / grid / chips.
  2. The Spatial Policy hard-vetoes any "primary" in a NAV zone while center
     chips exist -- even if the model insists.
  3. Satisfaction is economic (VLM observation deltas), never "URL changed".
  4. Chips that produce zero pixel change go dead for 150 s.
  5. Scene cache is keyed by (game-state, screenshot-hash); failures never
     replay.

ETHICS: burner wallet + testnet only; fund-moving native dialogs are denied
by default; patience-only Cloudflare handling (a Turnstile wants a human --
that's a --headed, persistent-profile problem, not a headless one).
Autonomous play may violate the game's ToS; that's your responsibility.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import signal
import sys
import time
import traceback
import warnings
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.async_api import async_playwright

# Pillow 12+ deprecation noise (getdata is fine until Pillow 14, 2027)
warnings.filterwarnings("ignore", message=".*getdata.*",
                        category=DeprecationWarning)

__version__ = "1.1.0"

# ═══════════════════════ HARDCODED LOGIN (change me) ═════════════════════════

EMAIL = os.environ.get("GAME_EMAIL", "your.player@example.com")
PASSWORD = os.environ.get("GAME_PASSWORD", "ChangeMe123!")

# ═══════════════════════ 1. CLI / CONFIG / LOGGING ═══════════════════════════

try:
    _RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:                      # PIL < 9.1
    _RESAMPLE = Image.BILINEAR

MAX_IMG_W = 1600          # wider viewports get downscaled (coords are rescaled)
DH_SIZE = 8               # dHash -> 64-bit scene signature
STOP_FILE = ".vision_agent_stop"

EMAIL_LOC = ('input[type="email"], input[name*="mail" i], '
             'input[autocomplete="email"], input[placeholder*="mail" i], '
             'input[id*="mail" i], input[name*="user" i], '
             'input[placeholder*="user" i], input[name*="login" i], '
             'input[name*="email" i]')


@dataclass
class Config:
    url: str = ""
    provider: str = "auto"              # auto | gemini | openai
    model: str = ""
    api_key: str = ""
    headless: bool = True
    viewport_w: int = 1440
    viewport_h: int = 900
    user_data_dir: str = ""
    slowmo: float = 0.0
    min_interval: float = 15.0          # min seconds between VLM calls
    max_calls_hour: int = 150
    start_state: str = "GATHER"
    max_cycles: int = 0
    max_minutes: float = 0.0
    memory_path: str = "vision_memory.json"
    log_path: str = "vision_agent.log"
    log_level: str = "INFO"
    pure_vision: bool = False          # disable DOM rect chips (canvas games)
    allow_edge_actions: bool = False    # permit "primary" clicks in NAV zones
    require_vlm: bool = False          # exit non-zero if selftest fails
    save_shots: bool = True            # save annotated screenshots to shots/
    user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    seed: int = -1


def parse_args() -> Config:
    ap = argparse.ArgumentParser(
        description="Spatial Vision-Agent: plays a browser game by looking at "
                    "pixels (VLM decides, local policy vets, mouse clicks).")
    ap.add_argument("url", nargs="?", default=os.environ.get("GAME_URL", ""))
    ap.add_argument("--provider", choices=["auto", "gemini", "openai"],
                    default="auto")
    ap.add_argument("--model", default="",
                    help="override default model (else auto-fallback chain)")
    ap.add_argument("--api-key", default="",
                    help="else $GEMINI_API_KEY / $GOOGLE_API_KEY / "
                         "$OPENAI_API_KEY")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--viewport", default="1440x900")
    ap.add_argument("--user-data-dir", default="")
    ap.add_argument("--slowmo", type=float, default=0.0)
    ap.add_argument("--min-interval", type=float, default=15.0,
                    help="min seconds between vision API calls")
    ap.add_argument("--max-calls-hour", type=int, default=150)
    ap.add_argument("--start-state", choices=["GATHER", "CRAFT", "SELL"],
                    default="GATHER")
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=0.0)
    ap.add_argument("--memory", default="vision_memory.json")
    ap.add_argument("--log-file", default="vision_agent.log")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--pure-vision", action="store_true",
                    help="no DOM rects at all; saliency chips only")
    ap.add_argument("--allow-edge-actions", action="store_true",
                    help="allow 'primary' clicks inside NAV zones")
    ap.add_argument("--require-vlm", action="store_true",
                    help="exit non-zero if the VLM selftest fails (CI)")
    ap.add_argument("--no-shots", action="store_true",
                    help="don't save annotated screenshots")
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
                 user_data_dir=a.user_data_dir.strip(), slowmo=a.slowmo,
                 min_interval=a.min_interval, max_calls_hour=a.max_calls_hour,
                 start_state=a.start_state, max_cycles=a.max_cycles,
                 max_minutes=a.max_minutes, memory_path=a.memory,
                 log_path=a.log_file, log_level=a.log_level,
                 pure_vision=a.pure_vision,
                 allow_edge_actions=a.allow_edge_actions,
                 require_vlm=a.require_vlm, save_shots=not a.no_shots,
                 seed=a.seed)
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

# DOM is used ONLY as a geometry oracle (rects drawn onto the screenshot as
# chips). No DOM text ever selects an action -- the VLM does that from pixels.
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
    out.push({x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height)});
    if (out.length >= 80) break;
  }
  return out;
}"""

RISKY_RE = re.compile(r"\b(approve|transfer|withdraw|burn|spend|swap|mint|"
                      r"send|sign(?:\s+transaction)?)\b", re.I)

# ═════════════════ 2. LOCAL VISION PRIMITIVES (PIL, zero cost) ══════════════

def dhash(img: Image.Image, size: int = DH_SIZE) -> int:
    g = img.convert("L").resize((size + 1, size), _RESAMPLE)
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        row = px[r * (size + 1):(r + 1) * (size + 1)]
        for c in range(size):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


_FONT_CACHE: dict = {}


def _font(size: int):
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
            f = ImageFont.load_default(size)          # PIL >= 10.1
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _components(mask: list, sw: int, sh: int):
    seen = bytearray(sw * sh)
    comps = []
    for i in range(sw * sh):
        if not mask[i] or seen[i]:
            continue
        stack = [i]
        seen[i] = 1
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


def detect_chips(img: Image.Image, max_chips: int = 20) -> list:
    """Visual saliency: glowing/saturated/high-contrast rectangles = buttons."""
    W, H = img.size
    sw, sh = max(64, W // 8), max(40, H // 8)
    small = img.convert("RGB").resize((sw, sh), _RESAMPLE)
    hsv = small.convert("HSV")
    h, s, v = hsv.split()
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


def _iou(a: dict, b: dict) -> float:
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    smaller = min(a["w"] * a["h"], b["w"] * b["h"])
    return inter / max(1, smaller)


def _merge_boxes(boxes: list, limit: int) -> list:
    boxes = sorted(boxes, key=lambda b: -(b["score"] * (b["w"] * b["h"]) ** 0.5))
    out = []
    for b in boxes:
        if any(_iou(b, o) > 0.55 for o in out):
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def prepare(img: Image.Image):
    """Downscale oversized screenshots; returns (image, scale_factor)."""
    W, H = img.size
    if W <= MAX_IMG_W:
        return img, 1.0
    f = MAX_IMG_W / float(W)
    return img.resize((MAX_IMG_W, int(H * f)), _RESAMPLE), f


def annotate(img: Image.Image, chips: list) -> Image.Image:
    """Make the screenshot LEGIBLE to a VLM: red NAV zones, green ACTION zone
    hint, 100 px grid with 200 px labels, numbered lime chips."""
    W, H = img.size
    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    top = int(H * 0.14)
    left = int(W * 0.15)
    bottom = int(H * 0.88)
    f_big, f_small = _font(20), _font(13)

    d.rectangle([0, 0, W, top], fill=(255, 40, 40, 26))
    d.rectangle([0, 0, left, H], fill=(255, 40, 40, 22))
    d.rectangle([0, bottom, W, H], fill=(255, 40, 40, 22))
    d.text((8, 6), "NAV ZONE (tabs -- avoid unless navigating)", font=f_small,
           fill=(255, 60, 60, 255))
    d.text((8, top + 6), "ACTION ZONE (primary action buttons live here)",
           font=f_small, fill=(60, 255, 120, 255))

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


def pixel_diff(a: Image.Image, b: Image.Image) -> float:
    size = (160, 100)
    da = list(a.convert("L").resize(size, _RESAMPLE).getdata())
    db = list(b.convert("L").resize(size, _RESAMPLE).getdata())
    return sum(abs(x - y) for x, y in zip(da, db)) / float(len(da))


def region_diff(a: Image.Image, b: Image.Image, box: tuple) -> float:
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

# ═════════════════ 3. VISION-LM CLIENTS (rate-limit aware) ══════════════════

class RateLimited(Exception):
    def __init__(self, retry_after: Optional[float] = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class Transient(Exception):
    pass


class BadConfig(Exception):
    """4xx from the provider: bad key / bad model name. Never retried;
    surfaced in full so the log tells you the truth."""


class GeminiVision:
    NAME = "gemini"
    FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash",
                       "gemini-1.5-flash", "gemini-1.5-pro")

    def __init__(self, key: str, model: str):
        self.key = key
        if model:
            self.models = [model] + [m for m in self.FALLBACK_MODELS
                                     if m != model]
        else:
            self.models = list(self.FALLBACK_MODELS)
        self.model = self.models[0]

    def _post(self, b64: str, prompt: str, model: str) -> str:
        body = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
            "generationConfig": {"temperature": 0.2, "max_output_tokens": 800,
                                 "response_mime_type": "application/json"}}
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
            raise BadConfig("http %d: %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Transient(str(e)[:160])
        cands = data.get("candidates") or []
        if not cands:
            raise Transient("no candidates (safety block?)")
        for p in (cands[0].get("content") or {}).get("parts") or []:
            if p.get("text"):
                return p["text"]
        raise Transient("empty response")

    def _ask_sync(self, jpeg_bytes: bytes, prompt: str) -> str:
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        errors = []
        for model in self.models:
            for attempt in (1, 2):
                try:
                    out = self._post(b64, prompt, model)
                    if self.model != model:
                        self.model = model       # sticky: lock onto it
                    return out
                except BadConfig as e:
                    errors.append("%s -> %s" % (model, e))
                    break                        # config error: next model
                except Transient as e:
                    errors.append("%s -> %s" % (model, e))
                    if attempt == 1:
                        time.sleep(2.5)
        raise Transient("; ".join(errors[-3:]) or "no models tried")

    async def ask(self, jpeg_bytes: bytes, prompt: str) -> str:
        return await asyncio.to_thread(self._ask_sync, jpeg_bytes, prompt)


class OpenAIVision:
    NAME = "openai"
    FALLBACK_MODELS = ("gpt-4o", "gpt-4o-mini")
    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self, key: str, model: str):
        self.key = key
        if model:
            self.models = [model] + [m for m in self.FALLBACK_MODELS
                                     if m != model]
        else:
            self.models = list(self.FALLBACK_MODELS)
        self.model = self.models[0]

    def _post(self, b64: str, prompt: str, model: str) -> str:
        body = {"model": model, "max_tokens": 800, "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + b64,
                        "detail": "high"}}]}]}
        req = urllib.request.Request(
            self.ENDPOINT, data=json.dumps(body).encode("utf-8"),
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

    def _ask_sync(self, jpeg_bytes: bytes, prompt: str) -> str:
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
        raise Transient("; ".join(errors[-3:]) or "no models tried")

    async def ask(self, jpeg_bytes: bytes, prompt: str) -> str:
        return await asyncio.to_thread(self._ask_sync, jpeg_bytes, prompt)


def make_vision(cfg: Config, log: logging.Logger):
    key = cfg.api_key
    provider = cfg.provider
    if provider == "auto":
        gk = key or os.environ.get("GEMINI_API_KEY") or \
            os.environ.get("GOOGLE_API_KEY")
        if gk:
            provider = "gemini"
            key = gk
        elif (key or os.environ.get("OPENAI_API_KEY")):
            provider = "openai"
            key = key or os.environ.get("OPENAI_API_KEY")
        else:
            raise SystemExit("no API key: set GEMINI_API_KEY or OPENAI_API_KEY")
    else:
        env = ("GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY")
        key = key or os.environ.get(env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("missing $%s" % env)
    v = (GeminiVision(key, cfg.model) if provider == "gemini"
         else OpenAIVision(key, cfg.model))
    log.info("vision provider: %s | model chain: %s", v.NAME,
             " -> ".join(v.models))
    return v

# ═════════════════ 4. DECISION PARSING & THE VLM PROMPT ═════════════════════

KINDS = ("primary", "nav", "dialog_confirm", "dialog_decline",
         "scroll", "wait", "none")


@dataclass
class Decision:
    kind: str
    chip: Optional[int] = None
    x: Optional[int] = None
    y: Optional[int] = None
    label: str = ""
    confidence: float = 0.5
    scene: str = ""
    notes: str = ""
    obs: dict = field(default_factory=dict)
    source: str = "vlm"          # vlm | cache | local
    raw: str = ""

    def to_dict(self):
        return {"kind": self.kind, "chip": self.chip, "x": self.x, "y": self.y,
                "label": self.label, "confidence": self.confidence,
                "scene": self.scene, "notes": self.notes,
                "obs": self.obs, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict):
        return cls(kind=str(d.get("kind") or "none"),
                   chip=d.get("chip"), x=d.get("x"), y=d.get("y"),
                   label=str(d.get("label") or ""),
                   confidence=float(d.get("confidence") or 0.5),
                   scene=str(d.get("scene") or ""),
                   notes=str(d.get("notes") or ""),
                   obs=dict(d.get("obs") or {}),
                   source=str(d.get("source") or "cache"))


def _extract_json(text: str):
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


def parse_decision(text: str) -> Decision:
    raw = _extract_json(text)
    if not isinstance(raw, dict):
        m = re.search(r"\b(?:chip|button|box)\s*#?\s*(\d{1,2})", text or "",
                      re.I)
        if m:
            return Decision(kind="primary", chip=int(m.group(1)),
                            label="recovered-from-text", confidence=0.2,
                            raw=(text or "")[:400])
        raise Transient("unparseable VLM response: %r" % (text or "")[:200])
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = "none"
    chip = raw.get("chip")
    try:
        chip = int(chip) if chip is not None else None
    except (TypeError, ValueError):
        chip = None
    x = y = None
    pt = raw.get("point") or {}
    if isinstance(pt, dict):
        try:
            x = int(round(float(pt.get("x"))))
        except (TypeError, ValueError):
            x = None
        try:
            y = int(round(float(pt.get("y"))))
        except (TypeError, ValueError):
            y = None
    obs = {}
    if isinstance(raw.get("observations"), dict):
        for k, v in raw["observations"].items():
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if val == val and abs(val) < 1e12:            # drop NaN/absurd
                obs[str(k).strip().lower()[:24]] = val
    return Decision(kind=kind, chip=chip, x=x, y=y,
                    label=str(raw.get("label") or "")[:60],
                    confidence=max(0.0, min(1.0, float(raw.get("confidence")
                                                         or 0.5))),
                    scene=str(raw.get("scene") or "")[:90],
                    notes=str(raw.get("notes") or "")[:160],
                    obs=obs, source="vlm", raw=(text or "")[:400])


PROMPT_HEAD = (
    "You are the eyes of an autonomous game-playing agent. The image is a "
    "screenshot of a browser game.\n"
    "- A translucent RED tint marks the NAVIGATION zones (top strip, left "
    "rail, bottom strip). Navigation tabs and menus live there.\n"
    "- The center of the screen is the ACTION zone; primary action buttons "
    "(gather, mine, chop, craft, forge, sell, confirm) live there.\n"
    "- Numbered LIME boxes mark every clickable region we detected; a "
    "coordinate grid is drawn with labels every 200 px.\n\n"
    "ANSWER WITH ONE JSON OBJECT ONLY, no other text:\n"
    '{"kind":"primary|nav|dialog_confirm|dialog_decline|scroll|wait|none",\n'
    ' "chip":<chip number or null>,\n'
    ' "point":{"x":<int>,"y":<int>},\n'
    ' "label":"<text visible on the element>",\n'
    ' "confidence":<0.0-1.0>,\n'
    ' "scene":"<one short line describing this screen>",\n'
    ' "observations":{"<resource/currency/item name>":<number>},\n'
    ' "notes":"<popups, errors, captcha, or anything unusual>"}\n\n'
    "RULES:\n"
    "1. If a PRIMARY ACTION for the objective is visible in the ACTION zone, "
    "choose it. NEVER choose a navigation tab when a primary action exists.\n"
    "2. Choose kind \"nav\" ONLY if no primary action for the objective is "
    "visible; then pick the tab that leads toward the objective.\n"
    "3. Prefer the \"chip\" field; set \"point\" to the CENTER of that chip "
    "in screenshot pixels (read them off the grid).\n"
    "4. Report every visible number (resources, currency, items, energy) in "
    "\"observations\".\n"
    "5. If a modal or popup covers the screen, choose dialog_confirm (to "
    "proceed) or dialog_decline (to dismiss).\n"
    "6. If nothing at all is actionable, say kind \"wait\" and explain in "
    "notes.\n")


def build_prompt(goal: str, chips: list, econ_obs: dict) -> str:
    table = "\n".join(
        "%d:(x=%d,y=%d,w=%d,h=%d)" % (i, c["x"], c["y"], c["w"], c["h"])
        for i, c in enumerate(chips, 1)) or "(none detected)"
    known = ", ".join("%s=%s" % (k, int(v) if float(v).is_integer() else v)
                      for k, v in list(econ_obs.items())[:8]) or "unknown"
    return (PROMPT_HEAD
            + "\nCURRENT OBJECTIVE: " + goal
            + "\nKNOWN GAME STATE (from previous observations): " + known
            + "\nDETECTED CLICKABLE REGIONS (screenshot pixels):\n" + table)

# ═════════════════ 5. RATE GOVERNOR & SCENE CACHE ═══════════════════════════

class Governor:
    """Keeps the VLM bill sane: min interval, hourly ceiling, 429 backoff."""

    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.last_call = 0.0
        self.backoff_until = 0.0
        self.backoff = 0.0
        self.hour = deque()
        self._blind_logged = 0.0

    def allow(self, force: bool = False) -> bool:
        now = time.time()
        if now < self.backoff_until:
            return False
        while self.hour and now - self.hour[0] > 3600:
            self.hour.popleft()
        if len(self.hour) >= self.cfg.max_calls_hour:
            if now - self._blind_logged > 60:
                self._blind_logged = now
                self.log.warning("VISION  | hourly ceiling hit (%d); "
                                 "blind mode", self.cfg.max_calls_hour)
            return False
        if not force and now - self.last_call < self.cfg.min_interval:
            return False
        return True

    def called(self):
        now = time.time()
        self.last_call = now
        self.hour.append(now)

    def rate_limited(self, retry_after: Optional[float]):
        base = retry_after if retry_after else max(30.0, self.backoff * 2
                                                   or 30.0)
        self.backoff = min(600.0, base)
        self.backoff_until = time.time() + self.backoff
        self.log.warning("VISION  | 429 rate limited; backing off %.0fs",
                         self.backoff)

    def transient_fail(self):
        self.backoff_until = time.time() + 20.0
        self.log.warning("VISION  | transient failure; 20s of blind mode")


class SceneCache:
    """(state, screenshot-hash) -> decision that worked. Replays are free."""

    def __init__(self):
        self.entries: dict = {}

    def key(self, state: str, scene_hex: str) -> str:
        return "%s:%s" % (state, scene_hex)

    def get_replayable(self, key: str):
        e = self.entries.get(key)
        if not e:
            return None
        if (e.get("outcome") == "success" and e["uses"] < 4
                and time.time() - e["ts"] < 1800):
            e["uses"] += 1
            return Decision.from_dict(e["decision"])
        return None

    def put(self, key: str, dec: Decision):
        self.entries[key] = {"decision": dec.to_dict(), "outcome": None,
                             "uses": 0, "ts": time.time(),
                             "scene": dec.scene}

    def mark(self, key: str, outcome: str):
        e = self.entries.get(key)
        if e and outcome in ("success", "fail"):
            e["outcome"] = outcome

    def trim(self, limit=300):
        if len(self.entries) <= limit:
            return
        keep = sorted(self.entries.items(),
                      key=lambda kv: kv[1].get("ts", 0))[-limit:]
        self.entries = dict(keep)

# ═════════════════ 6. SPATIAL POLICY (the tab-loop executioner) ═════════════

TOP_F, LEFT_F, BOTTOM_F, RIGHT_F = 0.14, 0.15, 0.12, 0.11
ZONE_PRIOR = {"center": 1.0, "bottom": 0.55, "right": 0.45,
              "left": 0.30, "top": 0.10}


def zone_of(x: float, y: float, W: int, H: int) -> str:
    if y < H * TOP_F:
        return "top"
    if x < W * LEFT_F:
        return "left"
    if y > H * (1 - BOTTOM_F):
        return "bottom"
    if x > W * (1 - RIGHT_F):
        return "right"
    return "center"


class SpatialPolicy:
    """Vets every decision in 2D before the mouse moves."""

    def __init__(self, cfg: Config, log: logging.Logger):
        self.cfg = cfg
        self.log = log

    def _dead_now(self, dead: dict, scene_hex: str, x: int, y: int) -> bool:
        now = time.time()
        for k in (self._dead_key(scene_hex, x, y), self._dead_key(None, x, y)):
            rec = dead.get(k)
            if rec and rec.get("until", 0) > now:
                return True
        return False

    def approve(self, dec: Decision, chips: list, W: int, H: int,
                econ_state: str, boredom: float, dead: dict,
                recent: deque, scene_hex: str = ""):
        """Returns (final_decision_or_None, reject_reason)."""
        if dec.kind in ("wait", "none", "scroll"):
            return dec, "ok"
        point = (dec.x, dec.y)
        if dec.chip and 1 <= dec.chip <= len(chips):
            chip = chips[dec.chip - 1]
            cx, cy = chip["x"] + chip["w"] // 2, chip["y"] + chip["h"] // 2
            if point[0] is None or point[1] is None:
                point = (cx, cy)
            elif (abs(point[0] - cx) <= 140 and abs(point[1] - cy) <= 140):
                point = (cx, cy)               # snap: guarantees a real hit
        if point[0] is None or point[1] is None:
            return Decision(kind="wait", notes="no point returned",
                            source=dec.source), "no_point"
        x = int(max(4, min(W - 5, point[0])))
        y = int(max(4, min(H - 5, point[1])))
        z = zone_of(x, y, W, H)

        if dec.kind == "primary" and z != "center":
            center_chips = [c for c in chips
                            if zone_of(c["x"] + c["w"] / 2,
                                       c["y"] + c["h"] / 2, W, H) == "center"]
            if center_chips and not self.cfg.allow_edge_actions:
                self.log.warning("POLICY  | VETO: %r called a %s-zone element "
                                 "'primary' (%d,%d); center options exist",
                                 dec.label, z, x, y)
                return None, "primary_in_nav_zone"
            dec.kind = "nav"                    # rare legit edge primary

        if dec.kind == "nav":
            if econ_state == "GATHER" and boredom < 0.7 and dec.source == "local":
                return None, "nav_not_needed"
            now = time.time()
            for (px, py, ts, kind) in recent:
                if (kind == "nav" and abs(px - x) < 30 and abs(py - y) < 30
                        and now - ts < 25):
                    return None, "nav_repeat"

        if dec.kind in ("primary", "nav", "dialog_confirm", "dialog_decline"):
            if self._dead_now(dead, scene_hex, x, y):
                return None, "dead_chip"

        dec.x, dec.y = x, y
        return dec, "ok"

    @staticmethod
    def _dead_key(scene_hex, x, y):
        if scene_hex:
            return "%s:%d:%d" % (scene_hex, x // 30, y // 30)
        return "*:%d:%d" % (x // 30, y // 30)

# ═════════════════ 7. ECONOMY (driven by VLM observations) ══════════════════

class Economy:
    """GATHER -> CRAFT -> SELL state machine whose only sensors are the
    numbers the VLM reports each time it looks at the screen."""

    def __init__(self, log: logging.Logger, start="GATHER"):
        self.log = log
        self.state = start
        self.state_age = 0
        self.obs: dict = {}
        self.prev: dict = {}
        self.currency = ""
        self.currency_votes: dict = {}
        self.items: set = set()
        self.mass_max = 0.0
        self.obs_idx = 0
        self.last_gain_idx: dict = {}
        self.loop: list = []
        self.loop_i = 0
        self.sell_fail = 0
        self.craft_fail = 0
        self.sell_block = 0
        self.craft_block = 0

    def to_dict(self):
        return {"state": self.state, "obs": self.obs, "currency":
                self.currency, "items": sorted(self.items),
                "loop": self.loop, "loop_i": self.loop_i}

    def load(self, blob):
        if not isinstance(blob, dict):
            return
        try:
            self.state = str(blob.get("state") or self.state)
            self.obs = {str(k): float(v) for k, v in
                        (blob.get("obs") or {}).items()}
            self.currency = str(blob.get("currency") or "")
            self.items = set(blob.get("items") or [])
            self.loop = [str(x) for x in (blob.get("loop") or [])]
            self.loop_i = int(blob.get("loop_i", 0))
        except Exception as e:
            self.log.warning("economy memory unreadable: %s", e)

    def ingest(self, obs: dict) -> dict:
        """New numbers in; deltas out. This is the agent's economy eyes."""
        clean = {}
        for k, v in obs.items():
            if isinstance(v, (int, float)) and v == v and 0 <= v < 1e12:
                clean[str(k).lower().strip()[:24]] = float(v)
        self.prev, self.obs = self.obs, clean
        self.obs_idx += 1
        deltas = {}
        for k, v in clean.items():
            old = self.prev.get(k)
            if old is None:
                deltas[k] = v                       # appeared
                self.last_gain_idx[k] = self.obs_idx
            elif abs(v - old) > max(0.4, 0.004 * abs(old)):
                deltas[k] = v - old
                if v > old:
                    self.last_gain_idx[k] = self.obs_idx
        return deltas

    def satisfaction(self, state: str, deltas: dict) -> bool:
        if not deltas:
            return False
        if state == "GATHER":
            return any(v > 0 for k, v in deltas.items()
                       if k != self.currency and k not in self.items)
        if state == "CRAFT":
            appeared = any(k not in self.items and v > 0
                           for k, v in deltas.items())
            consumed = any(v < 0 for k, v in deltas.items()
                           if k != self.currency)
            return appeared or consumed
        if state == "SELL":
            if self.currency and deltas.get(self.currency, 0) > 0:
                return True
            return any(v < 0 for k, v in deltas.items()
                       if k in self.items) \
                and any(v > 0 for k, v in deltas.items())
        return False

    def _mass(self) -> float:
        return sum(max(0.0, v) for k, v in self.obs.items()
                   if k != self.currency and k not in self.items)

    def _mass_ratio(self) -> float:
        m = self._mass()
        self.mass_max = max(self.mass_max, m)
        return (m / self.mass_max) if self.mass_max > 0 else 0.0

    def _gather_stalled(self) -> bool:
        last = max((self.last_gain_idx.get(k, -99)
                    for k in self.obs
                    if k != self.currency and k not in self.items),
                   default=-99)
        return (self.obs_idx - last) >= 2

    def decide(self, drives, stuck: int) -> str:
        if self.loop and drives.avarice > 0.72:
            return self.loop[self.loop_i % len(self.loop)]
        if stuck >= 10:
            return "EXPLORE"
        if drives.boredom > 0.78:
            return "EXPLORE"
        have_items = any(v > 0 for k, v in self.obs.items() if k in self.items)
        if have_items and self.obs_idx >= self.sell_block:
            want = "SELL"
        elif (self._mass_ratio() > 0.45
              and self.obs_idx >= self.craft_block) \
                or (self._gather_stalled() and self._mass_ratio() > 0.45):
            want = "CRAFT"
        else:
            want = "GATHER"
        if (want != self.state and self.state in ("GATHER", "CRAFT")
                and want in ("GATHER", "CRAFT") and self.state_age < 2):
            return self.state                    # hysteresis: no flapping
        if want != self.state:
            self.state, self.state_age = want, 0
        else:
            self.state_age += 1
        return self.state

    def apply(self, state: str, deltas: dict, satisfied: bool, drives):
        if not satisfied or not deltas:
            if state == "SELL":
                self.sell_fail += 1
                if self.sell_fail >= 6:
                    self.sell_block = self.obs_idx + 15
                    self.sell_fail = 0
            if state == "CRAFT":
                self.craft_fail += 1
                if self.craft_fail >= 6:
                    self.craft_block = self.obs_idx + 15
                    self.craft_fail = 0
            return
        self.sell_fail = self.craft_fail = 0
        if state == "CRAFT":
            for k, v in deltas.items():
                if v > 0 and k not in self.items and k != self.currency:
                    self.items.add(k)            # appeared on craft -> item
            if self.state == "CRAFT":
                self.state, self.state_age = "SELL", 0
        elif state == "SELL":
            for k, v in deltas.items():
                if v > 0:
                    self.currency_votes[k] = self.currency_votes.get(k, 0) + 1
            best = max(self.currency_votes, key=self.currency_votes.get,
                       default=None)
            if best and self.currency_votes.get(best, 0) >= 2:
                if best != self.currency:
                    self.log.info("ECON   | currency identified: %r", best)
                self.currency = best
            if self.state == "SELL":
                self.state, self.state_age = "GATHER", 0
        if state == "SELL" and self.currency \
                and deltas.get(self.currency, 0) > 0:
            if not ("GATHER" in self.loop and "CRAFT" in self.loop):
                self.loop = ["GATHER", "CRAFT", "SELL"]
                self.loop_i = 2
                drives.avarice = min(1.0, max(drives.avarice, 0.9))
                self.log.info("MONEY LOOP locked: gather->craft->sell | "
                              "avarice %.2f", drives.avarice)
        if self.loop and drives.avarice > 0.72:
            if self.loop[self.loop_i % len(self.loop)] == state:
                self.loop_i = (self.loop_i + 1) % len(self.loop)


@dataclass
class Drives:
    curiosity: float = 0.70
    boredom: float = 0.15
    avarice: float = 0.35

# ═════════════════ 8. MEMORY ═════════════════════════════════════════════════

class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.cache = SceneCache()
        self.dead: dict = {}
        self.econ: dict = {}
        self.stats = {"vlm_calls": 0, "cache_hits": 0, "blind": 0,
                      "clicks": 0, "econ_ok": 0}
        self.sessions = 0

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
            for k, v in (raw.get("cache") or {}).items():
                v["uses"] = 0
                self.cache.entries[str(k)] = v
            self.dead = raw.get("dead") or {}
            self.econ = raw.get("econ") or {}
            st = raw.get("stats") or {}
            self.stats.update({k: int(v) for k, v in st.items()
                               if k in self.stats})
            self.sessions = int(raw.get("sessions", 0))
        except Exception as e:
            print("memory partially unreadable: %s" % e, file=sys.stderr)

    def save(self, econ_blob: dict):
        try:
            self.cache.trim()
            dead = {k: v for k, v in self.dead.items()
                    if v.get("until", 0) > time.time() - 3600}
            data = {"version": 2, "cache": self.cache.entries, "dead": dead,
                    "econ": econ_blob, "stats": self.stats,
                    "sessions": self.sessions}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print("memory save failed: %s" % e, file=sys.stderr)

# ═════════════════ 9. THE AGENT ═════════════════════════════════════════════

GOAL_TEXT = {
    "GATHER": "gather resources (mine, chop, collect, harvest, claim) -- "
              "click the primary action button in the ACTION zone",
    "CRAFT": "craft items (forge, smelt, build, combine) -- click the "
             "primary craft button in the ACTION zone",
    "SELL": "sell items at the market (sell, list) -- click the primary "
            "sell button in the ACTION zone",
    "EXPLORE": "explore: open something not yet tried (a menu, a panel) to "
               "discover new game features",
}


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = build_logger(cfg)
        self.vlm = make_vision(cfg, self.log)
        self.gov = Governor(cfg, self.log)
        self.policy = SpatialPolicy(cfg, self.log)
        self.memory = Memory(Path(cfg.memory_path))
        self.memory.load()
        self.econ = Economy(self.log, cfg.start_state)
        self.econ.load(self.memory.econ)
        self.drives = Drives()
        self.dead = self.memory.dead
        self.vlm_ok = False
        self.page = None
        self.context = None
        self.stop = {"flag": False}
        self.cycle = 0
        self.stuck = 0
        self.scene_hex = ""
        self.last_scene_hex = ""
        self.last_ask_key = ""
        self.last_ask_ts = 0.0
        self.last_asked_state = ""
        self.pending = None          # (state, x, y, scene_hex, cache_key, ts)
        self.recent: deque = deque(maxlen=12)   # (x, y, ts, kind) page coords
        self.inv_scale = 1.0
        self._last_chips: list = []
        self._last_reload = 0.0
        self._console_errors = 0
        self.shots_dir = Path(os.environ.get("SHOTS_DIR", "shots"))

    # ---------------------------------------------------------- screenshots

    def _shot(self, name: str, img: Image.Image):
        if not self.cfg.save_shots:
            return
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(self.shots_dir / name, "JPEG",
                                    quality=80)
            files = sorted(self.shots_dir.glob("*.jpg"))
            for old in files[:-400]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------ selftest

    async def _vlm_selftest(self):
        """One tiny call that proves the whole API pipeline before we play.
        A 429 counts as OK (it means the key authenticated)."""
        img = Image.new("RGB", (64, 64), (24, 24, 24))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        try:
            txt = await self.vlm.ask(buf.getvalue(),
                                     'Reply with exactly {"ok":true}')
            self.vlm_ok = True
            self.log.info("VISION | selftest OK on %s: %s",
                          getattr(self.vlm, "model", "?"), (txt or "")[:60])
        except RateLimited as e:
            self.vlm_ok = True       # key is valid; just throttled right now
            self.log.warning("VISION | selftest rate-limited (valid key, "
                             "retry_after=%s)", e.retry_after)
        except Exception as e:
            self.vlm_ok = False
            self.log.error("VISION | SELFTEST FAILED -> %s", str(e)[:400])
            self.log.error("VISION | likely fixes: rotate the key / enable "
                           "the Generative Language API / pass "
                           "--model gemini-2.5-flash")

    # --------------------------------------------------------------- login

    async def _try_click(self, patterns) -> bool:
        for pat in patterns:
            try:
                b = self.page.get_by_role("button",
                                          name=re.compile(pat, re.I)).first
                if await b.count():
                    await b.click(timeout=2500)
                    return True
            except Exception:
                continue
        return False

    async def login(self) -> bool:
        page, log = self.page, self.log
        log.info("LOGIN  | navigating to %s", self.cfg.url)
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

        # cookie / age gates
        await self._try_click((r"^accept( all)?$", r"^agree", r"^got it$",
                               r"^ok$", r"^i understand$"))
        await page.wait_for_timeout(1200)

        opened_login = False
        email_stages = 0
        filled = False
        deadline = time.time() + 45
        while time.time() < deadline and not filled:
            try:
                n_pw = await page.locator('input[type="password"]').count()
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
                    submitted = False
                    try:
                        await pw.press("Enter")
                        submitted = True
                    except Exception:
                        pass
                    if not submitted:
                        await self._try_click((r"sign\s*in", r"log\s*in",
                                               r"^continue$", r"^submit$",
                                               r"^enter$"))
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
            if n_em and email_stages < 3:
                email_stages += 1
                try:
                    em = page.locator(EMAIL_LOC).first
                    await em.click(timeout=4000)
                    await em.fill(EMAIL, timeout=4000)
                    log.info("LOGIN  | email entered (stage %d)", email_stages)
                    await self._try_click((r"^continue$", r"^next$",
                                           r"submit", r"sign\s*in",
                                           r"log\s*in", r"^enter$"))
                    await page.wait_for_timeout(2500)
                    continue
                except Exception as e:
                    log.debug("LOGIN  | email stage issue: %s", str(e)[:100])
            elif not opened_login:
                # maybe the login form is behind a button
                opened_login = True
                if await self._try_click((r"^log\s*in$", r"^sign\s*in$",
                                         r"^login$", r"play now",
                                         r"enter.*game")):
                    log.info("LOGIN  | opened the login UI")
                    await page.wait_for_timeout(2500)
                    continue
            await page.wait_for_timeout(1500)

        if not filled:
            log.warning("LOGIN  | no login form appeared after 45s -- "
                        "continuing as guest/session (see "
                        "shots/login_end.jpg)")

        # wait for the form to go away / game to render (max 60s)
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                if await page.locator('input[type="password"]').count() == 0:
                    break
            except Exception:
                break
            await page.wait_for_timeout(2000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self._shot("login_end.jpg", await self.capture())
        log.info("LOGIN  | done | url=%s | title=%r", page.url[:90],
                 (await page.title())[:60])
        return True

    # ------------------------------------------------------------ capture

    async def capture(self):
        png = await self.page.screenshot(full_page=False)
        return Image.open(io.BytesIO(png)).convert("RGB")

    async def dom_chips(self, scale: float) -> list:
        if self.cfg.pure_vision:
            return []
        try:
            rects = await self.page.evaluate(JS_RECTS)
        except Exception:
            return []
        out = []
        for r in rects or []:
            out.append({"x": int(r["x"] * scale), "y": int(r["y"] * scale),
                        "w": int(r["w"] * scale), "h": int(r["h"] * scale),
                        "score": 1.0})
        return _merge_boxes(out, 24)

    # ------------------------------------------------------------- decide

    async def _decide(self, img, scale: float) -> Decision:
        cfg = self.cfg
        W, H = img.size
        chips = detect_chips(img)
        dom = await self.dom_chips(scale)
        if dom:
            chips = _merge_boxes(dom + chips, 24)
        self._last_chips = chips
        self.scene_hex = "%016x" % dhash(img)
        state = self.econ.state
        key = "%s:%s" % (state, self.scene_hex)

        state_changed = state != self.last_asked_state
        fresh_scene = (hamming(int(self.scene_hex, 16),
                               int(self.last_scene_hex, 16)) >= 8
                       if self.last_scene_hex else True)
        replay = self.memory.cache.get_replayable(key)
        same_key_retry_ok = not (key == self.last_ask_key
                                 and time.time() - self.last_ask_ts
                                 < 2 * cfg.min_interval)
        forced = state_changed or self.stuck >= 3 or not replay

        if replay and not (state_changed or self.stuck >= 3):
            self.memory.stats["cache_hits"] += 1
            self.log.info("SEE    | scene %s cached for %s -> replay (free)",
                          self.scene_hex[:8], state)
            self.last_scene_hex = self.scene_hex
            return replay

        if self.gov.allow(force=forced) and same_key_retry_ok:
            ann = annotate(img, chips)
            self._shot("ask_c%04d_%s.jpg" % (self.cycle, state.lower()), ann)
            buf = io.BytesIO()
            ann.save(buf, "JPEG", quality=75)
            prompt = build_prompt(GOAL_TEXT.get(state, state),
                                  chips, self.econ.obs)
            try:
                text = await self.vlm.ask(buf.getvalue(), prompt)
                dec = parse_decision(text)
                self.gov.called()
                self.memory.stats["vlm_calls"] += 1
                self.last_ask_key, self.last_ask_ts = key, time.time()
                self.last_asked_state = state
                self.memory.cache.put(key, dec)
                if dec.obs:
                    deltas = self.econ.ingest(dec.obs)
                    self._judge_pending(deltas)
                    if deltas:
                        self.log.info("ECON   | observed: %s",
                                      ", ".join("%s %+g" % kv
                                                for kv in deltas.items()))
                self.log.info("SEE    | %s: %r [%s conf %.2f] %s",
                              state, dec.label, dec.kind, dec.confidence,
                              ("- " + dec.scene[:60]) if dec.scene else "")
                if dec.notes:
                    self.log.info("NOTES  | %s", dec.notes)
                self.last_scene_hex = self.scene_hex
                return dec
            except RateLimited as e:
                self.gov.rate_limited(e.retry_after)
            except Transient as e:
                self.gov.transient_fail()
                self.log.warning("VISION  | transient: %s", str(e)[:220])
        return self._local_decision(chips, W, H, state)

    def _local_decision(self, chips: list, W: int, H: int,
                         state: str) -> Decision:
        """Blind mode: saliency + spatial prior, no API spend.
        Center-zone only when pursuing an economic goal (no nav-bar
        nomination spam); out-of-center explore picks are typed 'nav' so
        they pass the policy as navigation, not as fake primaries."""
        self.memory.stats["blind"] += 1
        now = time.time()
        alive = []
        for c in chips:
            k = SpatialPolicy._dead_key(
                self.scene_hex, c["x"] + c["w"] // 2, c["y"] + c["h"] // 2)
            rec = self.dead.get(k)
            if rec and rec.get("until", 0) > now:
                continue
            gk = SpatialPolicy._dead_key(None, c["x"] + c["w"] // 2,
                                        c["y"] + c["h"] // 2)
            grec = self.dead.get(gk)
            if grec and grec.get("until", 0) > now:
                continue
            alive.append(c)
        if not alive:
            if self.cycle % 3 == 0:
                return Decision(kind="scroll", source="local", label="shake")
            return Decision(kind="wait", source="local", label="blind wait",
                            notes="no live chips")
        explore = (state == "EXPLORE")
        pool = alive
        if not explore:
            center = [c for c in alive
                      if zone_of(c["x"] + c["w"] / 2, c["y"] + c["h"] / 2,
                                 W, H) == "center"]
            if center:
                pool = center

        def worth(c):
            z = zone_of(c["x"] + c["w"] / 2, c["y"] + c["h"] / 2, W, H)
            prior = ZONE_PRIOR.get(z, 0.3)
            if explore and z != "center":
                prior += 0.5
            recent_use = 0
            for (px, py, ts, _k) in self.recent:
                if (now - ts < 120
                        and abs(px * self.inv_scale - c["x"]) < 40
                        and abs(py * self.inv_scale - c["y"]) < 40):
                    recent_use += 1
            return (0.8 * prior + 0.5 * c["score"] - 0.25 * recent_use
                    + random.uniform(0, 0.15))

        best = max(pool, key=worth)
        bz = zone_of(best["x"] + best["w"] / 2, best["y"] + best["h"] / 2,
                     W, H)
        return Decision(kind=("primary" if (explore or bz == "center")
                              else "nav"),
                        source="local",
                        x=best["x"] + best["w"] // 2,
                        y=best["y"] + best["h"] // 2,
                        label="blind chip @(%d,%d)" % (best["x"], best["y"]),
                        confidence=0.2)

    def _judge_pending(self, deltas: dict):
        """Close the loop: did the last click actually move the economy?"""
        if not self.pending or not deltas:
            return
        state, x, y, scene_hex, ckey, ts = self.pending
        if time.time() - ts > 600:
            self.pending = None
            return
        sat = self.econ.satisfaction(state, deltas)
        self.econ.apply(state, deltas, sat, self.drives)
        if sat:
            self.memory.stats["econ_ok"] += 1
            self.memory.cache.mark(ckey, "success")
            self._clear_dead(scene_hex, x, y)
            self.stuck = 0
            self.drives.boredom = max(0.0, self.drives.boredom - 0.35)
            if self.econ.currency and deltas.get(self.econ.currency, 0) > 0:
                self.drives.avarice = min(1.0, self.drives.avarice + 0.2)
            self.log.info("ECON   | %s CONFIRMED by observation deltas", state)
        self.pending = None

    def _clear_dead(self, scene_hex: str, x: int, y: int):
        for k in (SpatialPolicy._dead_key(scene_hex, x, y),
                  SpatialPolicy._dead_key(None, x, y)):
            self.dead.pop(k, None)

    def _bump_dead(self, scene_hex: str, x: int, y: int):
        newly_dead = False
        for k in (SpatialPolicy._dead_key(scene_hex, x, y),
                  SpatialPolicy._dead_key(None, x, y)):
            rec = self.dead.setdefault(k, {"fails": 0, "until": 0})
            rec["fails"] += 1
            if rec["fails"] >= 2 and rec.get("until", 0) <= time.time():
                rec["until"] = time.time() + 150
                rec["fails"] = 0
                newly_dead = True
        if newly_dead:
            self.log.info("POLICY  | chip at (%d,%d) went dead for 150s", x, y)

    # --------------------------------------------------------------- act

    async def act(self, dec: Decision):
        page = self.page
        if dec.kind in ("wait", "none"):
            await asyncio.sleep(random.uniform(3.0, 6.0))
            return
        if dec.kind == "scroll":
            try:
                await page.mouse.wheel(0, random.choice((600, 900, -600)))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(1.0, 1.8))
            return
        x = int(dec.x * self.inv_scale)
        y = int(dec.y * self.inv_scale)
        x = max(3, min(self.cfg.viewport_w - 4, x))
        y = max(3, min(self.cfg.viewport_h - 4, y))
        self.log.info("ACT    | %s click at page(%d,%d) [%s] %r",
                      "nav" if dec.kind == "nav" else "mouse", x, y,
                      dec.source, dec.label)
        await self._human_click(x, y)
        self.memory.stats["clicks"] += 1
        self.recent.append((x, y, time.time(),
                            "nav" if dec.kind == "nav" else dec.kind))
        self.pending = (self.econ.state, dec.x, dec.y, self.scene_hex,
                        "%s:%s" % (self.econ.state, self.scene_hex),
                        time.time())
        await asyncio.sleep(random.uniform(2.0, 3.5))   # let animations settle

    async def _human_click(self, x: int, y: int):
        jx, jy = x + random.uniform(-3, 3), y + random.uniform(-3, 3)
        try:
            await self.page.mouse.move(jx, jy, steps=random.randint(5, 9))
            await asyncio.sleep(random.uniform(0.05, 0.16))
            await self.page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.11))
            await self.page.mouse.up()
        except Exception as e:
            self.log.debug("click failed: %s", str(e)[:100])

    # ------------------------------------------------------------ reflect

    async def _reflect(self, pre_img, post_img, dec: Decision):
        changed = False
        if (dec.kind in ("primary", "nav", "dialog_confirm", "dialog_decline")
                and dec.x is not None):
            px = int((dec.x or 0) * self.inv_scale)     # page coords
            py = int((dec.y or 0) * self.inv_scale)
            rd = region_diff(pre_img, post_img, (px, py, 4, 4))
            gd = pixel_diff(pre_img, post_img)
            changed = (rd > 4.5) or (hamming(dhash(pre_img),
                                             dhash(post_img)) >= 5)
            self.log.info("REFLECT| %s | region diff %.1f | global %.1f",
                          "world changed" if changed else "nothing happened",
                          rd, gd)
        if changed:
            self.stuck = 0
            self.drives.boredom = max(0.0, self.drives.boredom - 0.10)
        else:
            self.stuck += 1
            self.drives.boredom = min(1.0, self.drives.boredom + 0.12)
            self.drives.curiosity = min(1.0, self.drives.curiosity + 0.06)
            if (dec.kind in ("primary", "nav")
                    and dec.x is not None):
                self._bump_dead(self.scene_hex, dec.x, dec.y)
                ckey = "%s:%s" % (self.econ.state, self.scene_hex)
                if dec.source in ("vlm", "cache"):
                    self.memory.cache.mark(ckey, "fail")

    # -------------------------------------------------------------- loop

    async def run(self):
        cfg = self.cfg
        self.memory.sessions += 1
        self.log.info("boot | %s | session #%d | memory: %d cached scenes, "
                      "%d dead chips, currency=%r | shots=%s", cfg.url,
                      self.memory.sessions, len(self.memory.cache.entries),
                      len(self.dead), self.econ.currency or "?",
                      "on" if cfg.save_shots else "off")
        async with async_playwright() as pw:
            browser = None
            common = dict(headless=cfg.headless, args=_chromium_args(cfg),
                          slow_mo=cfg.slowmo)
            try:
                if cfg.user_data_dir:
                    self.context = await pw.chromium.launch_persistent_context(
                        cfg.user_data_dir,
                        viewport={"width": cfg.viewport_w,
                                  "height": cfg.viewport_h},
                        device_scale_factor=1,   # screenshot px == CSS px
                        ignore_https_errors=True, **common)
                else:
                    browser = await pw.chromium.launch(**common)
                    self.context = await browser.new_context(
                        viewport={"width": cfg.viewport_w,
                                  "height": cfg.viewport_h},
                        device_scale_factor=1, ignore_https_errors=True,
                        user_agent=cfg.user_agent)
            except Exception as e:
                self.log.error("browser launch failed: %s", e)
                return
            await self.context.add_init_script(JS_STEALTH)
            self.page = (self.context.pages[0] if self.context.pages
                         else await self.context.new_page())
            self.page.set_default_timeout(15000)
            self.page.on("dialog", self._on_dialog)
            self.page.on("console", self._on_console)

            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, self.stop.update,
                                            {"flag": True})
                except (NotImplementedError, RuntimeError):
                    pass

            await self._vlm_selftest()                 # truth in 5 seconds
            if not self.vlm_ok and cfg.require_vlm:
                self.log.error("VISION | --require-vlm set and selftest "
                              "failed; exiting so CI fails visibly")
                try:
                    await self.context.close()
                    if browser:
                        await browser.close()
                except Exception:
                    pass
                sys.exit(2)

            if not await self.login():
                return
            t0 = time.time()
            try:
                await self._loop(t0)
            finally:
                self.memory.save(self.econ.to_dict())
                st = self.memory.stats
                self.log.info("memory persisted | VLM calls %d | cache hits "
                              "%d | blind %d | clicks %d | econ confirmed %d",
                              st["vlm_calls"], st["cache_hits"], st["blind"],
                              st["clicks"], st["econ_ok"])
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
                    self.log.info("stop-file detected"); break
                if cfg.max_cycles and self.cycle >= cfg.max_cycles:
                    self.log.info("cycle budget reached"); break
                if cfg.max_minutes and (time.time() - t0) / 60 >= cfg.max_minutes:
                    self.log.info("time budget reached"); break
                if self.page.is_closed():
                    await self._recover(); continue

                self.cycle += 1
                pre = await self.capture()
                img, scale = prepare(pre)
                self.inv_scale = 1.0 / scale
                state = self.econ.decide(self.drives, self.stuck)

                dec = await self._decide(img, scale)
                final, reason = self.policy.approve(
                    dec, self._last_chips, img.size[0], img.size[1],
                    state, self.drives.boredom, self.dead, self.recent,
                    self.scene_hex)
                if final is None:
                    self.log.info("POLICY  | rejected (%s); falling back "
                                  "to wait", reason)
                    final = Decision(kind="wait", source="local",
                                     notes="policy: " + reason)

                await self.act(final)
                post = await self.capture()
                await self._reflect(pre, post, final)

                if self.stuck >= 10 and time.time() - self._last_reload > 300:
                    self._last_reload = time.time()
                    self.log.warning("RECOVER | hard reload (stuck %d)",
                                     self.stuck)
                    try:
                        await self.page.reload(
                            wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass

                if self.cycle % 20 == 0:
                    self._status()
                if self.cycle % 25 == 0:
                    self.memory.save(self.econ.to_dict())
                await asyncio.sleep(random.uniform(0.6, 1.4))
            except Exception:
                self.log.error("cycle %d crashed:\n%s", self.cycle,
                               traceback.format_exc()[-700:])
                await asyncio.sleep(2.0)

    def _status(self):
        st = self.memory.stats
        bank = ("%s %s" % (self.econ.currency,
                           int(self.econ.obs.get(self.econ.currency, 0)))
                if self.econ.currency else "unknown")
        items = (", ".join("%s x%d" % (k, int(v))
                           for k, v in self.econ.obs.items()
                           if k in self.econ.items and v > 0)[:80]
                 or "none")
        self.log.info("STATUS | c%d | %s | bank %s | items %s | stuck %d | "
                      "cur %.2f bor %.2f ava %.2f | calls %d cache %d "
                      "blind %d",
                      self.cycle, self.econ.state, bank, items, self.stuck,
                      self.drives.curiosity, self.drives.boredom,
                      self.drives.avarice, st["vlm_calls"],
                      st["cache_hits"], st["blind"])

    async def _recover(self):
        self.log.warning("page closed; reopening")
        try:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(15000)
            self.page.on("dialog", self._on_dialog)
            self.page.on("console", self._on_console)
            await self.page.goto(self.cfg.url,
                                 wait_until="domcontentloaded",
                                 timeout=60000)
            await self.login()
        except Exception as e:
            self.log.error("recovery failed: %s", e)
            await asyncio.sleep(2.0)

    # ------------------------------------------------- browser diplomacy

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

    def _on_console(self, m):
        try:
            if m.type == "error":
                self._console_errors += 1
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
