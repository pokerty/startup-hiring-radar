# Startup Hiring Radar

A small Python CLI that uses Exa to surface recently funded, Singapore-connected
Series A-C startups hiring backend, product, or forward-deployed engineers. It
prioritizes signals before they become crowded job-board listings and creates a
lead-focused dossier for each target.

## Architecture

`radar.py` runs a three-stage pipeline:

1. **Signal discovery** searches recent news for funding, Singapore engineering
   expansion, and founder/CTO hiring signals in AI, developer tools, and fintech.
   The lookback window defaults to 60 days; generic job boards are excluded.
   Candidate records retain matching article titles, URLs, publish dates, and
   highlights for the following stage.
2. **Company enrichment** deduplicates candidates and excludes selected mega-cap
   corporations, then separates time-sensitive and evergreen lookups:
   - Recent funding/news uses a 180-day published-date filter, includes the
     Stage 1 evidence in its query, requests the round stage, amount, date, and
     direct source URL, and asks Exa for content no older than 24 hours.
   - Careers/company details use an undated general search, so evergreen careers
     pages are not filtered out; it requests current roles, Singapore presence,
     and a concise factual company introduction.
   Funding stage/date/source and company facts are only as trustworthy as their
   retrieved sources. Before outreach, use the Google freshness-check link in
   each dossier to manually confirm the latest round and date; the CLI does not
   perform that Google check automatically. Strong matches are tagged Tier 1;
   candidates with incomplete funding, Singapore, or role evidence are retained
   as Tier 2 rather than discarded.
3. **Engineering lead lookup** finds up to two technical decision-makers and
   available profile URLs or recent-work highlights for each enriched company.

Responses are cached locally in `.exa_cache.json` using stage, query, and complete
search-parameter keys. Cache entries expire after 24 hours; Exa content retrieval
also uses `max_age_hours=24` for discovery, funding, and careers searches. Use
`--refresh` to bypass cached responses. The cache may contain Exa result text;
keep it local and do not commit it.

## Setup and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
touch .env
# Add EXA_API_KEY=your-key to .env in your editor. Do not copy the blank example
# over an existing .env; that would overwrite its contents.
python radar.py --limit 5 --days 60 --out-dir output
```

`.env` is intentionally not generated or committed. Set `EXA_API_KEY` in the
environment or in a local `.env` file.

Options:

```text
--limit N     Maximum company candidates to enrich (default: 5)
--days N      Recent-signal lookback period (default: 60)
--refresh     Ignore cached API responses
--include-seen Bypass the early name-only filter; duplicates are still rejected
--out-dir DIR Output directory (default: current directory)
```

`targets_master.csv` in the output directory is the persistent source of truth
for target deduplication. On first run only, if the master file does not exist,
the CLI migrates and deduplicates existing `targets*.csv` files into it. Existing
master-sheet columns and manually edited values (such as status or notes) are
preserved when new rows are appended; writes use a temporary file and atomic
replacement. New rows include `discovered_date` and a default `status` of `New`.

Each run that finds new companies also writes a uniquely timestamped Markdown
dossier, for example `targets_today_20261120_152345_123456.md`, containing only
those new targets. No Markdown file is created when there are no new targets;
no per-run CSV is written.

By default, known company names are filtered before enrichment to save Exa calls.
After enrichment, both canonical company name and normalized domain are checked
against the master sheet and this run's additions before lead lookup. The
`--include-seen` flag bypasses the early name-only filter, but master/domain
deduplication still prevents duplicate rows and lead lookups.
Candidate enrichment continues until the requested number of targets is found
or at most `3 × --limit` candidates have been attempted.

## Example dossier

```text
## Example Startup
### Company introduction
Founded by Example Founders in Singapore in 2021, Example Startup builds AI-agent
payment tools for B2B fintech teams.

- **Target fit:** Tier 1
- **Funding stage:** Series B
- **Latest funding date:** September 2026
- **Funding amount:** $20M
- **Funding source:** [Open source](https://example.com/funding)
- **Singapore presence:** Engineering hub in Singapore
- **Why now:** Announced a new engineering hub after its recent Series B
- **Hiring roles:** Backend Engineer, Product Engineer
- **Careers:** https://example.com/careers
- **Freshness check before outreach:** [Google latest funding search](https://www.google.com/search?q=Example+Startup+latest+funding+round)

### Target leads
- [Jane Doe, CTO](profile URL)
  - Recent work: Engineering leadership article or project highlight

### Cold email draft
Hi Jane Doe, I saw your $20M Series B round announced in September 2026, and was impressed by Example Startup's momentum.
I enjoyed your recent work on engineering leadership.
I'd love to hear how the team is approaching engineering growth as it scales.
Would you be open to a brief conversation next week?
```

## Cost and latency

The uncached upper bound is five discovery searches plus up to three searches
(recent funding, careers/company, and leads) for each of at most
`3 × --limit` candidate attempts (`5 + 9 × --limit`), so the default run is at
most 50 Exa calls. Failed enrichments may use fewer calls, and cache hits use
none. `--refresh` can increase API usage; use a smaller `--limit` while testing.
Exa pricing and latency depend on the account, query type, and current service
plan—check Exa's current pricing before scaling. Calls run sequentially to keep
progress and failure reporting straightforward.
