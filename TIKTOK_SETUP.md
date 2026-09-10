# TikTok Business Account API — setup

The MCP server ships 5 TikTok tools (`get_tiktok_account`, `get_tiktok_videos`,
`get_tiktok_video_comments`, `tiktok_exchange_auth_code`,
`tiktok_refresh_access_token`). They talk to the official **Organic Accounts
API v1.3** at `https://business-api.tiktok.com/open_api/v1.3`.

The client lives in [`tiktok.py`](tiktok.py) and is stdlib-only, so both the MCP
server and the scheduled report (which runs on a bare CI runner with nothing
installed) share one implementation. Run its checks with
`python3 tiktok.py --selftest`.

None of them work until someone at Tapmad completes the steps below. They need
a login and an "Authorize" click, so they cannot be automated — Claude can do
every step after them.

## Steps a human has to do

1. **File the Accounts API Access Application Form.** Since **20 March 2026**
   TikTok requires this form to be approved *before* you submit an app that
   requests the `TikTok Accounts` permission scope. Skipping it gets the app
   rejected. Start at <https://business-api.tiktok.com/portal/docs/accounts-api/v1.3>
   and follow the "Note" box at the top.

2. **Create the app** in the TikTok for Developers portal, owned by the Tapmad
   business account (not a personal account). Request the `TikTok Accounts`
   scope. Register a redirect URI you control — it has to byte-match later.

3. **Authorize the Tapmad TikTok account against the app.** Use the
   authorization URL shown on your own app's page in the portal. Copy it from
   there rather than hand-building it: the parameter names on the authorize
   step differ from the token step, and the portal shows the right one for your
   app type.

4. **Grab `auth_code` and `business_id` from the callback.** TikTok redirects to
   your redirect URI with `auth_code` in the query string. Keep the
   `business_id` (a.k.a. the account's open id) that comes back with the token —
   **there is no endpoint that lists it later.** `/tt_user/info/` and
   `/tt_user/business_get/` both 404; the only way to get it is this callback.

## Steps Claude can do from here

5. Exchange the code (verified live against the endpoint on 2026-09-10):

   ```
   tiktok_exchange_auth_code(auth_code="<from callback>",
                             redirect_uri="<exact registered URI>")
   ```

   It POSTs `client_id`, `client_secret`, `grant_type=authorization_code`,
   `auth_code`, `redirect_uri` to `/tt_user/oauth2/token/` and returns
   `access_token`, `refresh_token` and the account id.

   Step 5 runs **once**. It writes the access and refresh tokens to
   `~/.tapmad-tiktok-token.json` (mode 0600) and deliberately does not return
   them. From then on tokens renew themselves — see "Token expiry" below.

6. Only three values go in the `tapmad-fb` MCP env block in `~/.claude.json`,
   alongside the existing `FB_ACCESS_TOKEN`:

   | Variable | Value |
   |---|---|
   | `TIKTOK_CLIENT_ID` | app's client id |
   | `TIKTOK_CLIENT_SECRET` | app's client secret |
   | `TIKTOK_BUSINESS_ID` | account id from step 4/5 |

   Do **not** paste an access token into config. It would be stale within a
   day. (`TIKTOK_ACCESS_TOKEN` is still honoured as a last-resort override for
   one-off debugging, and `TIKTOK_TOKEN_FILE` relocates the cache.)

## Token expiry — handled, not your problem

TikTok access tokens live ~24 hours, unlike the never-expiring Meta system-user
token. Rather than have a scheduled job babysit that, the server treats the
**refresh token** as the real credential and the access token as a cache:

- reads mint a token on demand and reuse it until 5 minutes before expiry;
- a token that lapses mid-request (error `40105`) triggers one refresh and a
  silent retry, so a long sleep between calls doesn't fail a run;
- every refresh **re-saves the refresh token**, because TikTok may rotate it.
  This is the subtle one: a rotated token that isn't persisted works fine now
  and breaks the *next* refresh, a day later, far from the cause.

So nothing calls `tiktok_refresh_access_token` on a schedule. It exists only to
force a renewal by hand, e.g. to check the stored refresh token still works.

What still needs a human: if the refresh token itself is revoked or expires
(TikTok caps its lifetime), repeat steps 3–5. The tools will say exactly that
rather than returning empty data.

## For the scheduled report (GitHub Actions)

The 6-hourly report runs on a GitHub Actions runner, which has **no persistent
filesystem** — the token cache is discarded after every run. So each run
refreshes from the `TIKTOK_REFRESH_TOKEN` repo secret instead of from cache.

Add these four repo secrets (Settings → Secrets and variables → Actions).
They're optional: without them the report writes "not connected" to the TikTok
panel and every other section runs normally.

| Secret | From |
|---|---|
| `TIKTOK_CLIENT_ID` | app |
| `TIKTOK_CLIENT_SECRET` | app |
| `TIKTOK_REFRESH_TOKEN` | step 5 |
| `TIKTOK_BUSINESS_ID` | step 4/5 |

**The one thing to watch.** If TikTok both rotates the refresh token on use
*and* invalidates the old one, the secret goes stale after the first CI refresh
and later runs fail. On the local Mac this never happens — the rotated token is
saved to `~/.tapmad-tiktok-token.json`. In CI there is nowhere to save it.

This has not been observed yet (it can't be tested without live credentials),
and many providers accept a refresh token until it expires rather than on first
use. If it does happen you will see it immediately and unambiguously: the
Dashboard TikTok panel shows **"No data — TikTok not connected"** with the
refresh error on the line below, and the run log says `tiktok: SKIPPED — ...`.
The fix is to re-run step 5 and update the `TIKTOK_REFRESH_TOKEN` secret. If it
turns out to recur, the durable fix is to have the workflow write the rotated
token back via the GitHub secrets API, or to move the TikTok section back onto
the Mac where the cache persists.

## The other thing that will bite

**Analytics lag up to 5 days.** TikTok Studio says so on its own overview page,
so the last few days of any TikTok figure are provisional and will move after
you log them.

## Why not scrape the Business Suite instead

The logged-in Business Suite page at
`https://www.tiktok.com/business-suite/insight/overview` gets its numbers from
`POST /api/ba/business/suite/analytics/query/insights`, which requires
`msToken`, `X-Bogus` and `_signature` — anti-bot parameters minted by TikTok's
own JavaScript on every request. Reproducing them outside the browser means
reverse-engineering TikTok's bot protection: it breaks whenever they rotate the
signer, and it is not something to hang a daily report on. The official API
above is the supported path.
