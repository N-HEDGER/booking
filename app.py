"""Minimal Doodle replacement: pick a slot from slots.yaml, get logged + emailed."""
import csv
import smtplib
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode
from email.message import EmailMessage
from pathlib import Path

import streamlit as st
import yaml

HERE = Path(__file__).parent
SLOTS_FILE = HERE / "slots.yaml"
BOOKINGS_FILE = HERE / "bookings.csv"
FIELDS = ["booked_at", "slot", "name", "email", "slot_label"]
_lock = threading.Lock()  # serialises check-then-write across sessions


def load_config():
    cfg = yaml.safe_load(SLOTS_FILE.read_text())
    default_cap = cfg.get("capacity", 1)
    slots = []
    for s in cfg["slots"]:
        if isinstance(s, dict):
            start, cap = s["start"], s.get("capacity", default_cap)
        else:
            start, cap = s, default_cap
        start = start if isinstance(start, datetime) else datetime.strptime(str(start), "%Y-%m-%d %H:%M")
        slots.append({"start": start, "capacity": cap})
    cfg["slots"] = sorted(slots, key=lambda s: s["start"])
    return cfg


def _secret(key):
    try:
        return st.secrets.get(key)
    except FileNotFoundError:  # no secrets.toml at all
        return None


@st.cache_resource
def get_worksheet():
    """Google Sheet worksheet, or None if not configured / unreachable."""
    creds, sheet = _secret("gcp_service_account"), _secret("gsheet")
    if not (creds and sheet):
        return None
    try:
        import gspread

        ws = gspread.service_account_from_dict(dict(creds)).open_by_key(sheet["spreadsheet_id"]).sheet1
        if ws.row_values(1) != FIELDS:
            if not any(any(c.strip() for c in r) for r in ws.get_all_values()):
                ws.append_row(FIELDS)
            else:
                st.error("First row of the Google Sheet must be empty or match: " + ", ".join(FIELDS))
                st.stop()
        return ws
    except Exception as e:
        print(f"Google Sheets unavailable: {type(e).__name__}: {e}")
        return None


def read_bookings():
    """The sheet is the source of truth when configured (survives app restarts)."""
    ws = get_worksheet()
    if ws is not None:
        try:
            return ws.get_all_records()
        except Exception as e:
            print(f"Sheet read failed, using local CSV: {e}")
    if not BOOKINGS_FILE.exists():
        return []
    with BOOKINGS_FILE.open(newline="") as f:
        return list(csv.DictReader(f))


def append_booking(row):
    new_file = not BOOKINGS_FILE.exists()
    with BOOKINGS_FILE.open("a", newline="") as f:  # local backup, always
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    ws = get_worksheet()
    if ws is not None:
        try:
            ws.append_row([row[k] for k in FIELDS], value_input_option="RAW")
        except Exception as e:
            print(f"Sheet write failed (saved to CSV): {e}")


def fmt(dt):
    return dt.strftime("%a %d %b %Y, %H:%M")


def _ics_escape(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def make_ics(cfg, start, uid_seed, summary=None, description=None):
    """Minimal iCalendar event (floating local time, so it lands at the same wall-clock time)."""
    end = start + timedelta(minutes=cfg.get("duration_minutes", 45))
    f = "%Y%m%dT%H%M%S"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//booking//EN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:{uid_seed}@booking", f"DTSTAMP:{datetime.utcnow().strftime(f)}Z",
        f"DTSTART:{start.strftime(f)}", f"DTEND:{end.strftime(f)}",
        f"SUMMARY:{_ics_escape(summary or cfg['title'])}", f"LOCATION:{_ics_escape(cfg.get('location', ''))}",
        f"DESCRIPTION:{_ics_escape(description or '')}",
        "BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def gcal_link(cfg, start):
    end = start + timedelta(minutes=cfg.get("duration_minutes", 45))
    f = "%Y%m%dT%H%M%S"
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": cfg["title"], "dates": f"{start.strftime(f)}/{end.strftime(f)}",
        "location": cfg.get("location", ""), "ctz": cfg.get("timezone", "Europe/London"),
    })


