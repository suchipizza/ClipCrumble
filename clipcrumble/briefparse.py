"""parse: brief.md -> brief.yaml.

Rule: every extracted value carries a verbatim quote from the brief (`_evidence`). A field the brief does not
state gets the conservative default and an entry in `_unknowns` ("[?] UNSTATED — default: X"). `check()` re-verifies
every quote against brief.md, so hand edits (or an LLM pass) cannot slip in an unquoted value.
"""
from __future__ import annotations

import re
from pathlib import Path

from .util import Campaign, dump_yaml, load_yaml

# Conservative defaults (PRD §3.1). Applied only with an _unknowns entry.
DEFAULTS = {
    "marketplace": "Content Rewards",
    "format.aspect": "9:16",
    "format.min_seconds": 10,
    "format.max_seconds": 60,
    "format.hook_deadline_seconds": 2,
    "format.on_screen_text_throughout": True,
    "format.audio": "original footage audio only; no added music",
    "ai_rules": "never change anyone's face or voice with AI; never make up quotes",
    "brand_new_edits_only": True,
    "outside_footage_allowed": False,
    "audience.geo_rule": None,
    "audience.engagement_min": "0.20%",
    "economics": None,
    "post_must_stay_live_days": 30,
    "likes_must_stay_visible": True,
    "daily_cap_per_platform": 5,
    "exclusivity_days": 0,
    "watermark": None,
    "pinned_comment": None,
    "page_rule": "an established page you already run; no new campaign-only accounts",
    "mandatory_tags": [],
}
DEFAULT_TEXT = {
    "marketplace": "Content Rewards (submit the post link within 30 min of publishing)",
    "audience.geo_rule": "none — no country rule stated, so no demographics screen recording is required; "
                         "US % is still tracked per post",
    "economics": "unknown — no payout estimate",
    "watermark": "none",
    "pinned_comment": "none",
    "mandatory_tags": "no tag required",
}

PLATFORM_WORDS = {"tiktok": "tiktok", "instagram": "instagram", "reels": "instagram", "youtube": "youtube",
                  "shorts": "youtube", "facebook": "facebook", "x (twitter)": "x", "snapchat": "snapchat"}
GENRES = [("horror", "horror"), ("thriller", "horror"), ("supernatural", "horror"), ("reality", "reality_tv"),
          ("dating show", "reality_tv"), ("podcast", "podcast"), ("gaming", "gaming"), ("game", "gaming"),
          ("comedy", "film_promo"), ("interview", "comedy_interview"), ("film", "film_promo"), ("movie", "film_promo")]


def clean(md: str) -> str:
    """Markdown -> plain text we can quote against (unescape \\_ \\# \\:, drop ** and link targets)."""
    t = re.sub(r"\\([_#:*\[\]().!-])", r"\1", md)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", t)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"[ \t]+", " ", t)
    return t


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def section(text: str, *names: str) -> str:
    """Body of the first heading whose title contains one of names (until the next heading)."""
    for n in names:
        m = re.search(rf"^#+[^\n]*{re.escape(n)}[^\n]*\n(.*?)(?=^#+ |\Z)", text, re.I | re.M | re.S)
        if m:
            return m.group(1).strip()
    return ""


def sentence_with(text: str, pattern: str) -> str | None:
    for line in re.split(r"(?<=[.!?])\s+|\n", text):
        if re.search(pattern, line, re.I):
            return line.strip(" *-\t")
    return None


class Extract:
    def __init__(self, text: str):
        self.text = text
        self.y: dict = {}
        self.ev: dict = {}
        self.unk: dict = {}

    def put(self, key: str, value, quote: str | None):
        _set(self.y, key, value)
        if quote:
            self.ev[key] = quote.strip()

    def default(self, key: str, why: str = "UNSTATED"):
        val = DEFAULTS.get(key)
        _set(self.y, key, val)
        shown = DEFAULT_TEXT.get(key, val)
        self.unk[key] = f"[?] {why} — default: {shown}"


def _set(d: dict, dotted: str, v):
    ks = dotted.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = v


