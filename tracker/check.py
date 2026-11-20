"""Checker (runs every ~10 min): pulls every board in boards.json plus the SimplifyJobs aggregator
feed, keeps US-based, undergrad-eligible hardware intern/co-op postings, diffs against
data/postings.json, and posts new ones to Discord.

Filters:
  * US only: a posting needs at least one US location (multi-country postings are kept if one site
    is in the US). Workday searches use the board's own "United States" facet.
  * Undergrad only: titles naming PhD/Master's/MBA/graduate/new-grad are dropped, and the job
    description (or Simplify's degree field) is checked once per posting for grad-only language.

Env: DISCORD_WEBHOOK_URL (optional), SEND_TEST, SITE_URL, GITHUB_OUTPUT (set by Actions).
"""
import html, json, os, re, subprocess, sys, time, urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARDS = json.loads((ROOT / "tracker/boards.json").read_text())
STATE = ROOT / "data/postings.json"
UA = {"User-Agent": "internship-tracker/1.0 (+github actions)"}
SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
SOURCES = BOARDS + [{"company": "SimplifyJobs feed", "ats": "simplify"}]

# --- title filters -----------------------------------------------------------------------------
INTERN = re.compile(r"\b(intern|interns|internship|internships|co-?op|co-?ops)\b", re.I)
ROLE = re.compile(r"(rtl|asic|fpga|verification|\bdv\b|silicon|validation|hardware|digital|chip|\bsoc\b|"
                  r"emulation|logic design|ic design|circuit|test engineer|electrical|firmware|post-?si|bring-?up|dft)", re.I)
EXCLUDE = re.compile(r"(sales|finance|marketing|legal|hr intern|recruit|accounting|tax|procurement|supply chain|"
                     r"facilities|mechanical|quant|\bsenior\b|\bstaff\b|principal|manager)", re.I)
GRAD_TITLE = re.compile(r"(ph\.?\s?d|doctoral|post-?doc|master'?s|\bm\.?s\.?\b(?! office)|\bmba\b|\bgraduate\b|"
                        r"\bgrad\b|new college grad|\bncg\b|full[- ]time)", re.I)

# --- location ----------------------------------------------------------------------------------
STATE_NAMES = ("Alabama,Alaska,Arizona,Arkansas,California,Colorado,Connecticut,Delaware,Florida,Georgia,Hawaii,"
               "Idaho,Illinois,Indiana,Iowa,Kansas,Kentucky,Louisiana,Maine,Maryland,Massachusetts,Michigan,"
               "Minnesota,Mississippi,Missouri,Montana,Nebraska,Nevada,New Hampshire,New Jersey,New Mexico,New York,"
               "North Carolina,North Dakota,Ohio,Oklahoma,Oregon,Pennsylvania,Rhode Island,South Carolina,"
               "South Dakota,Tennessee,Texas,Utah,Vermont,Virginia,Washington,West Virginia,Wisconsin,Wyoming").split(",")
ABBR = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
        "OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_POS = re.compile(
    r"(united states|\bu\.?s\.?a\.?\b|\bu\.s\.|^us\b|\bus[-, ]|\bus$|remote.{0,10}\bus\b|"
    r"\b(" + "|".join(map(re.escape, STATE_NAMES)) + r")\b|"
    r"(,|-|\s)\s?(" + "|".join(ABBR) + r")\b(?!-)|"
    r"\b(sf|nyc|la|san francisco|new york|san jose|san diego|los angeles|boston|seattle|austin|chicago|"
    r"santa clara|sunnyvale|mountain view|palo alto|cupertino|milpitas|redmond|hillsboro|folsom|"
    r"bay area|silicon valley|pittsburgh|atlanta|denver|boulder|houston|dallas|phoenix|chandler|"
    r"tempe|portland|raleigh|durham|cambridge, ma|somerville|burlingame|irvine|san mateo)\b)", re.I)
NON_US = re.compile(r"(china|shanghai|beijing|shenzhen|taiwan|taipei|hsinchu|israel|tel aviv|petah|yokneam|haifa|"
                    r"india|bangalore|bengaluru|hyderabad|pune|noida|malaysia|penang|kulim|poland|gdansk|krakow|"
                    r"philippines|thailand|mexico|guadalajara|japan|tokyo|kanagawa|singapore|vietnam|ireland|cork|"
                    r"limerick|dublin|spain|valencia|italy|milan|florence|germany|munich|berlin|dresden|"
                    r"united kingdom|\buk\b|england|scotland|london|bristol|cambridge, uk|edinburgh|"
                    r"korea|seoul|canada|ontario|quebec|british columbia|toronto|ottawa|montreal|markham|"
                    r"waterloo|vancouver|kanata|australia|brisbane|sydney|new zealand|auckland|switzerland|"
                    r"zurich|adliswil|netherlands|amsterdam|eindhoven|denmark|aarhus|france|paris|rennes|"
                    r"sophia|grenoble|argentina|cordoba|hungary|budapest|norway|oslo|brazil|belo horizonte|"
                    r"hong kong|gurgaon|gift city|romania|czech|austria|belgium|sweden|finland|egypt|"
                    r", (on|qc|bc|ab)\b)", re.I)


