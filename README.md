# tripit-import

Bulk-forward travel confirmation emails from iCloud Mail to TripIt.

## Prerequisites

- Python 3.10+
- iCloud account with an [app-specific password](https://support.apple.com/en-us/102654)

## Setup

1. **Create `.env`** in the project directory:

   ```
   ICLOUD_EMAIL=you@icloud.com
   ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   ```

2. **Configure domains** in `import.py` if needed:
   - `INCLUDE_DOMAINS` — sender domains to consider

## Quick start

```bash
# dry run: preview matches, write results/<timestamp>.csv, nothing sent
python3 import.py --dry-run --since-date "01-Jan-2024" --max-emails 100 --debug

# inspect output
open results/

# small live test: send up to 5 emails
python3 import.py --send --max-sends 5 --delay 2

# run again — already-sent emails are skipped automatically
python3 import.py --send

# clear dedup state and start fresh
python3 import.py --reset-state --dry-run
```

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | on | Preview only; nothing sent |
| `--send` | — | Actually forward emails (overrides `--dry-run`) |
| `--since-date DATE` | `01-Jan-2016` | IMAP SINCE date filter |
| `--max-emails N` | 0 (unlimited) | Cap total UIDs fetched |
| `--max-sends N` | 0 (unlimited) | Stop after N sends |
| `--reset-state` | — | Delete `sent_ids.json` before running |
| `--debug` | — | Print per-email filter reasoning |
| `--domains DOMAINS` | config value | Comma-separated domain override |
| `--batch-size N` | 25 | Progress print interval during sends |
| `--delay SECS` | 1.0 | Seconds between sends |

## How deduplication works

Each processed email's `Message-ID` header (or IMAP UID as fallback) is stored in `sent_ids.json`. On subsequent runs, any ID already in that file is skipped before the full message is fetched. Failed sends are not added, so they can be retried.

## Output files

| File | Description |
|------|-------------|
| `sent_ids.json` | Auto-created; set of forwarded message IDs |
| `results/<timestamp>.csv` | Written each run to the `results/` folder; filename includes run timestamp |
| `.env` | You create; stores credentials (never commit this) |

`results/<timestamp>.csv` columns: `message_id, date, from, sender_domain, subject, matched, stage, reason`
