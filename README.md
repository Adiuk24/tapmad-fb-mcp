# Tapmad FB MCP — Meta Ads + Page & Instagram Monitoring

Open-source MCP server for **Facebook Ads management, Facebook Page monitoring, and Instagram monitoring** — built for [Tapmad](https://www.tapmad.com). Fork of the MIT-licensed [gomarble-ai/facebook-ads-mcp-server](https://github.com/gomarble-ai/facebook-ads-mcp-server), extended with page and Instagram tools.

## Tools (32)

**Ads (21, from upstream):** ad accounts, campaigns, adsets, ads, insights at every level, creatives, activity logs, pagination.

**Facebook Pages (6, added):**
| Tool | What it does |
|---|---|
| `list_pages` | Pages the token manages (id, name, fan_count) |
| `get_page_details` | About, fans, followers, rating |
| `get_page_posts` | Recent posts with reactions/comments/shares counts |
| `get_post_comments` | Comments on a post |
| `get_page_insights` | Page engagement/follows/video-views/views metrics |
| `get_post_insights` | Per-post clicks and reactions |
| `get_page_ad_posts` | Ad creatives (dark posts) running for the Page |

**Instagram (4, added):**
| Tool | What it does |
|---|---|
| `list_instagram_accounts` | IG business accounts linked to your Pages |
| `get_instagram_media` | Recent posts/reels with like & comment counts |
| `get_instagram_media_comments` | Comments on one post |
| `get_instagram_insights` | Account reach/follower metrics |

Page tools auto-derive Page access tokens from `/me/accounts`, so one user token drives everything.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Get a Meta access token from the [Graph API Explorer](https://developers.facebook.com/tools/explorer) (or a Business Manager system user for a non-expiring one) with scopes: `ads_read`, `pages_show_list`, `pages_read_engagement`, `instagram_basic` — plus `read_insights`, `pages_read_user_content`, `instagram_manage_insights` for full insights/comments coverage. Tools degrade gracefully when optional scopes are missing.

### Claude Code

```bash
claude mcp add tapmad-fb --scope user --env FB_ACCESS_TOKEN=YOUR_TOKEN -- \
  /path/to/.venv/bin/python /path/to/server.py
```

The token can also be passed as `--fb-token YOUR_TOKEN` (upstream style).

### Any MCP client

```json
{
  "mcpServers": {
    "tapmad-fb": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": { "FB_ACCESS_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```

## Changes vs upstream

- `FB_ACCESS_TOKEN` environment variable support (upstream is CLI-flag only)
- 10 new Page/Instagram monitoring tools
- Insights defaults updated for Graph API v22 metric deprecations (`page_impressions`, `page_fans`, `post_impressions` were removed by Meta)
- `mcp` pinned `<2` (v2 renamed FastMCP)

## License

MIT — see [LICENSE](LICENSE). Upstream copyright gomarble-ai.
