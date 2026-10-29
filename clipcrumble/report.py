"""report: review_bundle.md — everything needed to audit a run in a Claude chat — plus a PASS/FAIL self-check.
FAILs are reported, never fixed silently."""
from __future__ import annotations

import csv
import json

from .util import Campaign, load_json, load_yaml


def run(c: Campaign) -> bool:
    brief = load_yaml(c.root / "brief.yaml", {}) or {}
    runc = load_yaml(c.root / "run.yaml", {}) or {}
    inv = load_json(c.working / "inventory.json", {}) or {}
    gates = load_json(c.working / "ideas.gates.json", {}) or {}
    sched = load_json(c.out / "schedule.json", {}) or {}
    qa = [load_json(p) for p in sorted(c.ready.glob("*.qa.json"))]
    excl = load_json(c.working / "exclusions.json", {}) or {}
    review = load_json(c.working / "frames_review.json", {}) or {}
    L = [f"# Review bundle — {brief.get('title', c.slug)}", ""]
    sec = 0

    def h(t):
        nonlocal sec; sec += 1; L.extend(["", f"## {sec}. {t}", ""])

    h("Brief (as parsed)"); L += ["```yaml", (c.root / "brief.yaml").read_text() if (c.root / "brief.yaml").exists() else "", "```"]
    h("Run settings"); L += ["```yaml", json.dumps(runc, indent=1, default=str), "```"]
    h("Unknowns"); L += [f"- `{k}`: {v}" for k, v in (brief.get("_unknowns") or {}).items()]
    h("Footage inventory"); L += [f"- {f['alias']}: {f['file']} {f['type']} {f.get('duration')}s sha {f['sha256'][:12]}" for f in inv.get("files", [])]
    h("What ran"); L += [(c.logs / "ingest.log").read_text()[-3000:] if (c.logs / "ingest.log").exists() else "no ingest log"]
    h("Vision review + exclusions"); L += [f"- {a}: {v.get('summary', '')}" for a, v in review.items()] + \
        [f"- EXCLUDED {r['src']} {r['in']}–{r['out']}: {r['reason']}" for r in excl.get("ranges", [])] + \
        [f"- spoiler terms: {excl.get('spoiler_terms', [])}"]
    h("Concept gates"); L += [f"- kept: {gates.get('kept')}", f"- dropped: {json.dumps(gates.get('dropped', {}), ensure_ascii=False)}",
                              f"- hook shapes: {gates.get('hook_shape_counts')}"]
    h("Rendered files + QA"); L += [f"- {q['file']} {q.get('duration')}s {'PASS' if q['pass'] else 'FAIL ' + str([k for k, v in q['checks'].items() if not v])}" for q in qa]
    h("Captions (verbatim)"); L += [f"- **{q['tag']}**: {q['caption']}" for q in qa]
    h("Hooks"); L += [f"- {q['tag']}: {q['hook']}" for q in qa]
    h("Schedule"); L += [f"- {p['slot_local'][:16]} ({p['slot_target_tz'][11:16]} ET) {p['account']} {p['post_id']} {p.get('flags') or ''}" for p in sched.get("posts", [])]
    h("Metricool"); L += [f"- route: {sched.get('route')}; failed rows: {sched.get('failed')}",
                          f"- metricool.csv rows: {sum(1 for _ in open(c.out / 'metricool.csv')) - 1 if (c.out / 'metricool.csv').exists() else 0}"]
    h("Notes"); L += [f"- {n}" for n in sched.get("notes", [])]
    h("To-do"); L += [(c.out / "todo.md").read_text() if (c.out / "todo.md").exists() else "none"]
    checks = {
        "every brief value quoted or flagged [?]": bool(brief.get("_evidence")) and "_unknowns" in brief,
        "transcript ran": (c.working / "transcript.json").exists(),
        "vision review recorded": bool(review),
        "every rendered file passed QA": bool(qa) and all(q["pass"] for q in qa),
        "every scheduled post passed QA": all(any(q["file"] == p["file"] and q["pass"] for q in qa) for p in sched.get("posts", [])),
        "no API failures": not sched.get("failed"),
        "metricool.csv written": (c.out / "metricool.csv").exists(),
        "calendar written": (c.out / "calendar.ics").exists(),
    }
    h("Self-check"); L += [f"- {'PASS' if v else 'FAIL'} — {k}" for k, v in checks.items()]
    (c.out / "review_bundle.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    return all(checks.values())
