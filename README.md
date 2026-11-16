# Hardware Internship Tracker

Tracks Summer 2027 RTL / DV / FPGA / silicon-validation internships.

- `targets.csv` / `targets.md`: curated target list (edit `tracker/build_targets.py`, then run it).
- `tracker/boards.json`: job boards checked every hour by `.github/workflows/check.yml`.
- `tracker/check.py`: fetches the boards, filters to US hardware intern/co-op roles, and posts new ones to Discord (`DISCORD_WEBHOOK_URL` secret).
- `index.html`: the site (GitHub Pages).

To add a company, add it to `tracker/boards.json` (`greenhouse`/`lever`/`ashby` + slug, or `workday` + host/tenant/site).
