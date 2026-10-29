"""SQLite state (what the tool trusts) mirrored to tracker/tracker.csv (what Noémie reads and edits).

Global registries enforce: a file hash is scheduled once anywhere; a hook / caption first line is never reused
on the same account across campaigns.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from .util import REPO

DB = REPO / "clipcrumble" / "state.sqlite"
TRACKER = REPO / "tracker" / "tracker.csv"
STATUSES = ["RENDERED", "UPLOADED", "SCHEDULED", "PUBLISHED", "URL_VERIFIED", "SUBMITTED", "SECOND_STEP_DUE",
            "SECOND_STEP_DONE", "APPROVED", "REJECTED", "PAID", "CANCELLED"]
TRACKER_COLS = ["post_id", "campaign", "concept", "platform", "account", "file", "sha256", "hook", "caption",
                "slot_local", "slot_target_tz", "submit_deadline", "drive_id", "drive_url", "metricool_post_id",
                "status",
                # manual columns (re-imported by reconcile)
                "url", "submitted_at", "submission_id", "second_step_sent", "views", "us_pct", "engagement_pct",
                "payout_usd", "notes"]
MANUAL = {"url", "submitted_at", "submission_id", "second_step_sent", "views", "us_pct", "engagement_pct",
          "payout_usd", "notes"}

SCHEMA = """
create table if not exists posts (
  post_id text primary key, campaign text, concept text, platform text, account text, file text, sha256 text,
  hook text, caption text, slot_local text, slot_target_tz text, submit_deadline text, drive_id text, drive_url text,
  metricool_post_id text, status text, url text, submitted_at text, submission_id text, second_step_sent text,
  views text, us_pct text, engagement_pct text, payout_usd text, notes text);
create table if not exists registry_hashes (sha256 text primary key, post_id text, account text);
create table if not exists registry_hooks (account text, hook_norm text, post_id text, primary key (account, hook_norm));
create table if not exists registry_caption_lines (account text, line_norm text, post_id text, primary key (account, line_norm));
"""


def norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (s or "").lower()))


def connect(db: Path = DB) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def conflicts(con, post_id: str, account: str, sha: str, hook: str, first_line: str) -> list[str]:
    out = []
    r = con.execute("select post_id from registry_hashes where sha256=?", (sha,)).fetchone()
    if r and r["post_id"] != post_id:
        out.append(f"file hash already scheduled as {r['post_id']}")
    r = con.execute("select post_id from registry_hooks where account=? and hook_norm=?", (account, norm(hook))).fetchone()
    if r and r["post_id"] != post_id:
        out.append(f"hook already used on {account} by {r['post_id']}")
    r = con.execute("select post_id from registry_caption_lines where account=? and line_norm=?",
                    (account, norm(first_line))).fetchone()
    if r and r["post_id"] != post_id:
        out.append(f"caption first line already used on {account} by {r['post_id']}")
    return out


def registry_snapshot(con, accounts: list[str]) -> dict:
    q = ",".join("?" * len(accounts)) or "''"
    return dict(
        hooks=[r[0] for r in con.execute(f"select hook_norm from registry_hooks where account in ({q})", accounts)],
        caption_lines=[r[0] for r in con.execute(f"select line_norm from registry_caption_lines where account in ({q})", accounts)],
        hashes=[r[0] for r in con.execute("select sha256 from registry_hashes")])


def register(con, post: dict, first_line: str):
    con.execute("insert or replace into registry_hashes values (?,?,?)", (post["sha256"], post["post_id"], post["account"]))
    con.execute("insert or replace into registry_hooks values (?,?,?)", (post["account"], norm(post["hook"]), post["post_id"]))
    con.execute("insert or replace into registry_caption_lines values (?,?,?)", (post["account"], norm(first_line), post["post_id"]))


def upsert_post(con, post: dict):
    cols = [c for c in TRACKER_COLS if c in post]
    existing = con.execute("select * from posts where post_id=?", (post["post_id"],)).fetchone()
    if existing:
        sets = ",".join(f"{c}=?" for c in cols if c != "post_id")
        con.execute(f"update posts set {sets} where post_id=?", [post[c] for c in cols if c != "post_id"] + [post["post_id"]])
    else:
        con.execute(f"insert into posts ({','.join(cols)}) values ({','.join('?' * len(cols))})", [post[c] for c in cols])


def import_manual_edits(con, path: Path = TRACKER) -> int:
    """Noémie edits manual columns in tracker.csv; pull them into SQLite before any export."""
    if not path.exists():
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vals = {k: row.get(k, "") for k in MANUAL if row.get(k)}
            st = row.get("status")
            if st in STATUSES:
                vals["status"] = st
            if vals and con.execute("select 1 from posts where post_id=?", (row["post_id"],)).fetchone():
                con.execute(f"update posts set {','.join(f'{k}=?' for k in vals)} where post_id=?",
                            list(vals.values()) + [row["post_id"]])
                n += 1
    return n


def export(con, path: Path = TRACKER, campaign: str | None = None, out_copy: Path | None = None):
    import_manual_edits(con, path)
    con.commit()
    rows = con.execute("select * from posts order by slot_local, post_id").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    for p, flt in ((path, None), (out_copy, campaign)):
        if not p:
            continue
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRACKER_COLS)
            w.writeheader()
            for r in rows:
                if flt and r["campaign"] != flt:
                    continue
                w.writerow({k: r[k] or "" for k in TRACKER_COLS})