def us_status(loc):
    """True = has a US site, False = only non-US sites, None = can't tell."""
    parts = [p.strip() for p in re.split(r"[;|\n]| or |/(?=\s*[A-Z])", loc or "") if p.strip()]
    if any(US_POS.search(p) and not NON_US.search(p) for p in parts):
        return True
    if any(NON_US.search(p) for p in parts):
        return False
    return None


# --- degree level --------------------------------------------------------------------------------
GRAD = re.compile(r"(ph\.?\s?d|doctoral|doctorate|master'?s|\bm\.\s?s\.?\b|\bms\b(?=\s*(in|degree|or|/|,|student|program|candidate))|\bmsee\b|graduate (student|degree|"
                  r"program|level|school)|\bmba\b)", re.I)
UG = re.compile(r"(bachelor|undergrad|\bb\.\s?s\.?\b|\bbs\b|\bbsee\b|\bb\.?\s?eng|\bbsc\b|\bb\.?a\.?sc|"
                r"sophomore|junior|freshman|rising senior|senior year|associate'?s)", re.I)
GRAD_PURSUIT = re.compile(r"(pursuing|enrolled in|working towards?|candidate for)[^.;\n]{0,40}"
                          r"(ph\.?\s?d|doctora|master|\bm\.\s?s\b|\bms\b|graduate degree)", re.I)
UG_PURSUIT = re.compile(r"(pursuing|enrolled in|working towards?|candidate for)[^.;\n]{0,60}"
                        r"(bachelor|undergrad|\bb\.\s?s\b|\bbs\b|bsee|b\.?\s?eng)", re.I)


def grad_only(text):
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(text or ""))))
    if not t.strip():
        return False
    if GRAD_PURSUIT.search(t) and not UG_PURSUIT.search(t):
        return True
    return bool(GRAD.search(t)) and not UG.search(t)


# --- fetching ------------------------------------------------------------------------------------
def http_json(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={**UA, **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def curl_json(url, body=None):
    """Workday rate-limits Python's TLS client, so shell out to curl."""
    cmd = ["curl", "-s", "--fail", "--max-time", "60", "-H", "Accept: application/json", "-A", "Mozilla/5.0"]
    if body is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(body)]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"curl exit {r.returncode} for {url}")
    return json.loads(r.stdout)


def job(title, url, loc, desc=None, **kw):
    return {"title": title, "url": url, "location": loc or "", "desc": desc, **kw}


def workday_facets(facets):
    """Find this tenant's 'United States' location facet value, if it has one."""
    us = {}

    def walk(fs, param=None):
        for f in fs:
            if "values" in f:
                walk(f["values"], f.get("facetParameter") or param)
            elif param:
                d = f.get("descriptor", "")
                if re.fullmatch(r"(united states( of america)?|usa|us)", d.strip(), re.I) and param != "locations":
                    us.setdefault(param, []).append(f["id"])
    walk(facets)
    return us


def fetch_workday(b):
    base = f"https://{b['host']}/wday/cxs/{b['tenant']}/{b['site']}"
    first = curl_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"})
    # Job-type facets are unreliable (Intel files interns as "Full time"), so search text + US facet only.
    us = workday_facets(first.get("facets", []))
    out = {}
    for q in ["intern", "co-op"]:
        dry = 0
        for off in range(0, 1000, 20):
            d = curl_json(f"{base}/jobs", {"appliedFacets": us, "limit": 20, "offset": off, "searchText": q})
            posts = [j for j in d.get("jobPostings", []) if "title" in j]
            for j in posts:
                url = f"https://{b['host']}/{b['site']}{j['externalPath']}"
                # with the US facet applied every result has a US site; otherwise resolve later from the path
                loc = j.get("locationsText", "")
                if us and us_status(loc) is not True:
                    loc = f"{loc} (US)" if loc else "United States"
                out[url] = job(j["title"], url, loc, detail=f"{base}{j['externalPath']}")
            time.sleep(0.7)
            # results are relevance-ranked and some tenants return every job, so stop after 3 pages with no interns
            dry = 0 if any(INTERN.search(j["title"]) for j in posts) else dry + 1
            if len(posts) < 20 or off + 20 >= d.get("total", 0) or dry >= 3:
                break
    return list(out.values())


