"""ingest: fetch (only allowed sources) -> inventory -> scenes -> frames/dedupe -> transcript.

Every step is keyed by the file's sha256 and skipped when its output already exists.
What actually ran is logged; nothing is claimed that did not run.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np

from .util import Campaign, dump_json, ffmpeg, ffprobe_json, load_json, load_yaml, sha256

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
STILL_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class NeedsHuman(Exception):
    """Raised when a step needs a manual action; message goes to todo.md."""


# ---------------------------------------------------------------- fetch
DRIVE_RE = re.compile(r"https://drive\.google\.com/[^\s)\]]+")
YT_RE = re.compile(r"https://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+")
WALLED_RE = re.compile(r"https://(?:f\.io|[\w.]*frame\.io|[\w.]*notion\.(?:so|site)|whop\.com)/[^\s)\]]+")


def source_links(brief_text: str) -> dict:
    return {"drive": sorted(set(DRIVE_RE.findall(brief_text))),
            "youtube": sorted(set(YT_RE.findall(brief_text))),
            "walled": sorted(set(WALLED_RE.findall(brief_text)))}


def import_local(c: Campaign, paths: list[Path]) -> list[str]:
    """Copy footage the user downloaded (files, folders or Drive zips) into media/source, flattening names."""
    added = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            added += import_local(c, sorted(p.iterdir()))
        elif p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if info.is_dir() or Path(info.filename).name.startswith("."):
                        continue
                    if Path(info.filename).suffix.lower() not in VIDEO_EXT | STILL_EXT:
                        continue
                    dst = c.source / _flat_name(info.filename)
                    if not dst.exists():
                        with z.open(info) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)
                        added.append(dst.name)
        elif p.suffix.lower() in VIDEO_EXT | STILL_EXT:
            dst = c.source / _flat_name(p.name)
            if not dst.exists():
                shutil.copy2(p, dst)
                added.append(dst.name)
    return added


def _flat_name(name: str) -> str:
    n = re.sub(r"\s+", "_", name.strip().replace("/", "_"))
    n = re.sub(r"-\d{8}T\d{6}Z-\d+-\d+", "", n)  # Drive zip suffix
    n = re.sub(r"_+", "_", n).replace("-_", "_").replace("_-", "_")
    return n.strip("_")


def fetch(c: Campaign, brief_text: str) -> str:
    """Fetch from a public Drive folder/file or YouTube URL named in the brief. Anything else -> NeedsHuman."""
    have = [p for p in c.source.iterdir() if p.suffix.lower() in VIDEO_EXT | STILL_EXT] if c.source.exists() else []
    if have:
        return f"source already has {len(have)} files; fetch skipped"
    links = source_links(brief_text)
    if links["drive"]:
        import gdown
        for url in links["drive"]:
            if "/folders/" in url:
                gdown.download_folder(url=url, output=str(c.source / "_dl"), quiet=False, remaining_ok=True)
            else:
                gdown.download(url=url, output=str(c.source / "_dl") + "/", quiet=False, fuzzy=True)
        import_local(c, [c.source / "_dl"])
        shutil.rmtree(c.source / "_dl", ignore_errors=True)
        return f"fetched from Drive: {links['drive']}"
    if links["youtube"]:
        for url in links["youtube"]:
            subprocess.run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]", "-o",
                            str(c.source / "%(title).80s.%(ext)s"), url], check=True)
        return f"fetched from YouTube: {links['youtube']}"
    if links["walled"]:
        raise NeedsHuman(f"Download the approved footage manually from {', '.join(links['walled'])} "
                         f"and run `clipcrumble ingest {c.slug} --from <folder or zips>`.")
    raise NeedsHuman("The brief names no fetchable footage source. Put the approved footage in "
                     f"campaigns/{c.slug}/media/source/ or run `clipcrumble ingest {c.slug} --from <path>`.")


# ---------------------------------------------------------------- inventory
def _alias(stem: str) -> str:
    toks = [t for t in re.split(r"[_\-\s]+|(?<=[a-z])(?=[A-Z])", stem) if t]
    toks = [t for t in toks if not re.fullmatch(r"[vV]\d+|rev|\d{8,}", t)]
    ratio = next((("V" if a < b else "W") for a, b in
                  [tuple(map(int, m)) for m in re.findall(r"(\d+)x(\d+)", stem)]), "")
    words = [t for t in toks if not re.search(r"\d", t)]
    digits = "".join(re.findall(r"(?<![x\d])(\d{1,3})(?![x\d])", " ".join(t for t in toks if not re.search(r"\d+x\d+", t))))
    return ("".join(w[0] for w in words[:2]).upper() + digits + ratio) or "F"


def guess_type(name: str, dur: float | None) -> str:
    n = name.lower()
    if dur is None:
        return "gif" if n.endswith(".gif") else "still"
    for key, t in (("trailer", "trailer"), ("teaser", "trailer"), ("spot", "tv_spot"), ("bts", "bts"),
                   ("featurette", "bts"), ("interview", "interview"), ("poster", "still"), ("gif", "gif"),
                   ("clip", "clip"), ("episode", "full_episode")):
        if key in n:
            return t
    if dur and dur > 900:
        return "full_episode"
    return "clip"


def inventory(c: Campaign) -> dict:
    prev = {f["sha256"]: f for f in (load_json(c.working / "inventory.json", {}) or {}).get("files", [])}
    origin = (load_json(c.working / "origins.json", {}) or {})
    files, used = [], set()
    for p in sorted(c.source.iterdir(), key=lambda x: _natkey(x.name)):
        if p.suffix.lower() not in VIDEO_EXT | STILL_EXT:
            continue
        h = sha256(p)
        if h in prev and prev[h]["file"] == p.name:
            rec = prev[h]
        else:
            pr = ffprobe_json(p)
            v = next((s for s in pr["streams"] if s["codec_type"] == "video"), {})
            a = next((s for s in pr["streams"] if s["codec_type"] == "audio"), None)
            dur = float(pr["format"].get("duration") or 0) if p.suffix.lower() in VIDEO_EXT else None
            num, den = (v.get("r_frame_rate") or "0/1").split("/")
            rec = dict(file=p.name, sha256=h, width=v.get("width"), height=v.get("height"),
                       fps=round(int(num) / max(1, int(den)), 3), fps_rational=v.get("r_frame_rate"),
                       duration=round(dur, 3) if dur else None, audio=bool(a),
                       audio_channels=a.get("channels") if a else 0, kind="video" if dur else "still",
                       type=guess_type(p.name, dur), size_mb=round(p.stat().st_size / 1e6, 1))
        rec["origin"] = origin.get(p.name, rec.get("origin", "manual download"))
        al = rec.get("alias") or _alias(p.stem)
        base, k = al, 2
        while al in used:
            al = f"{base}_{k}"; k += 1
        rec["alias"] = al; used.add(al)
        files.append(rec)
    # same bytes twice = exact duplicate file
    seen = {}
    for f in files:
        f["exact_duplicate_of"] = seen.get(f["sha256"])
        seen.setdefault(f["sha256"], f["alias"])
    vids = [f for f in files if f["kind"] == "video"]
    inv = dict(files=files, total_minutes=round(sum(f["duration"] for f in vids) / 60, 1),
               types=sorted({f["type"] for f in files}), count=len(files))
    dump_json(inv, c.working / "inventory.json")
    return inv


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- scenes + frames
def _grab(path: Path, t: float, scale: str = "480:-2") -> "Image.Image":
    from PIL import Image
    b = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={scale}", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True).stdout
    return Image.open(io.BytesIO(b)).convert("RGB") if b else None


def _face_x(img, cascade) -> list[dict]:
    import cv2
    g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    faces = cascade.detectMultiScale(g, scaleFactor=1.1, minNeighbors=6, minSize=(g.shape[0] // 12,) * 2)
    W = g.shape[1]
    return [dict(x=round((fx + fw / 2) / W, 3), w=round(fw / W, 3)) for fx, fy, fw, fh in faces]


def _letterbox(img) -> dict:
    a = np.asarray(img.convert("L"), dtype=np.float32)
    rows = a.mean(axis=1); cols = a.mean(axis=0)
    top = int(np.argmax(rows > 12)); bot = int(np.argmax(rows[::-1] > 12))
    left = int(np.argmax(cols > 12)); right = int(np.argmax(cols[::-1] > 12))
    H, W = a.shape
    return dict(top=round(top / H, 3), bottom=round(bot / H, 3), left=round(left / W, 3), right=round(right / W, 3),
                luma=round(float(a.mean()), 1))


def scenes_and_frames(c: Campaign, inv: dict, every_s: float = 2.0) -> dict:
    """Scene cuts (PySceneDetect content detector), sampled frames every 2 s + one per scene, with
    face x-position (OpenCV Haar), letterbox/luma, perceptual hashes (full + centre crop) and contact sheets."""
    import cv2
    import imagehash
    from scenedetect import ContentDetector, detect
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    out = load_json(c.working / "frames.json", {}) or {}
    sheets = c.working / "sheets"; sheets.mkdir(parents=True, exist_ok=True)
    for f in inv["files"]:
        if f["kind"] != "video" or f["exact_duplicate_of"]:
            continue
        if out.get(f["alias"], {}).get("sha256") == f["sha256"]:
            continue
        path = c.source / f["file"]
        sc = detect(str(path), ContentDetector(threshold=27.0))
        scenes = [dict(n=i + 1, start=round(s.get_seconds(), 3), end=round(e.get_seconds(), 3)) for i, (s, e) in enumerate(sc)]
        if not scenes:
            scenes = [dict(n=1, start=0.0, end=f["duration"])]
        times = sorted({round(t, 2) for t in np.arange(0.5, f["duration"] - 0.2, every_s)} |
                       {round(s["start"] + min(0.4, (s["end"] - s["start"]) / 2), 2) for s in scenes})
        samples = []
        for t in times:
            im = _grab(path, t)
            if im is None:
                continue
            w, h = im.size
            crop = im.crop((int(w / 2 - h * 0.28), int(h * 0.1), int(w / 2 + h * 0.28), int(h * 0.9))) if w > h \
                else im.crop((0, int(h / 2 - w * 0.4), w, int(h / 2 + w * 0.4)))
            scene_n = next((s["n"] for s in scenes if s["start"] <= t < s["end"]), scenes[-1]["n"])
            samples.append(dict(t=t, scene=scene_n, faces=_face_x(im, cascade), **_letterbox(im),
                                phash=str(imagehash.phash(im)), chash=str(imagehash.phash(crop)), _img=im))
        _contact_sheets(f["alias"], samples, sheets)
        for s in samples:
            s.pop("_img")
        for s in scenes:
            fx = [fc["x"] for smp in samples if smp["scene"] == s["n"] for fc in smp["faces"]]
            s["face_x"] = round(float(np.median(fx)), 3) if fx else None
            lum = [smp["luma"] for smp in samples if smp["scene"] == s["n"]]
            s["dark"] = bool(lum and np.mean(lum) < 35)
        out[f["alias"]] = dict(file=f["file"], sha256=f["sha256"], scenes=scenes, samples=samples)
        dump_json(out, c.working / "frames.json")
        c.log("ingest", f"frames {f['alias']}: {len(scenes)} scenes, {len(samples)} samples")
    _dedupe(c, out)
    return out


def _contact_sheets(alias: str, samples: list, outdir: Path, per_sheet: int = 30, cols: int = 6):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 18)
    for k in range(0, len(samples), per_sheet):
        chunk = samples[k:k + per_sheet]
        tiles = []
        for s in chunk:
            im = s["_img"].copy(); im.thumbnail((320, 320))
            d = ImageDraw.Draw(im)
            d.rectangle([0, 0, im.width, 22], fill="black")
            d.text((4, 2), f"{alias} {s['t']:.1f}s sc{s['scene']}", fill="yellow", font=font)
            tiles.append(im)
        tw = max(t.width for t in tiles); th = max(t.height for t in tiles)
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (tw + 4), rows * (th + 4)), "white")
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * (tw + 4), (i // cols) * (th + 4)))
        sheet.save(outdir / f"{alias}_{k // per_sheet + 1:02d}.jpg", quality=80)


def _dedupe(c: Campaign, frames: dict, thresh: int = 10):
    """Mark scenes whose representative frame appears in another file (full-frame OR centre-crop pHash:
    vertical recuts never match their 16:9 masters on the full frame — DIGGER lesson)."""
    import imagehash
    reps = []
    for al, d in frames.items():
        for s in d["scenes"]:
            smp = [x for x in d["samples"] if x["scene"] == s["n"]]
            if smp:
                m = smp[len(smp) // 2]
                reps.append((al, s, imagehash.hex_to_hash(m["phash"]), imagehash.hex_to_hash(m["chash"])))
    for al, s, *_ in reps:
        s["dupes"] = []
    for i, (a1, s1, p1, c1) in enumerate(reps):
        for a2, s2, p2, c2 in reps[i + 1:]:
            if a1 == a2:
                continue
            d = min(p1 - p2, c1 - c2)
            if d <= thresh:
                s1["dupes"].append(f"{a2}#{s2['n']}@{s2['start']:.1f}")
                s2["dupes"].append(f"{a1}#{s1['n']}@{s1['start']:.1f}")
    dump_json(frames, c.working / "frames.json")
    n = sum(1 for _, s, *_ in reps if s["dupes"])
    c.log("ingest", f"dedupe: {len(reps)} scenes, {n} appear in more than one file")


# ---------------------------------------------------------------- transcript
def _pcm16k(path: Path) -> np.ndarray:
    raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# Stock phrases Whisper invents over music or silence (en + ja). Matched on text with spaces/punctuation removed.
HALLUCINATIONS = ["thankyou", "thanksforwatching", "transcriptionbycastingwords", "wellberightback",
                  "subtitlesbytheamaraorgcommunity", "pleasesubscribe", "ご視聴ありがとうございました",
                  "お疲れ様でした", "次回予告", "チャンネル登録"]


def is_hallucination(text: str) -> bool:
    t = re.sub(r"[\s\W_]+", "", text.replace("[?]", "")).lower()
    return bool(t) and any(t == h or (t.startswith(h) and len(t) <= len(h) + 4) for h in HALLUCINATIONS)


def transcribe(c: Campaign, inv: dict, model_name: str = "large-v3-turbo", language: str = "en",
               low_p: float = 0.5) -> dict:
    """faster-whisper with word timestamps. Audio decoded by ffmpeg (PyAV mismatch — DIGGER lesson).
    Words with probability < low_p are flagged low=True and shown as [?]."""
    out = load_json(c.working / "transcript.json", {}) or {}
    # Re-run a file when its bytes or the settings change. Without VAD, Whisper hallucinates
    # ("Thank you.", "Transcription by CastingWords") over music and silence.
    settings = dict(model=model_name, language_setting=language or "auto", vad=True)
    todo = [f for f in inv["files"] if f["kind"] == "video" and f["audio"] and not f["exact_duplicate_of"]
            and (out.get(f["alias"], {}).get("sha256") != f["sha256"]
                 or any(out.get(f["alias"], {}).get(k) != v for k, v in settings.items()))]
    if not todo:
        return out
    from faster_whisper import WhisperModel
    m = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=10)
    c.log("ingest", f"transcribe: model={model_name} lang={language} files={len(todo)}")
    for f in todo:
        segs, info = m.transcribe(_pcm16k(c.source / f["file"]), language=None if language in (None, "auto") else language,
                                  word_timestamps=True,
                                  beam_size=5, vad_filter=True, condition_on_previous_text=False)
        res, dropped = [], 0
        for s in segs:
            words = [dict(w=w.word.strip(), start=round(float(w.start), 3), end=round(float(w.end), 3),
                          p=round(float(w.probability), 3), low=bool(w.probability < low_p)) for w in (s.words or [])]
            text = " ".join(w["w"] + ("[?]" if w["low"] else "") for w in words)
            if is_hallucination(text):
                dropped += 1
                continue
            res.append(dict(start=round(float(s.start), 3), end=round(float(s.end), 3), text=text,
                            no_speech_prob=round(float(s.no_speech_prob), 3), words=words))
        out[f["alias"]] = dict(file=f["file"], sha256=f["sha256"], **settings, language=info.language,
                               language_probability=round(float(info.language_probability), 3), segments=res)
        dump_json(out, c.working / "transcript.json")
        c.log("ingest", f"transcribed {f['alias']}: {len(res)} segments, language={info.language}"
                        + (f", {dropped} stock-phrase segments dropped" if dropped else ""))
    return out


def run(c: Campaign, from_paths: list[Path] | None = None, skip_transcribe: bool = False) -> dict:
    c.ensure()
    run_cfg = load_yaml(c.root / "run.yaml", {}) or {}
    if from_paths:
        added = import_local(c, from_paths)
        c.log("ingest", f"imported {len(added)} files from {', '.join(map(str, from_paths))}")
    brief = (c.root / "brief.md").read_text(encoding="utf-8") if (c.root / "brief.md").exists() else ""
    c.log("ingest", fetch(c, brief))
    inv = inventory(c)
    c.log("ingest", f"inventory: {inv['count']} files, {inv['total_minutes']} min, types {inv['types']}")
    scenes_and_frames(c, inv)
    from . import subtitles
    subtitles.run(c, inv)
    if skip_transcribe:
        c.log("ingest", "transcribe: SKIPPED (--skip-transcribe)")
    else:
        transcribe(c, inv, run_cfg.get("whisper_model", "large-v3-turbo"), run_cfg.get("language", "auto"))
    c.log("ingest", "done. Vision review of sheets/ is a separate step: frames_review.json (see todo).")
    return inv
