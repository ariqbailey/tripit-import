#!/usr/bin/env python3

import argparse
import csv
import email
import email.header
import imaplib
import json
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

# =========================
# config
# =========================

TRIPIT_EMAIL = "plans@tripit.com"

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587

MAILBOX = "INBOX"

# airline / ota domains to include
INCLUDE_DOMAINS = [
    # us
    "united.com",
    "delta.com",
    "aa.com",
    "southwest.com",
    "alaskaair.com",
    "jetblue.com",
    "spirit.com",
    "flyfrontier.com",
    # europe / intl
    "ryanair.com",
    "flysas.com",
    "sas.se",
    "sas.dk",
    "tap.pt",
    "flytap.com",
    "lufthansa.com",
    "klm.com",
    "airfrance.com",
    "ba.com",
    "britishairways.com",
    "easyjet.com",
    "wizzair.com",
    "vueling.com",
    "iberia.com",
    "norwegian.com",
    "norwegian.no",
    "aerlingus.com",
    "turkishairlines.com",
    "emirates.com",
    "qatarairways.com",
    "etihad.com",
    # aggregators / otas
    "expedia.com",
    "booking.com",
    "trip.com",
    "gotogate.com",
    "edreams.com",
    "opodo.com",
    "kiwi.com",
    "kayak.com",
    "hopper.com",
]


# =========================
# state files
# =========================

SENT_IDS_FILE = Path("sent_ids.json")
RESULTS_CSV_FILE = Path("results.csv")
ENV_FILE = Path(".env")


# =========================
# env / credentials
# =========================

def load_env_file(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def load_credentials() -> tuple[str, str]:
    env = load_env_file(ENV_FILE)
    # env file values override actual environment
    merged = {**os.environ, **env}

    email_addr = merged.get("ICLOUD_EMAIL", "").strip()
    password = merged.get("ICLOUD_APP_PASSWORD", "").strip()

    missing = []
    if not email_addr:
        missing.append("ICLOUD_EMAIL")
    if not password:
        missing.append("ICLOUD_APP_PASSWORD")

    if missing:
        print(f"error: missing required credentials: {', '.join(missing)}", file=sys.stderr)
        print("set them in a .env file or as environment variables.", file=sys.stderr)
        sys.exit(1)

    return email_addr, password


# =========================
# deduplication state
# =========================

def load_sent_ids() -> set[str]:
    if not SENT_IDS_FILE.exists():
        return set()
    try:
        return set(json.loads(SENT_IDS_FILE.read_text()))
    except Exception:
        return set()


def save_sent_id(msg_id: str) -> None:
    ids = load_sent_ids()
    ids.add(msg_id)
    tmp = SENT_IDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(ids), indent=2))
    tmp.replace(SENT_IDS_FILE)


# =========================
# helpers
# =========================

def decode_header_value(value: str) -> str:
    if not value:
        return ""
    decoded_parts = email.header.decode_header(value)
    out = []
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)



def domain_matches(from_value: str, domains: list[str]) -> bool:
    from_lower = (from_value or "").lower()
    return any(domain in from_lower for domain in domains)


def get_message_id(msg: email.message.Message, uid: bytes) -> str:
    raw = msg.get("Message-ID", "").strip()
    return raw if raw else f"uid:{uid.decode(errors='ignore')}"


# =========================
# filtering
# =========================

def should_forward(
    msg: email.message.Message,
    domains: list[str],
) -> tuple[bool, str, str]:
    """Returns (matched, stage, reason)."""
    from_value = decode_header_value(msg.get("From", ""))
    if not domain_matches(from_value, domains):
        return False, "domain", "sender domain not in allowlist"
    return True, "domain", "matched"


# =========================
# imap
# =========================

def search_candidate_uids(
    imap_conn: imaplib.IMAP4_SSL,
    since_date: str,
    domains: list[str],
) -> list[bytes]:
    if not domains:
        return []

    if len(domains) == 1:
        from_clause = f'FROM "{domains[0]}"'
    else:
        # right-fold into nested OR: OR FROM d1 (OR FROM d2 FROM d3)
        from_clause = f'FROM "{domains[-1]}"'
        for d in reversed(domains[:-1]):
            from_clause = f'OR FROM "{d}" {from_clause}'

    criteria = f'SINCE {since_date} {from_clause}'
    status, data = imap_conn.uid('search', None, criteria.encode())
    if status != "OK":
        raise RuntimeError(f"imap search failed: {data}")
    return data[0].split()


