import html
import json
import os
import re
import smtplib
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from zoneinfo import ZoneInfo

API = "https://hagizra.news/api/v2/messages?offset={offset}&limit={limit}&direction=desc"
ASK_LIMIT = 100     # ask the site for this many flashes per request (it may return fewer)
MAX_PAGES = 15      # after a long gap, read at most this many pages
STATE_FILE = "last_id.txt"
MAX_PER_RUN = 100    # send at most this many emails per run; the rest go next run

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
    raise SystemExit(
        "חסרים פרטי ג'ימייל. יש להוסיף את GMAIL_ADDRESS ואת "
        "GMAIL_APP_PASSWORD ב-Settings > Secrets and variables > Actions."
    )
TO_EMAIL = os.environ.get("TO_EMAIL", "").strip() or GMAIL_ADDRESS


def fetch_page(offset):
    url = API.format(offset=offset, limit=ASK_LIMIT)
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["messages"]
        except Exception as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise last_error


def fetch_since(last_id):
    """Read newest-first pages until we reach flashes that were already handled.

    Returns (messages, covered). covered is False when we could not reach the
    last flash we handled, which means some flashes in between may be missed.
    """
    collected = {}
    offset = 0
    for _ in range(MAX_PAGES):
        batch = fetch_page(offset)
        ids = [m["id"] for m in batch]
        print(
            f"fetched offset={offset} asked={ASK_LIMIT} got={len(batch)}"
            + (f" ids {min(ids)}..{max(ids)}" if ids else "")
        )
        fresh = [m for m in batch if m["id"] not in collected]
        if not fresh:
            print("no new flashes in this page (end of data, or the site ignored offset)")
            break
        for m in fresh:
            collected[m["id"]] = m
        if last_id is None or min(ids) <= last_id:
            break
        offset += len(batch)

    covered = (
        last_id is None
        or not collected
        or min(collected) <= last_id
    )
    return list(collected.values()), covered


def read_last_id():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def write_last_id(value):
    with open(STATE_FILE, "w") as f:
        f.write(str(value))


def clean(text):
    # turn embedded video/image markers into plain links
    text = re.sub(r"\[(?:video|image)-embedded#\]\(([^)#]+)[^)]*\)", r"\1", text)
    return text.replace("**", "").strip()


def local_dt(ts):
    try:
        ts = re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        return datetime.now(ZoneInfo("Asia/Jerusalem"))


def to_html(text, time_str):
    body = html.escape(text)
    body = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', body)
    body = body.replace("\n", "<br>")
    return (
        '<div dir="rtl" style="direction:rtl; text-align:right; '
        'font-size:16px; line-height:1.6">'
        f"{time_str}<br><br>{body}<br><br>"
        '<a href="https://hagizra.news/">hagizra.news</a></div>'
    )


def send_email(server, message):
    text = clean(message["text"])
    dt = local_dt(message["timestamp"])
    day = dt.strftime("%d.%m.%Y")

    # same subject + same References root for the whole day -> one Gmail thread per day
    root_id = "<hagizra-" + dt.strftime("%Y-%m-%d") + "@hagizra.news>"

    mail = EmailMessage()
    mail["From"] = GMAIL_ADDRESS
    mail["To"] = TO_EMAIL
    mail["Subject"] = "מבזקי הגזרה " + day
    mail["Message-ID"] = make_msgid(domain="hagizra.news")
    mail["In-Reply-To"] = root_id
    mail["References"] = root_id
    time_str = dt.strftime("%H:%M")
    mail.set_content(f"{time_str}\n\n{text}\n\nhttps://hagizra.news/")
    mail.add_alternative(to_html(text, time_str), subtype="html")
    server.send_message(mail)


def send_notice(server, text):
    mail = EmailMessage()
    mail["From"] = GMAIL_ADDRESS
    mail["To"] = TO_EMAIL
    mail["Subject"] = "⚠️ מבזקי הגזרה: ייתכן שפוספסו מבזקים"
    mail["Message-ID"] = make_msgid(domain="hagizra.news")
    now = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
    mail.set_content(text)
    mail.add_alternative(to_html(text, now), subtype="html")
    server.send_message(mail)


def main():
    last_id = read_last_id()
    all_messages, covered = fetch_since(last_id)
    if not all_messages:
        return

    newest_id = max(m["id"] for m in all_messages)

    # first run: just remember where we are, do not spam old flashes
    if last_id is None:
        write_last_id(newest_id)
        print("First run - saved starting point:", newest_id)
        return

    new = [
        m for m in all_messages
        if m["id"] > last_id and not m.get("deleted") and not m.get("is_ads")
    ]
    new.sort(key=lambda m: m["id"])
    new = new[:MAX_PER_RUN]

    if not new:
        # keep the pointer moving past deleted/ad items
        if newest_id > last_id:
            write_last_id(newest_id)
        print("Nothing new")
        return

    notice = None
    if not covered:
        oldest = min(m["id"] for m in all_messages)
        gap = oldest - last_id - 1
        notice = (
            "ייתכן שפוספסו מבזקים. האתר מחזיר רק את המבזקים האחרונים, "
            "והפער בין המבזק האחרון ששלחתי לבין הישן ביותר שהצלחתי לקרוא "
            f"הוא עד {gap} מזהים (חלקם אולי פרסומות או הודעות שנמחקו)."
        )
        print("WARNING:", notice)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        if notice:
            send_notice(server, notice)
        for m in new:
            send_email(server, m)
            write_last_id(m["id"])
            print("Sent", m["id"])


if __name__ == "__main__":
    main()
