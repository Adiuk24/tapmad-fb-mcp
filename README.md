# Tapmad Social MCP

The Tapmad marketing team's MCP server for **Claude Code**: live Facebook Page, Instagram, TikTok, and Meta Ads data as tools Claude can call — plus the automated 6-hourly report that fills the team's Google Sheet tracker.

Built on the MIT-licensed [gomarble-ai/facebook-ads-mcp-server](https://github.com/gomarble-ai/facebook-ads-mcp-server), extended for Tapmad.

---

## Team quick start (2 minutes)

You need two things: **Claude Code** installed, and the **Tapmad access token** — ask Arif (Head of Business & Marketing) for it. Never commit or share the token outside the team.

Run this one command (replace `PASTE_TOKEN_HERE`):

```bash
claude mcp add tapmad-fb --scope user \
  --env FB_ACCESS_TOKEN=PASTE_TOKEN_HERE \
  -- uvx --from git+https://github.com/Adiuk24/tapmad-fb-mcp tapmad-fb-mcp
```

That's it. Open a new Claude Code session and try:

- *"Show me this week's Tapmad Facebook posts sorted by reactions"*
- *"What's tapmad.bd's Instagram reach this week?"*
- *"Summarize the comments on our latest reel"*

Facebook, Instagram and Meta Ads work immediately with that one token. **TikTok
needs a separate one-time setup** (a developer app on the Tapmad business
account) — see [`TIKTOK_SETUP.md`](TIKTOK_SETUP.md). Everything else keeps
working until that's done; the TikTok tools just report that they aren't
connected yet.

<details>
<summary>No <code>uv</code>? Install it first (or use pip instead)</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or with plain pip:

```bash
pip install git+https://github.com/Adiuk24/tapmad-fb-mcp
claude mcp add tapmad-fb --scope user --env FB_ACCESS_TOKEN=PASTE_TOKEN_HERE -- tapmad-fb-mcp
```
</details>

---

## What Claude can do with it (37 tools)

| Area | Tools | Examples |
|---|---|---|
| **Facebook Pages** | `list_pages`, `get_page_details`, `get_page_posts`, `get_post_comments`, `get_page_insights`, `get_post_insights`, `get_page_ad_posts` | post feeds with reactions/comments/shares, page engagement & video views, comment threads |
| **Instagram** | `list_instagram_accounts`, `get_instagram_media`, `get_instagram_media_comments`, `get_instagram_insights` | reels/posts with likes & comments, account reach & follower metrics |
| **TikTok** | `get_tiktok_account`, `get_tiktok_videos`, `get_tiktok_video_comments`, `tiktok_exchange_auth_code`, `tiktok_refresh_access_token` | account followers/views/profile views, per-video views & watch time, comment threads |
| **Meta Ads** (21 tools) | campaigns, adsets, ads, insights at every level, creatives, activity logs | campaign performance, spend, creative details |

All read-only. The token carries reporting scopes only — it cannot post, edit ads, or change settings.

**TikTok needs a one-time setup** before its tools work — a developer app on the
Tapmad business account plus an approved Accounts API access form. Steps are in
[`TIKTOK_SETUP.md`](TIKTOK_SETUP.md). TikTok access tokens expire every ~24h,
so the server stores the *refresh* token and mints access tokens on demand;
after the one-time setup nobody has to rotate anything.

## The automated report

[`daily_report.py`](daily_report.py) runs every 6 hours (scheduled on the team automation Mac) and keeps the **Tapmad Monthly Calendar** Google Sheet current:

- matches each planned content row to its live Facebook post **by caption** and fills link, views, reactions, comments
- rebuilds the monthly **Highlights** and **Entertainment** tabs from what actually got posted
- rebuilds a monthly **TikTok** tab — every TikTok post with views, reach, likes, comments, shares, watch time, and whether it was in the content calendar — plus a TikTok panel on the Dashboard
- logs **Instagram stories** before they expire (24h) into the **Stories Log** tab
- appends daily KPIs to the **📊 Dashboard** and posts feedback on what the team forgot (missing captions, unmatched Done rows, pending approvals)

Every section degrades independently: if one platform's token is missing or
expired, that section writes **"No data"** and the rest of the report still
runs. A missing number is never written as a zero — a zero would read as "we
posted nothing", which is a different and much worse claim.

Team rules that keep it working are on the sheet's **📖 Read Me First** tab. The short version: paste the exact caption you post, and never hand-edit the robot's metric columns.

The report runs itself — nobody on the team needs to run it. If the sheet looks stale, tell Arif.

## About the tokens

### Meta (Facebook + Instagram + Ads)

The team token is a **Meta Business Manager system-user token** (user `tapmad-mcp` in the Pi Pakistan portfolio): it never expires and is scoped to 8 read-only permissions (`ads_read`, `instagram_basic`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_read_engagement`, `pages_read_user_content`, `pages_show_list`, `read_insights`). If it's ever compromised, any Business Manager admin can revoke it: Business settings → System users → tapmad-mcp → Revoke tokens.

Notes on Meta API limits (not bugs):

- **Reach per post** is not available — Graph API v22 removed `post_impressions*`; per-post **video views** work instead.
- **Dark-post ad creatives** (`get_page_ad_posts`) need the `pages_manage_ads` scope, which the team token deliberately excludes.

### TikTok

TikTok works differently and the difference matters. Its access tokens live
**~24 hours**, so there is no equivalent of the never-expiring Meta token. The
server stores the **refresh token** as the real credential and mints access
tokens on demand: reads renew themselves, and a token that lapses mid-request
retries once. Nobody rotates anything by hand after the one-time setup.

- Config is `TIKTOK_CLIENT_ID`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_BUSINESS_ID` — **never** an access token, which would be stale within a day.
- Setup needs an approved **Accounts API access form** before the app is submitted (a TikTok requirement since 20 Mar 2026). Full steps: [`TIKTOK_SETUP.md`](TIKTOK_SETUP.md).
- TikTok analytics **lag up to 5 days**, so the newest TikTok figures are provisional and will move after they're logged.
- There is no API that lists your business accounts, so `TIKTOK_BUSINESS_ID` has to be captured from the authorization callback and kept.

Not used, deliberately: the logged-in TikTok Business Suite web page. Its
numbers come from a request signed with `msToken`/`X-Bogus`/`_signature`,
minted by TikTok's own JavaScript. Reproducing those means reverse-engineering
their bot protection, which breaks on every rotation — not something to hang a
daily report on.

## License

MIT — see [LICENSE](LICENSE). Tapmad's extensions are © Tapmad; the file also retains the copyright notice of [gomarble-ai's server](https://github.com/gomarble-ai/facebook-ads-mcp-server) this project started from, which the MIT license requires.