def fetch_headers(imap_conn: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message | None:
    status, data = imap_conn.uid('fetch', uid, '(RFC822.HEADER)')
    if status != "OK":
        return None
    for item in data:
        if isinstance(item, tuple):
            return email.message_from_bytes(item[1])
    return None


def fetch_full_message(imap_conn: imaplib.IMAP4_SSL, uid: bytes) -> tuple[email.message.Message, bytes] | None:
    status, data = imap_conn.uid('fetch', uid, '(BODY[])')
    if status != "OK":
        return None
    for item in data:
        if isinstance(item, tuple):
            raw = item[1]
            return email.message_from_bytes(raw), raw
    return None


# =========================
# smtp
# =========================

def build_forward_message(
    original_bytes: bytes,
    original_msg: email.message.Message,
    icloud_email: str,
) -> EmailMessage:
    subject = decode_header_value(original_msg.get("Subject", "")).strip()
    from_value = decode_header_value(original_msg.get("From", "")).strip()
    date_value = decode_header_value(original_msg.get("Date", "")).strip()

    forward = EmailMessage()
    forward["From"] = icloud_email
    forward["To"] = TRIPIT_EMAIL
    forward["Subject"] = f"Fwd for TripIt: {subject or 'travel confirmation'}"

    body = (
        "forwarding possible travel confirmation for tripit parsing.\n\n"
        f"original from: {from_value}\n"
        f"original date: {date_value}\n"
        f"original subject: {subject}\n"
    )
    forward.set_content(body)

    filename = "original_message.eml"
    if subject:
        safe_subject = "".join(c for c in subject if c.isalnum() or c in (" ", "-", "_")).strip()
        if safe_subject:
            filename = f"{safe_subject[:80]}.eml"

    forward.add_attachment(
        original_bytes,
        maintype="message",
        subtype="rfc822",
        filename=filename,
    )
    return forward


# =========================
# main
# =========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward travel confirmation emails to TripIt.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="preview only, nothing sent (default)")
    mode.add_argument("--send", action="store_true", help="actually send emails")
    parser.add_argument("--since-date", default="01-Jan-2016", metavar="DATE", help='e.g. "01-Jan-2024"')
    parser.add_argument("--max-emails", type=int, default=0, metavar="N", help="cap total UIDs fetched (0=unlimited)")
    parser.add_argument("--max-sends", type=int, default=0, metavar="N", help="stop after N sends (0=unlimited)")
    parser.add_argument("--reset-state", action="store_true", help="delete sent_ids.json before running")
    parser.add_argument("--debug", action="store_true", help="verbose per-email filter reasoning")
    parser.add_argument("--domains", default="", metavar="DOMAINS", help="comma-separated domain override")
    parser.add_argument("--batch-size", type=int, default=25, metavar="N", help="sends per progress update")
    parser.add_argument("--delay", type=float, default=1.0, metavar="SECS", help="seconds between sends")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    actually_send = args.send

    if args.reset_state:
        if SENT_IDS_FILE.exists():
            SENT_IDS_FILE.unlink()
            print("sent_ids.json deleted.")

    icloud_email, icloud_password = load_credentials()

    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else INCLUDE_DOMAINS

    sent_ids = load_sent_ids()
    print(f"loaded {len(sent_ids)} already-sent message IDs")

    print("connecting to icloud imap...")
    imap_conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap_conn.login(icloud_email, icloud_password)
    imap_conn.select(MAILBOX)

    all_uids = search_candidate_uids(imap_conn, args.since_date, domains)
    if args.max_emails:
        all_uids = all_uids[: args.max_emails]
    print(f"found {len(all_uids)} total messages since {args.since_date}")

    # --- pass 1: fetch headers, domain check, dedupe ---
    candidates: list[tuple[bytes, email.message.Message, str]] = []  # (uid, header_msg, msg_id)

    print("pass 1: scanning headers...")
    for i, uid in enumerate(all_uids, start=1):
        if i % 50 == 0 or i == len(all_uids):
            print(f"  [scanned {i} / {len(all_uids)}]")

        try:
            header_msg = fetch_headers(imap_conn, uid)
            if header_msg is None:
                continue

            from_value = decode_header_value(header_msg.get("From", ""))
            msg_id = get_message_id(header_msg, uid)
            if msg_id in sent_ids:
                if args.debug:
                    print(f"  [skip dedup] uid={uid.decode()} msg_id={msg_id!r}")
                continue

            candidates.append((uid, header_msg, msg_id))

        except Exception as exc:
            if args.debug:
                print(f"  [error header] uid={uid.decode(errors='ignore')}: {exc}")

    print(f"pass 1 done: {len(candidates)} candidates passed domain check and dedup")

    # --- pass 2: fetch full messages, apply keyword filters ---
    queued: list[dict] = []

    print("pass 2: fetching full messages and applying keyword filters...")
    csv_rows: list[dict] = []

    for uid, header_msg, msg_id in candidates:
        try:
            result = fetch_full_message(imap_conn, uid)
            if result is None:
                continue
            msg, raw_bytes = result

            matched, stage, reason = should_forward(msg, domains)

            from_value = decode_header_value(msg.get("From", ""))
            subject = decode_header_value(msg.get("Subject", ""))
            date_value = decode_header_value(msg.get("Date", ""))

            if args.debug:
                status = "MATCH" if matched else "skip"
                print(f"  [{status}] stage={stage} reason={reason!r} | {from_value} | {subject}")

            # extract domain from from_value for auditing
            sender_domain = ""
            from_lower = from_value.lower()
            at_idx = from_lower.rfind("@")
            if at_idx != -1:
                tail = from_lower[at_idx + 1:]
                sender_domain = tail.split(">")[0].split()[0].strip()

            csv_rows.append({
                "message_id": msg_id,
                "date": date_value,
                "from": from_value,
                "sender_domain": sender_domain,
                "subject": subject,
                "matched": matched,
                "stage": stage,
                "reason": reason,
            })

            if matched:
                queued.append({
                    "msg_id": msg_id,
                    "from": from_value,
                    "subject": subject,
                    "date": date_value,
                    "raw_bytes": raw_bytes,
                    "msg": msg,
                })

        except Exception as exc:
            if args.debug:
                print(f"  [error full] uid={uid.decode(errors='ignore')}: {exc}")

    imap_conn.logout()

    with RESULTS_CSV_FILE.open("w", newline="") as csv_fh:
        writer = csv.DictWriter(csv_fh, fieldnames=["message_id", "date", "from", "sender_domain", "subject", "matched", "stage", "reason"])
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"pass 2 done: {len(queued)} emails queued for forwarding")
    print(f"results written to {RESULTS_CSV_FILE}")

    # preview
    for i, item in enumerate(queued[:25], start=1):
        print(f"[preview {i}] {item['from']} | {item['subject']}")

    if not actually_send:
        print("\ndry run mode. pass --send to actually forward emails.")
        return

    if not queued:
        print("nothing to send.")
        return

    to_send = queued if not args.max_sends else queued[: args.max_sends]
    print(f"\nconnecting to icloud smtp to send {len(to_send)} emails...")
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls(context=context)
        smtp.login(icloud_email, icloud_password)

        sent_count = 0
        for i, item in enumerate(to_send, start=1):
            try:
                forward = build_forward_message(item["raw_bytes"], item["msg"], icloud_email)
                smtp.send_message(forward)
                save_sent_id(item["msg_id"])
                sent_count += 1
                print(f"[sent {sent_count} / {len(to_send)}] {item['from']} | {item['subject']}")
            except Exception as exc:
                print(f"[error] failed to send {item['msg_id']!r}: {exc}")

            if i % args.batch_size == 0:
                print(f"[progress] {sent_count} sent so far...")

            time.sleep(args.delay)

    print(f"\ndone. sent {sent_count} / {len(to_send)}.")


if __name__ == "__main__":
    main()