def send_email(subject, body, to=None, reply_to=None, tag=None, ics=None):
    """Returns (ok, message). Without SMTP secrets, prints to console instead."""
    conf = _secret("email")
    if not conf:
        print(f"[email not configured]\nSubject: {subject}\n{body}\n")
        return False, "Email not configured (printed to console)."
    msg = EmailMessage()
    msg["Subject"] = f"[{tag}] {subject}" if tag else subject
    if tag:
        msg["X-Study-Tag"] = tag
    msg["From"] = conf["username"]
    msg["To"] = to or conf["notify_to"]
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar",
                           filename="session.ics", params={"method": "PUBLISH"})
    try:
        with smtplib.SMTP(conf["smtp_host"], conf.get("smtp_port", 587), timeout=15) as s:
            s.starttls()
            s.login(conf["username"], conf["password"])
            s.send_message(msg)
        return True, "sent"
    except Exception as e:  # booking is already saved; don't lose it over email
        return False, f"{type(e).__name__}: {e}"


st.set_page_config(page_title="Book a session", page_icon="📅")
cfg = load_config()
st.title(cfg["title"])
st.markdown(cfg.get("description", ""))
if cfg.get("location"):
    st.caption(f"📍 {cfg['location']}")

bookings = read_bookings()
taken = {}
for b in bookings:
    taken[b["slot"]] = taken.get(b["slot"], 0) + 1

open_slots = [
    s for s in cfg["slots"]
    if s["start"] > datetime.now() and taken.get(s["start"].isoformat(), 0) < s["capacity"]
]

if "confirmed" in st.session_state:
    c = st.session_state.confirmed
    st.success(f"✅ Thanks {c['name']}, you're booked for {c['when']}.")
    st.info("📅 **Please add this session to your calendar now** so you don't forget it.")
    col1, col2 = st.columns(2)
    col1.link_button("Add to Google Calendar", c["gcal"], use_container_width=True)
    col2.download_button("Download .ics (Apple/Outlook)", c["ics"], file_name="session.ics",
                         mime="text/calendar", use_container_width=True)
    if c["emailed"]:
        st.caption(f"A confirmation with a calendar invite has been sent to {c['email']}.")
    st.stop()

if not open_slots:
    st.warning("Sorry, there are no free slots left.")
    st.stop()

with st.form("booking"):
    label = {s["start"].isoformat(): fmt(s["start"]) for s in open_slots}
    slot = st.radio("Available slots", list(label), format_func=label.get, index=None)
    name = st.text_input("Your name")
    email = st.text_input("Your email")
    submitted = st.form_submit_button("Book this slot", type="primary")

if submitted:
    name, email = name.strip(), email.strip()
    if not slot or not name or "@" not in email:
        st.error("Please choose a slot and enter your name and a valid email.")
    else:
        with _lock:
            current = read_bookings()  # re-check: someone may have just booked it
            chosen = next(s for s in open_slots if s["start"].isoformat() == slot)
            if sum(b["slot"] == slot for b in current) >= chosen["capacity"]:
                st.error("Sorry, that slot was just taken. Please pick another.")
                st.rerun()
            append_booking({"booked_at": datetime.now().isoformat(timespec="seconds"),
                            "slot": slot, "name": name, "email": email,
                            "slot_label": fmt(chosen["start"])})

        when = fmt(chosen["start"])
        tag = cfg.get("study_tag")
        ics = make_ics(cfg, chosen["start"], f"{slot}-{email}")
        organiser_ics = make_ics(cfg, chosen["start"], f"org-{slot}-{email}",
                                 summary=f"{cfg['title']} – {name}",
                                 description=f"Participant: {name}\nEmail: {email}")
        ok, info = send_email(
            f"New booking: {name} – {when}",
            f"Study: {tag}\nName: {name}\nEmail: {email}\nSlot: {when}\n\n"
            "Open the attached session.ics to add this session to your calendar.\n",
            reply_to=email, tag=tag, ics=organiser_ics,
        )
        emailed = False
        if cfg.get("confirm_participant", True) and ok:
            emailed, _ = send_email(
                f"Booking confirmed: {when}",
                f"Hi {name},\n\nYou're booked for {when}."
                + (f"\nLocation: {cfg['location']}" if cfg.get("location") else "")
                + "\n\nA calendar invite is attached - please add it to your calendar."
                + "\nIf you need to change this, just reply to this email.",
                to=email, ics=ics,
            )
        if not ok:
            print(f"Booking saved but notification failed: {info}")
        st.session_state.confirmed = {"name": name, "email": email, "when": when, "emailed": emailed,
                                      "ics": ics, "gcal": gcal_link(cfg, chosen["start"])}
        st.rerun()
