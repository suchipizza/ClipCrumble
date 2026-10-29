"""ideate: render the ideation prompt from brief + genre config + ingest artefacts; validate ideas.json and run
the compliance + caption gates; write ideas.md for approval.

The LLM step itself (reading ideate_prompt.md and writing ideas.json) is done by the operator session
(Claude Code) — the tool never trusts that output until `check()` passes it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import jsonschema

from . import captions as capmod
from . import registry
from .util import PKG, Campaign, dump_json, dump_yaml, load_json, load_yaml, tc

MIN_TOTAL = 18


def genre_cfg(name: str) -> dict:
    p = PKG / "virality" / f"{name}.yaml"
    if not p.exists():
        raise SystemExit(f"no genre config {p.name}; available: {[x.stem for x in (PKG / 'virality').glob('*.yaml')]}")
    return load_yaml(p)


def _ctx(c: Campaign) -> dict:
    brief = load_yaml(c.root / "brief.yaml")
    run = load_yaml(c.root / "run.yaml", {}) or {}
    inv = load_json(c.working / "inventory.json")
    excl = load_json(c.working / "exclusions.json", {"ranges": [], "spoiler_terms": []})
    return dict(brief=brief, run=run, inv=inv, excl=excl, genre=genre_cfg(brief.get("genre", "film_promo")))


def catalogue(c: Campaign, ctx: dict) -> str:
    """Compact, timestamped footage catalogue the strategist reads (transcript + scenes + vision notes)."""
    tr = load_json(c.working / "transcript.json", {}) or {}
    fr = load_json(c.working / "frames.json", {}) or {}
    review = load_json(c.working / "frames_review.json", {}) or {}
    ovr = load_json(c.working / "transcript_overrides.json", []) or []
    from . import subtitles
    subs = subtitles.clean(c)
    L = ["# FOOTAGE CATALOGUE", "",
         "Dialogue source: 'Official subtitles' are the film's own burned-in English lines (OCR'd, noise removed) — "
         "they are what gets captioned and the only text that may be quoted. A transcript in another language is a "
         "rough timing guide only.", "", "Inventory:"]
    for f in ctx["inv"]["files"]:
        dup = f" (exact duplicate of {f['exact_duplicate_of']})" if f["exact_duplicate_of"] else ""
        L.append(f"- {f['alias']}: {f['file']} · {f['type']} · {f['width']}x{f['height']} · "
                 f"{(f['duration'] or 0):.1f}s{dup}")
    for f in ctx["inv"]["files"]:
        a = f["alias"]
        if f["kind"] != "video" or f["exact_duplicate_of"]:
            continue
        L += ["", f"## {a} — {f['file']} ({f['duration']:.1f}s, {f['width']}x{f['height']})"]
        rv = review.get(a)
        if rv:
            L.append(f"Vision review: {rv.get('summary', '')}")
            for n in rv.get("notes", []):
                L.append(f"  - {n}")
        sc = fr.get(a, {}).get("scenes", [])
        if sc:
            L.append("Scenes (start–end · face_x · dark · also in):")
            L.append("  " + " | ".join(f"{s['start']:.1f}–{s['end']:.1f}"
                                         + (f" fx{s['face_x']}" if s.get("face_x") is not None else "")
                                         + (" DARK" if s.get("dark") else "")
                                         + (f" ={','.join(s['dupes'][:2])}" if s.get("dupes") else "") for s in sc))
        if subs.get(a):
            L.append("Official subtitles (English, burned in):")
            for q in subs[a]:
                L.append(f"  [{q['start']:.2f}–{q['end']:.2f}] {q['text']}")
        segs = tr.get(a, {}).get("segments", [])
        lang = tr.get(a, {}).get("language")
        if segs:
            L.append("Transcript:" if lang in (None, "en") else f"Audio transcript ({lang}, rough guide only):")
            for s in segs:
                if s.get("no_speech_prob", 0) > 0.8 and not s["words"]:
                    continue
                L.append(f"  [{s['start']:.2f}–{s['end']:.2f}] {s['text']}")
        for o in [o for o in ovr if o.get("src") == a]:
            L.append(f"  OVERRIDE [{o['in']:.2f}–{o['out']:.2f}] {o['text']} ({o.get('how', '')})")
    return "\n".join(L)


def render_prompt(c: Campaign) -> Path:
    ctx = _ctx(c)
    brief, run, genre = ctx["brief"], ctx["run"], ctx["genre"]
    con = registry.connect()
    reg = registry.registry_snapshot(con, run.get("accounts", []))
    platforms = brief.get("platforms") or ["tiktok", "instagram"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    tpl = env.from_string((PKG / "prompts" / "ideate.md").read_text(encoding="utf-8"))
    view_brief = dict(brief)
    view_brief.update(
        min_length_s=brief["format"]["min_seconds"], max_length_s=brief["format"]["max_seconds"],
        title=brief.get("title") or brief.get("campaign_name"),
        edit_rules="; ".join(brief.get("forbidden", [])),
        cta_required_text=brief["caption_rules"].get("cta_required_text") or "none required",
        tags=", ".join(t for t in brief.get("mandatory_tags") or [] if str(t).startswith("@")) or "none required",
        hashtags=" ".join(brief["caption_rules"].get("hashtags_in_order") or []) or "none required")
    view_run = dict(run)
    view_run.setdefault("concepts_target", 10)
    view_run.setdefault("target_audience_description", "US-first English-speaking entertainment followers")
    brief_yaml = Path(c.root / "brief.yaml").read_text(encoding="utf-8")
    text = tpl.render(
        genre=genre, brief=view_brief, run=view_run, platforms=platforms, slug=c.slug,
        inventory=dict(total_minutes=ctx["inv"]["total_minutes"], types=", ".join(ctx["inv"]["types"]),
                       aliases=[f["alias"] for f in ctx["inv"]["files"] if f["kind"] == "video"]),
        brief_yaml=brief_yaml, spoilers=json.dumps(ctx["excl"].get("spoiler_terms", [])),
        registry=dict(hooks=reg["hooks"] or "none", caption_lines=reg["caption_lines"] or "none",
                      hashes=f"{len(reg['hashes'])} hashes" if reg["hashes"] else "none"),
        exclusions=json.dumps(ctx["excl"].get("ranges", [])))
    out = c.working / "ideate_prompt.md"
    out.write_text(text + "\n\n" + catalogue(c, ctx) + "\n", encoding="utf-8")
    c.log("ideate", f"prompt rendered: {out.name} ({len(text.split())} words + catalogue); genre={brief.get('genre')}")
    return out


# ------------------------------------------------------------------ gates
def _overlap(a0, a1, b0, b1, pad=0.08):
    return a0 < b1 + pad and b0 < a1 + pad


def _words_in(c_tr: dict, src: str, t0: float, t1: float, subs: dict | None = None) -> str:
    """Spoken/official text inside a range: transcript words plus the official subtitle lines."""
    from .subtitles import in_range
    words = [w["w"] for s in c_tr.get(src, {}).get("segments", []) for w in s["words"] if t0 - 0.2 <= w["start"] <= t1]
    lines = [q["text"] for q in in_range((subs or {}).get(src, []), t0, t1)]
    return " ".join(words + lines)


def check(c: Campaign) -> dict:
    ctx = _ctx(c)
    brief, run, genre, inv, excl = ctx["brief"], ctx["run"], ctx["genre"], ctx["inv"], ctx["excl"]
    ideas = load_json(c.working / "ideas.json")
    if ideas is None:
        raise SystemExit(f"{c.working / 'ideas.json'} missing — run the prompt in working/ideate_prompt.md first")
    jsonschema.validate(ideas, load_json(PKG / "schemas" / "ideas.schema.json"))
    durations = {f["alias"]: f["duration"] for f in inv["files"] if f["kind"] == "video"}
    tr = load_json(c.working / "transcript.json", {}) or {}
    from . import subtitles
    subs = subtitles.clean(c)
    platforms = brief.get("platforms") or ["tiktok", "instagram"]
    fmt = brief["format"]
    con = registry.connect()
    accounts = run.get("accounts", [])
    results, all_caps, all_hooks = [], {}, []
    for k in ideas["concepts"]:
        fails, warns = [], []
        total = sum(k["scores"].values())
        k["total"] = total
        if k["scores"]["compliance"] == 0:
            fails.append("compliance scored 0")
        if total < MIN_TOTAL:
            fails.append(f"score {total} < {MIN_TOTAL}")
        dur = 0.0
        for s in k["segments"]:
            if s["src"] not in durations:
                fails.append(f"unknown source alias {s['src']}"); continue
            if not (0 <= s["in"] < s["out"] <= durations[s["src"]] + 0.05):
                fails.append(f"bad range {s['src']} {s['in']}–{s['out']} (file is {durations[s['src']]}s)")
            dur += s["out"] - s["in"]
            for r in excl.get("ranges", []):
                if r["src"] == s["src"] and _overlap(s["in"], s["out"], r["in"], r["out"]):
                    fails.append(f"segment {s['src']} {s['in']}–{s['out']} overlaps excluded {r['in']}–{r['out']} ({r['reason']})")
        k["computed_length_s"] = round(dur, 2)
        if not (fmt["min_seconds"] <= dur <= fmt["max_seconds"]):
            fails.append(f"length {dur:.1f}s outside {fmt['min_seconds']}–{fmt['max_seconds']}s")
        sib_plats = [s["platform"] for s in k["siblings"]]
        if sorted(sib_plats) != sorted(platforms):
            fails.append(f"siblings {sib_plats} != brief platforms {platforms}")
        shapes = [s["hook"]["shape"] for s in k["siblings"]]
        if len(set(shapes)) < len(shapes):
            fails.append("siblings share a hook shape")
        plain_starts = sum(1 for s in k["siblings"] if not s.get("first_segment_override") and not s.get("zoom_punch"))
        if plain_starts > 1:
            fails.append("more than one sibling has the default opening — first 2 s would not differ")
        for s in k["siblings"]:
            h = s["hook"]["text"]
            n = len(h.split())
            if n > genre["hook_max_words"]:
                fails.append(f"hook '{h}' has {n} words > {genre['hook_max_words']}")
            if "CAPS" in genre.get("hook_style", "") and h != h.upper():
                fails.append(f"hook '{h}' must be CAPS")
            for q in re.findall(r"[\"“]([^\"”]+)[\"”]", h):
                spoken = " ".join(_words_in(tr, seg["src"], seg["in"], seg["out"], subs) for seg in k["segments"])
                if registry.norm(q) not in registry.norm(spoken):
                    fails.append(f"quoted hook text not verbatim in the clip's dialogue/subtitles: {q!r}")
            for term in excl.get("spoiler_terms", []):
                if term.lower() in h.lower():
                    fails.append(f"hook contains spoiler term {term!r}")
            if any(registry.norm(h) == x for x in registry.registry_snapshot(con, accounts)["hooks"]):
                fails.append(f"hook already used on these accounts: {h}")
            all_hooks.append(h)
            text = capmod.build(s["caption"], brief, s["platform"])
            s["caption_text"] = text
            cf = capmod.check_caption(text, brief, s["platform"])
            if cf:
                fails += [f"{s['platform']} caption: {x}" for x in cf]
            all_caps[f"{k['id']}_{s['platform']}"] = text
        results.append(dict(id=k["id"], fails=fails, warns=warns))
    # set-level checks
    set_fails = capmod.check_set(all_caps, brief)
    dup_hooks = {h for h in all_hooks if all_hooks.count(h) > 1}
    by_id = {r["id"]: r for r in results}
    for key, fl in set_fails.items():
        by_id[key.rsplit("_", 1)[0]]["fails"] += [f"{key}: {x}" for x in fl]
    for k in ideas["concepts"]:
        for s in k["siblings"]:
            if s["hook"]["text"] in dup_hooks:
                by_id[k["id"]]["fails"].append(f"hook reused in this set: {s['hook']['text']}")
    kept = [k for k in ideas["concepts"] if not by_id[k["id"]]["fails"]]
    shape_counts = {sh: sum(1 for k in kept for s in k["siblings"] if s["hook"]["shape"] == sh) for sh in genre["hook_shapes"]}
    missing = [sh for sh, n in shape_counts.items() if n < genre.get("min_per_shape", 0)]
    report = dict(kept=[k["id"] for k in kept], dropped={r["id"]: r["fails"] for r in results if r["fails"]},
                  hook_shape_counts=shape_counts, missing_shapes=missing, inspection=ideas["inspection"])
    dump_json(ideas, c.working / "ideas.checked.json")
    dump_json(report, c.working / "ideas.gates.json")
    write_md(c, ideas, report, brief)
    c.log("ideate", f"gates: {len(kept)} kept, {len(report['dropped'])} dropped; missing hook shapes: {missing or 'none'}")
    return report


def write_md(c: Campaign, ideas: dict, rep: dict, brief: dict):
    L = [f"# Concepts — {brief.get('title')}", "",
         f"Inspection: transcript={ideas['inspection']['transcript']} · frames={ideas['inspection']['frames']} · "
         f"vision review={ideas['inspection']['vision_review']}. {ideas['inspection'].get('notes', '')}", "",
         "## Part 1 — ranked", "",
         "| # | id | concept | type | hooks (shape) | source | len | /30 | US | tier | gates |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k in sorted(ideas["concepts"], key=lambda x: x["rank"]):
        src = "; ".join(f"{s['src']} {tc(s['in'])[3:]}–{tc(s['out'])[3:]}" for s in k["segments"])
        hooks = "<br>".join(f"{s['platform'][:2].upper()}: {s['hook']['text']} ({s['hook']['shape']})" for s in k["siblings"])
        gate = "PASS" if k["id"] in rep["kept"] else "DROP"
        L.append(f"| {k['rank']} | {k['id']} | {k['title']} | {k['content_type']} | {hooks} | {src} | "
                 f"{k['computed_length_s']}s | {k['total']} | {k['us_appeal']} | {k['tier']} | {gate} |")
    L += ["", f"Hook-shape counts (kept): {rep['hook_shape_counts']}" +
          (f" — MISSING: {rep['missing_shapes']}" if rep["missing_shapes"] else ""), ""]
    if rep["dropped"]:
        L += ["### Dropped by the gates", ""] + [f"- **{i}**: " + "; ".join(f) for i, f in rep["dropped"].items()] + [""]
    L += ["## Part 2 — plans", ""]
    for k in sorted(ideas["concepts"], key=lambda x: x["rank"]):
        L += [f"### {k['id']} — {k['title']}", "",
              f"**Story:** {k['core_story']}  ", f"**Curiosity gap:** {k['curiosity_gap']}  ",
              f"**Why:** {k['why']}  ", f"**Ending:** {k['ending']}  ",
              f"**Scores:** {k['scores']} = {k['total']} · US appeal {k['us_appeal']} · tier {k['tier']}", "",
              "| # | src | in–out | dur | function | dialogue / action | crop |", "|---|---|---|---|---|---|---|"]
        for i, s in enumerate(k["segments"], 1):
            cr = s.get("crop") or {}
            L.append(f"| {i} | {s['src']} | {s['in']:.2f}–{s['out']:.2f}{' ~' if s.get('approximate') else ''} | "
                     f"{s['out'] - s['in']:.1f}s | {s.get('function', '')} | {s.get('dialogue', '')} | "
                     f"{cr.get('mode', 'crop')} x={cr.get('x', '')} z={cr.get('zoom', '')} |")
        L.append("")
        for s in k["siblings"]:
            opening = "override " + f"{s['first_segment_override']['src']} {s['first_segment_override']['in']:.2f}" \
                if s.get("first_segment_override") else (f"zoom punch ×{s['zoom_punch']}" if s.get("zoom_punch") else "default")
            L += [f"- **{s['platform']}** · hook `{s['hook']['text']}` ({s['hook']['shape']}) · opening: {opening}",
                  f"  caption: {s.get('caption_text', '')}"]
        if k.get("risks"):
            L.append(f"- risks: {'; '.join(k['risks'])}")
        L.append("")
    if ideas.get("recommendations"):
        L += ["## Part 3 — recommendations", "", "```", json.dumps(ideas["recommendations"], indent=1, ensure_ascii=False), "```"]
    (c.out / "ideas.md").write_text("\n".join(L) + "\n", encoding="utf-8")
