"""Shared helpers: campaign paths, logging, ffmpeg, hashing, yaml/json io."""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(os.environ.get("CLIPCRUMBLE_HOME", Path(__file__).resolve().parent.parent))
PKG = Path(__file__).resolve().parent
FONTS_DIR = "/System/Library/Fonts/Supplemental"


@dataclass(frozen=True)
class Campaign:
    slug: str

    @property
    def root(self) -> Path: return REPO / "campaigns" / self.slug
    @property
    def source(self) -> Path: return self.root / "media" / "source"
    @property
    def working(self) -> Path: return self.root / "media" / "working"
    @property
    def ready(self) -> Path: return self.root / "media" / "ready"
    @property
    def out(self) -> Path: return self.root / "out"
    @property
    def logs(self) -> Path: return self.root / "logs"

    def ensure(self) -> "Campaign":
        for p in (self.source, self.working, self.ready, self.out, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def log(self, stage: str, msg: str) -> str:
        """Append a line to logs/<stage>.log, print it, return 'file:line' for citations."""
        self.logs.mkdir(parents=True, exist_ok=True)
        f = self.logs / f"{stage}.log"
        line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"[{stage}] {msg}", flush=True)
        n = sum(1 for _ in open(f, encoding="utf-8"))
        return f"logs/{stage}.log:{n}"


def load_yaml(p: Path, default=None):
    if not Path(p).exists():
        return default
    return yaml.safe_load(Path(p).read_text(encoding="utf-8")) or default


def dump_yaml(obj, p: Path):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")


def load_json(p: Path, default=None):
    if not Path(p).exists():
        return default
    return json.loads(Path(p).read_text(encoding="utf-8"))


def dump_json(obj, p: Path):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=_np_default), encoding="utf-8")


def _np_default(o):  # numpy scalars sneak into json (DIGGER lesson)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not serialisable: {type(o)}")


def sha256(p: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


@functools.cache
def ffmpeg() -> str:
    """Static ffmpeg from imageio-ffmpeg: Homebrew's build lacks libass (DIGGER lesson)."""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if " ass " not in filters:
        raise RuntimeError(f"{exe} has no 'ass' filter; cannot burn overlays")
    return exe


def ffprobe_json(p: Path) -> dict:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)])
    return json.loads(out)


def tc(t: float) -> str:
    """seconds -> HH:MM:SS.mmm"""
    t = max(0.0, float(t))
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def tc_s(s: str | float) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    parts = [float(x) for x in str(s).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]
