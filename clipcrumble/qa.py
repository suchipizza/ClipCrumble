"""QA: automatic checks per rendered clip + a contact sheet per concept (to be looked at — DIGGER lesson: sheets
caught most bugs the checks missed) + manifest.csv. Fail = not ready; nothing failing is ever uploaded."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess

from . import captions as capmod
from . import registry
from .util import Campaign, dump_json, ffmpeg, ffprobe_json


def _frame(path, t, scale=None):
    from PIL import Image
    vf = ["-vf", f"scale={scale}"] if scale else []
    b = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", str(path), "-frames:v", "1", *vf,
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    return Image.open(io.BytesIO(b)).convert("RGB")


def _loudness(path) -> float | None:
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    try:
        j = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
        return float(j["input_i"])
    except Exception:
        return None


def run(c: Campaign, ctx, jobs: list[dict]) -> list[dict]:
    import imagehash
    from PIL import ImageDraw, ImageFont, ImageStat
    brief = ctx.brief
    fmt = brief["format"]
    con = registry.connect()
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 16)
    all_hooks = [j["sib"]["hook"]["text"] for j in jobs]
    by_concept: dict[str, list] = {}
    results = []
    for j in jobs:
        out = j["out"]; tag = j["tag"]
        r = dict(tag=tag, file=out.name, platform=j["sib"]["platform"], concept=j["concept"]["id"],
                 hook=j["sib"]["hook"]["text"], caption=j["sib"]["caption_text"], checks={})
        if not out.exists():
            r["checks"]["rendered"] = False; r["pass"] = False; results.append(r); continue
        pr = ffprobe_json(out)
        v = next(s for s in pr["streams"] if s["codec_type"] == "video")
        dur = float(pr["format"]["duration"])
        first = _frame(out, 0.0)
        # raw first frame without overlays, for sibling comparison
        s0 = j["segs"][0]
        from .render import vf_for
        raw = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{s0['in'] + 0.04:.3f}", "-i", str(c.source / ctx.inv[s0["src"]]["file"]),
                              "-filter_complex", f"[0:v]{vf_for(ctx, s0, s0['_zoom_punch'])},scale=270:480[v]", "-map", "[v]",
                              "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
        from PIL import Image
        raw_img = Image.open(io.BytesIO(raw)).convert("RGB")
        mid = _frame(out, dur / 2).convert("L")
        edges = [mid.crop((0, 0, 8, 1920)), mid.crop((1072, 0, 1080, 1920)), mid.crop((0, 0, 1080, 8)), mid.crop((0, 1912, 1080, 1920))]
        black_edges = sum(ImageStat.Stat(e).mean[0] < 6 for e in edges)
        first_luma = ImageStat.Stat(first.convert("L")).mean[0]
        lufs = _loudness(out)
        cap_fails = capmod.check_caption(j["sib"]["caption_text"], brief, j["sib"]["platform"])
        r["checks"] = {
            "resolution_1080x1920": (v["width"], v["height"]) == (1080, 1920),
            "fps_30": v.get("r_frame_rate") in ("30/1", "30000/1000"),
            f"duration_{fmt['min_seconds']}_{fmt['max_seconds']}": fmt["min_seconds"] <= dur <= fmt["max_seconds"],
            "hook_from_frame_0": j["overlay"]["hook_until"] > 0,
            "first_frame_not_black": first_luma > 12,
            "no_black_bars": black_edges <= 1,
            "text_throughout": _text_coverage(j, dur) >= 0.6,
            "loudness_-14": lufs is not None and -16.5 <= lufs <= -11.5,
            "audio_le_128kbps": _audio_kbps(out) <= 132,   # Instagram rejects >128 kbps at publish time
            "caption_rules": not cap_fails,
            "hook_unique_in_run": all_hooks.count(r["hook"]) == 1,
            "hash_not_in_registry": not con.execute("select 1 from registry_hashes where sha256=?", (_sha(out),)).fetchone(),
        }
        r.update(duration=round(dur, 2), size_mb=round(out.stat().st_size / 1e6, 1), lufs=lufs, sha256=_sha(out),
                 caption_fails=cap_fails, first_phash=str(imagehash.phash(raw_img)),
                 first_md5=hashlib.md5(first.tobytes()).hexdigest(), captions_burned=j["overlay"]["captions"])
        by_concept.setdefault(j["concept"]["id"], []).append((r, j))
        results.append(r)
    # sibling difference: hook text + first-frame pHash distance >= 6 (DIGGER threshold)
    for cid, pairs in by_concept.items():
        hs = [p[0] for p in pairs]
        dmin = min((imagehash.hex_to_hash(a["first_phash"]) - imagehash.hex_to_hash(b["first_phash"])
                    for i, a in enumerate(hs) for b in hs[i + 1:]), default=99)
        ok = len({h["hook"] for h in hs}) == len(hs) and dmin >= 6
        for h in hs:
            h["checks"]["siblings_differ"] = bool(ok); h["sibling_min_phash"] = int(dmin)
        _sheet(c, cid, pairs, font)
    for r in results:
        r["pass"] = all(r["checks"].values())
        dump_json(r, c.ready / f"{r['tag']}.qa.json")
        c.log("qa", f"{r['tag']} {r.get('duration')}s {r.get('size_mb')}MB "
                    + ("PASS" if r["pass"] else "FAIL " + str([k for k, v in r["checks"].items() if not v])))
    _manifest(c, results)
    return results


def _text_coverage(j, dur) -> float:
    spans = [(0, j["overlay"]["hook_until"])] + [(a, b) for a, b, *_ in j["overlay"]["captions"]]
    covered, t = 0.0, 0.0
    for a, b in sorted(spans):
        a = max(a, t)
        if b > a:
            covered += b - a; t = b
    return covered / dur if dur else 0


def _sha(p):
    h = hashlib.sha256(); h.update(open(p, "rb").read()); return h.hexdigest()


def _sheet(c: Campaign, cid: str, pairs, font):
    from PIL import Image, ImageDraw
    tiles = []
    for r, j in pairs:
        out = j["out"]; t = 0.0; times = [(0.0, "START")]
        for s in j["segs"]:
            times.append((t + s["_dur"] / 2, f"{s['src']} {s['in']:.1f}")); t += s["_dur"]
        times.append((max(0, t - 0.3), "END"))
        for tt, lab in times:
            im = _frame(out, tt, "180:320"); d = ImageDraw.Draw(im)
            d.rectangle([0, 300, 180, 320], fill="black"); d.text((2, 301), f"{r['platform'][:2]} {lab}", fill="yellow", font=font)
            tiles.append(im)
    cols = 10; rows = (len(tiles) + cols - 1) // cols
    S = Image.new("RGB", (cols * 182, rows * 322), "white")
    for i, im in enumerate(tiles):
        S.paste(im, ((i % cols) * 182, (i // cols) * 322))
    (c.working / "qa_sheets").mkdir(parents=True, exist_ok=True)
    S.save(c.working / "qa_sheets" / f"{cid}.jpg", quality=82)


def _manifest(c: Campaign, results):
    with open(c.out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "concept", "platform", "duration", "size_mb", "lufs", "qa", "failed_checks", "hook", "caption", "sha256"])
        for r in results:
            w.writerow([r["file"], r["concept"], r["platform"], r.get("duration"), r.get("size_mb"), r.get("lufs"),
                        "PASS" if r["pass"] else "FAIL", ";".join(k for k, v in r["checks"].items() if not v),
                        r["hook"], r["caption"], r.get("sha256", "")])


def _audio_kbps(path) -> float:
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=bit_rate",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip()) / 1000
    except ValueError:
        return 0.0
