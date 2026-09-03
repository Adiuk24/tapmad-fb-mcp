# Tapmad Social MCP

The Tapmad marketing team's MCP server for **Claude Code**: live Facebook Page, Instagram, and Meta Ads data as tools Claude can call — plus the automated 6-hourly report that fills the team's Google Sheet tracker.

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

## What Claude can do with it (32 tools)

| Area | Tools | Examples |
|---|---|---|
| **Facebook Pages** | `list_pages`, `get_page_details`, `get_page_posts`, `get_post_comments`, `get_page_insights`, `get_post_insights`, `get_page_ad_posts` | post feeds with reactions/comments/shares, page engagement & video views, comment threads |
| **Instagram** | `list_instagram_accounts`, `get_instagram_media`, `get_instagram_media_comments`, `get_instagram_insights` | reels/posts with likes & comments, account reach & follower metrics |
| **Meta Ads** (21 tools) | campaigns, adsets, ads, insights at every level, creatives, activity logs | campaign performance, spend, creative details |

All read-only. The token carries reporting scopes only — it cannot post, edit ads, or change settings.

## The automated report

[`daily_report.py`](daily_report.py) runs every 6 hours (scheduled on the team automation Mac) and keeps the **Tapmad Monthly Calendar** Google Sheet current:

- matches each planned content row to its live Facebook post **by caption** and fills link, views, reactions, comments
- rebuilds the monthly **Highlights** and **Entertainment** tabs from what actually got posted
- appends daily KPIs to the **📊 Dashboard** and posts feedback on what the team forgot (missing captions, unmatched Done rows, pending approvals)

Team rules that keep it working are on the sheet's **📖 Read Me First** tab. The short version: paste the exact caption you post, and never hand-edit the robot's metric columns.

To run it manually:

```bash
COMPOSIO_API_KEY=... COMPOSIO_USER_ID=... FB_ACCESS_TOKEN=... SHEET_ID=... python3 daily_report.py
```

## About the token

The team token is a **Meta Business Manager system-user token** (user `tapmad-mcp` in the Pi Pakistan portfolio): it never expires and is scoped to 8 read-only permissions (`ads_read`, `instagram_basic`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_read_engagement`, `pages_read_user_content`, `pages_show_list`, `read_insights`). If it's ever compromised, any Business Manager admin can revoke it: Business settings → System users → tapmad-mcp → Revoke tokens.

Notes on Meta API limits (not bugs):

- **Reach per post** is not available — Graph API v22 removed `post_impressions*`; per-post **video views** work instead.
- **Dark-post ad creatives** (`get_page_ad_posts`) need the `pages_manage_ads` scope, which the team token deliberately excludes.

## License

MIT — see [LICENSE](LICENSE). Upstream copyright gomarble-ai.
