# Metricool integration routes: CSV, REST API, MCP

Researched **2026-10-28** from live pages. Page dates are the `dateModified` value in each help-center page's metadata ("Updated N weeks ago" in the UI). Anything not confirmed is marked **UNVERIFIED**.

## Summary

| Route | Plan | Can schedule video? | Media by URL? | Gaps |
|---|---|---|---|---|
| **CSV import** (Planner → Import CSV) | Any plan with the Planner (plan limits apply) — plan gate itself UNVERIFIED | Yes: IG Reel, TikTok video, YouTube VIDEO/SHORT | Yes: `Picture Url 1..10`, public direct link. Google Drive `…view?usp=sharing` (public, **Editor** permission) and Dropbox `…?dl=0` are documented. | Manual upload in the UI, not an API. The template is only downloadable inside the app. Timezone handling is not documented. Recommended maximum is 50 rows per file. |
| **REST API** (`https://app.metricool.com/api`) | **Advanced or Custom only** | Yes: `POST /v2/scheduler/posts` | Yes: run the URL through `GET /actions/normalize/image/url?url=…` first, then put the returned URL in `media[]`. There is also a newer S3 presigned upload flow. | Docs disagree on the `media` shape (`["url"]` vs `{mediaId}`). Enum casing is not specified. Analytics are per network, and YouTube analytics only has a deprecated endpoint. |
| **MCP** (hosted `https://ai.metricool.com/mcp`; old OSS `mcp-metricool` on PyPI) | Hosted OAuth: **any plan, incl. Free** (Free = 20 posts/month). Token mode (`METRICOOL_USER_TOKEN`/`METRICOOL_USER_ID`) uses the API, so it needs **Advanced**. | Yes (`create_scheduled_post`) | Yes: public URL, Google Drive linked to the account, or Dropbox | It is LLM-driven, so it isn't a deterministic integration for a tool. The GitHub repo `metricool/mcp-metricool` now returns 404. The hosted tool schemas are not published. |

**Recommendation for ClipCrumble:** use the REST API if the account is on Advanced or Custom. Otherwise generate CSVs. The MCP is a convenience for humans or agents, not a pipeline backbone.

---

## 1. CSV bulk import

