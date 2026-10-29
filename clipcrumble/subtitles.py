"""Burned-in text OCR (macOS Vision framework, local).

Two uses:
1. Subtitle cues — official burned-in subtitles (e.g. English subs on a foreign-language film) become the caption
   source: verbatim official lines, never invented. Render crops the original subtitle band out of 16:9 sources
   and re-sets the lines as clean captions; on vertical sources the burned subs stay and no caption is drawn on top.
2. Other burned text — logos, title cards, critic quotes, dates — tagged so the strategist can avoid or use them.
"""
from __future__ import annotations

import difflib
import subprocess
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .util import Campaign, dump_json, ffmpeg, load_json

FPS = 2.0


def _ocr_frame(rgb: np.ndarray, w: int, h: int) -> list[dict]:
    import Quartz
    import Vision
    from Foundation import NSData
    data = NSData.dataWithBytes_length_(rgb.tobytes(), rgb.nbytes)
    provider = Quartz.CGDataProviderCreateWithCFData(data)
    img = Quartz.CGImageCreate(w, h, 8, 24, w * 3, Quartz.CGColorSpaceCreateDeviceRGB(),
                               Quartz.kCGBitmapByteOrderDefault, provider, None, False, Quartz.kCGRenderingIntentDefault)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    req.setRecognitionLanguages_(["en-US"])
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)[0]
        bb = obs.boundingBox()  # normalized, origin bottom-left
        x, y, bw, bh = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
        out.append(dict(text=str(cand.string()), conf=round(float(cand.confidence()), 3),
                        box=[round(x, 4), round(1 - y - bh, 4), round(bw, 4), round(bh, 4)]))
    return out


