"""schedule: QA-passed clips -> slots -> Drive upload -> metricool.csv -> (API scheduling) -> calendar.ics -> tracker.

Route resolution (`scheduler_route: auto`): API if a Metricool token is configured, else CSV only. CSV is always
written. Any API failure falls back to CSV for that row and is listed in todo.md.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from zoneinfo import ZoneInfo

from . import hosting as hostmod
from . import metricool, registry, slots, todo
from . import captions as capmod
from .util import REPO, Campaign, dump_json, load_json, load_yaml


def _env():
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def run(c: Campaign, dry_run: bool = False) -> dict:
    _env()
    brief = load_yaml(c.root / "brief.yaml"); runc = load_yaml(c.root / "run.yaml")
    accts = todo.accounts_cfg()
    ideas = load_json(c.working / "ideas.checked.json")
    qa = {p.name[:-8]: load_json(p) for p in c.ready.glob("*.qa.json")}
    passed = {k: v for k, v in qa.items() if v.get("pass")}
    notes = [f"{k}: QA FAIL — not scheduled" for k, v in qa.items() if not v.get("pass")]
    fit = todo.account_fit(brief, runc, accts)
    refused = {l.split()[1].rstrip(":") for l in fit if l.startswith("REFUSE")}
    plat_acct = {}
    for a in runc.get("accounts", []):
        if a in refused:
            continue
        p = accts[a]["platform"]
        if p in plat_acct:
            raise SystemExit(f"two accounts for {p} ({plat_acct[p]}, {a}): v1 schedules one account per platform per run")
        plat_acct[p] = a
    concepts = []
    for k in ideas["concepts"]:
        plats = [s["platform"] for s in k["siblings"] if f"{k['id']}_{s['platform']}" in passed and s["platform"] in plat_acct]
        if plats:
            concepts.append(dict(id=k["id"], tier=k["tier"], rank=k["rank"], platforms=plats))
    tz, tzt = runc.get("tz", "Europe/Zurich"), runc.get("target_audience_tz", "America/New_York")
    deadline = (brief.get("submission_chain") or [{}])[0].get("deadline_after_post_min")
    start = dt.date.fromisoformat(str(runc["start"]))
    con = registry.connect()
    month_used = con.execute("select count(*) from posts where status not in ('CANCELLED') and substr(slot_local,1,7)=?",
                             (start.strftime("%Y-%m"),)).fetchone()[0]
    assigns, n2 = slots.plan(concepts, plat_acct, start, int(runc.get("days", 3)), tz, tzt,
                             int(runc.get("posts_per_platform_per_day", 5)), runc.get("keyboard_hours"), deadline,
                             brief.get("daily_cap_per_platform"), runc.get("metricool_monthly_cap"), month_used,
                             runc.get("slots_target"))
    notes += n2
    # registry conflicts before anything leaves the machine
    posts = []
    for a in assigns:
        tag = f"{a.concept}_{a.platform}"; q = passed[tag]
        post_id = f"{c.slug}-{tag}-{a.account}"
        first = capmod._first_line(q["caption"])
        cf = registry.conflicts(con, post_id, a.account, q["sha256"], q["hook"], first)
        if cf:
            notes.append(f"{post_id}: NOT scheduled — {'; '.join(cf)}"); continue
        when = a.slot.local
        posts.append(dict(post_id=post_id, campaign=c.slug, concept=a.concept, platform=a.platform, account=a.account,
                          file=q["file"], sha256=q["sha256"], hook=q["hook"], caption=q["caption"], first_line=first,
                          when=when, slot_local=when.isoformat(), slot_target_tz=a.slot.target.isoformat(),
                          submit_deadline=a.deadline.isoformat() if a.deadline else "", flags=a.flags))
    route = runc.get("scheduler_route", "auto")
    if route == "auto":
        route = "api" if os.environ.get("METRICOOL_USER_TOKEN") else "csv"
    host = hostmod.RcloneDrive(runc.get("rclone_remote", "gdrive"), runc.get("drive_root", "ClipCrumble"))
    ok, msg = host.ready()
    dump_json(dict(ready=ok, msg=msg), c.out / "hosting_check.json")
    if not ok and not dry_run:
        todo.write(c, notes + [msg]); raise SystemExit(msg)
    for p in posts:
        if dry_run:
            p.update(drive_id="DRYRUN", drive_url=f"https://drive.example/{p['file']}")
        else:
            up = host.upload(c.ready / p["file"], c.slug)
            p.update(drive_id=up["drive_id"], drive_url=up["view_url"], download_url=up["download_url"])
        c.log("schedule", f"uploaded {p['file']} -> {p['drive_id']}")
    branded = any("partner" in h.lower() for h in brief["caption_rules"].get("hashtags_in_order", []))
    rows = []
    for p in posts:
        acc = accts.get(p["account"], {})
        brand_tz = ZoneInfo(acc.get("metricool_timezone") or tz)
        rows.append(metricool.csv_row(dict(platform=p["platform"], when=p["when"].astimezone(brand_tz), caption=p["caption"],
                                           media_url=p["drive_url"]), acc,
                                           branded and acc.get("tiktok_branded_toggle", True)))
    metricool.write_csv(rows, c.out / "metricool.csv")
    failed = []
    if route == "api" and not dry_run:
        api = metricool.MetricoolAPI()
        for p in posts:
            acc = accts.get(p["account"], {})
            try:
                media = api.normalize(p["download_url"], acc["metricool_blog_id"])
                res = api.schedule(api.body(p, acc, branded and acc.get("tiktok_branded_toggle", True), media), acc["metricool_blog_id"])
                p["metricool_post_id"] = str((res.get("data") or res).get("id", ""))
                c.log("schedule", f"Metricool API scheduled {p['post_id']} id={p['metricool_post_id']}")
            except Exception as e:  # fall back to CSV for this row
                failed.append(p["post_id"]); c.log("schedule", f"API FAIL {p['post_id']}: {e}")
    for p in posts:
        status = "SCHEDULED" if (route == "api" and p["post_id"] not in failed and not dry_run) else "UPLOADED"
        if dry_run:
            status = "RENDERED"
        registry.upsert_post(con, {**{k: p.get(k, "") for k in registry.TRACKER_COLS if k in p}, "status": status,
                                   "drive_id": p.get("drive_id", ""), "drive_url": p.get("drive_url", ""),
                                   "metricool_post_id": p.get("metricool_post_id", "")})
        if not dry_run:
            registry.register(con, p, p["first_line"])
    con.commit()
    registry.export(con, out_copy=c.out / "tracker.csv", campaign=c.slug)
    _ics(c, brief, posts, runc)
    sched = dict(route=route if not dry_run else "dry-run", failed=failed, notes=notes,
                 posts=[{k: v for k, v in p.items() if k not in ("when",)} for p in posts])
    dump_json(sched, c.out / "schedule.json")
    todo.write(c, notes)
    return sched


def _ics(c: Campaign, brief: dict, posts: list[dict], runc: dict):
    def f(t: dt.datetime): return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    title = brief.get("title") or c.slug
    where = brief.get("marketplace")
    dl = (brief.get("submission_chain") or [{}])[0].get("deadline_after_post_min") or 30
    ev = []
    for p in posts:
        t = p["when"]
        body = (f"post {p['post_id']}\\nfile {p['file']}\\nVERIFY LIVE FIRST, then copy URL and submit on {where} "
                f"within {dl} min\\ncaption: {p['caption']}\\nlog URL + submission id in tracker/tracker.csv")
        ev.append(("SLOT", f"{title} · {p['account']}/{p['platform']} · post + submit", t, t + dt.timedelta(minutes=dl), body))
    if posts:
        tz = ZoneInfo(runc.get("tz", "Europe/Zurich"))
        first = min(p["when"] for p in posts).date(); last = max(p["when"] for p in posts).date()
        live = int(brief.get("post_must_stay_live_days") or 30)
        d = first
        while d <= last + dt.timedelta(days=7):
            t = dt.datetime.combine(d, dt.time(10, 0), tzinfo=tz)
            ev.append(("DAILY", f"{title} · daily check", t, t + dt.timedelta(minutes=15),
                       "check views/engagement of live posts; update tracker; second steps due?"))
            d += dt.timedelta(days=1)
        t = dt.datetime.combine(last + dt.timedelta(days=live), dt.time(10, 0), tzinfo=tz)
        ev.append(("END", f"{title} · retention ends (posts may come down)", t, t + dt.timedelta(minutes=15),
                   f"posts had to stay live {live} days"))
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ClipCrumble//EN", "CALSCALE:GREGORIAN"]
    for kind, summ, a, b, body in ev:
        L += ["BEGIN:VEVENT", f"UID:{uuid.uuid5(uuid.NAMESPACE_URL, c.slug + summ + a.isoformat())}@clipcrumble",
              f"DTSTAMP:{f(dt.datetime.now(dt.timezone.utc))}", f"DTSTART:{f(a)}", f"DTEND:{f(b)}",
              f"SUMMARY:{summ}", f"DESCRIPTION:{body}"]
        if kind == "SLOT":
            L += ["BEGIN:VALARM", "TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:post going live", "END:VALARM"]
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    (c.out / "calendar.ics").write_text("\r\n".join(L) + "\r\n", encoding="utf-8")
