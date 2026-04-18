import json
import os
import re
import smtplib
import math
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

# ── Paths ─────────────────────────────────────────────────────────────────────
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root       = os.path.dirname(_script_dir)
DATA_FILE   = os.path.join(_root, "detailed_properties.json")

# ── Config from .env ──────────────────────────────────────────────────────────
EMAIL_SENDER    = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "")

EC2_IP       = os.getenv("EC2_IP", "")
EC2_PORT     = os.getenv("EC2_PORT", "80")
EC2_PATH     = os.getenv("EC2_PATH", "/create-link")
EC2_BASE_URL = f"http://{EC2_IP}:{EC2_PORT}{EC2_PATH}"

SNAPSHOT_FILE = os.path.join(_root, os.getenv("DATA_DIR", "data"), "email_snapshot.json")
BATCH_SIZE    = int(os.getenv("EMAIL_BATCH_SIZE", "10"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_communities() -> list[dict]:
    if not os.path.exists(DATA_FILE):
        print(f"ERROR: {DATA_FILE} not found. Run scraper first.")
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_name(c: dict) -> str:
    """Get community name — scraper uses 'community_name'."""
    return c.get("community_name") or c.get("name") or "Unnamed"


def _get_unit_image(unit: dict, community: dict = None) -> str:
    """Get the best image URL for a unit — filter junk, fall back to community."""
    skip = ["lifestyle", "kitchen_", "bedroom_", "bathroom_", "garage_",
            "backyard_", "amenity", "person", "people", "family",
            "map", "creekview_desktop", "pierhouse_desktop", "/home/",
            "classicdr", "greenwich", "joytowns", "winona", "highline"]

    # Check image_url — must be a real URL, not placeholder text
    img_url = _clean(unit.get("image_url", ""))
    if img_url and img_url.startswith("http"):
        if not any(kw in img_url.lower() for kw in skip):
            return img_url

    # From property_images — prefer building/exterior
    for img in (unit.get("property_images") or []):
        u = _clean(img.get("url", ""))
        if not u or not u.startswith("http"):
            continue
        if any(kw in u.lower() for kw in skip):
            continue
        return u

    # Fallback to community thumbnail/images
    if community:
        thumb = _clean(community.get("thumbnail_url", ""))
        if thumb and thumb.startswith("http"):
            return thumb
        for img in (community.get("all_images") or []):
            u = _clean(img.get("url", ""))
            if u and u.startswith("http"):
                return u

    return ""


def _get_unit_gallery(unit: dict) -> list[str]:
    """Get all valid image URLs for a unit."""
    skip = ["lifestyle", "kitchen_", "bedroom_", "bathroom_", "garage_",
            "backyard_", "amenity", "map", "creekview_desktop",
            "pierhouse_desktop", "/home/"]
    urls, seen = [], set()
    candidates = []
    if unit.get("image_url"):
        candidates.append(unit["image_url"])
    for img in (unit.get("property_images") or []):
        if img.get("url"):
            candidates.append(img["url"])
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        if not any(kw in u.lower() for kw in skip):
            urls.append(u)
    return urls


def _email_safe_img(url: str, cid_cache: dict = None) -> str:
    """Return the image URL as-is. All formats are passed through."""
    if not url:
        return ""
    # Must be a real URL
    if not url.startswith("http"):
        return ""
    return url


def _get_community_image(c: dict) -> str:
    """Get the best hero image — thumbnail_url first, then all_images."""
    thumb = _clean(c.get("thumbnail_url", ""))
    if thumb and thumb.startswith("http"):
        return thumb
    for img in (c.get("all_images") or []):
        u = _clean(img.get("url", ""))
        if u and u.startswith("http"):
            return u
    for unit in (c.get("properties") or []):
        u = _get_unit_image(unit)
        if u:
            return u
    return ""


def _validate_thumbnails_with_vision(communities: list[dict]) -> list[dict]:
    """Use OpenAI Vision to verify each community's thumbnail is a property exterior image."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return communities

    client = OpenAI(api_key=api_key)

    for c in communities:
        name = _get_name(c)
        current_thumb = c.get("thumbnail_url") or _get_community_image(c)
        if not current_thumb:
            continue

        all_imgs = c.get("all_images") or []
        # Also collect property images as candidates
        for unit in (c.get("properties") or []):
            if unit.get("image_url") and unit["image_url"] not in [i.get("url") for i in all_imgs]:
                all_imgs.append({"url": unit["image_url"]})
            for pi in (unit.get("property_images") or []):
                if pi.get("url") and pi["url"] not in [i.get("url") for i in all_imgs]:
                    all_imgs.append(pi)

        if len(all_imgs) < 2:
            continue  # only one image, nothing to choose from

        # Pre-filter: skip junk images, prefer building exteriors
        skip_words = ["lifestyle", "kitchen", "bedroom", "bathroom", "garage",
                       "backyard", "amenity", "map", "/home/", "desktop"]
        prefer_words = ["exterior", "front", "collections", "building", "render",
                        "town", "streetscape", "facade"]

        scored = []
        for i, img in enumerate(all_imgs):
            u = _clean(img.get("url", ""))
            if not u or not u.startswith("http"):
                continue
            ul = u.lower()
            score = 0
            for kw in prefer_words:
                if kw in ul: score += 10
            for kw in skip_words:
                if kw in ul: score -= 10
            scored.append((i, img, score))

        scored.sort(key=lambda x: -x[2])
        candidates = [(s[0], s[1]) for s in scored[:6]]

        if not candidates:
            continue

        print(f"  Validating thumbnail for {name} ({len(candidates)} candidates)...")

        try:
            content = [
                {"type": "text", "text": f"""Pick the BEST thumbnail for the community "{name}".
Choose the image that shows the PROPERTY EXTERIOR — the outside of homes/townhomes/buildings.
BEST: exterior rendering, streetscape, building facade.
AVOID: interior photos (kitchens, garages, bedrooms, bathrooms), maps, floor plans, amenity photos.
Reply with JSON only: {{"index": <number>, "reason": "brief reason"}}"""}
            ]
            for i, (orig_idx, img) in enumerate(candidates):
                url = _clean(img.get("url", ""))
                if url and url.startswith("http"):
                    content.append({"type": "text", "text": f"Image {i}:"})
                    content.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": content}],
                max_tokens=100,
                temperature=0,
                timeout=30,
            )
            raw = resp.choices[0].message.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            result = json.loads(raw)
            if isinstance(result, dict) and "index" in result:
                idx = result["index"]
                reason = result.get("reason", "")
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    best_url = _clean(candidates[idx][1].get("url", ""))
                    if best_url and best_url.startswith("http"):
                        c["thumbnail_url"] = best_url
                        print(f"    ✓ Thumbnail: image {idx} — {reason}")
                    continue

        except Exception as e:
            print(f"    ✗ Vision failed for {name}: {e}")

        # Fallback: pick by URL keywords
        for img in all_imgs:
            url = (img.get("url") or "").lower()
            for kw in ["exterior", "front", "streetscape", "facade", "rendering", "elevation"]:
                if kw in url:
                    c["thumbnail_url"] = img["url"]
                    print(f"    → Fallback thumbnail by keyword: {kw}")
                    break

    return communities


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:80] or "unnamed"


def _badge(status: str) -> tuple[str, str]:
    sl = (status or "").lower()
    if "coming soon" in sl:
        return "#2d6a4f", "#ffffff"
    if "quickstart" in sl or "quick start" in sl:
        return "#1a5276", "#ffffff"
    if "pre-construction" in sl or "preconstruction" in sl:
        return "#7d3c98", "#ffffff"
    if "available" in sl or "ready" in sl:
        return "#c9a84c", "#1a1a2e"
    return "#555577", "#ffffff"


def _clean(val) -> str:
    """Return empty string for placeholder/missing values."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("not specified", "n/a", "none", "null", "inquire",
                       "inquire for pricing", "available upon request", "tbd",
                       "not available", "unknown", "0", "not mentioned"):
        return ""
    return s


def _unit_summary(unit: dict) -> str:
    """Build a compact summary line: 3 bed · 2.5 bath · 1,740 sqft."""
    parts = []
    bed = _clean(unit.get("bedrooms"))
    if bed:
        parts.append(f"{bed} bed")
    bath = _clean(unit.get("bathrooms"))
    if bath:
        parts.append(f"{bath} bath")
    sqft = _clean(unit.get("sqft"))
    if sqft:
        parts.append(f"{sqft} sqft")
    garage = _clean(unit.get("garage"))
    if garage:
        parts.append(garage)
    return " &nbsp;·&nbsp; ".join(parts)


# ── Status filter ─────────────────────────────────────────────────────────────
TARGET_KEYWORDS = ["coming soon", "quickstart", "quick start", "quick-start",
                    "pre-construction", "preconstruction", "now selling"]

def _is_target(status: str) -> bool:
    if not status:
        return False
    s = status.lower().strip()
    return any(kw in s for kw in TARGET_KEYWORDS)


def filter_target_communities(communities: list[dict]) -> list[dict]:
    """Keep only Coming Soon / QuickStart communities."""
    filtered = [c for c in communities if _is_target(c.get("status", ""))]
    print(f"  Filtered: {len(filtered)}/{len(communities)} are coming soon / quickstart")
    return filtered


# ── OpenAI enrichment ─────────────────────────────────────────────────────────
def _empty(val) -> bool:
    """Check if a value is empty / placeholder."""
    if val is None:
        return True
    if isinstance(val, str):
        s = val.strip().lower()
        return s in ("", "not specified", "n/a", "none", "null", "inquire",
                      "inquire for pricing", "available upon request", "tbd")
    if isinstance(val, list):
        return len(val) == 0
    return False


def enrich_with_openai(communities: list[dict]) -> list[dict]:
    """Use OpenAI to fill in missing fields from the data that IS present."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("  No OPENAI_API_KEY — skipping enrichment")
        return communities

    client = OpenAI(api_key=api_key)

    for c in communities:
        c_name = _get_name(c)

        # Check what's missing at community level
        missing_comm = []
        for field in ["description", "location", "builder", "price_range"]:
            if _empty(c.get(field)):
                missing_comm.append(field)
        if _empty(c.get("features")):
            missing_comm.append("features")
        if _empty(c.get("amenities")):
            missing_comm.append("amenities")

        # Check what's missing in properties
        units_need_fill = False
        for unit in (c.get("properties") or []):
            for f in ["address", "price", "bedrooms", "bathrooms", "sqft", "description"]:
                if _empty(unit.get(f)):
                    units_need_fill = True
                    break

        if not missing_comm and not units_need_fill:
            continue

        print(f"  Enriching: {c_name} (missing: {', '.join(missing_comm) if missing_comm else 'unit fields'})")

        # Build a compact version of what we DO have — strip images to save tokens
        compact = {k: v for k, v in c.items() if k not in ("all_images", "property_images")}
        if "properties" in compact:
            compact["properties"] = []
            for unit in (c.get("properties") or []):
                u = {k: v for k, v in unit.items() if k not in ("property_images", "local_image", "image_url")}
                compact["properties"].append(u)
        existing = json.dumps(compact, indent=2, ensure_ascii=False, default=str)

        prompt = f"""Here is scraped data for a real-estate community. Some fields are empty or say "Not specified".

Using ONLY the information already present in this data, fill in the missing fields where possible.
For example:
- If the description mentions "3 bedrooms" but the bedrooms field is empty, fill it in.
- If the community has a price_range but individual units don't have prices, infer from context.
- If features are listed in the description, extract them into the features array.

DO NOT invent data that cannot be inferred from what's already here.
If you cannot fill a field, leave it exactly as-is.

Return the COMPLETE community JSON with filled-in fields.

Community data:
{existing[:12000]}"""

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You fill in missing fields in real-estate JSON data using only information already present. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=4096,
                timeout=60,
            )
            raw = resp.choices[0].message.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            enriched = json.loads(raw)
            if isinstance(enriched, dict) and enriched.get("community_name") or enriched.get("name"):
                # Merge: only overwrite fields that were empty
                for field in ["description", "location", "builder", "price_range",
                              "features", "amenities", "property_types", "contact_phone",
                              "completion_date", "total_units"]:
                    if _empty(c.get(field)) and not _empty(enriched.get(field)):
                        c[field] = enriched[field]

                # Merge unit fields
                old_units = c.get("properties") or []
                new_units = enriched.get("properties") or []
                for i, unit in enumerate(old_units):
                    if i < len(new_units):
                        for f in ["address", "floorplan", "price", "status",
                                  "bedrooms", "bathrooms", "sqft", "garage",
                                  "description", "features"]:
                            if _empty(unit.get(f)) and not _empty(new_units[i].get(f)):
                                unit[f] = new_units[i][f]

                print(f"    ✓ Enriched {c_name}")
            else:
                print(f"    ✗ Bad response for {c_name}")

        except Exception as e:
            print(f"    ✗ OpenAI error for {c_name}: {e}")

    return communities


# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(communities: list[dict], batch_num: int = 1, total_batches: int = 1) -> str:
    today       = datetime.now().strftime("%B %d, %Y")
    total_units = sum(len(c.get("properties") or []) for c in communities)
    batch_label = f" · Part {batch_num}/{total_batches}" if total_batches > 1 else ""

    builders = list(dict.fromkeys(c.get("builder", "") for c in communities if c.get("builder")))
    builder_label = " · ".join(builders) if builders else "Property Monitor"
    domains = list(dict.fromkeys(c.get("source_domain", "") for c in communities if c.get("source_domain")))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{builder_label} – Listings Alert</title>
</head>
<body style="margin:0;padding:0;background:#f4f1ec;font-family:'Georgia',serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1ec;padding:40px 0;">
<tr><td align="center">

<table width="640" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:4px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr>
    <td style="background:#1a1a2e;padding:40px 48px 36px;">
      <p style="margin:0 0 6px;font-size:11px;letter-spacing:3px;color:#c9a84c;
                text-transform:uppercase;font-family:Arial,sans-serif;">
        {builder_label} · GTA{batch_label}
      </p>
      <h1 style="margin:0 0 8px;font-size:28px;color:#ffffff;
                 font-weight:normal;letter-spacing:-0.5px;">New Listings Alert</h1>
      <p style="margin:0;font-size:13px;color:#8888aa;font-family:Arial,sans-serif;">
        {today} &nbsp;·&nbsp; {len(communities)} communities &nbsp;·&nbsp; {total_units} units
      </p>
    </td>
  </tr>
"""

    for community in communities:
        c_name    = _get_name(community)
        c_loc     = _clean(community.get("location"))
        c_url     = community.get("url", "#")
        c_builder = _clean(community.get("builder"))
        c_status  = _clean(community.get("status"))
        c_slug    = slugify(c_name)
        c_phone   = _clean(community.get("contact_phone"))
        c_desc    = _clean(community.get("description"))
        c_price   = _clean(community.get("price_range"))
        units     = community.get("properties") or []

        create_url = f"{EC2_BASE_URL}?community={c_slug}" if EC2_IP else "#"
        hero_img   = _email_safe_img(_clean(community.get("thumbnail_url")) or _get_community_image(community))

        bg_status, fg_status = _badge(c_status)

        hero_html = (
            f'<img src="{hero_img}" width="640" alt="{c_name}" '
            f'style="display:block;width:100%;height:220px;object-fit:cover;">'
        ) if hero_img else ""

        html += f"""
  <!-- ══════════ COMMUNITY: {c_name} ══════════ -->
  <tr>
    <td style="border-top:4px solid #1a1a2e;">{hero_html}</td>
  </tr>

  <tr>
    <td style="padding:24px 48px 12px;background:#f9f7f3;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="vertical-align:middle;">
            <span style="display:inline-block;background:{bg_status};color:{fg_status};
                         font-size:9px;font-family:Arial,sans-serif;letter-spacing:2px;
                         text-transform:uppercase;padding:3px 9px;border-radius:2px;margin-bottom:6px;">
              {c_status}
            </span>
            <p style="margin:4px 0 3px;font-size:10px;letter-spacing:3px;color:#c9a84c;
                      text-transform:uppercase;font-family:Arial,sans-serif;">
              {"&nbsp;·&nbsp;".join(p for p in [c_builder, c_loc] if p)}
            </p>
            <h2 style="margin:0;font-size:22px;color:#1a1a2e;font-weight:normal;">
              <a href="{c_url}" style="color:#1a1a2e;text-decoration:none;">{c_name}</a>
            </h2>
          </td>
          <td style="text-align:right;vertical-align:middle;padding-left:20px;width:160px;">
            <a href="{create_url}"
               style="display:inline-block;background:#c9a84c;color:#1a1a2e;
                      font-size:11px;font-family:Arial,sans-serif;letter-spacing:1.5px;
                      text-transform:uppercase;padding:11px 18px;
                      text-decoration:none;border-radius:2px;white-space:nowrap;">
              &#128279;&nbsp; Create Link
            </a>
          </td>
        </tr>
      </table>
"""
        # Community description + price range
        if c_desc or c_price or c_phone:
            html += '      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;"><tr><td>'
            if c_desc:
                html += f'<p style="margin:0 0 6px;font-size:12px;color:#666;font-family:Arial,sans-serif;line-height:1.5;">{c_desc[:300]}</p>'
            if c_price:
                html += f'<p style="margin:0 0 4px;font-size:14px;color:#1a1a2e;font-weight:bold;font-family:Arial,sans-serif;">{c_price}</p>'
            if c_phone:
                html += f'<p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;">Contact: {c_phone}</p>'
            html += '</td></tr></table>'

        html += """
    </td>
  </tr>
"""

        # Build a pool of community images for property cards
        comm_img_pool = []
        for img in (community.get("all_images") or []):
            u = (img.get("url") or "").lower()
            skip_kw = ["map", "lifestyle", "/home/", "creekview_desktop", "pierhouse_desktop"]
            if img.get("url") and not any(kw in u for kw in skip_kw):
                comm_img_pool.append(img["url"])

        # ── Individual units ──────────────────────────────────────────────
        for idx, unit in enumerate(units):
            u_address   = _clean(unit.get("address"))
            u_floorplan = _clean(unit.get("floorplan"))
            u_price     = _clean(unit.get("price"))
            u_status    = _clean(unit.get("status"))
            u_desc      = _clean(unit.get("description")).replace("\n", " · ").strip(" · ")
            u_img       = _email_safe_img(_get_unit_image(unit, community))
            # If no unit-specific image, pick from community pool (rotate)
            if not u_img and comm_img_pool:
                u_img = _email_safe_img(comm_img_pool[idx % len(comm_img_pool)])
            u_gallery   = _get_unit_gallery(unit)
            u_summary   = _unit_summary(unit)

            # Skip units with no meaningful data at all
            if not u_address and not u_floorplan and not u_price:
                continue

            bg, fg = _badge(u_status)

            # Main image
            u_img_html = (
                f'<img src="{u_img}" width="496" alt="{u_address}" '
                f'style="display:block;width:100%;max-height:200px;'
                f'object-fit:cover;border-radius:2px 2px 0 0;">'
            ) if u_img else ""

            # Thumbnail strip (up to 4 extra images)
            extra_imgs = [_email_safe_img(g) for g in u_gallery if _email_safe_img(g)][:4]
            thumb_html = ""
            if extra_imgs:
                thumbs = ""
                for ti in extra_imgs:
                    thumbs += (
                        f'<td style="padding:2px;">'
                        f'<img src="{ti}" width="118" height="80" alt="" '
                        f'style="display:block;width:118px;height:80px;object-fit:cover;border-radius:2px;">'
                        f'</td>'
                    )
                thumb_html = (
                    f'<table width="100%" cellpadding="0" cellspacing="0" '
                    f'style="background:#f5f3ef;"><tr>{thumbs}</tr></table>'
                )

            bottom_pad = "16px" if idx < len(units) - 1 else "4px"

            html += f"""
  <tr>
    <td style="padding:4px 48px {bottom_pad};background:#f9f7f3;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:3px;
                    border:1px solid #e8e4de;overflow:hidden;">
        <tr><td style="padding:0;">{u_img_html}</td></tr>
        {"<tr><td style='padding:0;'>" + thumb_html + "</td></tr>" if thumb_html else ""}
        <tr>
          <td style="padding:16px 20px 18px;">
            {"<span style=" + '"' + "display:inline-block;background:" + bg + ";color:" + fg + ";font-size:9px;font-family:Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;padding:3px 9px;border-radius:2px;" + '"' + ">" + u_status + "</span>" if u_status else ""}
            {"<h3 style=" + '"' + "margin:10px 0 2px;font-size:15px;color:#1a1a2e;font-weight:normal;" + '"' + ">" + u_address + "</h3>" if u_address else ""}
            {"<p style='margin:0 0 4px;font-size:11px;color:#999;font-family:Arial,sans-serif;'>" + u_floorplan + "</p>" if u_floorplan else ""}
            {"<p style='margin:0 0 4px;font-size:11px;color:#777;font-family:Arial,sans-serif;'>" + u_summary + "</p>" if u_summary else ""}
            {"<p style='margin:0 0 8px;font-size:12px;color:#666;font-family:Arial,sans-serif;line-height:1.5;'>" + u_desc[:200] + "</p>" if u_desc else ""}
            {"<p style='margin:0;font-size:20px;font-weight:bold;color:#1a1a2e;font-family:Arial,sans-serif;'>" + u_price + "</p>" if u_price else ""}
          </td>
        </tr>
      </table>
    </td>
  </tr>
"""

        html += """  <tr><td style="height:32px;background:#f9f7f3;"></td></tr>\n"""

    # ── Footer ────────────────────────────────────────────────────────────
    html += f"""
  <tr>
    <td style="background:#1a1a2e;padding:28px 48px;text-align:center;">
      <p style="margin:0;font-size:11px;color:#666688;
                 font-family:Arial,sans-serif;line-height:1.8;">
        Generated by {builder_label} Property Monitor<br>
        {"&nbsp;·&nbsp;".join(f'<a href="https://{d}" style="color:#c9a84c;text-decoration:none;">{d}</a>' for d in domains)}
      </p>
    </td>
  </tr>

</table>
</td></tr></table>
</body>
</html>"""

    return html


# ── Sending ───────────────────────────────────────────────────────────────────
def _send_batch(communities: list[dict], batch_num: int, total_batches: int, total_c: int):
    html_body   = build_html(communities, batch_num, total_batches)
    batch_label = f" (Part {batch_num}/{total_batches})" if total_batches > 1 else ""
    total_units = sum(len(c.get("properties") or []) for c in communities)
    builders      = list(dict.fromkeys(c.get("builder", "") for c in communities if c.get("builder")))
    builder_label = " · ".join(builders) if builders else "Property Monitor"

    subject = (
        f"{builder_label} – {total_c} Communities, "
        f"{total_units} Units{batch_label} · "
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
    print(f"  ✓ Batch {batch_num}/{total_batches} — {len(communities)} communities, {size_kb:.0f} KB")


def send_email(communities: list[dict]) -> bool:
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        print("ERROR: Set EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT in .env")
        return False
    if not EC2_IP:
        print("WARNING: EC2_IP not set — 'Create Link' buttons will point to '#'")
    if not communities:
        print("No communities to send.")
        return False

    total_batches = math.ceil(len(communities) / BATCH_SIZE)
    total_units   = sum(len(c.get("properties") or []) for c in communities)
    print(
        f"Sending {len(communities)} communities ({total_units} units) "
        f"in {total_batches} batch(es) → {EMAIL_RECIPIENT}"
    )

    try:
        for i in range(total_batches):
            batch = communities[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
            _send_batch(batch, i + 1, total_batches, len(communities))
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


# ── Change detection ──────────────────────────────────────────────────────────
def _make_snapshot(communities: list[dict]) -> dict:
    snap = {}
    for c in communities:
        c_name = _get_name(c)
        for unit in (c.get("properties") or []):
            key = f"{c_name}|{unit.get('address', '')}"
            snap[key] = f"{unit.get('price', '')}|{unit.get('status', '')}"
    return snap


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
    for key, val in new.items():
        community, address = key.split("|", 1)
        if key not in old:
            changes.append(f"NEW      [{community}]  {address}")
        elif old[key] != val:
            changes.append(f"UPDATED  [{community}]  {address}  →  {val}")
    return changes


# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    communities = load_communities()
    if not communities:
        return

    total_units = sum(len(c.get("properties") or []) for c in communities)
    print(f"Loaded {len(communities)} communities / {total_units} units from {DATA_FILE}")

    # Filter: only coming soon / quickstart
    communities = filter_target_communities(communities)
    if not communities:
        print("No coming soon / quickstart communities found — nothing to send.")
        return

    # Enrich missing fields with OpenAI
    print("Enriching missing data with OpenAI...")
    communities = enrich_with_openai(communities)

    # Validate thumbnails with OpenAI Vision
    print("Validating thumbnails with Vision...")
    communities = _validate_thumbnails_with_vision(communities)

    total_units = sum(len(c.get("properties") or []) for c in communities)
    print(f"Sending {len(communities)} communities / {total_units} units")

    new_snap = _make_snapshot(communities)
    old_snap = _load_snapshot()
    changes  = _detect_changes(old_snap, new_snap)

    if changes:
        print(f"{len(changes)} change(s) detected:")
        for ch in changes[:20]:
            print(f"  · {ch}")
        if len(changes) > 20:
            print(f"  … and {len(changes) - 20} more")
    else:
        print("First run — sending all communities.")

    success = send_email(communities)
    if success:
        _save_snapshot(new_snap)
        print("Snapshot saved.")


if __name__ == "__main__":
    run()
