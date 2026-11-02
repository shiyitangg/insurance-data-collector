# Public Insurance Data Collector (OpenFEMA NFIP)

A small, reproducible pipeline that **discovers** public insurance datasets, **collects**
them into SQLite, and **validates** the result before anything downstream uses it.

The data source is the [OpenFEMA API](https://www.fema.gov/about/openfema/api), the
Federal Emergency Management Agency's public data service. The default dataset is
**NFIP Redacted Claims** (`NfipClaims`, about 2.7M records): insurance claims filed under the National Flood
Insurance Program. The API is open, needs no key, and is published by FEMA for
public reuse.

## Why this exists

Collecting external data is easy to do badly. A script that works once, silently
collects duplicates on the second run, and hands a half-finished table to an
analyst is worse than no script. This project is built around three rules:

1. **Ask permission, and read the rules correctly.** `robots.txt` is fetched
   before any URL, a site's declared `Crawl-delay` is honoured, and requests
   are spaced out. See [Reading robots.txt](#reading-robotstxt) for a bug this
   surfaced.
2. **Re-running must be safe.** Rows are keyed on the source's own record id, so a
   second run adds nothing and loses nothing.
3. **Bad data should fail loudly, and odd data should still be visible.**
   Seven checks run after collection. A real defect exits non-zero; a finding
   that is unusual but legitimate is reported as a warning instead of blocking
   the run.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. What insurance datasets does FEMA publish?
python run.py discover

# 2. Collect New York flood claims from 2015 onward
python run.py collect --dataset NfipClaims

# 3. Validate what was collected
python run.py check --report reports/data_quality.txt
```

The database is written to `data/insurance.db` and is not committed.

## What each command does

### `discover`

Lists the datasets FEMA publishes and keeps the insurance-related ones. Two
independent paths are used so the catalogue does not depend on a single source:

- the OpenFEMA `DataSets` API, and
- the public HTML listing page, parsed with BeautifulSoup.

The HTML path is skipped automatically if `robots.txt` disallows it, so the
catalogue degrades to API-only rather than breaking. Results are written to the
`dataset_catalog` table with the source recorded for each row.

### `collect`

Pages through the chosen endpoint with `$top` and `$skip`, 1,000 records per
request. Each page is committed before the next request, so an interrupted run
leaves a consistent database and a `collection_runs` row that records how far it
got.

Each record is stored with:

- a `record_id` taken from the source's `id`, falling back to a SHA-256 hash of
  the row so the key is stable even if the source has no id;
- six commonly used fields extracted into typed columns; and
- the complete original JSON in `payload`, so no information is lost by the
  schema I chose today.

`INSERT ... ON CONFLICT DO NOTHING` makes re-collection idempotent. A second run
of the same range reports every row as a skipped duplicate:

```
{"rows_seen": 12500, "rows_written": 0, "duplicates_skipped": 12500}
```

### `check`

| Check | What it catches | Severity |
|---|---|---|
| `schema` | A column was dropped or renamed | error |
| `row_count` | Collection produced nothing | error |
| `duplicate_ids` | The dedup key stopped working | error |
| `null_rate:state` | More than 5% of rows missing a key field | error |
| `year_range` | Loss years before 1970 or in the future | error |
| `non_negative_payments` | Negative claim payments | warning |
| `run_reconciliation` | More rows stored than the collector reported writing | error |

The command prints a report and exits 1 when an error-severity check fails, so
it can gate a downstream job. `--strict` makes warnings fail too.

### Why one check is a warning

The first real collection, 12,500 New York claims from 2015 onward, failed on
`non_negative_payments`: one record had a building payment of -8,627.72.

Reading that record rather than adjusting the threshold:
`netBuildingPaymentAmount` was negative by the same amount,
`buildingDamageAmount` was 9,372, the most recent payment was posted about five
months after the date of loss, and no recovery amount was recorded. Negative
values carry meaning elsewhere in the dataset too (`waterDepth` is measured
against a reference level and goes negative). The pattern reads as a payment
that was later reversed or adjusted, which is a legitimate record, not a
collection fault.

So the check stays, keeps reporting the count and an example record id, and is
classified as a warning. A pipeline should not stop for it, and an analyst
summing payments should know it is there. This has not been confirmed against
FEMA's own documentation; it is an inference from the record's own fields.

## Reading robots.txt

The first version of this collector refused to fetch anything from FEMA. The
parser reported that `robots.txt` disallowed every URL, for every user agent,
including Googlebot. The published file says nothing of the kind: it blocks
`/admin/`, a few named bots, and the translated language paths, and never
mentions `/api/`.

The cause was in how the rules were being fetched, not in the rules. Python's
`RobotFileParser.read()` requests `robots.txt` with a `Python-urllib/3.x` user
agent. FEMA's CDN answers that agent with HTTP 403, and the robots convention
is to treat a 403 on `robots.txt` as "this whole site is off limits". The
parser was faithfully applying a rule it had never managed to read. `curl`
fetched the same file without trouble, which is what gave the mismatch away.

The fix is that `robots.txt` is now fetched by the same session, with the same
descriptive `User-Agent`, as the data requests. Interpretation is split into
`build_robots_parser()`, a pure function over a status code and the response
body, so the four cases can be tested directly: parse the rules on 2xx, allow
everything on 404, and refuse on 401, 403, or anything else unexpected.

Worth keeping in mind for other sources: a "robots.txt forbids this" error can
mean the rules were never read. Check what the file actually says before either
trusting the refusal or working around it.

## Tests

```bash
pip install pytest
pytest
```

23 tests, no network required.

`test_checks.py` injects a specific defect for each check (an empty table, a
null-heavy column, a year of 2999, a negative payment, a row count that does
not reconcile) and asserts the matching check fails, so the suite proves the
checks can catch bad data rather than only proving they run.

It also covers severity: a negative payment warns without blocking, a bad year
blocks, and `--strict` promotes warnings to failures.

`test_robots.py` covers the behaviour above, including the case that caused the
bug: a site that answers 403 to the fetch, and a file whose `Disallow: /` rule
applies to one named bot rather than to everyone.

## Project layout

```
collector/
  http_client.py   robots.txt, rate limiting, retry with backoff
  discover.py      dataset catalogue from the API and the HTML listing
  collect.py       paged collection into SQLite, idempotent by record id
  checks.py        seven data-quality checks
run.py             CLI: discover | collect | check
tests/             checks, record-id logic, and robots.txt handling
```

## Options

```bash
# A different state and period
python run.py collect --dataset NfipClaims --filter "state eq 'FL' and yearOfLoss ge 2020"

# A different dataset from the catalogue, by name
python run.py collect --dataset NfipPolicies --filter "propertyState eq 'NY'"

# Slow down further
python run.py --delay 3 collect
```

## Limits

- Typed columns accept a short list of aliases per field, because FEMA spells
  the same idea differently across datasets (`state` versus `propertyState`).
  An unrecognised name leaves that column null; the full row is always kept in
  `payload`, so nothing is lost.
- The checks are thresholds I chose, not FEMA's. The 5% null limit on `state` is
  a judgement call, not a published standard.
- `max_pages` defaults to 20, which caps a run at 20,000 records. The claims
  dataset holds about 2.7M records nationally; raise the limit, or filter to a
  narrower slice, before drawing conclusions about national totals.
- API versions differ per dataset (`NfipClaims` is v3, `NfipCommunityStatusBook`
  is v1), which is why `--dataset` reads the endpoint from the catalogue that
  `discover` built instead of assuming one.
- Collection has only been run against FEMA's API. Pointing it at another source
  would need its paging parameters checked first.

## Source and terms

Data: U.S. Federal Emergency Management Agency, OpenFEMA. FEMA asks that
products built on its data not claim FEMA endorsement, and this project makes no
such claim. The collector identifies itself with a descriptive User-Agent and
obeys `robots.txt` on every request.
