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
    d = composio("FACEBOOK_GET_USER_PAGES", {})
    for p in (d.get("response_data") or d).get("data", []):
        if p["id"] == PAGE and p.get("access_token"):
            return p["access_token"]
    return FBTOK

def graph(path, tok, **params):
    params["access_token"] = tok
    url = f"https://graph.facebook.com/v22.0/{path}?" + urllib.parse.urlencode(params)
    try:
        return json.load(urllib.request.urlopen(url, timeout=60))
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip().lower()

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
        r = json.load(urllib.request.urlopen(nxt, timeout=60))
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
        no_caption = sum(1 for i in range(len(content))
                         if content[i].strip() and not (caps[i].strip() if i < len(caps) else ""))
        done_unmatched = sum(1 for i in range(len(status))
                             if status[i].strip() == "Done"
                             and not (links[i][0] if i < len(links) else ""))
        pending = sum(1 for i in range(len(caps))
                      if caps[i].strip() and (approval[i].strip() if i < len(approval) else "") != "Complete")
        fb1 = (f"⚠ {no_caption} rows have content but NO caption — robot can't attach metrics (Rule 1)"
               if no_caption else "✓ All content rows have captions")
        fb2 = (f"⚠ {done_unmatched} rows marked Done but no FB post found — fix caption or paste the link (Rule 2)"
               if done_unmatched else "✓ Every Done row is matched to a live FB post")
        fb3 = f"◔ {pending} captioned rows still waiting for brand approval" if pending else "✓ No approvals pending"
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
        HDR = ["Date", "Match / Title", "Caption", "Link", "Views", "Reach", "Reactions", "Comments", "Shares", "In content calendar?"]
        for sect, pred in [(f"Highlights {label}", is_hl), (f"Entertainment {label}", is_ent)]:
            sel = sorted([p for p in month_posts if pred(p.get("message") or "")], key=lambda p: p["created_time"])
            rows_ = []
            for p in sel:
                m = p.get("message") or ""
                n = norm(m)
                v = pviews.get(p["id"]); v = v if v is not None else "No data"
                rows_.append([p["created_time"][:10], mname(m), m[:80].replace("\n", " "),
                              p.get("permalink_url", ""), v, "No data",
                              p.get("reactions", {}).get("summary", {}).get("total_count", 0),
                              p.get("comments", {}).get("summary", {}).get("total_count", 0),
                              (p.get("shares") or {}).get("count", 0),
                              "Yes" if (n[:40] in capkeys or n[:25] in capkeys) else "NOT IN CALENDAR"])
            tot = ["TOTAL", "", "", "", sum(r[4] for r in rows_ if isinstance(r[4], int)) or "No data", "No data",
                   sum(r[6] for r in rows_), sum(r[7] for r in rows_), sum(r[8] for r in rows_),
                   f"{sum(1 for r in rows_ if r[9] == 'Yes')}/{len(rows_)} in calendar"]
            try:
                composio("GOOGLESHEETS_CLEAR_VALUES", {"spreadsheet_id": SID, "range": f"{sect}!A2:J800"})
                composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": sect,
                    "first_cell_location": "A1", "valueInputOption": "USER_ENTERED", "values": [HDR] + rows_ + [tot]})
                print(f"{sect}: {len(rows_)} rows rebuilt")
            except Exception as e:
                print(f"{sect}: SKIPPED — {str(e)[:100]} (create the tab for the new month)")

    # 3. daily KPIs -> Dashboard (skip if today already logged)
    log = composio("GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": SID, "ranges": ["📊 Dashboard!A23:A400"]})
    dates = [row[0] for row in ((log.get("valueRanges") or [{}])[0].get("values") or []) if row]
    if str(today) in dates:
        print("dashboard: today already logged"); return
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
    row = [str(today), det.get("followers_count", ""), ig_followers,
           met.get("page_post_engagements", ""), met.get("page_video_views", ""), met.get("page_views_total", ""),
           len(p24), rx24, cm24, sh24, ig24, topmsg]
    composio("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": SID, "sheet_name": "📊 Dashboard",
        "first_cell_location": f"A{nextrow}", "valueInputOption": "USER_ENTERED", "values": [row]})
    print(f"dashboard: appended {today} at row {nextrow}")

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
    print("selftest OK")

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest(); sys.exit(0)
    print(f"=== tapmad daily report {datetime.datetime.now().isoformat()} ===")
    main()
    stamp()
