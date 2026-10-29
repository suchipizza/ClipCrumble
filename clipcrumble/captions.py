"""Caption builder + the caption similarity gate (fatal).

Order is fixed: curiosity line -> reply-bait question -> required CTA (own words) -> mandatory tags -> mandatory
hashtags last, in the brief's exact order.
"""
from __future__ import annotations

import re
from collections import Counter

TIKTOK_MAX_BEFORE_TAGS = 150
STOP = {"the", "a", "an", "is", "to", "of", "and", "or", "it", "in", "on", "this", "that", "you", "your", "now",
        "be", "will", "are", "was", "for", "with"}


def build(cap: dict, brief: dict, platform: str) -> str:
    cr = brief.get("caption_rules", {})
    tags = [t for t in (brief.get("mandatory_tags") or []) if str(t).startswith("@")]
    tags += [t for t in (brief.get("extra_tags") or []) if t not in tags]
    hashtags = cr.get("hashtags_in_order") or []
    body = " ".join(x.strip() for x in (cap["curiosity"], cap["question"], cap["cta"]) if x and x.strip())
    tail = " ".join(tags + hashtags)
    return f"{body} {tail}".strip()


def body_of(text: str) -> str:
    return re.sub(r"\s*(?:[@#][\w.]+\s*)+$", "", text).strip()


def tokens(s: str, strip: set[str] = frozenset()) -> list[str]:
    s = re.sub(r"[@#][\w.]+", " ", s.lower())
    s = s.replace("’", "'")
    toks = re.findall(r"[a-z0-9']+", s)
    return [t for t in toks if t not in strip]


def strip_set(brief: dict) -> set[str]:
    title = brief.get("title") or ""
    cta = (brief.get("caption_rules") or {}).get("cta_required_text") or ""
    return set(tokens(title)) | set(tokens(cta)) | {"watch", "film", "movie", "available", "rent", "buy", "stream"}


def ngrams(t: list[str], n: int = 3) -> set[tuple]:
    return {tuple(t[i:i + n]) for i in range(len(t) - n + 1)}


def edit_distance(a: list[str], b: list[str]) -> int:
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (x != y))
    return d[-1]


def sample_phrases(brief: dict) -> list[list[str]]:
    """The creative part of each sample caption (title, CTA, hashtags removed)."""
    st = strip_set(brief)
    out = []
    for s in brief.get("sample_captions") or []:
        for sent in re.split(r"(?<=[.!?\"])\s+", s):
            t = tokens(sent, st)
            if len(t) >= 2:
                out.append(t)
    return out


def check_caption(text: str, brief: dict, platform: str) -> list[str]:
    """Per-caption checks. Returns a list of failure reasons (empty = PASS)."""
    fails = []
    cr = brief.get("caption_rules", {})
    hashtags = cr.get("hashtags_in_order") or []
    # mandatory presence + order + last position
    if hashtags:
        tail = re.findall(r"#\w+", text)
        if tail[-len(hashtags):] != hashtags or not text.rstrip().endswith(hashtags[-1]):
            fails.append(f"hashtags must end the caption in this order: {' '.join(hashtags)}")
    for t in brief.get("mandatory_tags") or []:
        if str(t).startswith("@") and t not in text:
            fails.append(f"missing mandatory tag {t}")
    cta = cr.get("cta_required_text")
    if cta:
        low = text.lower()
        need = [w for w in ("rent", "buy") if w in cta.lower()] or tokens(cta)[:2]
        if not all(w in low for w in need):
            fails.append(f"CTA must say '{cta}' (missing: {', '.join(w for w in need if w not in low)})")
        title = brief.get("title")
        if title and title.lower() not in low:
            fails.append(f"CTA must name the film ({title})")
    # similarity with samples
    st = strip_set(brief)
    mine = tokens(body_of(text), st)
    for sp in sample_phrases(brief):
        shared = ngrams(mine) & ngrams(sp)
        if shared:
            fails.append(f"shares a 3-word sequence with a sample caption: {' '.join(next(iter(shared)))!r}")
        for i in range(max(1, len(mine) - len(sp) + 1)):
            if len(sp) >= 3 and edit_distance(mine[i:i + len(sp)], sp) <= 2:
                fails.append(f"too close to a sample caption line: {' '.join(sp)!r}")
                break
    if platform == "tiktok" and len(body_of(text)) > TIKTOK_MAX_BEFORE_TAGS:
        fails.append(f"TikTok caption body {len(body_of(text))} chars > {TIKTOK_MAX_BEFORE_TAGS}")
    if re.search(r"\b(link in bio|follow for part 2|comment .* for)\b", text, re.I):
        fails.append("generic engagement bait")
    return fails


def check_set(captions: dict[str, str], brief: dict, max_same_cta: int = 2) -> dict[str, list[str]]:
    """Cross-caption checks: CTA wording used by at most N posts; curiosity lines unique."""
    fails: dict[str, list[str]] = {k: [] for k in captions}
    cta_count = Counter()
    first = Counter()
    for k, txt in captions.items():
        cta_count[_cta_sentence(txt, brief)] += 1
        first[_first_line(txt)] += 1
    for k, txt in captions.items():
        c = _cta_sentence(txt, brief)
        if c and cta_count[c] > max_same_cta:
            fails[k].append(f"CTA wording used {cta_count[c]}× (max {max_same_cta}): {c!r}")
        if first[_first_line(txt)] > 1:
            fails[k].append(f"caption first line reused: {_first_line(txt)!r}")
    return fails


def _first_line(txt: str) -> str:
    return " ".join(tokens(re.split(r"(?<=[.!?])\s+", body_of(txt))[0]))


def _cta_sentence(txt: str, brief: dict) -> str:
    for s in re.split(r"(?<=[.!?])\s+", body_of(txt)):
        if "rent" in s.lower() or "buy" in s.lower():
            return " ".join(tokens(s))
    return ""
