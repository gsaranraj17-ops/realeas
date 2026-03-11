import json
import os
import smtplib
import math
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Paths ────────────────────────────────────────────────────────────────────
_root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE   = os.path.join( "detailed_properties.json")

# ── Config from .env ─────────────────────────────────────────────────────────
EMAIL_SENDER    = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "")

# EC2 endpoint — set EC2_IP and optionally EC2_PORT / EC2_PATH in .env
EC2_IP       = os.getenv("EC2_IP", "")          # e.g. 54.123.45.67
EC2_PORT     = os.getenv("EC2_PORT", "80")
EC2_PATH     = os.getenv("EC2_PATH", "/create-link")   # e.g. /create-link
EC2_BASE_URL = f"http://{EC2_IP}:{EC2_PORT}{EC2_PATH}"

SNAPSHOT_FILE = os.path.join(_root, os.getenv("DATA_DIR", "data"), "email_snapshot.json")

# Max properties per email — splits into batches to stay under Gmail's 25 MB limit
BATCH_SIZE = int(os.getenv("EMAIL_BATCH_SIZE", "30"))


# ── Data loading ──────────────────────────────────────────────────────────────
def load_properties() -> list[dict]:
    """Load the flat property list written by realtor_scraper.py."""
    if not os.path.exists(DATA_FILE):
        print(f"ERROR: {DATA_FILE} not found. Run scraper first.")
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(properties: list[dict], batch_num: int = 1, total_batches: int = 1) -> str:
    """
    Builds the HTML email body using only remote image URLs (no attachments).
    Keeping images as <img src="..."> tags means the email stays tiny — each
    image loads directly from the source website when the recipient opens the
    email, so 116 properties is no problem at all.
    Returns html_string only.
    """
    today        = datetime.now().strftime("%B %d, %Y")
    active_props = [p for p in properties if p.get("is_active", True)]
    batch_label  = f" · Part {batch_num}/{total_batches}" if total_batches > 1 else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Property Intelligence – Listings Alert</title>
</head>
<body style="margin:0;padding:0;background:#f4f1ec;font-family:'Georgia',serif;">

<!-- WRAPPER -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1ec;padding:40px 0;">
<tr><td align="center">

<!-- CARD -->
<table width="640" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:4px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr>
    <td style="background:#1a1a2e;padding:40px 48px 36px;">
      <p style="margin:0 0 6px;font-size:11px;letter-spacing:3px;color:#c9a84c;
                text-transform:uppercase;font-family:Arial,sans-serif;">
        Property Intelligence · GTA{batch_label}
      </p>
      <h1 style="margin:0 0 8px;font-size:28px;color:#ffffff;
                 font-weight:normal;letter-spacing:-0.5px;">
        New Listings Alert
      </h1>
      <p style="margin:0;font-size:13px;color:#8888aa;font-family:Arial,sans-serif;">
        {today} &nbsp;·&nbsp; {len(active_props)} properties
      </p>
    </td>
  </tr>
"""

    # ── Group by source_domain for visual separation ──────────────────────────
    grouped: dict[str, list[dict]] = {}
    for p in active_props:
        domain = p.get("source_domain", "unknown")
        grouped.setdefault(domain, []).append(p)

    for domain, props in grouped.items():
        html += f"""
  <!-- DOMAIN HEADER -->
  <tr>
    <td style="padding:32px 48px 4px;border-top:3px solid #1a1a2e;">
      <p style="margin:0 0 2px;font-size:10px;letter-spacing:3px;color:#c9a84c;
                text-transform:uppercase;font-family:Arial,sans-serif;">
        Source · {domain}
      </p>
    </td>
  </tr>
