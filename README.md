# Hardware Internship Tracker

Tracks US-based, undergrad-eligible RTL / DV / FPGA / silicon-validation internships and co-ops.

- `targets.csv` / `targets.md`: curated target list (edit `tracker/build_targets.py`, then run it).
- `tracker/boards.json`: company job boards checked every 10 minutes by `.github/workflows/check.yml`.
- `tracker/check.py`: fetches the boards plus the [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships)
  aggregator feed (which picks up postings from LinkedIn, Glassdoor, etc.), filters, and posts new ones to Discord
  (`DISCORD_WEBHOOK_URL` secret).
  - **US only:** a posting needs at least one US site. Workday searches use each board's own "United States" filter.
  - **Undergrad only:** titles naming PhD / Master's / MBA / graduate / new grad are dropped, and each new posting's
    description (or Simplify's degree field) is checked once for grad-only requirements.
- `index.html`: the site (GitHub Pages).

To add a company, add it to `tracker/boards.json` (`greenhouse`/`lever`/`ashby` + slug, or `workday` + host/tenant/site).
A newly added board is seeded quietly on its first run, so it doesn't flood Discord.

Glassdoor and LinkedIn have no public job APIs and block automated access, so they're covered through the Simplify feed.
When the 2028 recruiting cycle starts, point `SIMPLIFY` in `check.py` at the next year's repo.
