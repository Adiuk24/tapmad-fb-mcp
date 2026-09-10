#!/usr/bin/env python3
"""Tapmad daily social report: match tracker rows to live FB posts, fill
metrics, append daily KPIs to the Dashboard tab. Run by LaunchAgent each
morning; safe to re-run (idempotent).

Tokens come from the tapmad-fb MCP entry in ~/.claude.json.
"""
import json, re, time, unicodedata, datetime, urllib.request, urllib.parse, sys, os

PAGE = "251041461420485"
IG = "17841476533667147"
MONTH_TABS = ["September 2026", "October 2026", "November 2026", "December 2026"]

def col_letter(i):
    return chr(65 + i) if i < 26 else "A" + chr(65 + i - 26)

def find_cols(headers):
    """Locate columns by header text each run, so a team column insert (like the
    BRAND Approval column that appeared mid-September and shifted everything one
    column right) can never make us write into the wrong columns again."""
    def find(*names, contains=False):
        for n in names:
            for i, h in enumerate(headers):
                h = (h or "").strip()
                if (n.lower() in h.lower()) if contains else (h.lower() == n.lower()):
                    return i
        return None
    return {"caption": find("Caption"), "link": find("Fb Post Link", "Facebook"),
            "view": find("View"), "rx": find("Reaction"), "cm": find("Comment"),
            "content": find("Match Details", "Content Topic"),
            "status": find("Status", contains=True),
            "approval": find("Approval Status") or find("Approval", contains=True)}

# secrets/ids from env vars (cloud runs) with local ~/.claude.json fallback
def _cfg(key):
    if os.environ.get(key): return os.environ[key]
    try:
        return json.load(open(os.path.expanduser("~/.claude.json")))["mcpServers"]["tapmad-fb"]["env"][key]
    except Exception:
        raise SystemExit(f"missing config: set env {key}")
CK, UID, FBTOK, SID = _cfg("COMPOSIO_API_KEY"), _cfg("COMPOSIO_USER_ID"), _cfg("FB_ACCESS_TOKEN"), _cfg("SHEET_ID")

def composio(slug, args):
    body = json.dumps({"user_id": UID, "arguments": args}).encode()
    for attempt in range(6):
        req = urllib.request.Request(f"https://backend.composio.dev/api/v3/tools/execute/{slug}",
            data=body, headers={"x-api-key": CK, "Content-Type": "application/json"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=90))
        except Exception as e:
            r = {"error": str(e)}
        if r.get("successful"): return r.get("data") or {}
        if "429" in str(r.get("error", "")) or "Quota" in str(r.get("error", "")):
            time.sleep(20 * (attempt + 1)); continue
        raise RuntimeError(f"{slug}: {str(r.get('error'))[:200]}")
    raise RuntimeError(f"{slug}: retries exhausted")

def page_token():
    # system-user token (never expires) derives page tokens directly; Composio fallback
    try:
        r = graph("me/accounts", FBTOK, fields="id,access_token", limit=100)
        for p in r.get("data", []):
            if p["id"] == PAGE and p.get("access_token"):
                return p["access_token"]
    except Exception:
        pass
    d = composio("FACEBOOK_GET_USER_PAGES", {})
    for p in (d.get("response_data") or d).get("data", []):
        if p["id"] == PAGE and p.get("access_token"):
            return p["access_token"]
    return FBTOK