"""

        for prop in props:
            prop_id    = prop.get("id", "")
            name       = prop.get("name", "Unnamed Property")
            location   = prop.get("location", "")
            price      = prop.get("price", "Price on Request")
            status     = prop.get("status", "")
            prop_type  = prop.get("property_type", "")
            builder    = prop.get("builder", "")
            desc       = (prop.get("description") or "").replace("\n", " ").strip()
            prop_url   = prop.get("url", "#")

            # Image — remote URL only (no attachments = tiny email size)
            # Prefer image_url from scraper; skip entirely if not available
            img_url    = prop.get("image_url", "")
            img_html   = ""
            if img_url:
                img_html = (
                    f'<img src="{img_url}" width="544" alt="{name}" '
                    f'style="display:block;width:100%;max-height:260px;'
                    f'object-fit:cover;border-radius:2px 2px 0 0;">'
                )

            # Status badge colour
            sl = status.lower()
            if "coming soon" in sl or "launching" in sl:
                badge_bg, badge_fg = "#2d6a4f", "#ffffff"
            elif "ready" in sl or "available" in sl:
                badge_bg, badge_fg = "#c9a84c", "#1a1a2e"
            elif "sold" in sl:
                badge_bg, badge_fg = "#8b0000", "#ffffff"
            else:
                badge_bg, badge_fg = "#555577", "#ffffff"

            # EC2 "Create Link" URL — GET request so it works as a plain href
            create_link_url = f"{EC2_BASE_URL}?property_id={prop_id}" if EC2_IP else "#"

            html += f"""
  <!-- PROPERTY CARD  id={prop_id} -->
  <tr>
    <td style="padding:16px 48px 24px;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="background:#f9f7f3;border-radius:3px;overflow:hidden;">

        <!-- Image row -->
        <tr><td style="padding:0;">{img_html}</td></tr>

        <!-- Details row -->
        <tr>
          <td style="padding:20px 24px 22px;">

            <!-- Status badge -->
            <span style="display:inline-block;background:{badge_bg};color:{badge_fg};
                         font-size:10px;font-family:Arial,sans-serif;letter-spacing:2px;
                         text-transform:uppercase;padding:4px 10px;border-radius:2px;">
              {status or "N/A"}
            </span>

            <!-- Name -->
            <h3 style="margin:12px 0 3px;font-size:17px;color:#1a1a2e;
                        font-weight:normal;letter-spacing:0.3px;">
              {name}
            </h3>

            <!-- Location / type / builder meta -->
            <p style="margin:0 0 8px;font-size:12px;color:#888888;
                       font-family:Arial,sans-serif;">
              {" &nbsp;·&nbsp; ".join(filter(None, [location, prop_type, builder]))}
            </p>

            <!-- Price -->
            <p style="margin:0 0 10px;font-size:22px;color:#1a1a2e;
                       font-weight:bold;font-family:Arial,sans-serif;">
              {price or "Price on Request"}
            </p>

            <!-- Description -->
            {"<p style='margin:0 0 16px;font-size:13px;color:#555555;font-family:Arial,sans-serif;line-height:1.5;'>" + desc[:280] + ("…" if len(desc) > 280 else "") + "</p>" if desc else ""}

            <!-- CTA buttons -->
            <table cellpadding="0" cellspacing="0">
              <tr>
                <!-- View Listing -->
                <td style="padding-right:10px;">
                  <a href="{prop_url}"
                     style="display:inline-block;background:#1a1a2e;color:#c9a84c;
                            font-size:11px;font-family:Arial,sans-serif;letter-spacing:2px;
                            text-transform:uppercase;padding:10px 20px;
                            text-decoration:none;border-radius:2px;">
                    View Listing →
                  </a>
                </td>

                <!-- Create Link (sends property_id to EC2) -->
                <td>
                  <a href="{create_link_url}"
                     style="display:inline-block;background:#c9a84c;color:#1a1a2e;
                            font-size:11px;font-family:Arial,sans-serif;letter-spacing:2px;
                            text-transform:uppercase;padding:10px 20px;
                            text-decoration:none;border-radius:2px;">
                    &#128279; Create Link
                  </a>
                </td>
              </tr>
            </table>

            <!-- Tiny property ID for reference -->
            <p style="margin:14px 0 0;font-size:10px;color:#aaaaaa;
                       font-family:Arial,sans-serif;">
              Property ID: {prop_id}
            </p>

          </td>
        </tr>
      </table>
    </td>
  </tr>
"""

    html += f"""
  <!-- FOOTER -->
  <tr>
    <td style="background:#1a1a2e;padding:28px 48px;text-align:center;">
      <p style="margin:0;font-size:11px;color:#666688;
                 font-family:Arial,sans-serif;line-height:1.7;">
        Generated automatically by the Property Intelligence Agent.<br>
        "Create Link" buttons call
        <span style="color:#c9a84c;">{EC2_BASE_URL}?property_id=&lt;id&gt;</span>
      </p>
    </td>
  </tr>

</table></td></tr></table>
</body>
</html>"""

    return html


# ── Email sending ─────────────────────────────────────────────────────────────
def _send_one_batch(
    properties: list[dict],
    batch_num: int,
    total_batches: int,
    total_active: int,
) -> bool:
    """Build and send a single batch email."""
    html_body = build_html(properties, batch_num, total_batches)

    batch_label = f" (Part {batch_num}/{total_batches})" if total_batches > 1 else ""
    subject = (
        f"Property Alert – {total_active} listings{batch_label} · "
        f"{datetime.now().strftime('%b %d, %Y')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_bytes())

    size_kb = len(msg.as_bytes()) / 1024
    print(f"  ✓ Batch {batch_num}/{total_batches} sent — {len(properties)} props, {size_kb:.0f} KB")


def send_email(properties: list[dict]) -> bool:
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        print("ERROR: Set EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT in .env")
        return False
    if not EC2_IP:
        print("WARNING: EC2_IP not set in .env — 'Create Link' buttons will be inactive (#)")

    active = [p for p in properties if p.get("is_active", True)]
    if not active:
        print("No active properties to send.")
        return False

    # ── Split into batches so each email stays well under Gmail's 25 MB cap ──
    total_batches = math.ceil(len(active) / BATCH_SIZE)
    print(
        f"Sending {len(active)} properties in {total_batches} batch(es) "
        f"of up to {BATCH_SIZE} each → {EMAIL_RECIPIENT}"
    )

    try:
        for i in range(total_batches):
            batch = active[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
            _send_one_batch(batch, i + 1, total_batches, len(active))
        return True
    except Exception as e:
        print(f"✗ Failed to send: {e}")
        return False


# ── Change detection (snapshot) ───────────────────────────────────────────────
def _make_snapshot(properties: list[dict]) -> dict:
    return {
        str(p.get("id", "")): f"{p.get('price','')}|{p.get('status','')}|{p.get('name','')}"
        for p in properties
    }

def _load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_snapshot(snap: dict):
    os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)

def _detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    for pid, val in new.items():
        if pid not in old:
            changes.append(f"NEW  id={pid}  {val.split('|')[2]}")
        elif old[pid] != val:
            changes.append(f"UPDATED  id={pid}  {val}")
    return changes


# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    properties = load_properties()
    if not properties:
        return

    print(f"Loaded {len(properties)} properties from {DATA_FILE}")

    new_snap = _make_snapshot(properties)
    old_snap = _load_snapshot()
    changes  = _detect_changes(old_snap, new_snap)
    '''
    if not changes and old_snap:
        print("No changes detected — email NOT sent.")
        return
    '''
    if not changes:
        print(f"{len(changes)} change(s) detected:")
        for c in changes:
            print(f"  · {c}")
    else:
        print("First run — sending all current properties.")

    success = send_email(properties)
    if success:
        _save_snapshot(new_snap)
        print("Snapshot saved.")


if __name__ == "__main__":
    run()