def simplify_ok(x):
    if not (x.get("active") and x.get("is_visible", True)):
        return False
    terms = x.get("terms") or []
    yrs = [int(y) for t in terms for y in re.findall(r"20\d\d", t)]
    if yrs and max(yrs) < 2027:  # past terms
        return False
    degrees = x.get("degrees") or []
    if degrees and not any(re.search(r"bachelor|associate", d, re.I) for d in degrees):
        return False
    return True


def fetch(b):
    a = b["ats"]
    if a == "greenhouse":
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs", timeout=120)
        return [job(j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""),
                    detail=f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs/{j['id']}") for j in d["jobs"]]
    if a == "lever":
        d = http_json(f"https://api.lever.co/v0/postings/{b['slug']}?mode=json")
        return [job(j["text"], j["hostedUrl"], "; ".join(filter(None, [j.get("categories", {}).get("location", ""),
                    *j.get("categories", {}).get("allLocations", []), j.get("country", "")])),
                    desc=j.get("descriptionPlain", "") + " ".join(l.get("content", "") for l in j.get("lists", [])))
                for j in d]
    if a == "ashby":
        d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}")
        return [job(j["title"], j["jobUrl"], "; ".join(filter(None, [j.get("location", ""),
                    *[s.get("location", "") for s in j.get("secondaryLocations", [])],
                    ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")])),
                    desc=j.get("descriptionPlain", "")) for j in d["jobs"]]
    if a == "workday":
        return fetch_workday(b)
    if a == "simplify":
        d = http_json(SIMPLIFY, timeout=120)
        return [job(x["title"], x["url"], "; ".join(x.get("locations", [])), desc="",
                    company=x["company_name"].strip(), terms=x.get("terms", []))
                for x in d if x.get("category") == "Hardware" and simplify_ok(x)]
    raise ValueError(a)


def fetch_desc(j):
    """Full description for a candidate posting (only called once per new posting)."""
    if j.get("desc") is not None or not j.get("detail"):
        return j.get("desc") or "", None
    if "myworkdayjobs" in j["detail"]:
        info = curl_json(j["detail"]).get("jobPostingInfo", {})
        locs = "; ".join([info.get("location", ""), *info.get("additionalLocations", []),
                          (info.get("country") or {}).get("descriptor", "")])
        return info.get("jobDescription", ""), locs
    return http_json(j["detail"]).get("content", ""), None


def title_ok(title):
    return bool(INTERN.search(title) and ROLE.search(title) and not EXCLUDE.search(title) and not GRAD_TITLE.search(title))


def season(title, terms=()):
    t = (title + " " + " ".join(terms)).lower()
    for s in ("winter", "spring", "fall"):
        if s in t and "summer" not in t:
            return s.capitalize()
    return "Summer"


CATS = [("Design verification", r"verif|\bdv\b"), ("FPGA", r"fpga"),
        ("Validation / test", r"validat|test|bring-?up|post-?si|reliab|evaluation"),
        ("Digital / ASIC design", r"rtl|asic|digital|ic design|silicon|chip|soc|dft|circuit|logic"),
        ("Embedded / firmware", r"firmware|embedded"), ("Electrical / hardware", r".")]


def category(title):
    return next(c for c, p in CATS if re.search(p, title, re.I))


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def discord(msg_embeds):
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        return
    for i in range(0, len(msg_embeds), 10):  # Discord allows up to 10 embeds per message
        body = json.dumps({"username": "Internship Tracker", "embeds": msg_embeds[i:i + 10]}).encode()
        req = urllib.request.Request(hook, data=body, headers={**UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        time.sleep(1)


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = json.loads(STATE.read_text()) if STATE.exists() else {"postings": {}, "boards": {}}
    state.setdefault("rejected", {})  # url -> reason, so descriptions are only fetched once
    first_run = not state["postings"]
    known_boards = {k for k, v in state["boards"].items() if v.get("ok")}
    seen, rejected = state["postings"], state["rejected"]
    prev_open = {u for u, p in seen.items() if p.get("status") == "open"}
    prev_errors = state.get("errors", [])
    live, errors = {}, []

    def run(b):
        try:
            return b, fetch(b), None
        except Exception as e:
            return b, None, f"{type(e).__name__}: {e}"[:160]

    wd = [b for b in SOURCES if b["ats"] == "workday"]
    other = [b for b in SOURCES if b["ats"] != "workday"]
    with cf.ThreadPoolExecutor(12) as ex, cf.ThreadPoolExecutor(4) as wd_ex:  # Workday 429s if pushed harder
        wd_results = wd_ex.map(run, wd)
        results = list(ex.map(run, other)) + list(wd_results)

    # Candidates that pass the title filter; descriptions are checked only for postings not yet decided.
    cands, ok_sources = [], set()
    for b, jobs, err in results:
        state["boards"][b["company"]] = {"ok": err is None, "error": err, "checked": now}
        if err:
            errors.append(f"{b['company']}: {err}")
            continue
        ok_sources.add(b["company"])
        for j in jobs:
            if title_ok(j["title"]):
                j.setdefault("company", b["company"])
                j["source"] = b["company"]
                cands.append(j)

    def decide(j):
        url = j["url"]
        if url in seen and seen[url].get("status") == "open" and seen[url].get("verified"):
            return j, None
        if url in rejected:
            return j, rejected[url]
        us = us_status(j["location"])
        if us is False:
            return j, "non-US"
        try:
            desc, locs = fetch_desc(j)
        except Exception as e:
            return j, f"retry:{type(e).__name__}"
        if us is None and locs:
            us = us_status(locs)
            j["location"] = locs.strip("; ") if us else j["location"]
        if us is not True:
            return j, "non-US" if us is False else "location unknown"
        if grad_only(desc):
            return j, "grad-only"
        return j, None

    with cf.ThreadPoolExecutor(8) as ex:
        decided = list(ex.map(decide, [j for j in cands if "myworkdayjobs" not in j["url"]]))
    with cf.ThreadPoolExecutor(3) as ex:
        decided += list(ex.map(decide, [j for j in cands if "myworkdayjobs" in j["url"]]))

    board_titles = set()
    for j, why in decided:
        if why:
            if not why.startswith("retry"):
                rejected[j["url"]] = why
            continue
        if j["source"] != "SimplifyJobs feed":
            board_titles.add((norm(j["company"])[:5], norm(j["title"])))
    for j, why in decided:
        if why:
            continue
        if j["source"] == "SimplifyJobs feed" and (norm(j["company"])[:5], norm(j["title"])) in board_titles:
            continue  # same posting already found on the company's own board
        live[j["url"]] = {"company": j["company"], "title": j["title"].strip(), "location": j["location"],
                          "url": j["url"], "category": category(j["title"]), "source": j["source"], "verified": True,
                          "season": season(j["title"], j.get("terms", ()))}

    new = []
    for url, p in live.items():
        if url in seen and seen[url].get("status") == "open":
            seen[url].update(p, last_seen=now)
        else:
            p.update(first_seen=seen.get(url, {}).get("first_seen", now), last_seen=now, status="open")
            seen[url] = p
            if p["source"] in known_boards:  # a newly added source is seeded quietly
                new.append(p)
    closed = []
    for url, p in list(seen.items()):
        if url in live or p.get("status") != "open" or p.get("source", p["company"]) not in ok_sources:
            continue
        if url in rejected or not title_ok(p["title"]) or not p.get("verified") or us_status(p["location"]) is False:
            # failed or predates the current filters (e.g. dropped by the US facet): remove rather than "close"
            del seen[url]
            continue
        p.update(status="closed", closed_at=now)
        closed.append(p)
    # keep the rejection cache from growing forever: forget urls that no board lists anymore
    listed = {j["url"] for _, jobs, _ in results if jobs for j in jobs}
    state["rejected"] = {u: r for u, r in rejected.items() if u in listed}

    state.update(last_checked=now, errors=errors)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    n_open = sum(p["status"] == "open" for p in seen.values())
    print(f"{now}: {n_open} open, {len(new)} new, {len(closed)} closed, {len(errors)} board errors")
    for e in errors:
        print("  ERR", e)

    changed = bool(new or closed or {u for u, p in seen.items() if p["status"] == "open"} != prev_open
                   or errors != prev_errors)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")

    site = os.environ.get("SITE_URL", "")
    if first_run or os.environ.get("SEND_TEST") == "true":
        discord([{"title": "Internship tracker is live" if first_run else "Internship tracker test message",
                  "description": f"Tracking **{n_open}** US undergrad hardware intern/co-op postings across "
                                 f"{len(ok_sources)} sources. Checking every 10 minutes; "
                                 f"new postings will show up here.\n{site}", "color": 0x2F855A}])
    elif new:
        discord([{"title": f"{p['company']}: {p['title']}"[:256], "url": p["url"],
                  "description": f"{p['category']} · {p['location'][:150] or 'location n/a'}",
                  "color": 0x3182CE} for p in new])


if __name__ == "__main__":
    sys.exit(main())
