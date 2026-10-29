#!/usr/bin/env python3
"""Virality pass: re-render clips with an A/B on-screen title, an early flash frame, an end card, and brightened covers.

Reads each concept's `virality` block in media/working/ideas.checked.json. Writes to media/ready_v2/ and never
touches media/ready/. Same cut and captions as the original render (render.py), plus:
  - title (A or B) 0-2 s, two lines, auto-sized, top third
  - at 0.5 s a hard cut to the clip's most unsettling frame (flash.len s), then back into the scene
  - a 1.5 s end card appended after the last frame (dimmed freeze + twist line or King quote + CTA + film title)
  - cover JPG per clip and platform: clean frame, brightened ~25%, 3-4 word hook, 1:1 grid-crop check

Usage: virality_render.py <slug> <job> [<job> ...]    job = c01_tiktok_A, c03_instagram_B, ...
       virality_render.py <slug> --covers
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipcrumble import render as R  # noqa: E402
from clipcrumble.util import FONTS_DIR, Campaign, ffmpeg, load_json  # noqa: E402

FONT = "/System/Library/Fonts/Supplemental/Impact.ttf"       # titles + covers: condensed, reads large
END_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
TEXT_W = 1080 - R.SAFE_L - R.SAFE_R          # 840 px: lower screen (platform buttons on the right)
TITLE_W = 960                                 # top third has no side buttons


def two_lines(text: str, max_fs: int, width: int = TEXT_W, font: str = FONT) -> tuple[list[str], int]:
    """Split into at most 2 balanced lines and return the largest font size (<= max_fs) that fits the width."""
    w = text.split()
    cands = [[text]] + [[" ".join(w[:i]), " ".join(w[i:])] for i in range(1, len(w))]
    best = None
    for lines in cands:
        for fs in range(max_fs, 40, -2):
            f = ImageFont.truetype(font, fs)
            if max(f.getlength(l) for l in lines) <= width:
                break
        score = (fs, -abs(len(lines[0]) - len(lines[-1])))
        if len(lines) == 2 or len(w) <= 3:
            if best is None or score > best[0]:
                best = (score, lines, fs)
    return best[1], best[2]


def segs_for(ctx, concept, sib, flash: dict | None):
    segs = R.resolve(concept, sib)
    ctx_ovl = concept.get("context_overlays", [])
    if flash:
        # find the source position of the flash frame on the original timeline
        t0 = 0.0
        for s in segs:
            d = s["out"] - s["in"]
            if t0 <= flash["t"] < t0 + d:
                fsrc = dict(src=s["src"], in_=s["in"] + flash["t"] - t0, crop=s.get("crop"))
                break
            t0 += d
        s0 = segs[0]; at = flash["at"]
        a = dict(s0, out=s0["in"] + at)
        b = dict(s0, **{"in": s0["in"] + at}); b["_zoom_punch"] = s0["_zoom_punch"]
        fl = dict(src=fsrc["src"], crop=fsrc["crop"], _zoom_punch=None, _flash=True)
        fl["in"] = fsrc["in_"]; fl["out"] = fsrc["in_"] + flash["len"]
        segs = [a, fl, b] + segs[1:]
    for s in segs:
        fps = ctx.fps(s["src"])
        s["_n"] = max(1, round((s["out"] - s["in"]) * fps))
        s["_dur"] = float(s["_n"] / fps)
        s["_ctx"] = [] if s.get("_flash") else [r for r in ctx_ovl if r["src"] == s["src"] and r["in"] < s["out"] and r["out"] > s["in"]]
    return segs


def wrap(text: str, fs_max: int, width: int, max_lines: int) -> tuple[list[str], int]:
    """Greedy wrap into <= max_lines lines at the largest font size that fits."""
    for fs in range(fs_max, 30, -2):
        f = ImageFont.truetype(FONT, fs); lines, cur = [], ""
        for w in text.split():
            nxt = (cur + " " + w).strip()
            if f.getlength(nxt) <= width:
                cur = nxt
            else:
                lines.append(cur); cur = w
        lines.append(cur)
        if len(lines) <= max_lines and all(f.getlength(l) <= width for l in lines):
            return lines, fs
    return [text], 30


def end_card_lines(ec: dict) -> str:
    out = []
    if ec["kind"] == "king":
        l, fs = wrap(ec["text"], 96, TITLE_W, 2)
        out += [r"{\fs%d}%s" % (fs, x) for x in l]
        out.append(r"{\fs56}" + ec["by"])
    else:
        l, fs = wrap(ec["text"], 92, TITLE_W, 3)
        out += [r"{\fs%d}%s" % (fs, x) for x in l]
    out.append(r"{\fs40} ")
    _, tfs = wrap(ec["title"], 160, TITLE_W, 1)
    out.append(r"{\fs%d}%s" % (tfs, ec["title"]))
    out.append(r"{\fs80\c&H0040C8FF&}" + ec["cta"])
    return r"\N".join(out)


def build_ass(ctx, concept, sib, segs, total, title: str, path: Path, flash_win, tail: float):
    lines, fs = two_lines(title, 120, width=TITLE_W)
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Impact,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,8,3,8,60,60,{R.HOOK_MARGIN_V},1
Style: Cap,Arial Black,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,{R.SAFE_L},{R.SAFE_R},{R.CAP_MARGIN_V},1
Style: Sub,Arial,66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,{R.SAFE_L},{R.SAFE_R},{R.CAP_MARGIN_V},1
Style: Ctx,Arial Black,62,&H0080F0FF,&H0080F0FF,&H00000000,&H64000000,-1,-1,0,0,100,100,0,0,1,6,2,2,{R.SAFE_L},{R.SAFE_R},{R.CAP_MARGIN_V},1
Style: Dim,Arial,20,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: End,Impact,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    L = [f"Dialogue: 5,{R.ass_t(0)},{R.ass_t(2.0)},Hook,,0,0,0,,{R.esc(chr(10).join(lines))}"]
    caps = [e for e in R.caption_events(ctx, segs) if not (flash_win[0] - 0.01 <= e[0] < flash_win[1] - 0.01)]
    for st, en, txt, kind in caps:
        if en > st and kind != "burned":
            style = {"cap": "Cap", "sub": "Sub"}.get(kind, "Ctx")
            L.append(f"Dialogue: 1,{R.ass_t(st)},{R.ass_t(min(en, total))},{style},,0,0,0,,{R.esc(txt)}")
    ec = concept["virality"]["end_card"]
    L.append(f"Dialogue: 3,{R.ass_t(total)},{R.ass_t(total + tail)},Dim,,0,0,0,,"
             r"{\1a&H48&\bord0\shad0\p1}m 0 0 l 1080 0 1080 1920 0 1920{\p0}")
    L.append(f"Dialogue: 4,{R.ass_t(total)},{R.ass_t(total + tail)},End,,0,0,0,,{end_card_lines(ec)}")
    path.write_text(hdr + "\n".join(L) + "\n", encoding="utf-8")
    return dict(title_lines=lines, title_fs=fs, captions=len(caps))


def job(c: Campaign, ctx, concepts: dict, jid: str) -> dict:
    cid, plat, var = jid.split("_")
    concept = concepts[cid]; sib = next(s for s in concept["siblings"] if s["platform"] == plat)
    v = concept["virality"]
    out_dir = c.root / "media" / "ready_v2"; out_dir.mkdir(exist_ok=True)
    segs = segs_for(ctx, concept, sib, v["flash"])
    total = sum(s["_dur"] for s in segs)
    fw = (segs[0]["_dur"], segs[0]["_dur"] + segs[1]["_dur"])
    tail = float(v["end_card"]["seconds"])
    ass = c.working / "ass_v2" / f"{jid}.ass"; ass.parent.mkdir(parents=True, exist_ok=True)
    info = build_ass(ctx, concept, sib, segs, total, v["titles"][var]["text"], ass, fw, tail)
    sd = c.working / "seg_v2" / jid
    out = out_dir / f"{jid}.mp4"
    L = ["#!/bin/bash", "set -euo pipefail", f"FF={shlex.quote(ffmpeg())}", f"SD={shlex.quote(str(sd))}",
         'rm -rf "$SD"; mkdir -p "$SD"']
    for k, s in enumerate(segs):
        src = c.source / ctx.inv[s["src"]]["file"]
        fps = ctx.fps(s["src"])
        a_in = (f"[0:a]aresample=48000,afade=t=in:d=0.008,afade=t=out:st={max(0, s['_dur'] - 0.008):.4f}:d=0.008[a]"
                if ctx.inv[s["src"]]["audio"] else f"anullsrc=r=48000:cl=stereo,atrim=0:{s['_dur']:.4f}[a]")
        L.append(f'"$FF" -v error -y -ss {s["in"]:.3f} -i {shlex.quote(str(src))} -t {s["_dur"]:.4f} '
                 f'-filter_complex "[0:v]{R.vf_for(ctx, s, s["_zoom_punch"])}[v];{a_in}" -map "[v]" -map "[a]" '
                 f'-frames:v {s["_n"]} -c:v libx264 -qp 0 -preset ultrafast -pix_fmt yuv420p -r {fps} '
                 f'-c:a pcm_s16le -ac 2 "$SD/{k:02d}.mkv"')
        L.append(f"echo \"file '{k:02d}.mkv'\" >> \"$SD/list.txt\"")
    L += ['"$FF" -v error -y -f concat -safe 0 -i "$SD/list.txt" -c copy "$SD/joined.mkv"',
          f'LN=$("$FF" -hide_banner -i "$SD/joined.mkv" -vn -af loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 '
          f'| {shlex.quote(sys.executable)} -c "{R.LN_PY}")',
          f'"$FF" -v error -y -i "$SD/joined.mkv" -vf "fps=30,tpad=stop_mode=clone:stop_duration={tail},'
          f'ass={shlex.quote(str(ass))}:fontsdir={FONTS_DIR}" '
          f'-af "loudnorm=I=-14:TP=-1.5:LRA=11:$LN,apad=pad_dur={tail}" -c:v libx264 -preset slow -crf 20 -r 30 '
          f'-pix_fmt yuv420p -c:a aac -b:a 128k -ar 48000 -movflags +faststart -shortest {shlex.quote(str(out))}',
          'rm -rf "$SD"']
    sh = c.working / "render_v2" / f"{jid}.sh"; sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text("\n".join(L) + "\n")
    r = subprocess.run(["bash", str(sh)], capture_output=True, text=True)
    return dict(job=jid, ok=r.returncode == 0, err=r.stderr[-600:], total=round(total + tail, 2),
                flash_window=[round(x, 2) for x in fw], **info)


# ------------------------------------------------------------------ covers
def clean_frame(c: Campaign, ctx, concept, sib, t: float, dst: Path):
    segs = R.resolve(concept, sib); t0 = 0.0
    for s in segs:
        d = s["out"] - s["in"]
        if t0 <= t < t0 + d:
            src = c.source / ctx.inv[s["src"]]["file"]
            subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", f"{s['in'] + t - t0:.3f}", "-i", str(src),
                            "-frames:v", "1", "-vf", R.vf_for(ctx, s, s["_zoom_punch"]), str(dst)], check=True)
            return
        t0 += d
    raise ValueError(f"t={t} outside clip")


def brighten(im: Image.Image, target: float) -> tuple[Image.Image, float, float]:
    """Gamma lift (shadows/midtones up, highlights held) until mean luma is +target (20-30%)."""
    a = np.asarray(im).astype(np.float32) / 255
    base = float((a @ [0.299, 0.587, 0.114]).mean())
    lo, hi = 1.0, 2.5
    for _ in range(25):
        g = (lo + hi) / 2
        m = float(((a ** (1 / g)) @ [0.299, 0.587, 0.114]).mean())
        lo, hi = (g, hi) if m < base * (1 + target) else (lo, g)
    out = (a ** (1 / g)).clip(0, 1)
    return Image.fromarray((out * 255).astype(np.uint8)), g, float((out @ [0.299, 0.587, 0.114]).mean()) / base - 1


COVER_FS = 190          # larger than the in-video title (max 120)
COVER_CY = 1180         # text block centre: inside the 1:1 centre crop (y 420-1500)


def cover(c: Campaign, ctx, concept, plat: str) -> dict:
    v = concept["virality"]["cover"]; sib = next(s for s in concept["siblings"] if s["platform"] == plat)
    d = c.root / "media" / "ready_v2" / "covers"; d.mkdir(parents=True, exist_ok=True)
    raw = d / f"{concept['id']}_{plat}_raw.png"
    clean_frame(c, ctx, concept, sib, v["t"], raw)
    im, g, gain = brighten(Image.open(raw).convert("RGB"), v.get("brighten_target", 0.25))
    raw.unlink()
    lines, fs = two_lines(v["text"], COVER_FS, width=960)
    f = ImageFont.truetype(FONT, fs)
    dr = ImageDraw.Draw(im)
    lh = int(fs * 1.08); y = COVER_CY - lh * len(lines) // 2
    boxes = []
    for i, l in enumerate(lines):
        w = f.getlength(l); x = (1080 - w) / 2
        dr.text((x, y + i * lh), l, font=f, fill=(255, 255, 255), stroke_width=12, stroke_fill=(0, 0, 0))
        boxes.append((x, y + i * lh, x + w, y + i * lh + lh))
    # red rule under the text: the series mark
    by = y + lh * len(lines) + 18
    dr.rectangle((390, by, 690, by + 14), fill=(200, 16, 32))
    top = min(b[1] for b in boxes); bot = by + 14
    sq = (0, 420, 1080, 1500)
    fits = top >= sq[1] + 10 and bot <= sq[3] - 10 and min(b[0] for b in boxes) >= 20
    out = d / f"{concept['id']}_{plat}_cover.jpg"; im.save(out, quality=92)
    im.crop(sq).save(d / f"{concept['id']}_{plat}_cover_1x1check.jpg", quality=85)
    return dict(id=concept["id"], plat=plat, t=v["t"], gamma=round(g, 2), brighten=round(gain, 3), text=v["text"],
                font_px=fs, survives_1x1=fits, file=str(out))


def main():
    slug = sys.argv[1]; c = Campaign(slug); ctx = R.Ctx(c)
    concepts = {k["id"]: k for k in load_json(c.working / "ideas.checked.json")["concepts"] if "virality" in k}
    if sys.argv[2] == "--covers":
        res = [cover(c, ctx, concepts[k], p) for k in sorted(concepts) for p in ("tiktok", "instagram")]
    else:
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda j: job(c, ctx, concepts, j), sys.argv[2:]))
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