def ocr_file(path: str, width: int, height: int, fps: float = FPS) -> list[dict]:
    scale_w = 960 if width >= height else 608
    scale_h = int(round(height * scale_w / width / 2) * 2)
    proc = subprocess.Popen([ffmpeg(), "-v", "error", "-i", path, "-vf", f"fps={fps},scale={scale_w}:{scale_h}",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    frames, n, size = [], 0, scale_w * scale_h * 3
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        arr = np.frombuffer(buf, np.uint8)
        t = round((n + 0.5) / fps, 3)
        frames.append(dict(t=t, lines=[l for l in _ocr_frame(arr, scale_w, scale_h) if l["conf"] >= 0.3]))
        n += 1
    proc.wait()
    return frames


def _is_sub(line: dict) -> bool:
    x, y, w, h = line["box"]
    cx = x + w / 2
    return abs(cx - 0.5) < 0.12 and y > 0.25 and h < 0.12


def cues_from(frames: list[dict], fps: float = FPS) -> tuple[list[dict], list[dict]]:
    step = 1 / fps
    cues, other = [], []
    cur = None
    for f in frames:
        subs = sorted([l for l in f["lines"] if _is_sub(l)], key=lambda l: l["box"][1])
        for l in f["lines"]:
            if not _is_sub(l):
                other.append(dict(t=f["t"], **l))
        text = " ".join(l["text"].strip() for l in subs).strip()
        box = None
        if subs:
            x0 = min(l["box"][0] for l in subs); y0 = min(l["box"][1] for l in subs)
            x1 = max(l["box"][0] + l["box"][2] for l in subs); y1 = max(l["box"][1] + l["box"][3] for l in subs)
            box = [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]
        if cur and text and difflib.SequenceMatcher(None, cur["_texts"][-1].lower(), text.lower()).ratio() > 0.8:
            cur["end"] = round(f["t"] + step / 2, 3); cur["_texts"].append(text); cur["_boxes"].append(box)
            continue
        if cur:
            cues.append(cur); cur = None
        if text:
            cur = dict(start=round(max(0, f["t"] - step / 2), 3), end=round(f["t"] + step / 2, 3), _texts=[text], _boxes=[box])
    if cur:
        cues.append(cur)
    out = []
    for c in cues:
        text = Counter(c["_texts"]).most_common(1)[0][0]
        bx = [b for b in c["_boxes"] if b]
        box = [round(float(np.median([b[i] for b in bx])), 4) for i in range(4)]
        out.append(dict(start=c["start"], end=c["end"], text=text, box=box, frames=len(c["_texts"])))
    # group non-subtitle text into spans
    spans = []
    for o in sorted(other, key=lambda o: (o["text"], o["t"])):
        if spans and spans[-1]["text"] == o["text"] and o["t"] - spans[-1]["end"] <= step * 1.5:
            spans[-1]["end"] = round(o["t"] + step / 2, 3)
        else:
            spans.append(dict(text=o["text"], start=round(o["t"] - step / 2, 3), end=round(o["t"] + step / 2, 3), box=o["box"]))
    return out, sorted(spans, key=lambda s: s["start"])


def _job(args):
    alias, path, w, h = args
    return alias, ocr_file(path, w, h)


def run(c: Campaign, inv: dict, workers: int = 4) -> dict:
    out = load_json(c.working / "subtitles.json", {}) or {}
    todo = [(f["alias"], str(c.source / f["file"]), f["width"], f["height"]) for f in inv["files"]
            if f["kind"] == "video" and not f["exact_duplicate_of"] and out.get(f["alias"], {}).get("sha256") != f["sha256"]]
    shas = {f["alias"]: f["sha256"] for f in inv["files"]}
    if todo:
        c.log("ingest", f"subtitle OCR (macOS Vision, {FPS} fps): {len(todo)} files")
        with ProcessPoolExecutor(workers) as ex:
            for alias, frames in ex.map(_job, todo):
                cues, other = cues_from(frames)
                band = min((q["box"][1] for q in cues), default=None)
                out[alias] = dict(sha256=shas[alias], cues=cues, other_text=other,
                                  band_top=band, cue_count=len(cues))
                dump_json(out, c.working / "subtitles.json")
                c.log("ingest", f"OCR {alias}: {len(cues)} subtitle cues, {len(other)} other text spans"
                                + (f", subtitle band from y={band:.2f}" if band is not None else ""))
    return out


# ---------------------------------------------------------------- clean cues (used by ideate + render)
def _looks_like_subtitle(q: dict) -> bool:
    t = q["text"].strip()
    letters = sum(ch.isalpha() for ch in t)
    if letters < 2 or letters / max(1, len(t.replace(" ", ""))) < 0.7:
        return False                       # OCR noise: '* 32-84', '• L* 88*'
    if any("Ѐ" <= ch <= "ӿ" for ch in t):
        return False                       # Cyrillic = misread garbage on an English-sub film
    up = sum(ch.isupper() for ch in t)
    if len(t) > 15 and up / letters > 0.6:
        return False                       # ALL-CAPS card (critic quote, title), not a sentence-case subtitle
    if " " not in t and t[-1:] not in ".?!…" and not t[:1].isupper():
        return False                       # lone lowercase fragment, e.g. logo 'impictures'
    return q.get("frames", 1) >= 2 or t[-1:] in ".?!…"


def clean(c: Campaign) -> dict[str, list[dict]]:
    """Official subtitle lines per alias: OCR noise removed, then subtitle_overrides.json applied.

    subtitle_overrides.json: [{"src": "TPW", "start": 27.0, "text": "corrected line"} | {"src", "start", "drop": true}
    | {"src", "start", "end", "text", "add": true}] — corrections checked against the frame, never guessed."""
    raw = load_json(c.working / "subtitles.json", {}) or {}
    ovr = load_json(c.working / "subtitle_overrides.json", []) or []
    out = {}
    for alias, d in raw.items():
        cues = [dict(q, verified=False) for q in d.get("cues", []) if _looks_like_subtitle(q)]
        for o in [o for o in ovr if o["src"] == alias]:
            if o.get("add"):
                cues.append(dict(start=o["start"], end=o["end"], text=o["text"], box=o.get("box", [0.3, 0.74, 0.4, 0.05]),
                                 frames=0, verified=True))
                continue
            hit = [q for q in cues if abs(q["start"] - o["start"]) < 0.3]
            if not hit and not o.get("drop"):   # override may rescue a cue the noise filter removed
                hit = [dict(q, verified=False) for q in d.get("cues", []) if abs(q["start"] - o["start"]) < 0.3]
                cues += hit
            for q in hit:
                if o.get("drop"):
                    cues.remove(q)
                else:
                    q["text"] = o["text"]; q["verified"] = True
                    if "end" in o:
                        q["end"] = o["end"]
        out[alias] = sorted(cues, key=lambda q: q["start"])
    return out


def in_range(cues: list[dict], t0: float, t1: float, min_overlap: float = 0.25) -> list[dict]:
    return [q for q in cues if min(q["end"], t1) - max(q["start"], t0) >= min_overlap]
