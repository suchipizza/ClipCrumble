"""Metricool: CSV bulk-import file (always written — it is the audit copy) + REST API client (Advanced plan).

See docs/METRICOOL_ROUTES.md for sources and the open items still to test on the real account. Nothing here
publishes immediately: every post carries a future publicationDate and the client refuses past dates.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from pathlib import Path

import requests

# Field names from Metricool's help-center annex (2026-10-07). The in-app template header is UNVERIFIED —
# diff it once (docs/METRICOOL_ROUTES.md open item 1) and fix here if needed.
CSV_COLUMNS = [
    "Text", "Date", "Time", "Draft", "Facebook", "Twitter/X", "LinkedIn", "GBP", "Instagram", "Pinterest", "TikTok",
    "YouTube", "Threads", "Bluesky", "Picture Url 1", "Video Thumbnail Url", "Video Cover Frame",
    "Instagram Post Type", "Instagram Show Reel On Feed",
    "YouTube Video Title", "YouTube Video Type", "YouTube Video Privacy", "YouTube video for kids", "YouTube Video Category",
    "TikTok disable comments", "TikTok disable duet", "TikTok disable stitch", "TikTok Post Privacy",
    "TikTok Branded Content", "TikTok Your Brand", "TikTok Auto Add Music", "Brand name"]

NETWORK_COL = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}


def csv_row(post: dict, account: dict, branded: bool) -> dict:
    """post: platform, local datetime (aware, in the brand's planner tz), caption, media_url, youtube_title."""
    row = {c: "" for c in CSV_COLUMNS}
    for p, col in NETWORK_COL.items():
        row[col] = "TRUE" if post["platform"] == p else "FALSE"
    for col in ("Facebook", "Twitter/X", "LinkedIn", "GBP", "Pinterest", "Threads", "Bluesky"):
        row[col] = "FALSE"
    t: dt.datetime = post["when"]
    row.update({"Text": post["caption"], "Date": t.strftime("%Y-%m-%d"), "Time": t.strftime("%H:%M:%S"), "Draft": "FALSE",
                "Picture Url 1": post["media_url"], "Brand name": account.get("metricool_brand", "")})
    if post["platform"] == "instagram":
        row.update({"Instagram Post Type": "REEL", "Instagram Show Reel On Feed": "TRUE"})
    if post["platform"] == "youtube":
        row.update({"YouTube Video Title": post.get("youtube_title", "")[:100], "YouTube Video Type": "SHORT",
                    "YouTube Video Privacy": "PUBLIC", "YouTube video for kids": "FALSE",
                    "YouTube Video Category": "FILM_ANIMATION"})
    if post["platform"] == "tiktok":
        personal = (account.get("tiktok_account_type") or "personal").lower() == "personal"
        row.update({"TikTok disable comments": "FALSE", "TikTok disable duet": "FALSE", "TikTok disable stitch": "FALSE",
                    "TikTok Auto Add Music": "FALSE" if personal else ""})
        if personal:
            row.update({"TikTok Post Privacy": "PUBLIC_TO_EVERYONE",
                        "TikTok Branded Content": "TRUE" if branded else "FALSE", "TikTok Your Brand": "FALSE"})
    return row


def write_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


class MetricoolAPI:
    BASE = "https://app.metricool.com/api"

    def __init__(self, token: str | None = None, user_id: str | None = None):
        self.token = token or os.environ.get("METRICOOL_USER_TOKEN")
        self.user_id = user_id or os.environ.get("METRICOOL_USER_ID")
        if not (self.token and self.user_id):
            raise RuntimeError("METRICOOL_USER_TOKEN / METRICOOL_USER_ID not set (put them in .env)")
        self.s = requests.Session()
        self.s.headers.update({"X-Mc-Auth": self.token, "Content-Type": "application/json"})

    def _q(self, blog_id=None, **kw):
        q = {"userId": self.user_id, **kw}
        if blog_id:
            q["blogId"] = blog_id
        return q

    def brands(self) -> list[dict]:
        r = self.s.get(f"{self.BASE}/admin/simpleProfiles", params=self._q(), timeout=30)
        r.raise_for_status()
        return r.json()

    def normalize(self, url: str, blog_id: str) -> str:
        r = self.s.get(f"{self.BASE}/actions/normalize/image/url", params=self._q(blog_id, url=url), timeout=600)
        r.raise_for_status()
        txt = r.text.strip()
        if txt.startswith("{"):
            j = r.json()
            return j.get("url") or j.get("data") or txt
        return txt.strip('"')

    @staticmethod
    def body(post: dict, account: dict, branded: bool, media_url: str) -> dict:
        when: dt.datetime = post["when"]
        if when <= dt.datetime.now(when.tzinfo) + dt.timedelta(minutes=10):
            raise ValueError(f"refusing to schedule {post['post_id']} at {when} — not in the future")
        b = {"publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": str(when.tzinfo)},
             "text": post["caption"], "providers": [{"network": post["platform"]}], "media": [media_url],
             "autoPublish": True, "draft": False}
        if post["platform"] == "instagram":
            b["instagramData"] = {"type": "REEL", "showReelOnFeed": True}
        elif post["platform"] == "tiktok":
            personal = (account.get("tiktok_account_type") or "personal").lower() == "personal"
            b["tiktokData"] = {"privacyOption": "PUBLIC_TO_EVERYONE", "disableComment": False, "disableDuet": False,
                               "disableStitch": False, "autoAddMusic": False,
                               "commercialContentThirdParty": bool(branded and personal),
                               "commercialContentOwnBrand": False}
        elif post["platform"] == "youtube":
            b["youtubeData"] = {"title": post.get("youtube_title", "")[:100], "type": "short", "privacy": "public",
                                "madeForKids": False, "category": "FILM_ANIMATION", "tags": []}
        return b

    def schedule(self, body: dict, blog_id: str) -> dict:
        r = self.s.post(f"{self.BASE}/v2/scheduler/posts", params=self._q(blog_id), json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Metricool {r.status_code}: {r.text[:300]}")
        return r.json()

    def get(self, post_id: str, blog_id: str) -> dict:
        r = self.s.get(f"{self.BASE}/v2/scheduler/posts/{post_id}", params=self._q(blog_id), timeout=30)
        r.raise_for_status()
        return r.json()

    def list(self, blog_id: str, start: dt.datetime, end: dt.datetime, tz: str) -> list[dict]:
        r = self.s.get(f"{self.BASE}/v2/scheduler/posts", timeout=30, params=self._q(
            blog_id, start=start.strftime("%Y-%m-%dT%H:%M:%S"), end=end.strftime("%Y-%m-%dT%H:%M:%S"), timezone=tz,
            extendedRange="false"))
        r.raise_for_status()
        j = r.json()
        return j.get("data", j) if isinstance(j, dict) else j
