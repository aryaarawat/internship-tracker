"""Hourly checker: pulls every board in boards.json, keeps US hardware intern/co-op
postings, diffs against data/postings.json, and posts new ones to Discord.

Env: DISCORD_WEBHOOK_URL (optional; if unset, nothing is sent).
"""
import json, os, re, subprocess, sys, time, urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARDS = json.loads((ROOT / "tracker/boards.json").read_text())
STATE = ROOT / "data/postings.json"
UA = {"User-Agent": "internship-tracker/1.0 (+github actions)"}

INTERN = re.compile(r"\b(intern\w*|co-?op|student)\b", re.I)
ROLE = re.compile(r"(rtl|asic|fpga|verification|\bdv\b|silicon|validation|hardware|digital|chip|\bsoc\b|"
                  r"emulation|logic design|ic design|circuit|test engineer|electrical|firmware|post-?si|bring-?up|dft)", re.I)
EXCLUDE = re.compile(r"(ph\.?d|\bms\b|master|graduate intern|mba|sales|finance|marketing|legal|hr intern|"
                     r"recruit|accounting|tax|procurement|supply chain|facilities|mechanical|quant)", re.I)
NON_US = re.compile(r"(china|shanghai|beijing|taiwan|taipei|israel|tel aviv|petah|yokneam|india|bangalore|"
                    r"hyderabad|malaysia|penang|kulim|poland|gdansk|philippines|thailand|mexico|japan|tokyo|"
                    r"kanagawa|singapore|vietnam|ireland|cork|limerick|spain|valencia|italy|milan|florence|"
                    r"germany|munich|berlin|united kingdom|\buk\b|london|bristol|cambridge, uk|edinburgh|"
                    r"korea|seoul|canada|toronto|ottawa|montreal|australia|brisbane|sydney|new zealand|auckland|"
                    r"switzerland|zurich|adliswil|netherlands|amsterdam|denmark|aarhus|france|paris|rennes|"
                    r"argentina|cordoba|hungary|budapest|norway|oslo|brazil|belo horizonte|hong kong|gurgaon|gift city)", re.I)
CATS = [("Design verification", r"verif|\bdv\b"), ("FPGA", r"fpga"),
        ("Validation / test", r"validat|test|bring-?up|post-?si|reliab|evaluation"),
        ("Digital / ASIC design", r"rtl|asic|digital|ic design|silicon|chip|soc|dft|circuit|logic"),
        ("Embedded / firmware", r"firmware|embedded"), ("Electrical / hardware", r".")]


def http_json(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={**UA, **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch(b):
    a = b["ats"]
    if a == "greenhouse":
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs", timeout=120)
        return [(j["title"], j["absolute_url"], (j.get("location") or {}).get("name", "")) for j in d["jobs"]]
    if a == "lever":
        d = http_json(f"https://api.lever.co/v0/postings/{b['slug']}?mode=json")
        return [(j["text"], j["hostedUrl"], j.get("categories", {}).get("location", "")) for j in d]
    if a == "ashby":
        d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}")
        return [(j["title"], j["jobUrl"], j.get("location", "")) for j in d["jobs"]]
    if a == "workday":  # Workday rate-limits Python's TLS client, so shell out to curl
        out = []
        url = f"https://{b['host']}/wday/cxs/{b['tenant']}/{b['site']}/jobs"
        for q in ["intern", "co-op", "intern verification", "intern FPGA", "intern design", "intern validation"]:
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
            r = subprocess.run(["curl", "-s", "--max-time", "60", "-X", "POST", "-H", "Content-Type: application/json",
                                "-H", "Accept: application/json", "-A", "Mozilla/5.0", "-d", body, url],
                               capture_output=True, text=True)
            d = json.loads(r.stdout)
            out += [(j["title"], f"https://{b['host']}/{b['site']}{j['externalPath']}", j.get("locationsText", ""))
                    for j in d.get("jobPostings", []) if "title" in j]
            time.sleep(1.5)
        return out
    raise ValueError(a)


def relevant(title, loc):
    return (INTERN.search(title) and ROLE.search(title) and not EXCLUDE.search(title)
            and not NON_US.search(f"{title} {loc}"))


def season(title):
    t = title.lower()
    for s in ("winter", "spring", "fall"):
        if s in t and "summer" not in t:
            return s.capitalize()
    return "Summer"


def category(title):
    return next(c for c, p in CATS if re.search(p, title, re.I))


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
    first_run = not state["postings"]
    seen = state["postings"]
    live, errors = {}, []

    def run(b):
        try:
            return b, fetch(b), None
        except Exception as e:
            return b, None, str(e)[:120]

    wd = [b for b in BOARDS if b["ats"] == "workday"]
    other = [b for b in BOARDS if b["ats"] != "workday"]
    with cf.ThreadPoolExecutor(12) as ex:
        results = list(ex.map(run, other))
    results += [run(b) for b in wd]  # sequential to avoid Workday 429s

    ok_companies = set()
    for b, jobs, err in results:
        state["boards"][b["company"]] = {"ok": err is None, "error": err, "checked": now}
        if err:
            errors.append(f"{b['company']}: {err}")
            continue
        ok_companies.add(b["company"])
        for title, url, loc in jobs:
            if relevant(title, loc):
                live[url] = {"company": b["company"], "title": title.strip(), "location": loc, "url": url,
                             "category": category(title), "season": season(title)}

    new = []
    for url, p in live.items():
        if url in seen and seen[url].get("status") == "open":
            seen[url].update(p, last_seen=now)
        else:
            p.update(first_seen=seen.get(url, {}).get("first_seen", now), last_seen=now, status="open")
            seen[url] = p
            new.append(p)
    closed = []
    for url, p in seen.items():  # only close if that company's board fetched fine this run
        if p.get("status") == "open" and url not in live and p["company"] in ok_companies:
            p.update(status="closed", closed_at=now)
            closed.append(p)

    state.update(last_checked=now, errors=errors)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    n_open = sum(p["status"] == "open" for p in seen.values())
    print(f"{now}: {n_open} open, {len(new)} new, {len(closed)} closed, {len(errors)} board errors")
    for e in errors:
        print("  ERR", e)

    site = os.environ.get("SITE_URL", "")
    if first_run or os.environ.get("SEND_TEST") == "true":
        discord([{"title": "Internship tracker is live" if first_run else "Internship tracker test message",
                  "description": f"Tracking **{n_open}** US hardware intern/co-op postings across "
                                 f"{len(ok_companies)} company boards. Checking every hour; "
                                 f"new postings will show up here.\n{site}", "color": 0x2F855A}])
    elif new:
        discord([{"title": f"{p['company']}: {p['title']}"[:256], "url": p["url"],
                  "description": f"{p['category']} · {p['location'] or 'location n/a'}",
                  "color": 0x3182CE} for p in new])


if __name__ == "__main__":
    sys.exit(main())