def _get_json(req, timeout=60, tries=4):
    """Retry transient network failures. An unattended cron run must not die on
    one timed-out read: the 2026-09-04 03:00 run crashed on a Graph pagination
    read timeout ~200 posts in. HTTPError is deliberately NOT retried — Facebook
    returns real API errors as 4xx JSON bodies that callers parse for meaning."""
    for attempt in range(tries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == tries - 1: raise
            time.sleep(5 * (attempt + 1))

def graph(path, tok, **params):
    params["access_token"] = tok
    url = f"https://graph.facebook.com/v22.0/{path}?" + urllib.parse.urlencode(params)
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip().lower()

# " [row 4, 9, 12 +3 more]" — the feedback panel names the rows to fix, not just a count.
def rowlist(rows, cap=8):
    if not rows: return ""
    more = f" +{len(rows) - cap} more" if len(rows) > cap else ""
    return f" [row {', '.join(str(r) for r in rows[:cap])}{more}]"

# post_id -> video views. Reach is NOT here on purpose: post_impressions,
# post_impressions_unique, post_engaged_users and post_activity are all rejected by
# Graph v22.0 for this page ("must be a valid insights metric"), so Reach stays "No data".
VIEWS = {}

# words the header over each pattern cell may contain (any one is enough).
# KEEP matches anything. Sept heads its link column "Fb Post Link", Oct+ "Facebook".
HDR_EXPECT = {"LINK": ("link", "facebook", "fb"), "VIEWS": ("view",),
              "RX": ("reaction",), "CM": ("comment",)}

def verify_layout(pat, hdr):
    """Return a list of misalignments between a month's pattern and its real header row.
    A shifted config silently wrote reactions into 'View' for all of September; this
    turns that class of bug into a refusal to write instead of bad data on the sheet."""
    problems = []
    for k, cell in enumerate(pat):
        want = HDR_EXPECT.get(cell)
        if not want: continue
        got = (hdr[k] if k < len(hdr) else "") or ""
        if not any(w in got.strip().lower() for w in want):
            problems.append(f"pattern[{k}]={cell} expects a {'/'.join(want)} header, "
                            f"sheet says {got.strip()!r}")
    return problems

def fetch_views(ids, tok):
    """Batch post_video_views into the VIEWS cache. Skips ids already fetched, so
    calling this for tracker rows and again for Highlights costs one lookup per post."""
    todo = [i for i in dict.fromkeys(ids) if i and i not in VIEWS]
    for bi in range(0, len(todo), 50):
        chunk = todo[bi:bi + 50]
        batch = json.dumps([{"method": "GET", "relative_url": f"{pid}/insights?metric=post_video_views"}
                            for pid in chunk])
        body = urllib.parse.urlencode({"access_token": tok, "batch": batch}).encode()
        try:
            br = json.load(urllib.request.urlopen(
                urllib.request.Request("https://graph.facebook.com/v22.0", data=body), timeout=120))
            for pid, res in zip(chunk, br):
                if res and res.get("code") == 200:
                    d = json.loads(res["body"]).get("data", [])
                    if d and d[0].get("values"):
                        VIEWS[pid] = d[0]["values"][0].get("value")
        except Exception as e:
            print("views batch err:", e)
    return VIEWS

REACH = {}
WATCH = {}       # post_id -> avg time watched (ms)
MEDIA_TYPE = {}  # post_id -> video | photo | album | share ...
VID_OF = {}      # post_id -> video id

def fetch_media(ids, tok):
    """Batch attachments: media type + video target id, cached."""
    todo = [i for i in dict.fromkeys(ids) if i and i not in MEDIA_TYPE]
    for bi in range(0, len(todo), 50):
        chunk = todo[bi:bi + 50]
        batch = json.dumps([{"method": "GET", "relative_url": f"{p}/attachments?fields=media_type,target"} for p in chunk])
        body = urllib.parse.urlencode({"access_token": tok, "batch": batch}).encode()
        try:
            br = json.load(urllib.request.urlopen(
                urllib.request.Request("https://graph.facebook.com/v22.0", data=body), timeout=120))
            for pid, res in zip(chunk, br):
                if res and res.get("code") == 200:
                    d = json.loads(res["body"]).get("data", [])
                    if d:
                        MEDIA_TYPE[pid] = d[0].get("media_type", "")
                        t = (d[0].get("target") or {}).get("id")
                        if t: VID_OF[pid] = t
                    else:
                        MEDIA_TYPE[pid] = "text"
        except Exception as e:
            print("attachments batch err:", e)

def fetch_reach(ids, tok):
    """post_impressions_unique (reach) lives on /{video_id}/video_insights in v22 —
    the post-level insights edge rejects it. Also captures avg watch time (ms)."""
    fetch_media(ids, tok)
    vids = [(p, VID_OF[p]) for p in dict.fromkeys(ids) if p in VID_OF and p not in REACH]
    for bi in range(0, len(vids), 50):
        chunk = vids[bi:bi + 50]
        # v22 quirk: post_impressions_unique is rejected as an explicit metric but
        # is included in the default (unfiltered) video_insights response
        batch = json.dumps([{"method": "GET",
            "relative_url": f"{v}/video_insights"} for _, v in chunk])
        body = urllib.parse.urlencode({"access_token": tok, "batch": batch}).encode()
        try:
            br = json.load(urllib.request.urlopen(
                urllib.request.Request("https://graph.facebook.com/v22.0", data=body), timeout=120))
            for (pid, _), res in zip(chunk, br):
                if res and res.get("code") == 200:
                    for m in json.loads(res["body"]).get("data", []):
                        if not m.get("values"): continue
                        if m.get("name") == "post_impressions_unique":
                            REACH[pid] = m["values"][0].get("value")
                        elif m.get("name") == "post_video_avg_time_watched":
                            WATCH[pid] = m["values"][0].get("value")
        except Exception as e:
            print("video_insights batch err:", e)
    return REACH

def main():
    today = datetime.date.today()
    tab = today.strftime("%B %Y")
    tok = page_token()

    # 1. fetch this month's posts with engagement
    posts, url = [], f"{PAGE}/published_posts"
    params = {"since": str(today.replace(day=1) - datetime.timedelta(days=3)),
              "until": str(today + datetime.timedelta(days=1)),
              "limit": 50, "fields": "id,message,created_time,permalink_url,shares,"
              "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"}
    r = graph(url, tok, **params)
    while True:
        posts.extend(r.get("data", []))
        nxt = (r.get("paging") or {}).get("next")
        if not nxt or len(posts) > 2000: break
        r = _get_json(nxt)
    print(f"{tab}: {len(posts)} posts fetched")

    # 2. fill tracker metrics by caption match (header-aware)
    cfg = tab in MONTH_TABS
    if cfg:
        by_prefix = {}
        for p in posts:
            for L in (40, 25):
                k = norm(p.get("message"))[:L]
                if k: by_prefix.setdefault((L, k), []).append(p)
        hdr = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID, "ranges": [f"{tab}!A1:Z1"]})
        headers = ((hdr.get("valueRanges") or [{}])[0].get("values") or [[]])[0]
        C = find_cols(headers)
        missing = [k for k in ("caption", "link", "rx", "cm") if C[k] is None]
        if missing:
            raise RuntimeError(f"{tab}: headers not found for {missing} — column renamed? headers={headers}")
        Lc = {k: col_letter(v) for k, v in C.items() if v is not None}
        got = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID,
            "ranges": [f"{tab}!{Lc['caption']}2:{Lc['caption']}1000", f"{tab}!{Lc['link']}2:{Lc['link']}1000",
                       f"{tab}!{Lc['content']}2:{Lc['content']}1000", f"{tab}!{Lc['status']}2:{Lc['status']}1000",
                       f"{tab}!{Lc['approval']}2:{Lc['approval']}1000"]})
        vrs = got.get("valueRanges") or []
        col = lambda k: [row[0] if row else "" for row in (vrs[k].get("values") or [])]
        caps, curlink, content, status, approval = col(0), col(1), col(2), col(3), col(4)
        n = max(len(caps), len(curlink), 1)
        links, rxs, cms = [], [], []
        filled = 0
        for i in range(n):
            cap = caps[i] if i < len(caps) else ""
            old = curlink[i] if i < len(curlink) else ""
            hit = None
            if cap and len(cap.strip()) >= 10:
                for L in (40, 25):
                    hs = by_prefix.get((L, norm(cap)[:L]), [])
                    if hs:
                        hit = max(hs, key=lambda p: p.get("reactions", {}).get("summary", {}).get("total_count", 0))
                        break
            if hit:
                links.append([old or hit.get("permalink_url", "")])
                rxs.append([hit.get("reactions", {}).get("summary", {}).get("total_count", 0)])
                cms.append([hit.get("comments", {}).get("summary", {}).get("total_count", 0)])
                filled += 1
            else:
                links.append([old]); rxs.append([""]); cms.append([""])
        for letter, vals in [(Lc["link"], links), (Lc["rx"], rxs), (Lc["cm"], cms)]:
            composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": tab,
                "first_cell_location": f"{letter}2", "valueInputOption": "USER_ENTERED", "values": vals})
        if C.get("view") is not None:
            hitids = {}
            for i in range(n):
                cap = caps[i] if i < len(caps) else ""
                if cap and len(cap.strip()) >= 10:
                    for L in (40, 25):
                        hs = by_prefix.get((L, norm(cap)[:L]), [])
                        if hs:
                            hitids[i] = max(hs, key=lambda p: p.get("reactions", {}).get("summary", {}).get("total_count", 0))["id"]
                            break
            pv = fetch_views(list(hitids.values()), tok)
            vw = [[pv.get(hitids[i], "No data") if i in hitids else ""] for i in range(n)]
            composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": tab,
                "first_cell_location": f"{Lc['view']}2", "valueInputOption": "USER_ENTERED", "values": vw})
        print(f"tracker: {filled} rows matched and updated (cols {Lc})")

        # team feedback: what the robot could not do, and why
        no_caption = [i + 2 for i in range(len(content))
                      if content[i].strip() and not (caps[i].strip() if i < len(caps) else "")]
        done_unmatched = [i + 2 for i in range(len(status))
                          if status[i].strip() == "Done"
                          and not (links[i][0] if i < len(links) else "")]
        pending = [i + 2 for i in range(len(caps))
                   if caps[i].strip() and (approval[i].strip() if i < len(approval) else "") != "Complete"]
        fb1 = (f"⚠ {len(no_caption)} rows have content but NO caption — robot can't attach metrics (Rule 1)"
               f"{rowlist(no_caption)}" if no_caption else "✓ All content rows have captions")
        fb2 = (f"⚠ {len(done_unmatched)} rows marked Done but no FB post found — fix caption or paste the link (Rule 2)"
               f"{rowlist(done_unmatched)}" if done_unmatched else "✓ Every Done row is matched to a live FB post")
        fb3 = (f"◔ {len(pending)} captioned rows still waiting for brand approval"
               if pending else "✓ No approvals pending")
        composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
            "first_cell_location": "J5", "valueInputOption": "RAW",
            "values": [[f"Last check: {str(datetime.datetime.now())[:16]} — {fb3}"], [fb1], [fb2]]})
        print("feedback:", fb1, "|", fb2, "|", fb3)

        # top 5 posts of the month by reactions
        top5 = sorted(posts, key=lambda p: p.get("reactions", {}).get("summary", {}).get("total_count", 0), reverse=True)[:5]
        tv = [[i + 1, (p.get("message") or "")[:38].replace("\n", " "),
               p.get("reactions", {}).get("summary", {}).get("total_count", 0),
               p.get("comments", {}).get("summary", {}).get("total_count", 0),
               f'=HYPERLINK("{p.get("permalink_url", "")}","Open \u2197")'] for i, p in enumerate(top5)]
        composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
            "first_cell_location": "J15", "valueInputOption": "USER_ENTERED", "values": tv})

        # rebuild this month's Highlights / Entertainment tabs (full format)
        import re as _re
        def is_hl(m): return bool(_re.search(r"highlights?\s*\|", m, _re.I))
        ENT_KW = ["#drama", "#entertainment", "#pakistanidrama", "#movie", "nauroz", "nauroj",
                  "drama", "ott", "web series", "#natok", "baaghi", "maan jaao"]
        def is_ent(m): return any(k in m.lower() for k in ENT_KW) and not is_hl(m)
        def mname(m):
            r = _re.search(r"([A-Za-z][A-Za-z0-9 .'-]{2,40}\s+vs\.?\s+[A-Za-z][A-Za-z0-9 .'-]{2,40})", m)
            return r.group(1).strip() if r else ""
        capkeys = set()
        for c in caps:
            if c and len(c.strip()) >= 10:
                n = norm(c); capkeys.add(n[:40]); capkeys.add(n[:25])
        month_start = str(today.replace(day=1))
        label = today.strftime("%b %Y")
        month_posts = [p for p in posts if p["created_time"][:10] >= month_start]
        # batch views for this month's HL/Ent posts
        need = [p["id"] for p in month_posts if is_hl(p.get("message") or "") or is_ent(p.get("message") or "")]
        pviews = fetch_views(need, tok)
        preach = fetch_reach(need, tok)
        HDR = ["Date", "Match / Title", "Caption", "Link", "Views", "Reach", "Reactions", "Comments", "Shares", "Avg watch (s)", "In content calendar?"]
        for sect, pred in [(f"Highlights {label}", is_hl), (f"Entertainment {label}", is_ent)]:
            sel = sorted([p for p in month_posts if pred(p.get("message") or "")], key=lambda p: p["created_time"])
            rows_ = []
            for p in sel:
                m = p.get("message") or ""
                n = norm(m)
                v = pviews.get(p["id"]); v = v if v is not None else "No data"
                rch = preach.get(p["id"]); rch = rch if rch is not None else "No data"
                w = WATCH.get(p["id"]); w = round(w / 1000, 1) if w is not None else "No data"
                rows_.append([p["created_time"][:10], mname(m), m[:80].replace("\n", " "),
                              p.get("permalink_url", ""), v, rch,
                              p.get("reactions", {}).get("summary", {}).get("total_count", 0),
                              p.get("comments", {}).get("summary", {}).get("total_count", 0),
                              (p.get("shares") or {}).get("count", 0), w,
                              "Yes" if (n[:40] in capkeys or n[:25] in capkeys) else "NOT IN CALENDAR"])
            watches = [r[9] for r in rows_ if isinstance(r[9], (int, float))]
            tot = ["TOTAL", "", "", "", sum(r[4] for r in rows_ if isinstance(r[4], int)) or "No data",
                   sum(r[5] for r in rows_ if isinstance(r[5], int)) or "No data",
                   sum(r[6] for r in rows_), sum(r[7] for r in rows_), sum(r[8] for r in rows_),
                   round(sum(watches) / len(watches), 1) if watches else "No data",
                   f"{sum(1 for r in rows_ if r[10] == 'Yes')}/{len(rows_)} in calendar"]
            try:
                composio("GOOGLESHEETS_CLEAR_VALUES", {"spreadsheet_id": SID, "range": f"{sect}!A2:K800"})
                composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": sect,
                    "first_cell_location": "A1", "valueInputOption": "USER_ENTERED", "values": [HDR] + rows_ + [tot]})
                print(f"{sect}: {len(rows_)} rows rebuilt")
            except Exception as e:
                print(f"{sect}: SKIPPED — {str(e)[:100]} (create the tab for the new month)")

        # format performance split: video vs static, whole month
        fetch_media([p["id"] for p in month_posts], tok)
        vidp = [p for p in month_posts if MEDIA_TYPE.get(p["id"]) == "video"]
        statp = [p for p in month_posts if MEDIA_TYPE.get(p["id"]) in ("photo", "album")]
        def _avg_rx(L):
            return round(sum(p.get("reactions", {}).get("summary", {}).get("total_count", 0) for p in L) / len(L), 1) if L else 0
        av, ast_ = _avg_rx(vidp), _avg_rx(statp)
        edge = f"{round(av / ast_, 1)}x video" if ast_ else "n/a"
        composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "\U0001F4CA Dashboard",
            "first_cell_location": "P8", "valueInputOption": "USER_ENTERED", "values": [
            [f"FORMAT — {label.upper()}", ""],
            ["Video / reels", f"{len(vidp)} posts · avg {av} rx"],
            ["Static / photo", f"{len(statp)} posts · avg {ast_} rx"],
            ["Engagement edge", edge]]})
        print(f"format split: video {len(vidp)} (avg {av} rx) vs static {len(statp)} (avg {ast_} rx)")

        # TikTok: own tab + own panel, matched against the same planned captions
        update_tiktok(today, capkeys)

    # 3.5 Instagram stories log (runs every pass — stories vanish after 24h)
    try:
        update_stories()
    except Exception as e:
        print("stories log err:", str(e)[:120])

    # 3. daily KPIs -> Dashboard (skip the append if today already logged)
    log = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID, "ranges": ["📊 Dashboard!A23:A400"]})
    dates = [row[0] for row in ((log.get("valueRanges") or [{}])[0].get("values") or []) if row]
    if str(today) in dates:
        print("dashboard: today already logged")
        fill_growth(22 + len(dates)); return
    nextrow = 23 + len(dates)
    yest = today - datetime.timedelta(days=1)
    ins = graph(f"{PAGE}/insights", tok, metric="page_post_engagements,page_follows,page_video_views,page_views_total",
                period="day", since=str(yest), until=str(today))
    met = {m["name"]: (m["values"][-1].get("value") if m.get("values") else "") for m in ins.get("data", [])}
    det = graph(PAGE, tok, fields="followers_count")
    p24 = [p for p in posts if p["created_time"][:10] >= str(yest)]
    rx24 = sum(p.get("reactions", {}).get("summary", {}).get("total_count", 0) for p in p24)
    cm24 = sum(p.get("comments", {}).get("summary", {}).get("total_count", 0) for p in p24)
    sh24 = sum((p.get("shares") or {}).get("count", 0) for p in p24)
    top = max(p24, key=lambda p: p.get("reactions", {}).get("summary", {}).get("total_count", 0), default=None)
    topmsg = f"{(top.get('message') or '')[:60].strip()} ({top['reactions']['summary']['total_count']} reactions)" if top else ""
    igm = graph(f"{IG}/media", FBTOK, fields="timestamp", limit=25)
    if "error" in igm:  # expired/revoked IG token must read as missing data, not zero posts
        ig24, ig_followers = "No data — IG token expired", "No data"
        print("IG token dead:", str(igm["error"].get("message"))[:80])
    else:
        ig24 = sum(1 for m in igm.get("data", []) if (m.get("timestamp") or "") >= f"{yest}T00:00:00")
        iga = graph(IG, FBTOK, fields="followers_count")
        ig_followers = iga.get("followers_count", "No data")
    metrics = [det.get("followers_count", ""), ig_followers,
               met.get("page_post_engagements", ""), met.get("page_video_views", ""),
               met.get("page_views_total", "")]
    row = ([str(today)]
           + [v for i, m in enumerate(metrics) for v in (m, growth_cell(nextrow, i))]
           + [len(p24), topmsg])
    # reactions / comments tiles on the glance panel have no log column to INDEX from
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": "G6", "valueInputOption": "RAW", "values": [[rx24, cm24]]})
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": f"A{nextrow}", "valueInputOption": "USER_ENTERED", "values": [row]})
    print(f"dashboard: appended {today} at row {nextrow}")
    fill_growth(nextrow)

