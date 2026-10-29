#!/usr/bin/env python3
"""Check captions against a brief's sample captions (auto-reject guard).

For every caption sentence vs every sample sentence (hashtags removed, lowercased, word tokens):
  FAIL if token-trigram Jaccard >= 0.5, or if both share their first 3 words.
Also FAIL if a caption opens with a sample's lead ("nobody", "your nightmares", a quoted "amazing and haunting"),
lacks the rent-or-buy CTA, lacks the film title, or lacks a required hashtag.

Usage: check_captions.py <campaign-slug> [--file captions.json]
  default source: campaigns/<slug>/media/working/ideas.checked.json (siblings' caption_text)
  --file: JSON object {id: caption}
Exit code 1 if anything fails.
"""
import json
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BANNED_OPENERS = [r"nobody\b", r"your nightmares\b", r"[\"“']?amazing and haunting"]


def toks(s: str) -> list[str]:
    s = re.sub(r"#\w+", " ", s.lower()).replace("’", "'")
    return re.findall(r"[a-z0-9']+", s)


def sentences(s: str) -> list[str]:
    s = re.sub(r"#\w+", " ", s)
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\s+[—–]\s+", s) if len(toks(x)) > 0]


def tri(t: list[str]) -> set:
    return {tuple(t[i:i + 3]) for i in range(len(t) - 2)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def check(caps: dict[str, str], brief: dict) -> dict[str, list[str]]:
    samples = list({tuple(toks(x)): toks(x) for s in brief.get("sample_captions", []) for x in sentences(s)}.values())
    title = (brief.get("title") or "").lower()
    tags = (brief.get("caption_rules") or {}).get("hashtags_in_order") or []
    out = {}
    for k, cap in caps.items():
        f = []
        for sent in sentences(cap):
            t = toks(sent)
            for s in samples:
                j = jaccard(tri(t), tri(s))
                if j >= 0.5:
                    f.append(f"trigram Jaccard {j:.2f} with sample sentence {' '.join(s)!r}: {sent!r}")
                if len(t) >= 3 and len(s) >= 3 and t[:3] == s[:3]:
                    f.append(f"first 3 words match sample sentence {' '.join(s)!r}: {sent!r}")
        lead = cap.strip().lower()
        for b in BANNED_OPENERS:
            if re.match(b, lead):
                f.append(f"opens like a sample ({b})")
        low = cap.lower()
        if not ("rent" in low and "buy" in low):
            f.append("missing rent-or-buy CTA")
        if title and title not in low:
            f.append(f"missing film title {title.upper()}")
        for h in tags:
            if h not in cap:
                f.append(f"missing {h}")
        out[k] = f
    return out


def main():
    slug = sys.argv[1]
    root = REPO / "campaigns" / slug
    brief = yaml.safe_load(open(root / "brief.yaml"))
    if "--file" in sys.argv:
        caps = json.load(open(sys.argv[sys.argv.index("--file") + 1]))
    else:
        ideas = json.load(open(root / "media" / "working" / "ideas.checked.json"))
        caps = {f"{c['id']}_{s['platform']}": s["caption_text"] for c in ideas["concepts"] for s in c["siblings"]}
    res = check(caps, brief)
    bad = 0
    for k in sorted(res):
        print(f"{'FAIL' if res[k] else 'PASS'}  {k}")
        for r in res[k]:
            print(f"      - {r}")
        bad += bool(res[k])
    print(f"\n{len(res) - bad}/{len(res)} captions pass")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
