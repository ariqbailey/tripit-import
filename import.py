#!/usr/bin/env python3

import email
import imaplib
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

# =========================
# config
# =========================

ICLOUD_EMAIL = "ariqbailey@icloud.com"
ICLOUD_APP_PASSWORD = ""  # apple app-specific password
TRIPIT_EMAIL = "plans@tripit.com"

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587

MAILBOX = "INBOX"

# adjust this if you want a tighter or broader window
SINCE_DATE = "01-Jan-2016"

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

# subject/body signals that are usually good
INCLUDE_KEYWORDS = [
    "confirmation",
    "itinerary",
    "receipt",
    "e-ticket",
    "eticket",
    "booking",
    "reservation",
    "trip confirmation",
    "booking confirmation",
]

# strong negatives to avoid noise / duplicate operational emails
EXCLUDE_KEYWORDS = [
    "check-in",
    "check in",
    "boarding",
    "gate",
    "last call",
    "upgrade",
    "priority boarding",
    "bag drop",
    "delayed",
    "delay",
    "cancelled",
    "canceled",
    "flight status",
    "reminder",
]

# safety
MAX_FORWARD = 300
SLEEP_BETWEEN_SENDS_SECONDS = 1.0
DRY_RUN = True  # change to False to actually send


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


def extract_text_from_message(msg: email.message.Message) -> str:
    parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", "")).lower()

            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    parts.append(html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))

    return "\n".join(parts).lower()


def domain_matches(from_value: str) -> bool:
    from_lower = (from_value or "").lower()
    return any(domain in from_lower for domain in INCLUDE_DOMAINS)


def keyword_in_text(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def should_forward(msg: email.message.Message) -> tuple[bool, str]:
    from_value = decode_header_value(msg.get("From", ""))
    subject = decode_header_value(msg.get("Subject", ""))
    body_text = extract_text_from_message(msg)

    haystack = f"{subject}\n{body_text}".lower()

    if not domain_matches(from_value):
        return False, "sender domain not in allowlist"

    if keyword_in_text(haystack, EXCLUDE_KEYWORDS):
        return False, "matched exclusion keyword"

    if not keyword_in_text(haystack, INCLUDE_KEYWORDS):
        return False, "no inclusion keyword found"

    return True, "matched"


def build_forward_message(
    original_bytes: bytes,
    original_msg: email.message.Message,
) -> EmailMessage:
    subject = decode_header_value(original_msg.get("Subject", "")).strip()
    from_value = decode_header_value(original_msg.get("From", "")).strip()
    date_value = decode_header_value(original_msg.get("Date", "")).strip()

    forward = EmailMessage()
    forward["From"] = ICLOUD_EMAIL
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


def search_candidate_ids(imap_conn: imaplib.IMAP4_SSL) -> list[bytes]:
    # broad initial search; detailed filtering happens in python
    status, data = imap_conn.search(None, "SINCE", SINCE_DATE)
    if status != "OK":
        raise RuntimeError("imap search failed")
    return data[0].split()


def fetch_message(imap_conn: imaplib.IMAP4_SSL, msg_id: bytes) -> tuple[email.message.Message, bytes]:
    status, data = imap_conn.fetch(msg_id, "(RFC822)")
    if status != "OK":
        raise RuntimeError(f"failed to fetch message id {msg_id!r}")

    raw_bytes = None
    for item in data:
        if isinstance(item, tuple):
            raw_bytes = item[1]
            break

    if raw_bytes is None:
        raise RuntimeError(f"no raw bytes returned for message id {msg_id!r}")

    parsed = email.message_from_bytes(raw_bytes)
    return parsed, raw_bytes


def main() -> None:
    print("connecting to icloud imap...")
    imap_conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap_conn.login(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
    imap_conn.select(MAILBOX)

    candidate_ids = search_candidate_ids(imap_conn)
    print(f"found {len(candidate_ids)} total messages since {SINCE_DATE}")

    matched = []
    skipped = []

    for msg_id in candidate_ids:
        try:
            msg, raw_bytes = fetch_message(imap_conn, msg_id)
            ok, reason = should_forward(msg)

            subject = decode_header_value(msg.get("Subject", ""))
            from_value = decode_header_value(msg.get("From", ""))

            record = {
                "id": msg_id.decode(),
                "from": from_value,
                "subject": subject,
                "raw_bytes": raw_bytes,
                "msg": msg,
                "reason": reason,
            }

            if ok:
                matched.append(record)
            else:
                skipped.append(record)

        except Exception as exc:
            skipped.append({
                "id": msg_id.decode(errors="ignore"),
                "from": "",
                "subject": "",
                "reason": f"error: {exc}",
            })

    print(f"matched {len(matched)} messages")
    print(f"skipped {len(skipped)} messages")

    matched.sort(
        key=lambda x: parsedate_to_datetime(x["msg"].get("Date")) if x.get("msg") and x["msg"].get("Date") else 0
    )

    for i, item in enumerate(matched[:25], start=1):
        print(f"[preview {i}] {item['from']} | {item['subject']}")

    if DRY_RUN:
        print("\ndry run enabled. nothing sent.")
        return

    if len(matched) > MAX_FORWARD:
        raise RuntimeError(
            f"refusing to send {len(matched)} messages; increase MAX_FORWARD if intentional"
        )

    print("\nconnecting to icloud smtp...")
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls(context=context)
        smtp.login(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)

        for i, item in enumerate(matched, start=1):
            forward = build_forward_message(item["raw_bytes"], item["msg"])
            smtp.send_message(forward)
            print(f"[sent {i}/{len(matched)}] {item['from']} | {item['subject']}")
            time.sleep(SLEEP_BETWEEN_SENDS_SECONDS)

    print("\ndone.")


if __name__ == "__main__":
    main()