# Daily log layout: every metric is immediately followed by its own growth rate, so a
# rate always sits beside the number it measures. Metric i is at column 1+2i, its
# growth at 2+2i; date is A, top post is the last column. Keep in sync with `metrics`
# in main() — the selftest asserts the two stay the same length.
# Two other places read these columns by position and break if the order changes:
# the Dashboard glance tiles + sparklines (rows 6-7, metric columns B,D,F,H,J,L) and
# the FILTER blocks on the "📈 Charts" tab (rows 40+ / 430+) that feed its 6 charts.
LOG_PAIRS = [("FB followers", "FB Growth rate"), ("IG followers", "IG Growth rate"),
             ("FB engagements", "FB engagements growth"), ("FB video views", "FB video views growth"),
             ("FB page views", "FB page views growth")]
# Reactions / comments / shares / IG posts are NOT logged (removed 2026-09-06 as clutter —
# the glance tiles G6:H6 get today's reactions/comments written directly instead).
LOG_TAIL = ["FB posts 24h", "Top post"]
LOG_HDR = ["Date"] + [h for pair in LOG_PAIRS for h in pair] + LOG_TAIL
LOG_W = len(LOG_HDR)

def growth_cell(r, i=0):
    """Day-over-day growth of metric `i` as a real number (0.0019), so charts and the
    glance tiles can consume it. A formula, not a computed value, so a hand-corrected
    number re-derives its own rate. IFERROR blanks the cells with no base: row 23, a
    zero yesterday, or a "No data" string from a dead token.

    Display as a percent comes from the cell's number format, which the Sheets
    connector cannot set directly (GOOGLESHEETS_FORMAT_CELL is fonts/colours only).
    It was primed once by writing the literal "0.00%" to every growth column, rows
    23-1000, then clearing the values — the format survives the clear and survives
    every formula write after it. If growth ever shows as 0.0019 again, re-prime."""
    c = col_letter(1 + 2 * i)
    return f'=IFERROR(({c}{r}-{c}{r-1})/{c}{r-1},"")'

