import datetime as dt

from clipcrumble import slots

Z, NY = "Europe/Zurich", "America/New_York"


def concepts(n):
    return [dict(id=f"c{i:02d}", tier="A" if i <= 3 else "B", rank=i, platforms=["tiktok", "instagram"]) for i in range(1, n + 1)]


def test_dst_offset_changes_per_date():
    # 31 Oct: EDT (UTC-4) vs CET (UTC+1) -> 5 h; 2 Nov: EST (UTC-5) -> 6 h
    before = slots.day_slots(dt.date(2026, 10, 31), ["10:00"], Z, NY, None, None)[0]
    after = slots.day_slots(dt.date(2026, 11, 2), ["10:00"], Z, NY, None, None)[0]
    assert before.local.strftime("%H:%M") == "15:00"
    assert after.local.strftime("%H:%M") == "16:00"


def test_out_of_hours_slot_is_moved_not_dropped():
    # 18:00 ET on 2 Nov = 00:00 Zurich -> outside 13:00-23:30 with a 30-min deadline -> moved to 23:00
    s = slots.day_slots(dt.date(2026, 11, 2), slots.DEFAULT_SLOTS_ET, Z, NY, "13:00-23:30", 30)
    assert len(s) == 5
    last = max(s, key=lambda x: x.local)
    assert last.moved and last.local.strftime("%H:%M") == "23:00"
    assert all(x.local.strftime("%H:%M") >= "13:00" for x in s)


def test_instagram_offset_respects_keyboard_end():
    s = slots.day_slots(dt.date(2026, 11, 2), ["18:00"], Z, NY, "13:00-23:30", 30, extra_min=5)[0]
    assert s.local.strftime("%H:%M") == "22:55"


def test_no_move_without_short_deadline():
    s = slots.day_slots(dt.date(2026, 11, 2), ["18:00"], Z, NY, "13:00-23:30", 24 * 60)[0]
    assert not s.moved and s.local.strftime("%H:%M") == "00:00"


def test_plan_caps_and_order():
    a, notes = slots.plan(concepts(12), {"tiktok": "TT1", "instagram": "IG1"}, dt.date(2026, 10, 30), 3, Z, NY,
                          per_platform_per_day=5, keyboard="13:00-23:30", deadline_min=30, daily_cap=4)
    per = {}
    for x in a:
        per.setdefault((x.slot.local.date() if x.slot.local.hour >= 6 else x.slot.local.date() - dt.timedelta(days=1), x.platform), []).append(x)
    assert all(len(v) <= 4 for v in per.values())
    # TikTok first, IG 5 min after the same slot
    tt = [x for x in a if x.platform == "tiktok"]; ig = [x for x in a if x.platform == "instagram"]
    assert len(tt) == len(ig) == 12
    # siblings of a concept never share a slot pair
    pairs = {(x.slot.date, x.slot.target_time, x.concept) for x in tt}
    assert not any((y.slot.date, y.slot.target_time, y.concept) in pairs for y in ig)
    # tier A first at premium slots on day 1
    day1 = [x for x in tt if x.slot.date == dt.date(2026, 10, 30)]
    assert {x.concept for x in day1 if x.slot.premium} <= {"c01", "c02", "c03"}


def test_monthly_cap_truncates_with_note():
    a, notes = slots.plan(concepts(5), {"tiktok": "TT1", "instagram": "IG1"}, dt.date(2026, 10, 30), 1, Z, NY,
                          per_platform_per_day=5, monthly_cap=20, already_this_month=14)
    assert len(a) == 6 and any("monthly cap" in n for n in notes)


def test_tiktok_gap():
    a, _ = slots.plan(concepts(10), {"tiktok": "TT1"}, dt.date(2026, 11, 2), 2, Z, NY, per_platform_per_day=5,
                      keyboard="13:00-23:30", deadline_min=30)
    t = sorted(x.slot.local for x in a)
    assert all((y - x) >= dt.timedelta(minutes=5) for x, y in zip(t, t[1:]))