def extract(md: str) -> dict:
    t = clean(md)
    e = Extract(t)

    m = re.search(r"^#\s*(.+)$", t, re.M)
    title = m.group(1).strip() if m else None
    e.put("campaign_name", title, title)
    film = re.sub(r"\s*(clipping|campaign|brief|x\b).*$", "", title or "", flags=re.I).strip() or None
    e.put("title", film, film if film and film in t else None)

    over = section(t, "Overview", "The movie", "Goal")
    if over:
        first = over.split("\n")[0].strip()
        e.put("what_is_promoted", first, first)
    syn = section(t, "Synopsis")
    if syn:
        e.put("synopsis", syn.split("\n")[0].strip(), syn.split("\n")[0].strip())

    # marketplace
    mk = None
    for word, name in (("content rewards", "Content Rewards"), ("whop", "Whop"), ("clipfarm", "ClipFarm on Whop"),
                       ("trafficwalk", "TrafficWalk"), ("vyro", "Vyro"), ("clipping.app", "clipping.app")):
        q = sentence_with(t, re.escape(word))
        if q:
            mk = (name, q); break
    if mk:
        e.put("marketplace", mk[0], mk[1])
    else:
        e.default("marketplace")

    # platforms
    plat_sec = section(t, "Platform", "Where to post")
    plats, pq = [], []
    for line in plat_sec.splitlines():
        low = line.lower()
        for w, p in PLATFORM_WORDS.items():
            if w in low and p not in plats:
                plats.append(p); pq.append(line.strip(" *-\t>0123456789."))
    if plats:
        e.put("platforms", plats, " | ".join(dict.fromkeys(pq)))
    else:
        _set(e.y, "platforms", ["tiktok", "instagram"]); e.unk["platforms"] = "[?] UNSTATED — default: tiktok, instagram"

    # footage
    src = re.search(r"https://(?:drive\.google\.com|f\.io|[\w.]*frame\.io|(?:www\.)?youtube\.com|youtu\.be|[\w.]*dropbox\.com)/\S+?(?=[\s)\]]|$)", md.replace("\\", ""))
    if src:
        url = src.group(0)
        fid = re.escape(url.split("?")[0].rstrip("/").split("/")[-1])
        q = sentence_with(t, fid) or sentence_with(t, r"approved (?:content|footage)|footage from")
        e.put("approved_footage_source", url, q)
    else:
        _set(e.y, "approved_footage_source", None); e.unk["approved_footage_source"] = "[?] UNSTATED — footage must be provided manually"
    q = sentence_with(t, r"only (?:the )?approved|approved (?:campaign )?footage only|no outside footage|nothing else")
    if q:
        e.put("outside_footage_allowed", False, q)
    else:
        e.default("outside_footage_allowed")

    # format
    q = sentence_with(t, r"9:16|vertical")
    e.put("format.aspect", "9:16", q) if q else e.default("format.aspect")
    m = re.search(r"(\d{1,3})\s*(?:to|-|–)\s*(\d{1,3})\s*(?:s\b|sec|seconds)", t, re.I)
    if m:
        q = sentence_with(t, re.escape(m.group(0)))
        e.put("format.min_seconds", int(m.group(1)), q); e.put("format.max_seconds", int(m.group(2)), q)
    else:
        e.default("format.min_seconds"); e.default("format.max_seconds")
    m = re.search(r"first\s+(\d+)\s*(?:s\b|sec|seconds)", t, re.I)
    e.put("format.hook_deadline_seconds", int(m.group(1)), sentence_with(t, re.escape(m.group(0)))) if m \
        else e.default("format.hook_deadline_seconds")
    q = sentence_with(t, r"on-screen text|captions throughout|sound off")
    e.put("format.on_screen_text_throughout", True, q) if q else e.default("format.on_screen_text_throughout")
    q = sentence_with(t, r"original audio|copyrighted music|music")
    e.put("format.audio", q, q) if q else e.default("format.audio")

    q = sentence_with(t, r"\bAI\b")
    if q:
        e.put("ai_rules", q, q)
        hi = re.search(r"no AI (?:auto )?clipp|AI[- ]generated (?:clips|edits) (?:are )?not allowed|auto[- ]clipp", t, re.I)
        e.put("ai_tooling_risk", "high" if hi else "low", sentence_with(t, hi.group(0)) if hi else q)
    else:
        e.default("ai_rules"); _set(e.y, "ai_tooling_risk", "low")
        e.unk["ai_tooling_risk"] = "[?] UNSTATED — default: low (brief says nothing about AI tools)"
    q = sentence_with(t, r"brand-new edits|original (?:edits|content)|no repost")
    e.put("brand_new_edits_only", True, q) if q else e.default("brand_new_edits_only")

    # tone / creative
    for key, pat in (("tone", r"^\s*\*?\s*Tone:?\s*(.+)$"), ("creative_direction", r"Creative Direction:?\s*(.+)$")):
        m = re.search(pat, t, re.I | re.M)
        if m:
            e.put(key, m.group(1).strip(), m.group(1).strip())
    # genre
    for w, g in GENRES:
        q = sentence_with(t, rf"\b{w}\b")
        if q:
            e.put("genre", g, q); break
    else:
        _set(e.y, "genre", "film_promo"); e.unk["genre"] = "[?] UNSTATED — default: film_promo"

    # captions: hashtags, CTA, tags, samples
    hl = re.search(r"(?:Required|Mandatory) Hashtags?:?\s*(.+)", t, re.I) or re.search(r"REQUIRED DISCLOSURE:?\s*(#\w+)", t)
    tags = re.findall(r"#\w+", hl.group(1)) if hl else []
    if tags:
        e.put("caption_rules.hashtags_in_order", tags, hl.group(0).strip())
    else:
        _set(e.y, "caption_rules.hashtags_in_order", []); e.unk["caption_rules.hashtags_in_order"] = "[?] UNSTATED — no mandatory hashtag found"
    q = sentence_with(t, r"end (?:it |every caption )?with #|must be the last|at the end of the caption")
    if q:
        e.put("caption_rules.hashtags_last", True, q)
    else:
        _set(e.y, "caption_rules.hashtags_last", True)
        e.unk["caption_rules.hashtags_last"] = "[?] UNSTATED — default: mandatory hashtags placed last, in the brief's order"
    m = re.search(r"(?:must|should) communicate that the film is\s+(.+?)\.|include the (.+?) CTA", t, re.I)
    if m:
        cta = (m.group(1) or m.group(2)).strip()
        e.put("caption_rules.cta_required_text", cta, sentence_with(t, re.escape(m.group(0)[:40])))
    else:
        _set(e.y, "caption_rules.cta_required_text", None)
    tag_q = sentence_with(t, r"\btag\b.*(?:handle|@)|tag the|tag @")
    handles = sorted(set(re.findall(r"@[A-Za-z0-9_.]{3,}", t)))
    if tag_q:
        e.put("mandatory_tags", handles or [tag_q], tag_q)
    else:
        e.default("mandatory_tags")
        if handles:
            oq = sentence_with(t, re.escape(handles[0]))
            e.put("official_handles", handles, oq)
    samples = re.findall(r"^\s*(?!.*\bOption\b)([^\n#*][^\n]{20,}?#\w+[^\n]*)$", section(t, "Caption Example", "Sample Caption"), re.M)
    e.put("sample_captions", [s.strip() for s in samples], None)
    q = sentence_with(t, r"copy|reuse|variation of the sample")
    if q:
        e.put("caption_rules.no_sample_copies", True, q)
    q = sentence_with(t, r"write (?:your )?own|original captions")
    if q:
        e.put("caption_rules.write_own", True, q)

    # forbidden: bullet lines under guidelines/rules/guardrails that are prohibitions
    forb = []
    for sec in ("Submission Guidelines", "Guardrails", "Rules", "Don't", "Do not"):
        for line in section(t, sec).splitlines():
            l = line.strip(" *-\t>|")
            if re.search(r"\b(avoid|don't|do not|never|no |not allowed|will not|forbidden|only)\b", l, re.I) and len(l) > 8:
                forb.append(l)
    for l in re.findall(r"^\s*\*\s*\*?\*?Don't[^\n]+", t, re.M):
        forb.append(l.strip(" *"))
    if e.y.get("caption_rules", {}).get("no_sample_copies"):
        forb.append(e.ev["caption_rules.no_sample_copies"])
    forb = list(dict.fromkeys(forb))
    e.put("forbidden", forb, None)
    e.y.setdefault("forbidden_defaults", [
        "revealing the ending, the twist or a character's fate (spoiler list from synopsis + footage)",
        "invented quotes, invented scenes, misleading re-ordering of footage",
        "AI-altered faces or voices", "outside footage, other films, fan-made assets",
        "review scores / box office unless the brief supplies them verbatim",
        "NSFW, politics, talent's private life", "the same file on two pages or platforms"])

    # audience / economics / ops
    m = re.search(r"(\d{2})\s*%\+?\s*(?:of (?:your )?views )?(?:from|in) the US", t, re.I)
    if m:
        e.put("audience.geo_rule", f">={m.group(1)}% US views", sentence_with(t, re.escape(m.group(0))))
        q = sentence_with(t, r"screen recording")
        e.put("audience.evidence", q, q) if q else None
    else:
        e.default("audience.geo_rule")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*engagement", t, re.I)
    e.put("audience.engagement_min", m.group(1) + "%", sentence_with(t, re.escape(m.group(0)))) if m else e.default("audience.engagement_min")
    m = re.search(r"CPM\s*\$?(\d+(?:\.\d+)?)", t, re.I)
    if m:
        e.put("economics.cpm_usd", float(m.group(1)), sentence_with(t, re.escape(m.group(0))))
        for key, pat in (("max_per_clip_usd", r"MAX PER CLIP\s*\$?(\d+)"), ("min_payout_usd", r"MIN PAYOUT\s*\$?(\d+(?:\.\d+)?)")):
            mm = re.search(pat, t, re.I)
            if mm:
                e.put(f"economics.{key}", float(mm.group(1)), mm.group(0))
    else:
        e.default("economics")
    m = re.search(r"at least\s+(\d+)\s*days|(\d+)\s*days live|up (?:for )?at least (\d+) days", t, re.I)
    e.put("post_must_stay_live_days", int(next(g for g in m.groups() if g)), sentence_with(t, re.escape(m.group(0)))) if m \
        else e.default("post_must_stay_live_days")
    q = sentence_with(t, r"likes visible|hidden likes")
    e.put("likes_must_stay_visible", True, q) if q else e.default("likes_must_stay_visible")
    m = re.search(r"(\d+)\s*(?:posts?|videos?|clips?)\s*(?:per|/|a)\s*(?:account\s*)?(?:per\s*)?day", t, re.I)
    e.put("daily_cap_per_platform", int(m.group(1)), sentence_with(t, re.escape(m.group(0)))) if m else e.default("daily_cap_per_platform")
    m = re.search(r"(\d+)\s*days?.{0,40}(?:exclusiv|another campaign|other campaign)", t, re.I)
    e.put("exclusivity_days", int(m.group(1)), sentence_with(t, re.escape(m.group(0)))) if m else e.default("exclusivity_days")
    q = sentence_with(t, r"watermark")
    e.put("watermark", q, q) if q else e.default("watermark")
    q = sentence_with(t, r"pin(?:ned)? (?:a )?comment")
    e.put("pinned_comment", q, q) if q else e.default("pinned_comment")
    q = sentence_with(t, r"page type|established page|new .*accounts")
    e.put("page_rule", q, q) if q else e.default("page_rule")

    # submission chain
    chain = []
    if e.y.get("marketplace") in ("Content Rewards", "ClipFarm on Whop", "Whop") or "marketplace" in e.unk:
        chain.append(dict(step="submit post link", where=e.y["marketplace"], deadline_after_post_min=30,
                          evidence="public post URL"))
        if "marketplace" in e.unk or True:
            e.unk.setdefault("submission_chain.deadline", "[?] UNSTATED — default: submit the link within 30 min of publishing (Content Rewards rule)")
    sr = e.y.get("audience", {}).get("evidence")
    if sr:
        chain.append(dict(step="ticket / demographics", where="per brief", deadline_after_post_min=None,
                          when="once analytics show demographics and min payout views are reached", evidence=sr))
    _set(e.y, "submission_chain", chain)
    _set(e.y, "second_step", chain[1] if len(chain) > 1 else None)
    if len(chain) == 1:
        e.unk["second_step"] = "[?] UNSTATED — no demographics/ticket step in the brief, so no screen recording to make"

    e.y["_evidence"] = e.ev
    e.y["_unknowns"] = e.unk
    return e.y