**Sources**
- [How to schedule posts in batch with a CSV file](https://help.metricool.com/how-to-schedule-posts-in-batch-with-a-csv-file-in-metricool-3zwxj): dateModified **2026-10-07**. The old URL `/en/article/…-3wihqx/` serves the same page.
- [Common troubleshooting when importing CSV](https://help.metricool.com/common-troubleshooting-when-importing-csv-into-metricool-m2lcv): **2026-05-14**
- [Publishing requirements for images and videos](https://help.metricool.com/publishing-requirements-for-images-and-videos-from-metricool-vfc8n): **2026-10-20**
- [How to manage your calendar](https://help.metricool.com/how-to-manage-your-calendar-in-metricool-xhlbw): **2026-10-20**

### Template
- You download the template inside the app: Planning calendar → options menu → **Import CSV** → **Download template**. A Google Sheets "Social Media Marketing Template" is also mentioned, but no public URL is given. *(CSV page, 2026-10-07)*
- **The header row has not been copied verbatim. UNVERIFIED.** Metricool doesn't publish a public link to the template CSV, and a web search found no copy of it. The page's "Annex: Available Fields in the Template" lists the field names below, in the order the page gives them. Treat them as the expected headers until you can diff them against the downloaded file. The annex abbreviates repeated columns ("Picture Url 1 (up to 10)"), so exact suffixes and order are UNVERIFIED.
- Rules: "don't delete or add columns". Boolean fields take `TRUE`/`FALSE`. Save as UTF-8 (in Excel, "CSV UTF-8").

Field names as listed in the annex (2026-10-07):
```
Text | Date | Time | Draft | Facebook | Twitter/X | LinkedIn | GBP | Instagram | Pinterest | TikTok | YouTube | Threads | Bluesky |
Picture Url 1 (up to 10) | Alt text picture 1 (up to 10) | Document title | Shortener | Video Thumbnail Url | Video Cover Frame |
Twitter/X Can reply | Twitter/X Type | Twitter/X Poll Duration minutes | Twitter/X Poll Option 1 (up to 4) |
Pinterest Board | Pinterest Pin Title | Pinterest Pin Link | Pinterest Pin New Format |
Instagram Post Type | Instagram Show Reel On Feed |
YouTube Video Title | YouTube Video Type | YouTube Video Privacy | YouTube video for kids | YouTube Video Category | YouTube Video Tags | YouTube Playlist | YouTube Notify Subscribers |
GBP Post Type | Facebook Post Type | Facebook Title | First Comment Text |
TikTok Title | TikTok disable comments | TikTok disable duet | TikTok disable stitch | TikTok Post Privacy (personal accounts only) |
TikTok Branded Content (personal accounts only) | TikTok Your Brand (personal accounts only) | TikTok Auto Add Music (personal accounts only) | TikTok Photo Cover Index |
TikTok musicId | TikTok music title | TikTok music author | TikTok music previewUrl | TikTok music thumbnailUrl | TikTok music soundVolume | TikTok music originalVolume | TikTok music startMillis | TikTok music endMillis |
LinkedIn Type | LinkedIn Poll Question | LinkedIn Poll Option 1 (up to 4) | LinkedIn Poll Duration | LinkedIn Show link preview | LinkedIn Images as Carousel |
Threads Reply Control | Threads Is Spoiler | Threads Post Type | Brand name (Optional)
```
It is UNVERIFIED whether the real header text includes the "(personal accounts only)" and "(Optional)" suffixes.

### Date, time and timezone
- Date example `YYYY-MM-DD`, time example `00:00:00`. When you import, you pick the date and time format in a dropdown, and it has to match the file. Other documented examples are `MM/DD/YY` with AM/PM. *(CSV page 2026-10-07; troubleshooting 2026-05-14)*
- **Timezone: UNVERIFIED.** No page says which timezone the CSV times are read in. The planner uses the account-settings timezone by default, and you can change it per brand in the Planning dropdown *(calendar page 2026-10-20)*. Assume the import uses the active planner timezone of the target brand, but test it.

### Media
- Use `Picture Url 1`…`10`. Each must be a public URL pointing directly to the image or video file. For a carousel, put each URL in its own column, not all in one cell. *(CSV page; troubleshooting)*
- **Google Drive:** share as "anyone with the link" with **Editor** permission, and the URL must end in `view?usp=sharing`. The page doesn't distinguish images from videos, so a Drive **video** link is documented as accepted but not specifically confirmed for video (UNVERIFIED for large videos). *(CSV page 2026-10-07)*
- **Dropbox:** public link ending in `?dl=0`. *(same)*
- MP4 is preferred. MOV is auto-converted to MP4. Instagram rejects audio above 128 kbps. HEVC and HDR video are not compatible with auto-publish. *(CSV page; troubleshooting; media requirements 2026-10-20)*
- Video limits *(media requirements 2026-10-20)*:
  - Instagram: MOV/MP4, ≤500 MB, 3 s–15 min, 9:16, ≤25 Mbps.
  - TikTok: MP4/MOV, ≤500 MB, 3 s–10 min.
  - YouTube: MP4/MOV, ≤500 MB, Shorts ≤3 min.
- `Video Cover Frame`: frame offset for IG Reels, Pinterest and TikTok. Formats: `151456` ms, `31.456`, `2:31.456`, `2min 31s 456ms`, `PT2M31.456S`. Pick the matching "Duration format" when you import.
- `Video Thumbnail Url` works for IG reels, TikTok **business**, and YouTube videos.

### Per-network columns (2026-10-07 annex)
- **Instagram Reels:**
  - `Instagram` = TRUE.
  - `Instagram Post Type` = `POST` | `REEL` | `TRIAL_REEL` | `STORY` (required, or the row is ignored).
  - `Instagram Show Reel On Feed` = true/false.
- **TikTok:**
  - `TikTok` = TRUE.
  - `TikTok Title`: max 90 characters. **Only for TikTok images, not videos.** Video caption comes from `Text`.
  - `TikTok disable comments`, `TikTok disable duet`, `TikTok disable stitch`: true/false.
  - `TikTok Post Privacy` = `PUBLIC_TO_EVERYONE` | `MUTUAL_FOLLOW_FRIENDS` | `SELF_ONLY` | `FOLLOWER_OF_CREATOR` (personal accounts only).
  - `TikTok Branded Content`: true = promoting another brand ("Branded Content").
  - `TikTok Your Brand`: true = promoting yourself ("Brand Organic").
  - Both brand flags are personal accounts only.
- **YouTube Shorts:**
  - `YouTube` = TRUE.
  - `YouTube Video Title`: max 100 characters.
  - `YouTube Video Type` = `VIDEO` | `SHORT`.
  - `YouTube Video Privacy` = `PUBLIC` | `PRIVATE` | `UNLISTED`.
  - `YouTube video for kids`: true/false.
  - `YouTube Video Category`: e.g. `MUSIC`. The troubleshooting page says YouTube **needs a category**.
  - `YouTube Video Tags`: comma-separated.
  - `YouTube Playlist`: exact name, case-sensitive.
  - `YouTube Notify Subscribers`: blank = notify.
- `First Comment Text` is free text. Writing `FALSE` in it posts "FALSE" as the comment. *(troubleshooting)*

### Brand name and limits
- `Brand name` is optional. If it's empty, the post goes to the brand you're currently in. The name must match exactly and is case-sensitive. Use one row per brand. *(CSV page; troubleshooting)*
- There's no hard row limit, but Metricool **recommends ≤50 posts per file**, because large files slow down or fail. *(CSV page; troubleshooting)*
- Rows with non-public media or missing required fields are reported as "X posts ignored". *(troubleshooting)*

---

## 2. REST API

**Sources**
- [Basic Guide for API Integration](https://help.metricool.com/basic-guide-for-api-integration-r97af): **2026-10-16**
- [Common questions and errors when using the API](https://help.metricool.com/common-questions-and-errors-when-using-the-api-8x9nq): **2026-07-17**
- [API Access](https://help.metricool.com/api-access-export-your-metricool-data-to-other-tools-and-automate-tasks-x8ln5): **2026-10-08**
- [API Limitations per Social Network](https://help.metricool.com/api-limitations-per-social-network-n7zlr): **2026-10-15**
- [API docs index](https://app.metricool.com/resources/apidocs/index.html): undated, footer "© 2024"
- [swagger.json](https://app.metricool.com/api/swagger.json): OpenAPI 3.0.1, `info.version` 2.0.0, 593 paths, fetched 2026-10-28. The server sends no Last-Modified header.
- [API PDF](https://static.metricool.com/API+DOC/API+English.pdf): Last-Modified 2024-09-04, so it's stale.
- Old official MCP source `mcp-metricool` 1.1.9 on [PyPI](https://pypi.org/project/mcp-metricool/): uploaded 2025-10-07. [github.com/metricool/mcp-metricool](https://github.com/metricool/mcp-metricool) returns **404** as of 2026-10-28; the only remaining public repo in the org is `metricool/servers`.

### Basics
- **Plan:** Advanced or Custom only. Free and Starter have no API access. *(Basic guide 2026-10-16)*
- **Base URL:** `https://app.metricool.com/api` *(swagger info; Basic guide)*
- **Auth:**
  - Header `X-Mc-Auth: <userToken>`, plus query params `userId` and `blogId` on every call.
  - Send `Content-Type: application/json` when there's a body.
  - The token is under Account Settings → API.
  - *(Basic guide; swagger info)*
- **List brands:**
  - `GET /admin/simpleProfiles?userId=…` returns `PublicBlog[]`. Each item has `id` (the blogId), `label`, `timezone`, `tiktokAccountType` (BUSINESS/PERSONAL), `instagramConnectionType`, and per-network handles. *(Basic guide; swagger)*
  - The MCP source uses `GET /v2/settings/brands?userId=…` instead. *(mcp-metricool 1.1.9 `tools.py`)*

### Scheduling endpoints (Basic guide 2026-10-16 + swagger)
| Action | Endpoint |
|---|---|
| Create | `POST /v2/scheduler/posts?blogId=&userId=`. Body is the `ScheduledPost` schema. Optional `jobId` query param. |
| List (calendar) | `GET /v2/scheduler/posts?blogId=&userId=&start=2026-11-01T00:00:00&end=2026-11-07T23:59:59&timezone=Europe/Madrid`. The MCP also passes `extendedRange=false`. |
| Get one | `GET /v2/scheduler/posts/{id}` |
| Update / patch / delete | `PUT`, `PATCH` or `DELETE /v2/scheduler/posts/{id}` |
| Post history | `GET /v2/scheduler/posts/{uuid}/events` |

**Status:** each entry in `providers[]` is a `ProviderStatus` with `{network, id, status, publicUrl, detailedStatus}`. `status` is one of `PUBLISHED | PUBLISHING | PENDING | AWAITING_CONFIRMATION | ERROR | DRAFT` *(swagger)*. Poll GET by id to track publishing.

### `ScheduledPost` body (swagger, 2026-10-28)
- **Required:** `publicationDate {dateTime (required), timezone}`, `text`, `providers[]`.
- **Optional:**
  - `media: string[]`
  - `mediaAltText: string[]`
  - `autoPublish`
  - `draft`
  - `firstCommentText`
  - `saveExternalMediaFiles` (boolean, undocumented)
  - `videoCoverMilliseconds` (int)
  - `videoThumbnailUrl`
  - `shortener`
  - `brandName`
  - `targetBrandId`
  - `descendants[]`
  - `smartLinkData`
  - `brandTagIds[]`
- **`instagramData`:** `type`, `showReelOnFeed`, `autoPublish`, `collaborators[]`, `isAiGenerated`, `audioName`, `audioConfiguration`, `shareTrialAutomatically`, `tags`, `carouselTags`, `boost…`
- **`tiktokData`:**
  - `disableComment`, `disableDuet`, `disableStitch`
  - `privacyOption`
  - `commercialContentThirdParty`: the CSV's "Branded Content". This mapping is inferred from the descriptions.
  - `commercialContentOwnBrand`: the CSV's "Your Brand" / Brand Organic.
  - `title`, `autoAddMusic`, `photoCoverIndex`, `music{…}`, `articleLink`, `isAigc`
  - **The fields `brandedContent` and `brandContent` do not exist.**
- **`youtubeData`:** `title`, `type`, `privacy`, `tags[]`, `category`, `playlistId`, `madeForKids`, `isAiGeneratedContent`, `notifySubscribers`.

The swagger gives **no enums** for `type`/`privacy`. The old MCP docstring uses lowercase YouTube values (`"video"|"short"`, `"public"|"unlisted"|"private"`), while the CSV uses uppercase. **Casing is UNVERIFIED, so test it.** Network IDs `"instagram"`, `"tiktok"` and `"youtube"` come from the MCP source.

### Example bodies
These are assembled from the swagger plus the field defaults in the mcp-metricool 1.1.9 docstring. Shapes are confirmed. Exact enum casing for `type` and `privacy` and the `media` shape are still to be tested.

**TikTok video**
```json
{
  "publicationDate": { "dateTime": "2026-11-03T18:30:00", "timezone": "Europe/Paris" },
  "text": "Caption with #hashtags",
  "providers": [ { "network": "tiktok" } ],
  "media": [ "https://<url returned by normalize>.mp4" ],
  "autoPublish": true,
  "draft": false,
  "videoCoverMilliseconds": 1500,
  "tiktokData": {
    "privacyOption": "PUBLIC_TO_EVERYONE",
    "disableComment": false,
    "disableDuet": false,
    "disableStitch": false,
    "commercialContentThirdParty": false,
    "commercialContentOwnBrand": false,
    "autoAddMusic": false
  }
}
```

**Instagram Reel**
```json
{
  "publicationDate": { "dateTime": "2026-11-03T18:30:00", "timezone": "Europe/Paris" },
  "text": "Caption",
  "providers": [ { "network": "instagram" } ],
  "media": [ "https://<url returned by normalize>.mp4" ],
  "autoPublish": true,
  "draft": false,
  "instagramData": { "type": "REEL", "showReelOnFeed": true }
}
```

**YouTube Short:** add this block to the same envelope.
```json
"providers": [ { "network": "youtube" } ],
"youtubeData": { "title": "≤100 chars", "type": "short", "privacy": "public", "madeForKids": false, "tags": [], "category": "ENTERTAINMENT" }
```
The old MCP docstring requires a title and `madeForKids` for YouTube, and says Reels must include a video. Valid categories include FILM_ANIMATION, MUSIC, PEOPLE_BLOGS, COMEDY, ENTERTAINMENT, EDUCATION, and others.

`autoPublish: false` means a manual mobile-notification publish instead of auto-publish. This comes from a community doc ([inematds/pubmetricool](https://github.com/inematds/pubmetricool), 2026-08) and is UNVERIFIED officially.

### Media handling (conflicting sources)
1. **Normalize:** `GET /actions/normalize/image/url?url=<public URL>`. It has an optional `folder` param, which community code sets to `PLANNER`. The swagger summary says it "Validates that the URL is publicly accessible and transforms it into a URL from a metricool repository". Despite the `image` in the path, community code uses it for video too. The help center says links must be public and must not expire, and that "private or temporary URLs are skipped and the post is scheduled without media". *(swagger; Basic guide 2026-10-16)*
2. **What goes into `media`:**
   - The help center (2026-10-16 and 2026-07-17) says `"media": {"mediaId": "ID_OF_MEDIA"}`.
   - **The swagger types `media` as `array<string>`, and no `mediaId` exists on ScheduledPost.**
   - Every code example uses `"media": ["<url>"]`: the old official MCP (empty list default), [vicampuzano/metricool-mcp](https://github.com/vicampuzano/metricool-mcp) (2026-08-21, which normalizes each URL and treats the response as a **plain-text URL**), and inematds.
   - **Use a list of URL strings. The `mediaId` form looks wrong or outdated (UNVERIFIED).**
   - The trusted hosts that skip normalization are `static.metricool.com`, `metricool-download.s3.eu-west-1.amazonaws.com` and `metricool-data.s3.eu-west-1.amazonaws.com` *(vicampuzano `media_normalizer.py`)*.
3. **Newer direct upload (swagger only, no help article):**
   1. `PUT /v2/media/s3/upload-transactions` with `{resourceType:"planner", contentType:"video/mp4", fileExtension:"mp4", parts:[{size,startByte,endByte,hash(base64 SHA-256)}]}`. It returns a `presignedUrl` (simple, under 5 MB) or an `uploadId` plus per-part presigned URLs (5 MB or more).
   2. Upload to S3.
   3. `PATCH /v2/media/s3/upload-transactions` with `{multipart:{uploadId,key,parts:[{partNumber,etag}]}}` or `{simple:{fileUrl}}`. It returns `fileUrl` / `convertedFileUrl` (MOV is converted to MP4).
   - It can return 403 "Feature not enabled for user". **Whether API tokens can use it is UNVERIFIED.**

### Analytics for published posts (swagger)
All take `from`/`to` (ISO, e.g. `2026-10-01T00:00:00`), an optional `timezone`, plus `blogId`/`userId`.
- `GET /v2/analytics/reels/instagram`
- `GET /v2/analytics/posts/instagram`
- `GET /v2/analytics/posts/tiktok`
- YouTube: the old MCP calls `/v2/analytics/posts/youtube`, **which is not in the current swagger**. The swagger only has `GET /stats/youtube/videos`, which is marked **deprecated**. YouTube post analytics are therefore UNVERIFIED.
- Generic metrics/timelines are also exposed. See the swagger and the Data Studio field list in vicampuzano's `data/`.

### API limits relevant to shorts
*(API limitations page 2026-10-15)*
- Instagram can't make a Reel from static images.
- TikTok custom cover images only work on Business accounts (personal accounts use a frame), and TikTok Stories aren't supported.
- YouTube Shorts can't get custom thumbnails.

---

## 3. MCP server

**Sources**
- [MCP Basics](https://help.metricool.com/mcp-basics-what-it-is-and-what-its-for-xbv20): **2026-10-16**
- [How to connect Metricool's MCP](https://help.metricool.com/how-to-connect-metricools-mcp-eqp9h): **2026-09-02**
- [MCP limits and plan requirements](https://help.metricool.com/mcp-limits-and-plan-requirements-h72jg): **2026-10-14**
- [FAQs about the Metricool MCP](https://help.metricool.com/faqs-about-the-metricool-mcp-1i3w0): **2026-10-22**
- [MCP vs API](https://help.metricool.com/mcp-vs-api-access-what-is-the-difference-5y3ib): **2026-10-07**
- [Schedule and edit posts via MCP](https://help.metricool.com/schedule-and-edit-posts-through-an-llm-with-metricools-mcp-p8y4b): **2026-07-16**
- [More scheduling options with MCP](https://help.metricool.com/more-scheduling-options-with-metricools-mcp-ylkdd): **2026-10-14**

### Hosted server
- **URL:** `https://ai.metricool.com/mcp` (production). Auth is OAuth; n8n can use header `X-Mc-Auth: <API key>` instead.
- **Plan:** works on **any plan, including Free**, with plan limits applied (Free: 20 scheduled posts per month, 30 days of analytics). A token-based connection goes through the API and needs Advanced. *(limits 2026-10-14; connect 2026-09-02)*
- **Tools named in the docs:**
  - Get available metrics, Get Analytics Data, Get Best Time to post, Get Schedule posts, Create Scheduled Post, Update Scheduled Post *(Basics 2026-10-16)*
  - `create_scheduled_post`, `update_scheduled_post` *(2026-07-16)*
  - `get_brands`, `update_schedule_post` *(FAQ 2026-10-22; the names aren't consistent)*
  - The exact live tool names and input schemas are **UNVERIFIED**, because Metricool doesn't publish them. Connect a client and run list-tools.
- **Video by URL: yes.**
  - Media must be reachable by link: a public URL, a file in Google Drive linked to the Metricool account, or Dropbox. ChatGPT can also attach files. *(More options 2026-10-14)*
  - Instagram needs an image or video, and YouTube needs a video and a title. *(same)*
  - Each network, date or brand becomes its own post. Different times per network are supported, as are recurring posts on any plan and multiple brands per request.
- **Not covered:** inbox, comments, SmartLinks, GBP reviews, autolists, Canva/Drive file browsing, and Ads management. *(Basics; FAQ)*

### Self-hosted OSS package
- `mcp-metricool` on PyPI, v1.1.9, 2025-10-07. The FAQ (2026-10-22) still points to GitHub `metricool/mcp-metricool`, **but that repo returns 404**.
- **Env vars:** `METRICOOL_USER_TOKEN`, `METRICOOL_USER_ID`. Run it with `uvx --upgrade mcp-metricool`. The README requires **Advanced** (API access).
- **Tools:**
  - `get_brands`, `get_brands_complete`
  - `get_instagram_reels|posts|stories`, `get_tiktok_videos`, `get_youtube_videos`, `get_facebook_*`, `get_thread_posts`, `get_x_posts`, `get_bluesky_posts`, `get_linkedin_posts`, `get_pinterest_pins`, `get_twitch_videos`
  - ads campaigns (FB, Google, TikTok), `get_network_competitors(_posts)`, `get_pinterest_boards`
  - `post_schedule_post(date, blog_id, info)`, `get_scheduled_posts`, `get_best_time_to_post`, `update_schedule_post`
  - `get_metrics`, plus analytics-by-metric tools
- `post_schedule_post` POSTs `info` unchanged to `/v2/scheduler/posts?…&integrationSource=MCP`. It does **not** call normalize, so you must give it a normalized or public URL.

### Community server
[vicampuzano/metricool-mcp](https://github.com/vicampuzano/metricool-mcp) (2026-08-21) runs at `https://mcp.metricool.ai/mcp`. It references Metricool's internal Java tickets, but its affiliation is **UNVERIFIED**. Tools: `get_brand_settings`, `get_scheduled_posts`, `get_best_time_to_post_by_network`, `create_scheduled_post`, `update_scheduled_post`, `get_analytics_available_metrics`, `get_analytics_data_by_metrics`. It normalizes media URLs automatically.

---

## Open items to test on the real account
1. Download the CSV template in the Planner and paste the **exact header row** into this file. Diff it against the annex list, including the "(personal accounts only)" suffixes, column order and Picture Url numbering.
2. CSV timezone: import a row at `10:00:00` into a brand whose planner timezone is different from the account timezone, and check where the post lands.
3. CSV with a Google Drive **video** link (`view?usp=sharing`, public with Editor permission) and with a Dropbox `?dl=0` video. Do both import and auto-publish for IG Reel, TikTok and YT Short? Test a file over 100 MB.
4. CSV: does `YouTube Video Type=SHORT` work without `YouTube Video Category`? The troubleshooting page says category is required.
5. API: call `GET /actions/normalize/image/url?url=<public mp4>`. Is the response plain text or JSON? How long does it take for a 100 MB video? Does it accept Drive or Dropbox links?
6. API: `POST /v2/scheduler/posts` with `"media": ["<normalized url>"]`, then try `"media": {"mediaId": …}`, to see which works.
7. API enum casing: `youtubeData.type` `"short"` vs `"SHORT"`, and `privacy` `"public"` vs `"PUBLIC"`.
8. API: TikTok on a **personal** account vs a business account, checking `privacyOption`, the commercial-content flags and `videoThumbnailUrl` (the docs say a custom cover is business only).
9. API: is `PUT /v2/media/s3/upload-transactions` usable with an API token, or does it return 403 "Feature not enabled"?
10. API: poll `GET /v2/scheduler/posts/{id}` after the publish time. Confirm the `providers[].status` transitions, and that `publicUrl` gets filled in.
11. API: is there a working YouTube video analytics endpoint? Try `/v2/analytics/posts/youtube` even though it's not in the swagger, and `/stats/youtube/videos`.
12. MCP: connect `https://ai.metricool.com/mcp` and list the tools. Record the exact names and the `create_scheduled_post` input schema, especially the media field and whether it normalizes automatically.
13. Confirm the account's plan (Advanced or Custom?) and API usage limits (Account Settings → API, `GET /v2/settings/users/{userId}/api-usage-stats`).

---

## 4. Hosted MCP: confirmed in use (2026-10-28/29)

Verified by scheduling about 75 posts through `https://ai.metricool.com/mcp` on a Starter plan (OAuth, no API token).

- **Tools:** `getBrandSettings`, `createScheduledPost`, `updateScheduledPost`, `getScheduledPosts`, `getBestTimeToPostByNetwork`, review tools, analytics. **There is no delete tool.** Removing posts is manual in the web app.
- **Media:** a public Google Drive `https://drive.google.com/file/d/<id>/view?usp=sharing` link works in `media[]` and in `videoThumbnailUrl` (custom cover). At create time Metricool copies the file to `static.metricool.com`. Replacing the Drive file afterwards changes nothing; update the post with the new URL.
- **TikTok:** `tiktokData.title` is **required** (error "TikTok title is required"). Set it to the full caption; for TikTok video posts it may be what TikTok shows as the caption.
- **Instagram:** `instagramData: {"type": "REEL", "showReelOnFeed": true}`.
- **Testing:** create with `"draft": true`, check the result, then call `updateScheduledPost` with `"draft": false`. An update returns a **new `id`**; the `uuid` stays the same. Send the full post content on update.
- **Times:** `publicationDate` takes a local `dateTime` plus an IANA `timezone` (use the brand's timezone from `getBrandSettings`).
- **Audio:** Instagram rejects audio above **128 kbps**, and Metricool does not check this when you create the post. The renderer now encodes AAC at 128k.
- **Several campaigns on one account:** list scheduled posts first and avoid scheduling two posts in the same minute on the same network.
