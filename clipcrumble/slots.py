"""Slot engine.

Slots are defined in the target-audience time zone (default ET) and converted per date with zoneinfo — never a
fixed offset (US DST ends 1 Nov 2026: ET->CET is 5 h before, 6 h after). Constraint stack, smallest wins:
campaign daily cap, posts_per_platform_per_day, Metricool monthly cap, IG <= 30 reels/24 h, YouTube <= 10/24 h,
TikTok >= 5 min gap. TikTok posts first, Instagram +5 min. A slot whose submission deadline is <= 1 h must end
inside keyboard hours: such slots are MOVED (flagged), never dropped.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

PLATFORM_ORDER = {"tiktok": 0, "instagram": 1, "youtube": 2}
PLATFORM_OFFSET_MIN = {"tiktok": 0, "instagram": 5, "youtube": 10}
PER_24H = {"instagram": 30, "youtube": 10, "tiktok": 10_000}
DEFAULT_SLOTS_ET = ["10:00", "12:00", "14:00", "16:00", "18:00"]
PREMIUM_ET = ["12:00", "18:00"]


@dataclass
class Slot:
    date: dt.date
    target_time: str                    # HH:MM in target tz (as designed)
    local: dt.datetime                  # aware, local tz (after any move)
    target: dt.datetime                 # aware, target tz (after any move)
    premium: bool
    moved: bool = False
    move_reason: str = ""


@dataclass
class Assignment:
    concept: str
    platform: str
    account: str
    slot: Slot
    deadline: dt.datetime | None
    flags: list[str] = field(default_factory=list)


def _hm(s: str) -> dt.time:
    h, m = s.split(":"); return dt.time(int(h), int(m))


def day_slots(date: dt.date, slots_target: list[str], tz_local: str, tz_target: str, keyboard: str | None,
              deadline_min: int | None, premium: list[str] = PREMIUM_ET, extra_min: int = 0) -> list[Slot]:
    """Slots for one calendar day *in the target tz*, converted to local. If a short deadline applies, slots
    (plus the platform offset extra_min and the deadline) must fit inside keyboard hours; otherwise moved."""
    L, T = ZoneInfo(tz_local), ZoneInfo(tz_target)
    out = []
    for hm in slots_target:
        tgt = dt.datetime.combine(date, _hm(hm), tzinfo=T)
        loc = tgt.astimezone(L)
        s = Slot(date=date, target_time=hm, local=loc, target=tgt, premium=hm in premium)
        if keyboard and deadline_min is not None and deadline_min <= 60:
            k0, k1 = (_hm(x) for x in keyboard.split("-"))
            # keyboard window on the local day the slot falls in; slots past midnight belong to the previous evening
            base = loc.date() if loc.time() >= dt.time(6, 0) else loc.date() - dt.timedelta(days=1)
            w0 = dt.datetime.combine(base, k0, tzinfo=L)
            w1 = dt.datetime.combine(base, k1, tzinfo=L)
            latest = w1 - dt.timedelta(minutes=deadline_min + extra_min)
            if loc < w0:
                s.local, s.moved, s.move_reason = w0, True, f"before keyboard hours {keyboard}"
            elif loc > latest:
                s.local, s.moved, s.move_reason = latest, True, f"submission deadline would end after {k1.strftime('%H:%M')}"
            s.target = s.local.astimezone(T)
        out.append(s)
    # moved slots can collide; keep at least 20 min apart, walking backwards from the latest
    out.sort(key=lambda x: x.local)
    for i in range(len(out) - 2, -1, -1):
        if out[i + 1].local - out[i].local < dt.timedelta(minutes=20):
            out[i].local = out[i + 1].local - dt.timedelta(minutes=20)
            out[i].target = out[i].local.astimezone(T)
            out[i].moved = True; out[i].move_reason = out[i].move_reason or "spaced from a moved slot"
    return out


def plan(concepts: list[dict], accounts: dict[str, str], start: dt.date, days: int, tz_local: str, tz_target: str,
         per_platform_per_day: int, keyboard: str | None = None, deadline_min: int | None = 30,
         daily_cap: int | None = None, monthly_cap: int | None = None, already_this_month: int = 0,
         slots_target: list[str] | None = None) -> tuple[list[Assignment], list[str]]:
    """concepts: [{id, tier, rank, platforms:[...]}] ranked best first. accounts: platform -> account id.
    Returns (assignments, notes)."""
    notes: list[str] = []
    slots_target = slots_target or DEFAULT_SLOTS_ET
    n = min(x for x in (per_platform_per_day, daily_cap or 10**6, len(slots_target)) if x)
    if n < len(slots_target):
        # keep the premium slots, drop from the edges of the day
        keep = [s for s in slots_target if s in PREMIUM_ET]
        rest = [s for s in slots_target if s not in PREMIUM_ET]
        while len(keep) < n and rest:
            keep.append(rest.pop(len(rest) // 2))
        slots_target = sorted(keep[:n])
        notes.append(f"{n} posts per platform per day (smallest of run/brief caps) → slots {slots_target} ET")
    ranked = sorted(concepts, key=lambda k: ({"A": 0, "B": 1, "C": 2}[k.get("tier", "B")], k["rank"]))
    per_day = len(slots_target)
    groups = [ranked[d * per_day:(d + 1) * per_day] for d in range(days)]
    if len(ranked) > per_day * days:
        notes.append(f"{len(ranked) - per_day * days} concepts do not fit in {days} days × {per_day} slots: "
                     f"{[k['id'] for k in ranked[per_day * days:]]}")
    out: list[Assignment] = []
    for d, group in enumerate(groups):
        if not group:
            continue
        date = start + dt.timedelta(days=d)
        for platform, acct in sorted(accounts.items(), key=lambda x: PLATFORM_ORDER.get(x[0], 9)):
            off = PLATFORM_OFFSET_MIN.get(platform, 0)
            sl = day_slots(date, slots_target, tz_local, tz_target, keyboard, deadline_min, extra_min=off)
            prem = [s for s in sl if s.premium]; norm_ = [s for s in sl if not s.premium]
            members = [k for k in group if platform in k["platforms"]]
            order = prem + norm_
            if PLATFORM_ORDER.get(platform, 0) > 0 and len(members) > 1:
                # rotate so a concept's siblings never share a slot pair (same-day A/B by different hour)
                members = members[1:] + members[:1]
            for k, s in zip(members, order):
                s2 = Slot(**{**s.__dict__})
                s2.local = s.local + dt.timedelta(minutes=off); s2.target = s2.local.astimezone(ZoneInfo(tz_target))
                dl = s2.local + dt.timedelta(minutes=deadline_min) if deadline_min else None
                a = Assignment(concept=k["id"], platform=platform, account=acct, slot=s2, deadline=dl)
                if s2.moved:
                    a.flags.append(f"moved: {s2.move_reason}")
                out.append(a)
    out.sort(key=lambda a: (a.slot.local, PLATFORM_ORDER.get(a.platform, 9)))
    # per-24h platform caps and TikTok 5-min gap
    for p, cap in PER_24H.items():
        times = [a.slot.local for a in out if a.platform == p]
        for i, t in enumerate(times):
            if sum(1 for u in times if t - dt.timedelta(hours=24) < u <= t) > cap:
                notes.append(f"{p}: more than {cap} posts in 24 h at {t:%Y-%m-%d %H:%M}")
    tt = [a for a in out if a.platform == "tiktok"]
    for x, y in zip(tt, tt[1:]):
        if (y.slot.local - x.slot.local) < dt.timedelta(minutes=5):
            y.slot.local = x.slot.local + dt.timedelta(minutes=5); y.flags.append("pushed for TikTok 5-min gap")
    if monthly_cap is not None:
        room = monthly_cap - already_this_month
        if len(out) > room:
            cut = sorted(out, key=lambda a: a.slot.local)[max(0, room):]
            notes.append(f"Metricool monthly cap {monthly_cap}: {len(cut)} posts not scheduled: {[f'{a.concept}/{a.platform}' for a in cut]}")
            out = [a for a in out if a not in cut]
    # siblings check (day 1)
    by_slot = {}
    for a in out:
        by_slot.setdefault((a.slot.date, a.slot.target_time), set()).add(a.concept)
    return out, notes