def check(md: str, y: dict) -> list[str]:
    """Every _evidence quote must appear verbatim (modulo whitespace/markdown/quote style) in the brief."""
    hay = norm(clean(md))
    bad = []
    for k, q in (y.get("_evidence") or {}).items():
        parts = [p for p in str(q).split(" | ") if p.strip()]
        for p in parts:
            if norm(p) not in hay:
                bad.append(f"{k}: quote not found in brief: {p[:80]!r}")
    return bad


def run(c: Campaign, force: bool = False) -> dict:
    md = (c.root / "brief.md").read_text(encoding="utf-8")
    path = c.root / "brief.yaml"
    if path.exists() and not force:
        y = load_yaml(path)
        c.log("parse", "brief.yaml exists (hand edits kept); re-checking quotes. Use --force to re-extract.")
    else:
        y = extract(md)
        dump_yaml(y, path)
        c.log("parse", f"brief.yaml written: {len(y['_evidence'])} quoted fields, {len(y['_unknowns'])} unknowns")
    bad = check(md, y)
    for b in bad:
        c.log("parse", "QUOTE CHECK FAIL " + b)
    if bad:
        raise SystemExit(f"{len(bad)} brief.yaml values are not quoted from the brief — fix brief.yaml or mark them [?].")
    return y