def fill_growth(lastrow):
    """Rewrite the header and every growth cell in one pass: read the logged metrics
    back, regenerate the formulas around them, write the block once. Cheaper than one
    call per column, and it repairs a rate that someone pasted over."""
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": "A22", "valueInputOption": "RAW", "values": [LOG_HDR]})
    if lastrow < 24:  # row 23 is the first day — nothing to compare against
        print("growth: header only (need 2+ logged days)"); return
    end = col_letter(LOG_W - 1)
    got = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID,
        "ranges": [f"📊 Dashboard!A23:{end}{lastrow}"]})
    out = []
    for n, raw in enumerate((got.get("valueRanges") or [{}])[0].get("values") or []):
        r, row_no = (raw + [""] * LOG_W)[:LOG_W], 23 + n
        out.append([r[0]] + [v for i in range(len(LOG_PAIRS))
                             for v in (r[1 + 2 * i], growth_cell(row_no, i) if row_no > 23 else "")]
                   + r[1 + 2 * len(LOG_PAIRS):LOG_W])
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": "A23", "valueInputOption": "USER_ENTERED", "values": out})
    print(f"growth: {len(LOG_PAIRS)} rates written for rows 24-{lastrow}")

# --- TikTok (Organic Accounts API) ---
#
# Deliberately NOT added to LOG_PAIRS. The Dashboard glance tiles and their
# sparklines INDEX the daily log by column position (B,D,F,H,J,L) and the
# "📈 Charts" FILTER blocks do the same; inserting a sixth metric pair shifts
# "FB posts 24h" from L to N and silently breaks a tile, a sparkline and a
# chart, whose formulas live in the sheet rather than in this file. TikTok gets
# its own monthly tab and its own Dashboard panel instead — same approach as
# Stories Log — so nothing that already works has to move. Migrating the log to
# six pairs is a separate job that has to rewrite those formulas too.

