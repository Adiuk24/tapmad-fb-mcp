"""TikTok Business Account (Organic Accounts API v1.3) — shared client.

Stdlib only, on purpose. Both callers need it and they have different
constraints: server.py is the MCP (has requests, has mcp), daily_report.py runs
on a bare GitHub Actions runner with nothing installed. Keeping this urllib-only
lets the report import it directly instead of dragging FastMCP into a cron job.

It lives in one file so the token-refresh logic exists exactly once — the
rotation handling below is the kind of thing that silently rots if it is
copy-pasted into two places.

Endpoint surface verified against the live API on 2026-09-10 by probing each
path with a deliberately invalid token: a real path answers code 40105
("Access token is incorrect or has been revoked"), a wrong one answers 40006
("no schema found"). /business/get/, /business/video/list/ and
/business/comment/list/ are real; /tt_user/info/ and /tt_user/business_get/ are
404, so there is NO endpoint that lists the accounts a token can see —
business_id comes from the OAuth callback and is configured, never discovered.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

API_VERSION = "v1.3"
BASE_URL = f"https://business-api.tiktok.com/open_api/{API_VERSION}"

# Access tokens live ~24h, so a token pasted into config is a job that works
# today and breaks tomorrow. The durable credential is the refresh_token:
# stored once, then access tokens are minted from it on demand.
TOKEN_FILE = os.environ.get("TIKTOK_TOKEN_FILE") or os.path.expanduser(
    "~/.tapmad-tiktok-token.json")
EXPIRY_MARGIN = 300  # mint a new token this many seconds before the old one dies

ACCOUNT_FIELDS = ["username", "display_name", "profile_image", "followers_count",
                  "following_count", "likes", "video_views", "profile_views",
                  "comments", "shares", "audience_countries", "audience_genders"]

VIDEO_FIELDS = ["item_id", "create_time", "caption", "share_url", "embed_url",
                "video_views", "likes", "comments", "shares", "reach",
                "video_duration", "full_video_watched_rate",
                "total_time_watched", "average_time_watched", "impression_sources"]


class TikTokError(RuntimeError):
    """A non-zero `code` in a TikTok response body."""
    def __init__(self, code, message, request_id=None):
        self.code, self.message = code, message
        super().__init__(f"TikTok API error {code}: {message}"
                         + (f" (request_id {request_id})" if request_id else ""))


class NotConfigured(RuntimeError):
    """No usable credentials. Callers should record absence, never a zero."""


def _request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        # TikTok returns real errors as JSON bodies on 4xx too.
        try:
            return json.loads(e.read())
        except Exception:
            raise RuntimeError(f"TikTok HTTP {e.code} for {urllib.parse.urlsplit(url).path}") from None


def _unwrap(body):
    if body.get("code"):
        raise TikTokError(body.get("code"), body.get("message"), body.get("request_id"))
    return body.get("data", body)


# --- token storage -------------------------------------------------------

def load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_tokens(data):
    """Persist 0600 — this holds a long-lived credential. Written to a temp file
    and renamed so an interrupted write cannot leave truncated JSON that locks
    us out of our own refresh token."""
    try:
        tmp = f"{TOKEN_FILE}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, TOKEN_FILE)
    except OSError as e:
        # A read-only or ephemeral filesystem (CI) must not fail the run; the
        # token still works for this process. See ROTATION note below.
        print(f"tiktok: could not persist token cache ({e}) — continuing", file=sys.stderr)


def refresh(refresh_token=None, client_id=None, client_secret=None):
    """Mint a fresh access token and persist the result.

    ROTATION: TikTok may return a NEW refresh_token here. It is always saved,
    because a rotated token that isn't persisted works now and breaks the *next*
    refresh — a day later, far from the cause. On an ephemeral runner the save
    is a no-op, so if TikTok also invalidates the old token the stored secret
    has to be updated by hand; the caller surfaces that rather than hiding it.
    """
    cached = load_tokens()
    current = (refresh_token or cached.get("refresh_token")
               or os.environ.get("TIKTOK_REFRESH_TOKEN"))
    if not current:
        raise NotConfigured(
            "No TikTok refresh_token: run the one-time authorize + exchange "
            "(see TIKTOK_SETUP.md).")
    data = _unwrap(_request(f"{BASE_URL}/tt_user/oauth2/refresh_token/",
        data=json.dumps({
            "client_id": client_id or os.environ.get("TIKTOK_CLIENT_ID"),
            "client_secret": client_secret or os.environ.get("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token",
            "refresh_token": current}).encode(),
        headers={"Content-Type": "application/json"}))
    saved = {"access_token": data.get("access_token"),
             "refresh_token": data.get("refresh_token") or current,
             "expires_at": time.time() + float(data.get("expires_in") or 86400)}
    save_tokens(saved)
    return saved


def exchange_code(auth_code, redirect_uri, client_id=None, client_secret=None):
    """Trade the callback's auth_code for tokens and persist them. Run once.

    The five body fields and their exact names were confirmed against the live
    endpoint: client_id, client_secret, grant_type, auth_code, redirect_uri
    (redirect_uri must byte-match the one registered on the app)."""
    data = _unwrap(_request(f"{BASE_URL}/tt_user/oauth2/token/",
        data=json.dumps({
            "client_id": client_id or os.environ.get("TIKTOK_CLIENT_ID"),
            "client_secret": client_secret or os.environ.get("TIKTOK_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "auth_code": auth_code,
            "redirect_uri": redirect_uri}).encode(),
        headers={"Content-Type": "application/json"}))
    if data.get("refresh_token"):
        save_tokens({"access_token": data.get("access_token"),
                     "refresh_token": data["refresh_token"],
                     "expires_at": time.time() + float(data.get("expires_in") or 86400)})
    return data


def access_token():
    """A live access token, refreshed when the cached one is inside
    EXPIRY_MARGIN of expiry. Nothing scheduled needs to track token lifetime.

    Order: a pinned token (--tiktok-token / TIKTOK_ACCESS_TOKEN_PINNED) for
    one-off debugging, then the refreshable cache, then a hand-pasted
    TIKTOK_ACCESS_TOKEN as a last resort."""
    if "--tiktok-token" in sys.argv:
        i = sys.argv.index("--tiktok-token") + 1
        if i >= len(sys.argv):
            raise NotConfigured("--tiktok-token provided but no token value followed it")
        return sys.argv[i]
    if os.environ.get("TIKTOK_ACCESS_TOKEN_PINNED"):
        return os.environ["TIKTOK_ACCESS_TOKEN_PINNED"]
    cached = load_tokens()
    if cached.get("access_token") and cached.get("expires_at", 0) - EXPIRY_MARGIN > time.time():
        return cached["access_token"]
    if cached.get("refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN"):
        return refresh()["access_token"]
    if os.environ.get("TIKTOK_ACCESS_TOKEN"):
        return os.environ["TIKTOK_ACCESS_TOKEN"]
    raise NotConfigured(
        "No TikTok credentials: set TIKTOK_CLIENT_ID / TIKTOK_CLIENT_SECRET / "
        "TIKTOK_REFRESH_TOKEN, or run the one-time exchange. See TIKTOK_SETUP.md.")


def business_id(explicit=None):
    bid = explicit or os.environ.get("TIKTOK_BUSINESS_ID")
    if not bid:
        raise NotConfigured(
            "business_id required: set TIKTOK_BUSINESS_ID. TikTok has no endpoint "
            "that lists your business accounts — it comes from the OAuth callback.")
    return bid


# --- reads ---------------------------------------------------------------

def call(path, bid=None, _allow_refresh=True, **params):
    """GET a Business API endpoint.

    Two differences from Meta's Graph API, both load-bearing: lists/dicts go as
    JSON (TikTok rejects Graph's comma-joined `fields`), and failures arrive as
    HTTP 200 with a non-zero `code`. Raising on that code is the point — a
    revoked token otherwise returns an empty `data` that reads downstream as
    "this account posted nothing", silently zeroing a report instead of
    failing it."""
    token = access_token()
    query = {"business_id": business_id(bid)}
    for key, value in params.items():
        if value is None:
            continue
        query[key] = json.dumps(value) if isinstance(value, (list, dict)) else value
    body = _request(f"{BASE_URL}{path}?" + urllib.parse.urlencode(query),
                    headers={"Access-Token": token})
    if body.get("code") == 40105 and _allow_refresh and (
            load_tokens().get("refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN")):
        # 40105 is both "revoked" and "expired". The margin above normally
        # prevents it, but a token can lapse between the check and the request
        # landing. Refresh once and retry; _allow_refresh stops a loop when the
        # refreshed token is still rejected.
        refresh()
        return call(path, bid, _allow_refresh=False, **params)
    return _unwrap(body)


def account(bid=None, fields=None, start_date=None, end_date=None):
    return call("/business/get/", bid, fields=fields or ACCOUNT_FIELDS,
                start_date=start_date, end_date=end_date)


def videos(bid=None, fields=None, max_count=20, cursor=None, filters=None):
    return call("/business/video/list/", bid, fields=fields or VIDEO_FIELDS,
                max_count=max(1, min(max_count, 20)), cursor=cursor, filters=filters)


def video_comments(video_id, bid=None, max_count=20, cursor=None, include_replies=False):
    return call("/business/comment/list/", bid, video_id=video_id,
                max_count=max(1, min(max_count, 20)), cursor=cursor,
                include_replies=include_replies)


def all_videos(bid=None, page_cap=30):
    """Page video/list to the end. Raises if the response shape is not what we
    expect, so an API change shows up as a loud failure on the first run
    instead of a report that quietly says zero posts."""
    out, cursor = [], None
    for _ in range(page_cap):
        page = videos(bid, cursor=cursor) or {}
        items = page.get("videos", page.get("list"))
        if items is None:
            raise RuntimeError(f"unexpected video/list shape, keys={sorted(page)}")
        out.extend(items)
        cursor = page.get("cursor")
        if not page.get("has_more") or not cursor:
            break
    return out


def selftest():
    """python tiktok.py --selftest — no network, no real credentials."""
    global TOKEN_FILE
    import stat, tempfile
    TOKEN_FILE = os.path.join(tempfile.mkdtemp(), "tok.json")
    os.environ.update(TIKTOK_BUSINESS_ID="biz123", TIKTOK_CLIENT_ID="cid",
                      TIKTOK_CLIENT_SECRET="csec")
    for k in ("TIKTOK_REFRESH_TOKEN", "TIKTOK_ACCESS_TOKEN", "TIKTOK_ACCESS_TOKEN_PINNED"):
        os.environ.pop(k, None)
    sys.argv = [a for a in sys.argv if a != "--tiktok-token"]

    seen, posts = {}, []
    real = globals()["_request"]

    def fake(url, data=None, headers=None):
        if data:  # an OAuth POST
            posts.append(json.loads(data))
            # TikTok rotates the refresh token here.
            return {"code": 0, "data": {"access_token": f"at-{len(posts)}",
                                        "refresh_token": f"rt-{len(posts)}",
                                        "expires_in": 86400}}
        seen.update(url=url, headers=headers)
        return seen.get("reply", {"code": 0, "data": {"ok": True}})

    globals()["_request"] = fake
    try:
        # 1. one-time exchange seeds the cache
        exchange_code("ac", "https://x/cb")
        assert posts[-1]["grant_type"] == "authorization_code", posts[-1]
        assert load_tokens()["refresh_token"] == "rt-1", load_tokens()
        assert stat.S_IMODE(os.stat(TOKEN_FILE).st_mode) == 0o600

        # 2. a live token is reused, and lists go out as JSON not CSV
        n = len(posts)
        assert call("/business/get/", fields=["username", "likes"]) == {"ok": True}
        assert len(posts) == n, "a valid token must not trigger a refresh"
        assert 'fields=%5B%22username%22%2C+%22likes%22%5D' in seen["url"], seen["url"]
        assert "business_id=biz123" in seen["url"] and "start_date" not in seen["url"]
        assert seen["headers"]["Access-Token"] == "at-1", seen["headers"]

        # 3. proactive refresh sends the ROTATED token and stores the next one
        c = load_tokens(); c["expires_at"] = time.time() + 10; save_tokens(c)
        call("/business/get/")
        assert posts[-1]["refresh_token"] == "rt-1", posts[-1]
        assert seen["headers"]["Access-Token"] == "at-2", seen["headers"]
        assert load_tokens()["refresh_token"] == "rt-2", load_tokens()

        # 4. a token that lapses mid-flight self-heals exactly once
        calls = {"n": 0}
        def expire_once(url, data=None, headers=None):
            if data: return fake(url, data, headers)
            calls["n"] += 1
            seen.update(headers=headers)
            return ({"code": 40105, "message": "revoked"} if calls["n"] == 1
                    else {"code": 0, "data": {"recovered": True}})
        globals()["_request"] = expire_once
        assert call("/business/get/") == {"recovered": True}, "40105 should self-heal"
        assert calls["n"] == 2, calls
        globals()["_request"] = fake

        # 5. a persistent error raises with its code, and does not loop
        seen["reply"] = {"code": 40105, "message": "revoked", "request_id": "r"}
        try:
            call("/business/get/")
        except TikTokError as e:
            assert e.code == 40105, e
        else:
            raise AssertionError("a persistent non-zero code must raise")
        seen["reply"] = {"code": 0, "data": {"videos": [{"item_id": "1"}], "has_more": False}}

        # 6. max_count is clamped to TikTok's ceiling of 20
        videos(max_count=500)
        assert "max_count=20" in seen["url"], seen["url"]
        assert all_videos() == [{"item_id": "1"}]

        # 7. an unrecognised list shape fails loudly instead of returning []
        seen["reply"] = {"code": 0, "data": {"items": [], "has_more": False}}
        try:
            all_videos()
        except RuntimeError as e:
            assert "unexpected video/list shape" in str(e), e
        else:
            raise AssertionError("an unknown response shape must raise")

        # 8. with nothing configured, absence is signalled — never a zero
        TOKEN_FILE = os.path.join(tempfile.mkdtemp(), "none.json")
        try:
            access_token()
        except NotConfigured:
            pass
        else:
            raise AssertionError("missing credentials must raise NotConfigured")
    finally:
        globals()["_request"] = real
    print("tiktok selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print(__doc__)
