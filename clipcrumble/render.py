"""render: approved concepts -> one shell script per clip -> MP4 + caption.txt, then QA.

Settings carried over from DIGGER (LESSONS_LEARNED §6): frame-accurate cuts to lossless intermediates, concat by
stream copy, one final encode (fps 30, ASS burn-in, loudnorm -14 LUFS, x264 crf 20, AAC 128k (Instagram limit), faststart).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import numpy as np

from .util import FONTS_DIR, Campaign, dump_json, ffmpeg, load_json, load_yaml

SAFE_L, SAFE_R = 60, 180          # platform UI on the right
HOOK_MARGIN_V, CAP_MARGIN_V = 330, 440
CENSOR = [(r"\bfucking\b", "F***ING"), (r"\bfuck\b", "F***"), (r"\bshit\b", "S***"), (r"\bdick\b", "D***"),
          (r"\bbitch\b", "B****")]


# reads ffmpeg's loudnorm JSON from stdin, prints the second-pass parameters
LN_PY = ("import sys,json;t=sys.stdin.read();j=json.loads(t[t.rindex('{'):t.rindex('}')+1]);"
         "print('measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true'"
         "%(j['input_i'],j['input_tp'],j['input_lra'],j['input_thresh'],j['target_offset']))")


def esc(s: str) -> str:
    return s.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def ass_t(t: float) -> str:
    t = max(0.0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


class Ctx:
    def __init__(self, c: Campaign):
        self.c = c
        self.brief = load_yaml(c.root / "brief.yaml")
        from .ideate import genre_cfg
        self.genre = genre_cfg(self.brief.get("genre", "film_promo"))
        self.inv = {f["alias"]: f for f in load_json(c.working / "inventory.json")["files"]}
        tr = load_json(c.working / "transcript.json", {}) or {}
        run = load_yaml(c.root / "run.yaml", {}) or {}
        from . import subtitles
        # Official burned-in subtitles are the caption source where a file has them. A non-English transcript is a
        # rough guide only and is never burned in as captions.
        self.cues = subtitles.clean(c)
        en = str(run.get("language", "auto")).lower() in ("en", "auto")
        self.words = {a: [w for s in d["segments"] for w in s["words"]] for a, d in tr.items()
                      if en and d.get("language") == "en" and not self.cues.get(a)}
        self.overrides = load_json(c.working / "transcript_overrides.json", []) or []
        ex = load_json(c.working / "exclusions.json", {}) or {}
        self.no_caption = ex.get("no_caption", [])          # [{src,in,out,text|null}]
        self.frames = load_json(c.working / "frames.json", {}) or {}
        self.bars = {a: self._bars(a) for a in self.frames}

    def _bars(self, alias):
        """Median letterbox fractions over non-dark samples (bars are black, content usually isn't)."""
        allsm = self.frames[alias]["samples"]
        sm = [s for s in allsm if s["luma"] > 25]
        if len(sm) < 5:   # very dark file (e.g. PC5): use its brightest third instead
            sm = sorted(allsm, key=lambda s: -s["luma"])[:max(1, len(allsm) // 3)]
        if not sm:
            return dict(top=0, bottom=0, left=0, right=0)
        med = {k: float(np.median([s[k] for s in sm])) for k in ("top", "bottom", "left", "right")}
        return {k: (v if v > 0.02 else 0.0) for k, v in med.items()}

    def fps(self, alias) -> Fraction:
        return Fraction(self.inv[alias]["fps_rational"] or "24/1")


# ------------------------------------------------------------------ video
def is_vertical(f: dict) -> bool:
    return f["width"] * 16 == f["height"] * 9 or abs(f["width"] / f["height"] - 9 / 16) < 0.02


def sub_top(ctx: Ctx, seg: dict) -> float | None:
    """Top (0–1) of the burned-in subtitles shown during this segment of a widescreen source, else None."""
    if is_vertical(ctx.inv[seg["src"]]):
        return None
    from .subtitles import in_range
    tops = [q["box"][1] for q in in_range(ctx.cues.get(seg["src"], []), seg["in"], seg["out"], 0.05)]
    return min(tops) if tops else None


def vf_for(ctx: Ctx, seg: dict, zoom_punch: float | None = None) -> str:
    """Reframe to 1080x1920; crop.gamma (e.g. 1.3) lifts very dark footage for phone screens (exposure only)."""
    g = (seg.get("crop") or {}).get("gamma")
    base = _vf(ctx, seg, zoom_punch)
    return base + (f",eq=gamma={g}:saturation=1.05" if g and g != 1 else "")


def _vf(ctx: Ctx, seg: dict, zoom_punch: float | None = None) -> str:
    f = ctx.inv[seg["src"]]; W, H = f["width"], f["height"]
    crop = seg.get("crop") or {}
    mode = crop.get("mode") or ("native" if is_vertical(f) else "crop")
    b = ctx.bars.get(seg["src"], dict(top=0, bottom=0, left=0, right=0))
    # active picture after removing letterbox / pillarbox bars
    x0 = int(W * b["left"]) // 2 * 2; y0 = int(H * b["top"]) // 2 * 2
    aw = (W - x0 - int(W * b["right"])) // 2 * 2; ah = (H - y0 - int(H * b["bottom"])) // 2 * 2
    st = sub_top(ctx, seg)
    if st is not None and mode != "native":   # keep the old subtitle out of frame; the line is re-set as a caption
        ah = min(ah, (int(H * (st - 0.012)) - y0) // 2 * 2)
    z = (crop.get("zoom") or 1.0) * (zoom_punch or 1.0)
    cx = crop.get("x", 0.5)
    if mode == "native":
        if z > 1.0:
            cw = int(aw / z) // 2 * 2; ch = int(ah / z) // 2 * 2
            return f"crop={cw}:{ch}:{x0 + (aw - cw) // 2}:{y0 + (ah - ch) // 2},scale=1080:1920:flags=lanczos,setsar=1"
        return f"crop={aw}:{ah}:{x0}:{y0},scale=1080:1920:flags=lanczos,setsar=1"
    if mode == "pillarbox":
        fg_h = int(1080 * ah / aw * 1.4 * z) // 2 * 2   # foreground 1.4x (x zoom/punch) wider than the frame, cropped around x
        fg_w = int(fg_h * aw / ah) // 2 * 2
        return (f"crop={aw}:{ah}:{x0}:{y0},split[a][b];"
                f"[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:2,eq=brightness=0.04:saturation=1.15[bg];"
                f"[b]scale={fg_w}:{fg_h},crop=1080:{fg_h}:'max(0,min(iw-1080,{cx}*iw-540))':0[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2-60,setsar=1")
    ch = int(ah / z) // 2 * 2; cw = int(ch * 9 / 16) // 2 * 2
    if cw > aw:
        cw = aw // 2 * 2; ch = int(cw * 16 / 9) // 2 * 2
    x = int(max(0, min(aw - cw, cx * aw - cw / 2))) // 2 * 2 + x0
    y = int(max(0, min(ah - ch, crop.get("y", 0.5) * ah - ch / 2))) // 2 * 2 + y0
    return f"crop={cw}:{ch}:{x}:{y},scale=1080:1920:flags=lanczos,setsar=1"


def resolve(concept: dict, sib: dict) -> list[dict]:
    segs = [dict(s) for s in concept["segments"]]
    if sib.get("first_segment_override"):
        segs[0] = dict(sib["first_segment_override"])
    for s in segs:
        s["_zoom_punch"] = None
    if sib.get("zoom_punch"):
        segs[0]["_zoom_punch"] = sib["zoom_punch"]
    return segs


# ------------------------------------------------------------------ captions
def caption_events(ctx: Ctx, segs: list[dict]) -> list[tuple]:
    """(start, end, text, kind) in output time. Chunks are built across cuts (DIGGER lesson: per-segment chunks drop
    words at cut points). Uncertain ranges get no caption; a bracketed context overlay is used instead when given."""
    ev, toks, t0 = [], [], 0.0
    for s in segs:
        a, i, o = s["src"], s["in"], s["out"]; d = s["_dur"]
        for r in ctx.no_caption:
            if r["src"] == a and r["in"] < o and r["out"] > i and r.get("text"):
                ev.append((t0 + max(0, r["in"] - i), t0 + min(d, r["out"] - i), r["text"], "ctx"))
        for r in s.get("_ctx", []):
            ev.append((t0 + max(0, r["in"] - i), t0 + min(d, r["out"] - i), r["text"], "ctx"))
        sup = [(r["in"], r["out"]) for r in ctx.no_caption if r["src"] == a] + [(r["in"], r["out"]) for r in s.get("_ctx", [])]
        from .subtitles import in_range
        for q in in_range(ctx.cues.get(a, []), i, o):
            if any(r0 <= q["start"] < r1 for r0, r1 in sup):
                continue
            kind = "burned" if is_vertical(ctx.inv[a]) else "sub"   # vertical sources keep their own subtitles
            ev.append((t0 + max(0, q["start"] - i), t0 + min(d, q["end"] - i), q["text"], kind))
        ovs = [x for x in ctx.overrides if x["src"] == a and x["in"] < o and x["out"] > i]
        for w in ctx.words.get(a, []):
            if not (i - 0.06 <= w["start"] < o - 0.06) or w["p"] < 0.25:
                continue
            if any(r0 <= w["start"] < r1 for r0, r1 in sup) or any(x["in"] <= w["start"] < x["out"] for x in ovs):
                continue
            toks.append(dict(t=t0 + max(0, w["start"] - i), e=t0 + min(w["end"], o) - i, w=w["w"], segend=t0 + d))
        for x in ovs:   # verified text spread over the override span
            st = t0 + max(x["in"], i) - i; en = t0 + min(x["out"], o) - i; ow = x["text"].split()
            toks += [dict(t=st + k * (en - st) / len(ow), e=st + (k + 1) * (en - st) / len(ow), w=w_, segend=t0 + d)
                     for k, w_ in enumerate(ow)]
        t0 += d
    total = t0
    toks.sort(key=lambda k: k["t"])
    chunks, cur = [], []
    for k in toks:
        txt = " ".join(c["w"] for c in cur + [k])
        if cur and (len(cur) >= 4 or len(txt) > 18 or k["t"] - cur[-1]["e"] > 1.0):
            chunks.append(cur); cur = []
        cur.append(k)
        if re.search(r"[.?!…]$", k["w"]) and k["w"].lower() not in ("mr.", "mrs.", "dr.", "ms."):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    for n, ch in enumerate(chunks):
        st = ch[0]["t"]; en = min(max(ch[-1]["e"] + 0.25, st + 0.5), ch[-1]["segend"] + 0.6, total)
        if n + 1 < len(chunks):
            en = min(en, chunks[n + 1][0]["t"] - 0.02)
        if en - st < 0.15:
            continue
        txt = " ".join(x["w"] for x in ch)
        for p, r in CENSOR:
            txt = re.sub(p, r, txt, flags=re.I)
        ev.append((st, en, txt.upper(), "cap"))
    return ev


def build_ass(ctx: Ctx, concept: dict, sib: dict, segs: list[dict], total: float, path) -> dict:
    hook_until = ctx.genre["hook_window_s"] + 0.5
    end = concept.get("end_card") or None
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,8,{SAFE_L},{SAFE_R},{HOOK_MARGIN_V},1
Style: Cap,Arial Black,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,{SAFE_L},{SAFE_R},{CAP_MARGIN_V},1
Style: Sub,Arial,66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,{SAFE_L},{SAFE_R},{CAP_MARGIN_V},1
Style: Ctx,Arial Black,62,&H0080F0FF,&H0080F0FF,&H00000000,&H64000000,-1,-1,0,0,100,100,0,0,1,6,2,2,{SAFE_L},{SAFE_R},{CAP_MARGIN_V},1
Style: End,Arial Black,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,18,0,8,{SAFE_L + 20},{SAFE_R + 20},{HOOK_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    L = [f"Dialogue: 2,{ass_t(0)},{ass_t(min(hook_until, total))},Hook,,0,0,0,,{esc(sib['hook']['text'])}"]
    end_start = total - (end["last_seconds"] if end else 0)
    caps = caption_events(ctx, segs)
    for st, en, txt, kind in caps:
        if en > st and kind != "burned":
            style = {"cap": "Cap", "sub": "Sub"}.get(kind, "Ctx")
            L.append(f"Dialogue: 1,{ass_t(st)},{ass_t(en)},{style},,0,0,0,,{esc(txt)}")
    if end:
        L.append(f"Dialogue: 3,{ass_t(end_start)},{ass_t(total)},End,,0,0,0,,{esc(end['text'])}")
    open(path, "w", encoding="utf-8").write(hdr + "\n".join(L) + "\n")
    return dict(hook_until=hook_until, captions=[(round(a, 2), round(b, 2), t, k) for a, b, t, k in caps],
                end_card=end["text"] if end else None)


# ------------------------------------------------------------------ scripts
def script_for(ctx: Ctx, concept: dict, sib: dict) -> dict:
    c = ctx.c
    tag = f"{concept['id']}_{sib['platform']}"
    segs = resolve(concept, sib)
    ctx_ovl = concept.get("context_overlays", [])
    for s in segs:
        fps = ctx.fps(s["src"])
        s["_n"] = max(1, round((s["out"] - s["in"]) * fps))
        s["_dur"] = float(s["_n"] / fps)
        s["_ctx"] = [r for r in ctx_ovl if r["src"] == s["src"] and r["in"] < s["out"] and r["out"] > s["in"]]
    total = sum(s["_dur"] for s in segs)
    ass_dir = c.working / "ass"; ass_dir.mkdir(parents=True, exist_ok=True)
    ass = ass_dir / f"{tag}.ass"
    overlay = build_ass(ctx, concept, sib, segs, total, ass)
    sd = c.working / "seg" / tag
    out = c.ready / f"{tag}.mp4"
    L = ["#!/bin/bash", f"# {tag}: {concept['title']} — generated by clipcrumble render", "set -euo pipefail",
         f"FF={shlex.quote(ffmpeg())}", f"SD={shlex.quote(str(sd))}", 'rm -rf "$SD"; mkdir -p "$SD"']
    for k, s in enumerate(segs):
        src = c.source / ctx.inv[s["src"]]["file"]
        fps = ctx.fps(s["src"])
        has_audio = ctx.inv[s["src"]]["audio"]
        a_in = (f"[0:a]aresample=48000,afade=t=in:d=0.008,afade=t=out:st={max(0, s['_dur'] - 0.008):.4f}:d=0.008[a]"
                if has_audio else f"anullsrc=r=48000:cl=stereo,atrim=0:{s['_dur']:.4f}[a]")
        L.append(f'"$FF" -v error -y -ss {s["in"]:.3f} -i {shlex.quote(str(src))} -t {s["_dur"]:.4f} '
                 f'-filter_complex "[0:v]{vf_for(ctx, s, s["_zoom_punch"])}[v];{a_in}" -map "[v]" -map "[a]" '
                 f'-frames:v {s["_n"]} -c:v libx264 -qp 0 -preset ultrafast -pix_fmt yuv420p -r {fps} '
                 f'-c:a pcm_s16le -ac 2 "$SD/{k:02d}.mkv"   # {s["src"]} {s["in"]:.2f}-{s["out"]:.2f}')
        L.append(f"echo \"file '{k:02d}.mkv'\" >> \"$SD/list.txt\"")
    L += ['"$FF" -v error -y -f concat -safe 0 -i "$SD/list.txt" -c copy "$SD/joined.mkv"',
          # two-pass loudnorm: one pass undershoots quiet dialogue scenes (c01 measured -17 LUFS)
          f'LN=$("$FF" -hide_banner -i "$SD/joined.mkv" -vn -af loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json -f null - 2>&1 '
          f'| {shlex.quote(sys.executable)} -c "{LN_PY}")',
          f'"$FF" -v error -y -i "$SD/joined.mkv" -vf "fps=30,ass={shlex.quote(str(ass))}:fontsdir={FONTS_DIR}" '
          f'-af "loudnorm=I=-14:TP=-1.5:LRA=11:$LN" -c:v libx264 -preset slow -crf 20 -r 30 -pix_fmt yuv420p '
          f'-c:a aac -b:a 128k -ar 48000 -movflags +faststart {shlex.quote(str(out))}',
          'rm -rf "$SD"']
    sh = c.working / "render" / f"{tag}.sh"; sh.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(L) + "\n"
    key = hashlib.sha256((body + ass.read_text() + "".join(ctx.inv[s["src"]]["sha256"] for s in segs)).encode()).hexdigest()
    sh.write_text(body); os.chmod(sh, 0o755)
    (c.ready / f"{tag}.caption.txt").write_text(sib["caption_text"].rstrip() + "\n", encoding="utf-8")
    return dict(tag=tag, script=sh, out=out, key=key, total=total, segs=segs, overlay=overlay, sib=sib, concept=concept)


def run(c: Campaign, approve: list[str], workers: int = 4, force: bool = False) -> list[dict]:
    from . import qa
    ideas = load_json(c.working / "ideas.checked.json")
    gates = load_json(c.working / "ideas.gates.json")
    if ideas is None:
        raise SystemExit("run `clipcrumble ideate --check` first")
    bad = [i for i in approve if i not in gates["kept"]]
    if bad:
        raise SystemExit(f"not approvable (dropped by gates or unknown): {bad}")
    ctx = Ctx(c)
    jobs = [script_for(ctx, k, s) for k in ideas["concepts"] if k["id"] in approve for s in k["siblings"]]
    keys = load_json(c.working / "render" / "keys.json", {}) or {}

    def go(j):
        if not force and j["out"].exists() and keys.get(j["tag"]) == j["key"]:
            return j["tag"], 0, "unchanged — skipped"
        r = subprocess.run(["bash", str(j["script"])], capture_output=True, text=True)
        return j["tag"], r.returncode, (r.stderr or "")[-800:]

    with ThreadPoolExecutor(workers) as ex:
        for (tag, rc, err), j in zip(ex.map(go, jobs), jobs):
            if rc == 0:
                keys[tag] = j["key"]
            c.log("render", f"{tag}: {'OK' if rc == 0 else 'FAIL ' + err}")
    dump_json(keys, c.working / "render" / "keys.json")
    return qa.run(c, ctx, jobs)