TT_PANEL_CELL = "S5"          # free column block; A-Q are taken by other panels
TT_HDR = ["Date", "Caption", "Link", "Views", "Reach", "Likes", "Comments",
          "Shares", "Avg watch (s)", "Watched in full", "In content calendar?"]

def _tiktok_api():
    """tiktok.py owns the credentials and the ~24h token refresh. It is
    stdlib-only so it imports on the bare CI runner this report runs on —
    importing server.py here instead would need requests + mcp and the TikTok
    section would skip on every scheduled run. Imported lazily so even a
    missing file degrades this one section rather than killing the report."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tiktok
    return tiktok

def tt_date(v):
    """create_time is a unix timestamp; tolerate an ISO string too."""
    if v in (None, ""): return ""
    if isinstance(v, (int, float)) or str(v).isdigit():
        return datetime.datetime.fromtimestamp(
            int(v), datetime.timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:10]

def tt_rows(vids, capkeys, month_start):
    rows = []
    for v in vids:
        if tt_date(v.get("create_time")) < month_start: continue
        cap = (v.get("caption") or "").replace("\n", " ")
        n = norm(cap)
        w = v.get("average_time_watched")
        rate = v.get("full_video_watched_rate")
        rows.append([
            tt_date(v.get("create_time")), cap[:80], v.get("share_url", ""),
            v.get("video_views", "No data"), v.get("reach", "No data"),
            v.get("likes", 0), v.get("comments", 0), v.get("shares", 0),
            round(float(w) / 1000, 1) if w not in (None, "") else "No data",
            f"{round(float(rate) * 100, 1)}%" if rate not in (None, "") else "No data",
            "Yes" if (n[:40] in capkeys or n[:25] in capkeys) else "NOT IN CALENDAR"])
    return sorted(rows, key=lambda r: r[0])

def update_tiktok(today, capkeys):
    """Rebuild this month's TikTok tab and refresh the Dashboard TikTok panel.
    Writes an explicit 'not connected' note rather than zeros when the account
    has no credentials yet — a zero here would read as 'TikTok posted nothing'."""
    label = today.strftime("%b %Y")
    sect = f"TikTok {label}"
    try:
        tt = _tiktok_api()
        acct = tt.account() or {}
        vids = tt.all_videos()
    except Exception as e:
        note = str(e).split("\n")[0][:150]
        composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "\U0001F4CA Dashboard",
            "first_cell_location": TT_PANEL_CELL, "valueInputOption": "RAW", "values": [
            [f"TIKTOK — {label.upper()}", ""],
            ["Status", "No data — TikTok not connected"],
            ["Fix", "See TIKTOK_SETUP.md (one-time app + authorize)"],
            ["Last error", note]]})
        print("tiktok: SKIPPED —", note)
        return

    rows = tt_rows(vids, capkeys, str(today.replace(day=1)))
    num = lambda i: sum(r[i] for r in rows if isinstance(r[i], (int, float)))
    tot = ["TOTAL", "", "", num(3) or "No data", num(4) or "No data", num(5),
           num(6), num(7), "", "",
           f"{sum(1 for r in rows if r[10] == 'Yes')}/{len(rows)} in calendar"]
    try:
        ensure_tab(sect, TT_HDR)
        composio("GOOGLESHEETS_CLEAR_VALUES", {"spreadsheet_id": SID, "range": f"{sect}!A2:K800"})
        composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": sect,
            "first_cell_location": "A1", "valueInputOption": "USER_ENTERED",
            "values": [TT_HDR] + rows + [tot]})
        print(f"{sect}: {len(rows)} rows rebuilt")
    except Exception as e:
        print(f"{sect}: tab write failed — {str(e)[:100]}")

    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "\U0001F4CA Dashboard",
        "first_cell_location": TT_PANEL_CELL, "valueInputOption": "USER_ENTERED", "values": [
        [f"TIKTOK — {label.upper()}", ""],
        ["Followers", acct.get("followers_count", "No data")],
        ["Profile views", acct.get("profile_views", "No data")],
        ["Posts this month", len(rows)],
        ["Views / likes", f"{num(3)} / {num(5)}"],
        ["In calendar", tot[10]]]})
    print(f"tiktok: {acct.get('followers_count','?')} followers, {len(rows)} posts this month")


STORIES_TAB = "Stories Log"

def ensure_tab(title, header):
    d = composio("GOOGLESHEETS_GET_SHEET_NAMES", {"spreadsheet_id": SID})
    names = (d.get("response_data") or d).get("sheet_names") or []
    if title in names: return
    r = composio("GOOGLESHEETS_ADD_SHEET", {"spreadsheet_id": SID})
    sid_ = r["replies"][0]["addSheet"]["sheetId"]
    composio("GOOGLESHEETS_UPDATE_SHEET_PROPERTIES", {"spreadsheetId": SID,
        "updateSheetProperties": {"properties": {"sheetId": sid_, "title": title}, "fields": "title"}})
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": title,
        "first_cell_location": "A1", "valueInputOption": "USER_ENTERED", "values": [header]})

def update_stories():
    """Log IG stories before they expire (24h); refresh metrics while active."""
    ensure_tab(STORIES_TAB, ["Date", "Platform", "Type", "Posted (UTC)", "Story ID",
                             "Views", "Reach", "Replies", "Interactions", "Note"])
    r = graph(f"{IG}/stories", FBTOK, fields="id,media_type,timestamp")
    if "error" in r:
        print("stories: IG token issue —", str(r["error"].get("message"))[:60]); return
    active = r.get("data", [])
    got = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID, "ranges": [f"{STORIES_TAB}!E2:E1000"]})
    ids = [(row[0] if row else "") for row in ((got.get("valueRanges") or [{}])[0].get("values") or [])]
    rowof = {v: i + 2 for i, v in enumerate(ids) if v}
    nextrow = 2 + len(ids)
    for st in active:
        met = {"views": "", "reach": "", "replies": "", "total_interactions": ""}
        note = ""
        ins = graph(f"{st['id']}/insights", FBTOK, metric="views,reach,replies,total_interactions")
        if "data" in ins:
            for m in ins["data"]:
                if m.get("values"): met[m["name"]] = m["values"][0].get("value")
        else:
            emsg = str(ins.get("error", {}).get("message", ""))
            note = "Low viewers" if "Not enough viewers" in emsg else emsg[:40]
        vals = [st["timestamp"][:10], "Instagram", st.get("media_type", ""), st.get("timestamp", "")[:16],
                st["id"], met["views"], met["reach"], met["replies"], met["total_interactions"], note]
        target = rowof.get(st["id"])
        if target:  # refresh metrics on existing row
            composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": STORIES_TAB,
                "first_cell_location": f"F{target}", "valueInputOption": "USER_ENTERED",
                "values": [[met["views"], met["reach"], met["replies"], met["total_interactions"], note]]})
        else:
            composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": STORIES_TAB,
                "first_cell_location": f"A{nextrow}", "valueInputOption": "USER_ENTERED", "values": [vals]})
            rowof[st["id"]] = nextrow; nextrow += 1
    print(f"stories: {len(active)} active logged/refreshed")

def stamp():
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": "D2", "valueInputOption": "RAW",
        "values": [[str(datetime.datetime.now())[:16]]]})

def selftest():
    """python daily_report.py --selftest — no network, no sheet writes."""
    sept = ["Day","Date","Category","Content Type","Match Details","Graphic Asset","Caption",
            "Suggested Caption from Brand ","Posting Time","StatusSocial Media\nPosting Status",
            "Design By","Approval Status","BRAND Approval","News Link","Planning",
            "Fb Post Link","View","Reaction","Comment"]
    C = find_cols(sept)
    assert col_letter(C["link"]) == "P" and col_letter(C["view"]) == "Q", C
    assert col_letter(C["rx"]) == "R" and col_letter(C["cm"]) == "S", C
    assert col_letter(C["caption"]) == "G" and col_letter(C["approval"]) == "L", C
    octh = ["Day","Date","Posting Time","Asset Required","Match Details","Category",
            "Approval Status","Done By","Link","Caption","Facebook","Instagram","View","Reaction","Comment"]
    C2 = find_cols(octh)
    assert col_letter(C2["link"]) == "K" and col_letter(C2["caption"]) == "J", C2
    assert find_cols(["Day","Date"])["link"] is None
    assert growth_cell(25, 0) == '=IFERROR((B25-B24)/B24,"")', growth_cell(25, 0)
    assert growth_cell(25, 1).startswith("=IFERROR((D25-D24)"), growth_cell(25, 1)  # IG followers
    assert growth_cell(25, 4).startswith("=IFERROR((J25-J24)"), growth_cell(25, 4)  # last paired metric
    assert rowlist([]) == "" and rowlist([4, 9]) == " [row 4, 9]", rowlist([4, 9])
    assert rowlist(list(range(2, 12))) == " [row 2, 3, 4, 5, 6, 7, 8, 9 +2 more]", rowlist(list(range(2, 12)))
    # TikTok rows must line up with their header, and the calendar-match column
    # must stay last — the TOTAL row and the panel both read row[10].
    assert len(TT_HDR) == 11 and TT_HDR[10] == "In content calendar?", TT_HDR
    assert tt_date(1788998400) == "2026-09-10", tt_date(1788998400)
    assert tt_date("2026-09-10T06:57:24+0000") == "2026-09-10" and tt_date(None) == ""
    _r = tt_rows([{"create_time": 1788998400, "caption": "Shanaka owns the stage",
                   "video_views": 10, "likes": 2, "average_time_watched": 3500,
                   "full_video_watched_rate": 0.42},
                  {"create_time": 1787184000, "caption": "last month, must drop"}],
                 {norm("Shanaka owns the stage")[:40]}, "2026-09-01")
    assert len(_r) == 1, _r                       # the pre-month post is excluded
    assert _r[0][10] == "Yes", _r                 # planned caption matches the tracker
    assert _r[0][8] == 3.5 and _r[0][9] == "42.0%", _r
    assert tt_rows([{"create_time": 1788998400, "caption": "ad hoc"}], set(), "2026-09-01")[0][10] \
        == "NOT IN CALENDAR"
    assert LOG_HDR[:3] == ["Date", "FB followers", "FB Growth rate"], LOG_HDR
    assert LOG_HDR[-2:] == ["FB posts 24h", "Top post"] and LOG_W == 13, LOG_HDR
    # every metric header must sit one column left of its own growth header
    for i, (m, g) in enumerate(LOG_PAIRS):
        assert LOG_HDR[1 + 2 * i] == m and LOG_HDR[2 + 2 * i] == g, (i, LOG_HDR)
    print("selftest OK")

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest(); sys.exit(0)
    print(f"=== tapmad daily report {datetime.datetime.now().isoformat()} ===")
    main()
    stamp()
