"""
Team La'Casa – Property Landing Page Server
============================================
pip install flask python-dotenv openai

Structure modeled after mycreekviewcollectivetowns.com
Theme: Dark navy (#0a1628) + white text + light blue accents
"""

import json, os, re, smtplib, uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string, send_file, abort
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app    = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

DATA_FILE     = os.getenv("DATA_FILE",  "detailed_properties.json")
LEADS_FILE    = os.getenv("LEADS_FILE", "leads.json")
ADMIN_TOKEN   = os.getenv("ADMIN_TOKEN", "changeme")
CONFIRMED_DIR = os.getenv("CONFIRMED_DIR", "confirmed_pages")
UPLOAD_DIR    = os.getenv("UPLOAD_DIR",    "uploads")
EMAIL_SENDER  = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD= os.getenv("EMAIL_PASSWORD", "")
NOTIFY_EMAIL  = os.getenv("NOTIFY_EMAIL", EMAIL_SENDER)

os.makedirs(CONFIRMED_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify(t):
    t = (t or "").lower().strip()
    t = re.sub(r"[^\w\s-]", "", t)
    return re.sub(r"[\s_]+", "-", t)[:80] or "unnamed"

def load_communities():
    if not os.path.exists(DATA_FILE): return []
    with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)

def _name(c): return c.get("community_name") or c.get("name") or "Unnamed"

def _clean(v):
    if v is None: return ""
    s = str(v).strip()
    return "" if s.lower() in ("not specified","n/a","none","null","inquire",
        "inquire for pricing","available upon request","tbd","not available",
        "unknown","not mentioned") else s

def _hero_img(c):
    # Use thumbnail_url first (vision-picked property exterior)
    thumb = _clean(c.get("thumbnail_url"))
    if thumb and thumb.startswith("http"):
        return thumb
    for i in (c.get("all_images") or []):
        if i.get("url"): return i["url"]
    for u in (c.get("properties") or []):
        if u.get("image_url"): return u["image_url"]
        for i in (u.get("property_images") or []):
            if i.get("url"): return i["url"]
    return ""

def _unit_img(u, community=None):
    img = _clean(u.get("image_url", ""))
    if img and img.startswith("http"):
        return img
    for i in (u.get("property_images") or []):
        url = _clean(i.get("url", ""))
        if url and url.startswith("http"):
            return url
    # Fall back to community images
    if community:
        thumb = _clean(community.get("thumbnail_url", ""))
        if thumb and thumb.startswith("http"):
            return thumb
        for i in (community.get("all_images") or []):
            url = _clean(i.get("url", ""))
            if url and url.startswith("http"):
                return url
    return ""

def _gallery(c):
    urls, seen = [], set()
    for i in (c.get("all_images") or []):
        u = i.get("url","")
        if u and u not in seen: seen.add(u); urls.append(u)
    for unit in (c.get("properties") or []):
        for i in (unit.get("property_images") or []):
            u = i.get("url","")
            if u and u not in seen: seen.add(u); urls.append(u)
    return urls


def _categorize_images(images: list[str], name: str) -> dict:
    """Use OpenAI to categorize images into exterior, interior, amenity groups."""
    if not images or len(images) < 2:
        return {"exterior": images, "interior": [], "amenity": []}

    entries = [f"{i}: {url}" for i, url in enumerate(images)]

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Categorize real-estate images by URL. Return JSON only."},
                {"role": "user", "content": f"""Categorize these images for "{name}" into groups based on their URL.

EXTERIOR: images showing outside of homes, buildings, streetscape, renderings, front views, collections
INTERIOR: images showing kitchens, bedrooms, bathrooms, living rooms, garages, indoor spaces
AMENITY: images showing parks, trails, outdoors, backyards, lifestyle, community spaces, nature

{chr(10).join(entries)}

Return JSON: {{"exterior": [indices], "interior": [indices], "amenity": [indices]}}"""}
            ],
            max_tokens=300, temperature=0, timeout=20,
        )
        raw = r.choices[0].message.content.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
        result = json.loads(raw)
        cats = {}
        for cat in ["exterior", "interior", "amenity"]:
            indices = result.get(cat, [])
            cats[cat] = [images[i] for i in indices if isinstance(i, int) and 0 <= i < len(images)]
        return cats
    except Exception:
        # Fallback: categorize by URL keywords
        cats = {"exterior": [], "interior": [], "amenity": []}
        for url in images:
            ul = url.lower()
            if any(kw in ul for kw in ["exterior", "front", "collections", "building", "render", "town", "streetscape"]):
                cats["exterior"].append(url)
            elif any(kw in ul for kw in ["kitchen", "bedroom", "bathroom", "garage", "ensuite", "living"]):
                cats["interior"].append(url)
            elif any(kw in ul for kw in ["outdoor", "backyard", "park", "trail", "lifestyle", "community", "nature"]):
                cats["amenity"].append(url)
            else:
                cats["exterior"].append(url)
        return cats

def _desc(c):
    """Generate a detailed marketing description using OpenAI."""
    name = _name(c)
    loc = _clean(c.get("location"))
    builder = _clean(c.get("builder")) or "the developer"
    status = _clean(c.get("status"))
    price_range = _clean(c.get("price_range"))
    existing = _clean(c.get("description", ""))
    features = c.get("features") or []
    amenities = c.get("amenities") or []
    prop_types = c.get("property_types") or []
    units = c.get("properties") or []
    contact = _clean(c.get("contact_phone"))

    unit_lines = []
    for u in units[:6]:
        parts = []
        if _clean(u.get("address")): parts.append(_clean(u.get("address")))
        if _clean(u.get("floorplan")): parts.append(_clean(u.get("floorplan")))
        if _clean(u.get("price")): parts.append(_clean(u.get("price")))
        if _clean(u.get("bedrooms")): parts.append(f"{_clean(u.get('bedrooms'))} bed")
        if _clean(u.get("bathrooms")): parts.append(f"{_clean(u.get('bathrooms'))} bath")
        if _clean(u.get("sqft")): parts.append(f"{_clean(u.get('sqft'))} sqft")
        if parts:
            unit_lines.append(" | ".join(parts))

    prompt = f"""You are a luxury real-estate copywriter. Write a LONG, detailed, compelling marketing description
for this community. The description MUST be 5-6 paragraphs, approximately 500-600 words minimum.

Paragraph 1: Introduce the community — its name, location, the developer, and what makes it unique.
Paint a vivid picture of the lifestyle, the neighbourhood character, and the feeling of living here.

Paragraph 2: Describe the homes in detail — types available, sizes, number of bedrooms and bathrooms,
square footage ranges, garage options, and architectural style. Be specific with every data point provided.

Paragraph 3: Detail the features and finishes — kitchen finishes, flooring, smart home technology,
bathroom upgrades, appliances, and any included upgrades or incentives. Make the reader feel the quality.

Paragraph 4: Highlight the location advantages extensively — nearby highways and transit (with drive times),
schools, hospitals, shopping centres, parks, conservation areas, golf courses, and entertainment.
Describe the commute convenience and weekend lifestyle possibilities.

Paragraph 5: Discuss the investment value — why this community is a smart purchase, the growth of the area,
the reputation of the developer, and the long-term value proposition.

Paragraph 6: End with a strong call to action — why register now, VIP pricing, priority access to floor plans,
limited availability, and how to get in touch. Create urgency.

IMPORTANT: Write at least 500 words. Be detailed and specific. Use flowing prose, no headings, no bullet points.
Include ALL specific details from the data below. Do not invent facts not in the data, but elaborate on what is given.

Community: {name}
Location: {loc}
Developer: {builder}
Status: {status}
Price Range: {price_range or 'Contact for pricing'}
Home Types: {', '.join(prop_types) if prop_types else 'Various'}
Features: {', '.join(features[:10]) if features else 'Modern finishes included'}
Amenities: {', '.join(amenities[:8]) if amenities else 'Community amenities'}
Contact: {contact}
Existing description: {existing}

Available units:
{chr(10).join(unit_lines) if unit_lines else 'Details coming soon'}

Output only the paragraphs, separated by blank lines. No headings. Minimum 500 words."""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a luxury real-estate copywriter. You ALWAYS write detailed, long-form content. Your descriptions are NEVER shorter than 500 words. You write in flowing paragraphs with rich detail."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=2000,
            temperature=0.7,
            timeout=60,
        )
        result = r.choices[0].message.content.strip()
        if len(result) > 100:
            return result
    except Exception as e:
        app.logger.warning("OpenAI description failed: %s", e)

    # Fallback: build a detailed description from available data
    parts = []
    if existing and len(existing) > 30:
        parts.append(existing)
    else:
        parts.append(
            f"{name} is an exciting new community by {builder} in {loc}. "
            f"This {status.lower() if status else 'upcoming'} development offers "
            f"{', '.join(prop_types).lower() if prop_types else 'modern homes'} "
            f"designed for today's lifestyle."
        )
    if price_range:
        parts.append(f"Pricing starts {price_range}.")
    if prop_types:
        parts.append(f"Home types include {', '.join(prop_types)}.")
    if features:
        parts.append(f"Standard features include {', '.join(features[:6])}.")
    if unit_lines:
        parts.append("Available homes: " + "; ".join(unit_lines[:4]) + ".")
    parts.append(f"Register now for priority access, VIP pricing, and exclusive floor plans for {name}.")
    if contact:
        parts.append(f"Contact us at {contact} for more information.")
    return "\n\n".join(parts)

def save_lead(lead):
    leads = []
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE,"r",encoding="utf-8") as f: leads = json.load(f)
    leads.append(lead)
    with open(LEADS_FILE,"w",encoding="utf-8") as f: json.dump(leads,f,indent=2,default=str)

def _email(subject, body):
    if not all([EMAIL_SENDER,EMAIL_PASSWORD,NOTIFY_EMAIL]): return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"],msg["From"],msg["To"] = subject,EMAIL_SENDER,NOTIFY_EMAIL
        msg.attach(MIMEText(body,"html","utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_SENDER,EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER,NOTIFY_EMAIL,msg.as_bytes())
    except Exception as e: app.logger.error("Email failed: %s",e)

# ── HTML Template ─────────────────────────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ name }} – {{ builder }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#101d42;--navy2:#152452;--navy3:#1a2d5e;
  --accent:#c8d8ea;--accent2:#9bb5d0;
  --white:#ffffff;--offwhite:#f4f7fa;--light:#e4e9f0;
  --text:#1a202c;--muted:#64748b;
}
html{scroll-behavior:smooth}
body{font-family:'Inter',sans-serif;color:var(--text);background:var(--white)}

/* ═══ TOOLBAR ═══ */
.toolbar{position:fixed;top:0;left:0;right:0;z-index:1000;
  background:var(--navy);padding:10px 32px;
  display:flex;align-items:center;justify-content:space-between;
  box-shadow:0 2px 20px rgba(0,0,0,.3)}
.tb-brand{font-family:'Playfair Display',serif;font-size:18px;color:var(--white);font-weight:600;letter-spacing:1px}
.tb-status{font-size:10px;letter-spacing:2px;text-transform:uppercase;
  padding:4px 14px;border-radius:20px;font-weight:600;margin-left:16px}
.tb-status.draft{background:rgba(197,213,228,.15);color:var(--accent);border:1px solid rgba(197,213,228,.3)}
.tb-status.editing{background:rgba(72,187,120,.15);color:#48bb78;border:1px solid rgba(72,187,120,.3)}
.tb-status.saved{background:rgba(72,187,120,.3);color:#9ae6b4;border:1px solid rgba(72,187,120,.5)}
.tb-right{display:flex;gap:10px;align-items:center}
.tb-btn{font-family:'Inter',sans-serif;font-size:10px;font-weight:600;
  letter-spacing:2px;text-transform:uppercase;padding:9px 20px;
  border-radius:4px;cursor:pointer;border:none;transition:all .2s}
.tb-btn-outline{background:transparent;color:var(--white);border:1px solid rgba(255,255,255,.25)}
.tb-btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.tb-btn-outline.active{background:rgba(72,187,120,.15);border-color:#48bb78;color:#48bb78}
.tb-btn-primary{background:var(--accent2);color:var(--navy)}
.tb-btn-primary:hover{background:var(--accent);transform:translateY(-1px)}
.tb-btn-primary:disabled{opacity:.4;cursor:not-allowed}
body.has-toolbar{padding-top:52px}

/* Editable highlights */
body.edit-mode [data-editable]{outline:2px dashed rgba(197,213,228,.5);outline-offset:3px;cursor:text;border-radius:2px}
body.edit-mode [data-editable]:hover{outline-color:var(--accent2);background:rgba(197,213,228,.06)}
body.edit-mode [data-editable]:focus{outline-color:#48bb78;outline-style:solid}
body.edit-mode [data-editable-img]{position:relative;cursor:pointer}
body.edit-mode [data-editable-img]::after{content:'✎ Change';position:absolute;bottom:8px;right:8px;
  background:rgba(10,22,40,.85);color:var(--accent);font-size:10px;letter-spacing:1px;
  padding:4px 10px;border-radius:3px;opacity:0;transition:opacity .3s;pointer-events:none}
body.edit-mode [data-editable-img]:hover::after{opacity:1}

/* ═══ TOP BANNER ═══ */
.top-banner{background:var(--navy2);text-align:center;padding:14px 20px;position:relative;z-index:1}
.top-banner p{font-size:12px;color:var(--accent);letter-spacing:1px;font-weight:500}
.top-banner a{color:var(--white);text-decoration:underline;font-weight:600;margin-left:8px}

/* ═══ HERO ═══ */
.hero{position:relative;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
.hero-bg{position:fixed;top:0;left:0;right:0;height:100vh;background-size:cover;background-position:center;z-index:-1}
.hero-overlay{position:fixed;top:0;left:0;right:0;height:100vh;
  background:linear-gradient(180deg,rgba(10,22,40,.5) 0%,rgba(10,22,40,.75) 100%);z-index:-1}
.hero-content{position:relative;z-index:2;text-align:center;max-width:800px;padding:60px 24px;
  animation:fadeUp .8s ease-out both}
@keyframes fadeUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:none}}
.hero-badge{display:inline-block;font-size:11px;letter-spacing:3px;text-transform:uppercase;
  color:var(--accent);border:1px solid rgba(197,213,228,.3);padding:6px 20px;border-radius:30px;
  margin-bottom:24px;font-weight:600}
.hero h1{font-family:'Playfair Display',serif;font-size:clamp(36px,6vw,64px);color:var(--white);
  font-weight:700;line-height:1.1;margin-bottom:20px}
.hero h1 em{font-style:italic;color:var(--accent)}
.hero-sub{font-size:16px;color:rgba(255,255,255,.7);line-height:1.7;margin-bottom:32px;max-width:600px;margin-left:auto;margin-right:auto}
.hero-prices{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin-bottom:36px}
.hero-price-tag{background:rgba(255,255,255,.1);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.15);
  padding:12px 24px;border-radius:6px;text-align:center}
.hero-price-tag .label{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:4px}
.hero-price-tag .value{font-family:'Playfair Display',serif;font-size:22px;color:var(--white);font-weight:600}
.hero-cta{display:inline-block;background:var(--white);color:var(--navy);font-size:12px;font-weight:700;
  letter-spacing:3px;text-transform:uppercase;padding:16px 40px;border-radius:4px;text-decoration:none;
  transition:all .2s;border:none;cursor:pointer}
.hero-cta:hover{background:var(--accent);transform:translateY(-2px);box-shadow:0 8px 30px rgba(0,0,0,.3)}

/* ═══ SECTION COMMON ═══ */
.section{padding:80px 24px;position:relative;z-index:1}
.section-dark{background:var(--navy);color:var(--white)}
.section-light{background:var(--offwhite)}
.section-white{background:var(--white)}
.wrap{max-width:1100px;margin:0 auto}
.section-label{font-size:11px;letter-spacing:4px;text-transform:uppercase;color:var(--accent2);font-weight:600;margin-bottom:10px}
.section-dark .section-label{color:var(--accent)}
.section-title{font-family:'Playfair Display',serif;font-size:clamp(28px,4vw,42px);font-weight:700;line-height:1.15;margin-bottom:20px}
.section-dark .section-title{color:var(--white)}

/* ═══ ABOUT / DESCRIPTION ═══ */
.about-section{max-width:780px;margin:0 auto;position:relative}
.about-card{background:var(--white);border-radius:12px;padding:48px 44px;
  box-shadow:0 4px 30px rgba(16,29,66,.08);border:1px solid rgba(16,29,66,.06);
  position:relative;overflow:hidden}
.about-card::before{content:'';position:absolute;top:0;left:0;right:0;height:4px;
  background:linear-gradient(90deg,var(--navy) 0%,var(--accent2) 100%)}
.about-card .section-label{text-align:center}
.about-card .section-title{text-align:center;margin-bottom:24px}
.about-card .about-text{font-size:15px;color:#4a5568;line-height:2;text-align:center}
.about-card .about-text p{margin-bottom:16px}
.about-card .about-text p:last-child{margin-bottom:0}
.about-card .about-divider{width:60px;height:2px;background:var(--accent2);margin:0 auto 24px;border-radius:2px}

/* ═══ QUICK FACTS ═══ */
.facts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px;margin-top:32px}
.fact-card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:20px 24px}
.fact-label{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:6px;font-weight:600}
.fact-value{font-size:15px;color:var(--white);line-height:1.6}

/* ═══ GALLERY ═══ */
.gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-top:28px}
.gallery-grid img{width:100%;height:220px;object-fit:cover;border-radius:4px;cursor:zoom-in;
  transition:transform .3s,box-shadow .3s}
.gallery-grid img:hover{transform:scale(1.02);box-shadow:0 8px 24px rgba(0,0,0,.15)}

/* ═══ UNITS ═══ */
.units-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;margin-top:32px}
.unit-card{background:var(--white);border-radius:6px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06);
  transition:transform .3s,box-shadow .3s}
.unit-card:hover{transform:translateY(-4px);box-shadow:0 12px 36px rgba(0,0,0,.12)}
.unit-img{height:200px;overflow:hidden;background:var(--light);position:relative}
.unit-img img{width:100%;height:100%;object-fit:cover;transition:transform .4s}
.unit-card:hover .unit-img img{transform:scale(1.05)}
.unit-badge{position:absolute;top:10px;left:10px;font-size:9px;letter-spacing:2px;font-weight:700;
  text-transform:uppercase;padding:4px 10px;border-radius:3px}
.badge-qs{background:var(--navy);color:var(--accent)}
.badge-cs{background:#2d6a4f;color:#fff}
.badge-avail{background:var(--accent2);color:var(--navy)}
.badge-def{background:rgba(0,0,0,.5);color:#fff}
.unit-body{padding:20px 22px 24px}
.unit-addr{font-family:'Playfair Display',serif;font-size:17px;color:var(--navy);margin-bottom:4px;font-weight:600}
.unit-fp{font-size:11px;color:var(--muted);margin-bottom:6px}
.unit-specs{font-size:11px;color:var(--muted);margin-bottom:8px}
.unit-desc{font-size:12px;color:#666;line-height:1.6;margin-bottom:12px}
.unit-price{font-family:'Playfair Display',serif;font-size:24px;font-weight:700;color:var(--navy)}

/* ═══ REGISTER FORM ═══ */
.reg-grid{display:grid;grid-template-columns:1fr 1fr;gap:60px;align-items:start}
.reg-copy p{font-size:14px;color:rgba(255,255,255,.55);line-height:1.85;margin-top:16px}
.reg-form{background:rgba(255,255,255,.04);border:1px solid rgba(197,213,228,.15);border-radius:6px;padding:36px}
.frow{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.fg{margin-bottom:14px}
.fg label{display:block;font-size:10px;letter-spacing:2px;text-transform:uppercase;
  color:rgba(255,255,255,.4);margin-bottom:6px;font-weight:600}
.fg input,.fg select,.fg textarea{width:100%;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.12);border-radius:4px;padding:11px 14px;color:#fff;
  font-family:'Inter',sans-serif;font-size:13px;outline:none;transition:border-color .2s}
.fg input::placeholder,.fg textarea::placeholder{color:rgba(255,255,255,.2)}
.fg input:focus,.fg select:focus,.fg textarea:focus{border-color:var(--accent2)}
.fg select option{background:var(--navy);color:#fff}
.fg textarea{resize:vertical;min-height:80px}
.btn-register{width:100%;background:var(--white);color:var(--navy);border:none;padding:14px;
  font-family:'Inter',sans-serif;font-size:11px;font-weight:700;letter-spacing:3px;
  text-transform:uppercase;border-radius:4px;cursor:pointer;transition:all .2s;margin-top:4px}
.btn-register:hover{background:var(--accent);transform:translateY(-1px)}
.form-msg{display:none;padding:12px;border-radius:4px;font-size:13px;margin-top:12px;text-align:center}
.form-msg.ok{background:rgba(72,187,120,.2);border:1px solid rgba(72,187,120,.4);color:#9ae6b4}
.form-msg.err{background:rgba(220,38,38,.2);border:1px solid rgba(220,38,38,.4);color:#fca5a5}

/* ═══ FOOTER ═══ */
footer{background:#0b1530;padding:40px 24px;text-align:center;position:relative;z-index:1}
footer .brand{font-family:'Playfair Display',serif;font-size:22px;color:var(--white);font-weight:700;margin-bottom:8px}
footer p{font-size:11px;color:rgba(255,255,255,.25);line-height:1.8;letter-spacing:.5px}
footer a{color:var(--accent2);text-decoration:none}

/* ═══ MODALS ═══ */
.modal-overlay{position:fixed;inset:0;z-index:2000;background:rgba(10,22,40,.85);
  backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;
  opacity:1;transition:opacity .3s}
.modal-overlay.closing{opacity:0;pointer-events:none}
.modal-overlay[style*="display:none"]{display:none!important}
.modal-box{background:var(--white);border-radius:8px;padding:40px;max-width:480px;width:92%;
  box-shadow:0 24px 60px rgba(0,0,0,.4);animation:fadeUp .4s ease-out both}
.modal-box h2{font-family:'Playfair Display',serif;font-size:28px;color:var(--navy);margin-bottom:16px}
.modal-box p{font-size:13px;color:var(--muted);line-height:1.7;margin-bottom:14px}
.modal-actions{display:flex;gap:10px;margin-top:20px}
.modal-btn{flex:1;font-family:'Inter',sans-serif;font-size:11px;font-weight:700;
  letter-spacing:2px;text-transform:uppercase;padding:13px 20px;border-radius:4px;
  cursor:pointer;border:none;transition:all .2s;text-align:center}
.modal-btn-dark{background:var(--navy);color:var(--white)}
.modal-btn-dark:hover{background:var(--navy2)}
.modal-btn-light{background:transparent;color:var(--navy);border:1px solid var(--light)}
.modal-btn-light:hover{border-color:var(--navy)}

/* Image edit modal */
.img-modal{position:fixed;inset:0;z-index:2000;background:rgba(10,22,40,.9);
  display:none;align-items:center;justify-content:center}
.img-modal.open{display:flex}
.img-modal-box{background:var(--white);border-radius:6px;padding:32px;max-width:480px;width:92%;box-shadow:0 20px 50px rgba(0,0,0,.4)}
.img-modal-box h3{font-family:'Playfair Display',serif;font-size:20px;color:var(--navy);margin-bottom:16px}
.img-modal-box label{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:6px}
.img-modal-box input[type="text"]{width:100%;padding:10px 14px;border:1px solid var(--light);border-radius:4px;
  font-family:'Inter',sans-serif;font-size:13px;outline:none}
.img-modal-box input:focus{border-color:var(--accent2)}
.img-preview{width:100%;max-height:160px;object-fit:cover;border-radius:4px;margin:12px 0;display:none}

/* Lightbox */
#lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}
#lb.open{display:flex}
#lb img{max-width:90vw;max-height:88vh;object-fit:contain;border-radius:4px}
#lb-x{position:absolute;top:16px;right:24px;color:#fff;font-size:28px;cursor:pointer;opacity:.6;transition:opacity .2s}
#lb-x:hover{opacity:1}

/* Toast */
.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(80px);z-index:3000;
  background:var(--navy);color:var(--white);padding:12px 24px;border-radius:4px;font-size:12px;
  letter-spacing:1px;box-shadow:0 6px 24px rgba(0,0,0,.3);opacity:0;transition:all .3s;pointer-events:none}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

@media(max-width:768px){
  .reg-grid,.frow{grid-template-columns:1fr}
  .about-highlights{grid-template-columns:1fr 1fr}
  .hero-content{padding:40px 20px}
  .toolbar{padding:8px 16px;flex-wrap:wrap;gap:6px}
  .section{padding:50px 16px}
  footer{padding:30px 16px}
}
</style>
</head>
<body class="has-toolbar">
"""

# Continue the PAGE template
PAGE += r"""
<!-- TOOLBAR -->
<div class="toolbar" id="toolbar">
  <div style="display:flex;align-items:center">
    <span class="tb-brand">{{ builder | upper }}</span>
    <span class="tb-status draft" id="tbStatus">DRAFT</span>
  </div>
  <div class="tb-right">
    <button class="tb-btn tb-btn-outline" id="btnEdit" onclick="toggleEdit()">✎ Edit</button>
    <button class="tb-btn tb-btn-outline" id="btnSave" onclick="saveDraft()" style="display:none">💾 Save</button>
    <button class="tb-btn tb-btn-primary" id="btnConfirm" onclick="confirmPage()">✓ Confirm Link</button>
  </div>
</div>

<!-- WELCOME MODAL -->
<div class="modal-overlay" id="welcomeModal">
  <div class="modal-box">
    <h2>Your page is ready</h2>
    <p>This landing page has been generated for <strong>{{ name }}</strong>. Click below to start editing text, images, and content. When done, save your changes and confirm to publish.</p>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-dark" onclick="closeModal();toggleEdit()" style="width:100%">Start Editing</button>
    </div>
  </div>
</div>

<!-- CONFIRM DIALOG -->
<div class="modal-overlay" id="confirmDialog" style="display:none">
  <div class="modal-box" style="max-width:400px">
    <h2>Publish this page?</h2>
    <p>This will create a permanent shareable link. All current edits will be included.</p>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-light" onclick="cancelConfirm()">Cancel</button>
      <button class="modal-btn modal-btn-dark" onclick="doConfirm()">Yes, Publish</button>
    </div>
  </div>
</div>

<!-- IMAGE EDIT MODAL -->
<div class="img-modal" id="imgModal">
  <div class="img-modal-box">
    <h3>Replace Image</h3>
    <label>Image URL</label>
    <input type="text" id="imgUrlInput" placeholder="https://example.com/image.jpg">
    <img id="imgPreview" class="img-preview" src="" alt="Preview">
    <div style="display:flex;gap:10px;margin-top:14px">
      <button class="tb-btn tb-btn-outline" style="flex:1;color:var(--navy);border-color:var(--light)" onclick="cancelImgEdit()">Cancel</button>
      <button class="tb-btn tb-btn-primary" style="flex:1" onclick="applyImgEdit()">Apply</button>
    </div>
  </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- LIGHTBOX -->
<div id="lb" onclick="closeLb()"><span id="lb-x">&times;</span><img id="lb-img" src="" alt=""></div>


<!-- ═══ TOP BANNER ═══ -->
<div class="top-banner" data-editable="banner">
  <p>{{ status }} — {{ builder }} · {{ location }}
    <a href="#register">Register Now →</a>
  </p>
</div>


<!-- ═══ HERO ═══ -->
<section class="hero">
  <div class="hero-bg" data-editable-img="hero" style="background-image:url('{{ hero_image }}')"></div>
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <span class="hero-badge">{{ status }}</span>
    <h1 data-editable="hero-title">{{ name }}</h1>
    <p class="hero-sub" data-editable="hero-sub">{{ description.split('\n')[0][:200] }}</p>
    {% if units %}
    <div class="hero-prices">
      {% for u in units[:3] %}
      {% if u.price %}
      <div class="hero-price-tag">
        <div class="label">{{ u.floorplan or u.address or 'Home' }}</div>
        <div class="value">{{ u.price }}</div>
      </div>
      {% endif %}
      {% endfor %}
    </div>
    {% endif %}
    <a href="#register" class="hero-cta">Register Your Interest</a>
  </div>
</section>


<!-- ═══ ABOUT / DESCRIPTION ═══ -->
<section class="section section-white" style="box-shadow:0 -20px 60px rgba(10,22,40,.15)">
  <div class="wrap">
    <div class="about-section">
      <div class="about-card">
        <p class="section-label">About</p>
        <h2 class="section-title" data-editable="about-title">{{ name }}</h2>
        <div class="about-divider"></div>
        <div class="about-text" data-editable="description">
          {% for p in description.split('\n\n') %}
          <p>{{ p }}</p>
          {% endfor %}
        </div>
      </div>
    </div>
  </div>
</section>


<!-- ═══ EXTERIOR GALLERY ═══ -->
{% if exterior_images %}
<section class="section section-light">
  <div class="wrap">
    <p class="section-label">Exterior</p>
    <h2 class="section-title">Homes &amp; Streetscape</h2>
    <div class="gallery-grid">
      {% for img in exterior_images[:8] %}
      <img src="{{ img }}" alt="{{ name }}" loading="lazy" onclick="openLb(this.src)" data-editable-img="ext-{{ loop.index0 }}">
      {% endfor %}
    </div>
  </div>
</section>
{% endif %}

<!-- ═══ INTERIOR GALLERY ═══ -->
{% if interior_images %}
<section class="section section-white">
  <div class="wrap">
    <p class="section-label">Interior</p>
    <h2 class="section-title">Inside the Homes</h2>
    <div class="gallery-grid">
      {% for img in interior_images[:8] %}
      <img src="{{ img }}" alt="{{ name }} interior" loading="lazy" onclick="openLb(this.src)" data-editable-img="int-{{ loop.index0 }}">
      {% endfor %}
    </div>
  </div>
</section>
{% endif %}

<!-- ═══ AMENITY GALLERY ═══ -->
{% if amenity_images %}
<section class="section section-light">
  <div class="wrap">
    <p class="section-label">Amenities &amp; Lifestyle</p>
    <h2 class="section-title">Community Living</h2>
    <div class="gallery-grid">
      {% for img in amenity_images[:8] %}
      <img src="{{ img }}" alt="{{ name }} amenity" loading="lazy" onclick="openLb(this.src)" data-editable-img="amen-{{ loop.index0 }}">
      {% endfor %}
    </div>
  </div>
</section>
{% endif %}


<!-- ═══ AVAILABLE UNITS ═══ -->
{% if units %}
<section class="section section-white">
  <div class="wrap">
    <p class="section-label">Available Homes</p>
    <h2 class="section-title">Properties at {{ name }}</h2>
    <div class="units-grid">
      {% for u in units %}
      {% if u.address or u.floorplan or u.price %}
      {% set sl = (u.status or '')|lower %}
      {% if 'quickstart' in sl %}{% set bc='badge-qs' %}
      {% elif 'coming soon' in sl %}{% set bc='badge-cs' %}
      {% elif 'available' in sl or 'ready' in sl %}{% set bc='badge-avail' %}
      {% else %}{% set bc='badge-def' %}{% endif %}
      <div class="unit-card">
        <div class="unit-img">
          {% if u.image_url %}<img src="{{ u.image_url }}" alt="{{ u.address or u.floorplan }}" loading="lazy" onclick="openLb(this.src)" data-editable-img="unit-{{ loop.index0 }}">{% endif %}
          {% if u.status %}<span class="unit-badge {{ bc }}">{{ u.status }}</span>{% endif %}
        </div>
        <div class="unit-body">
          {% if u.address %}<h3 class="unit-addr" data-editable="addr-{{ loop.index0 }}">{{ u.address }}</h3>{% endif %}
          {% if u.floorplan %}<p class="unit-fp" data-editable="fp-{{ loop.index0 }}">{{ u.floorplan }}</p>{% endif %}
          {% set specs=[] %}
          {% if u.bedrooms %}{% set _=specs.append(u.bedrooms~' bed') %}{% endif %}
          {% if u.bathrooms %}{% set _=specs.append(u.bathrooms~' bath') %}{% endif %}
          {% if u.sqft %}{% set _=specs.append(u.sqft~' sqft') %}{% endif %}
          {% if u.garage %}{% set _=specs.append(u.garage) %}{% endif %}
          {% if specs %}<p class="unit-specs">{{ specs|join(' · ') }}</p>{% endif %}
          {% if u.description %}<p class="unit-desc" data-editable="desc-{{ loop.index0 }}">{{ u.description[:180] }}</p>{% endif %}
          {% if u.price %}<p class="unit-price" data-editable="price-{{ loop.index0 }}">{{ u.price }}</p>{% endif %}
        </div>
      </div>
      {% endif %}
      {% endfor %}
    </div>
  </div>
</section>
{% endif %}


<!-- ═══ BUILDER ABOUT ═══ -->
{% if builder %}
<section class="section section-light">
  <div class="wrap" style="max-width:800px;text-align:center">
    <p class="section-label">The Developer</p>
    <h2 class="section-title" data-editable="builder-name">{{ builder }}</h2>
    <p class="about-text" style="margin-top:16px;text-align:center" data-editable="builder-about">
      {{ builder }} is committed to providing homebuyers with an above-standard benchmark of quality at every touchpoint in their home buying journey.
    </p>
  </div>
</section>
{% endif %}


<!-- ═══ QUICK FACTS ═══ -->
<section class="section section-dark">
  <div class="wrap">
    <p class="section-label">Quick Facts</p>
    <h2 class="section-title">Community Details</h2>
    <div class="facts-grid">
      {% if builder %}<div class="fact-card"><div class="fact-label">Developer</div><div class="fact-value" data-editable="fact-builder">{{ builder }}</div></div>{% endif %}
      {% if location %}<div class="fact-card"><div class="fact-label">Location</div><div class="fact-value" data-editable="fact-location">{{ location }}</div></div>{% endif %}
      {% if price_range %}<div class="fact-card"><div class="fact-label">Pricing</div><div class="fact-value" data-editable="fact-price">{{ price_range }}</div></div>{% endif %}
      {% if status %}<div class="fact-card"><div class="fact-label">Status</div><div class="fact-value">{{ status }}</div></div>{% endif %}
      {% if contact_phone %}<div class="fact-card"><div class="fact-label">Contact</div><div class="fact-value">{{ contact_phone }}</div></div>{% endif %}
      {% for pt in property_types %}<div class="fact-card"><div class="fact-label">Home Type</div><div class="fact-value">{{ pt }}</div></div>{% endfor %}
      {% if features %}
      <div class="fact-card" style="grid-column:1/-1"><div class="fact-label">Features &amp; Finishes</div>
        <div class="fact-value" data-editable="fact-features">{{ features | join(', ') }}</div></div>
      {% endif %}
    </div>
  </div>
</section>


<!-- ═══ REGISTER ═══ -->
<section class="section section-dark" id="register">
  <div class="wrap">
    <div class="reg-grid">
      <div class="reg-copy">
        <p class="section-label">Register Your Interest</p>
        <h2 class="section-title">Be the First to Know</h2>
        <p data-editable="reg-copy">Register for priority access, detailed floor plans, pricing updates,
          and exclusive launch event invitations for {{ name }} in {{ location }}.</p>
      </div>
      <form class="reg-form" id="regForm">
        <input type="hidden" name="community" value="{{ name }}">
        <input type="hidden" name="community_slug" value="{{ slug }}">
        <div class="frow">
          <div class="fg"><label>First Name *</label><input type="text" name="first_name" placeholder="John" required></div>
          <div class="fg"><label>Last Name *</label><input type="text" name="last_name" placeholder="Smith" required></div>
        </div>
        <div class="fg"><label>Email *</label><input type="email" name="email" placeholder="john@example.com" required></div>
        <div class="fg"><label>Phone</label><input type="tel" name="phone" placeholder="+1 (416) 000-0000"></div>
        <div class="frow">
          <div class="fg"><label>Interest</label>
            <select name="unit_interest"><option value="">Any available</option>
            {% for u in units %}<option value="{{ u.address }}">{{ u.address or u.floorplan }}</option>{% endfor %}
            </select></div>
          <div class="fg"><label>Timeline</label>
            <select name="timeline"><option value="">Select...</option>
            <option>ASAP</option><option>Within 3 months</option><option>Within 6 months</option><option>Just exploring</option>
            </select></div>
        </div>
        <div class="fg"><label>Message</label><textarea name="message" placeholder="Any questions..."></textarea></div>
        <button type="submit" class="btn-register">Register Interest</button>
        <div class="form-msg" id="formMsg"></div>
      </form>
    </div>
  </div>
</section>


<!-- ═══ FOOTER ═══ -->
<footer>
  <p>&copy; {{ year }} {{ builder }}<br>
    <a href="{{ community_url }}" target="_blank">{{ builder }}</a> · Prices &amp; availability subject to change.
    {% if contact_phone %}<br>{{ contact_phone }}{% endif %}
  </p>
</footer>
"""

PAGE += r"""
<script>
let editMode=false, currentImgTarget=null;
const SLUG="{{ slug }}";

function closeModal(){const m=document.getElementById('welcomeModal');m.classList.add('closing');setTimeout(()=>m.remove(),300)}

function toggleEdit(){
  editMode=!editMode;
  document.body.classList.toggle('edit-mode',editMode);
  const btn=document.getElementById('btnEdit'),st=document.getElementById('tbStatus'),sv=document.getElementById('btnSave');
  if(editMode){
    btn.classList.add('active');btn.textContent='✎ Editing…';
    st.className='tb-status editing';st.textContent='EDITING';sv.style.display='';
    document.querySelectorAll('[data-editable]').forEach(el=>el.contentEditable='true');
    document.querySelectorAll('[data-editable-img]').forEach(el=>{
      el._ec=function(e){if(!editMode)return;e.preventDefault();e.stopPropagation();openImgEdit(el)};
      el.addEventListener('click',el._ec)});
    toast('Edit mode ON — click any text or image');
  } else {
    btn.classList.remove('active');btn.textContent='✎ Edit';
    st.className='tb-status draft';st.textContent='DRAFT';sv.style.display='none';
    document.querySelectorAll('[data-editable]').forEach(el=>el.contentEditable='false');
    document.querySelectorAll('[data-editable-img]').forEach(el=>{if(el._ec)el.removeEventListener('click',el._ec)});
    toast('Edit mode OFF');
  }
}

async function saveDraft(){
  const btn=document.getElementById('btnSave');btn.disabled=true;btn.textContent='💾 Saving…';
  const els=['toolbar','toast','imgModal','welcomeModal'];
  els.forEach(id=>{const e=document.getElementById(id);if(e)e.style.display='none'});
  const cd=document.getElementById('confirmDialog');if(cd)cd.style.display='none';
  document.body.classList.remove('has-toolbar');
  const html='<!DOCTYPE html>\n'+document.documentElement.outerHTML;
  els.forEach(id=>{const e=document.getElementById(id);if(e)e.style.display=''});
  document.body.classList.add('has-toolbar');
  try{
    const r=await fetch('/save-draft',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({slug:SLUG,html})});
    if(r.ok){toast('Edits saved');const s=document.getElementById('tbStatus');s.textContent='SAVED';
      setTimeout(()=>s.textContent='EDITING',2000)}
    else throw new Error('Save failed');
  }catch(e){toast('Error: '+e.message)}
  btn.disabled=false;btn.textContent='💾 Save';
}

function confirmPage(){
  document.getElementById('confirmDialog').style.display='flex';
}
function cancelConfirm(){
  document.getElementById('confirmDialog').style.display='none';
}

async function doConfirm(){
  document.getElementById('confirmDialog').style.display='none';
  const btn=document.getElementById('btnConfirm');btn.disabled=true;btn.textContent='Publishing…';
  if(editMode)toggleEdit();
  const els=['toolbar','toast','imgModal'];
  els.forEach(id=>{const e=document.getElementById(id);if(e)e.style.display='none'});
  const cd=document.getElementById('confirmDialog');if(cd)cd.style.display='none';
  const wm=document.getElementById('welcomeModal');if(wm)wm.style.display='none';
  document.body.classList.remove('has-toolbar');
  document.querySelectorAll('[data-editable]').forEach(el=>el.removeAttribute('contenteditable'));
  const html='<!DOCTYPE html>\n'+document.documentElement.outerHTML;
  els.forEach(id=>{const e=document.getElementById(id);if(e)e.style.display=''});
  document.body.classList.add('has-toolbar');
  try{
    const r=await fetch('/confirm-page',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({slug:SLUG,html})});
    const d=await r.json();
    if(r.ok&&d.url){
      document.getElementById('tbStatus').className='tb-status saved';
      document.getElementById('tbStatus').textContent='PUBLISHED';
      btn.textContent='✓ Published';
      document.getElementById('btnSave').style.display='none';
      document.getElementById('btnEdit').style.display='none';
      toast('Published! Redirecting…');setTimeout(()=>location.href=d.url,1500);
    } else throw new Error(d.error||'Failed');
  }catch(e){toast('Error: '+e.message);btn.disabled=false;btn.textContent='✓ Confirm Link'}
}

function openImgEdit(el){
  currentImgTarget=el;
  let src=el.tagName==='IMG'?el.src:el.style.backgroundImage.replace(/url\(['"]?/,'').replace(/['"]?\)/,'');
  document.getElementById('imgUrlInput').value=src;
  const p=document.getElementById('imgPreview');p.src=src;p.style.display=src?'block':'none';
  document.getElementById('imgModal').classList.add('open');
}
document.getElementById('imgUrlInput').addEventListener('input',function(){
  const p=document.getElementById('imgPreview');p.src=this.value;p.style.display=this.value?'block':'none'});
function cancelImgEdit(){document.getElementById('imgModal').classList.remove('open');currentImgTarget=null}
function applyImgEdit(){
  const url=document.getElementById('imgUrlInput').value.trim();if(!url){cancelImgEdit();return}
  if(currentImgTarget.tagName==='IMG')currentImgTarget.src=url;
  else currentImgTarget.style.backgroundImage=`url('${url}')`;
  cancelImgEdit();toast('Image updated');
}

function openLb(s){if(editMode)return;document.getElementById('lb-img').src=s;document.getElementById('lb').classList.add('open')}
function closeLb(){document.getElementById('lb').classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeLb();cancelImgEdit();cancelConfirm()}});

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3000)}

// Registration form
document.getElementById('regForm').addEventListener('submit',async function(e){
  e.preventDefault();const btn=this.querySelector('.btn-register'),msg=document.getElementById('formMsg');
  btn.disabled=true;btn.textContent='Submitting…';msg.style.display='none';
  const data=Object.fromEntries(new FormData(this));
  try{const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(r.ok){msg.className='form-msg ok';msg.textContent='✓ Thank you! We will be in touch shortly.';this.reset();
      this.querySelector('[name=community]').value=data.community;this.querySelector('[name=community_slug]').value=data.community_slug}
    else throw new Error('Failed')}
  catch(e){msg.className='form-msg err';msg.textContent='✗ '+e.message}
  finally{msg.style.display='block';btn.disabled=false;btn.textContent='Register Interest'}
});

// Scroll reveal
const obs=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting){e.target.style.opacity='1';e.target.style.transform='none'}})},{threshold:.1});
document.querySelectorAll('.unit-card').forEach(c=>{c.style.opacity='0';c.style.transform='translateY(20px)';c.style.transition='all .5s';obs.observe(c)});
</script>
</body></html>"""


NOT_FOUND = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Not Found</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter&display=swap" rel="stylesheet">
<style>body{font-family:'Inter',sans-serif;background:#0a1628;display:flex;align-items:center;
justify-content:center;min-height:100vh;text-align:center;padding:40px;color:#fff}
h1{font-family:'Playfair Display',serif;font-size:72px;font-weight:700;margin-bottom:12px}
p{color:rgba(255,255,255,.5);font-size:14px;line-height:1.7}
code{color:#8bacc8;background:rgba(255,255,255,.08);padding:2px 8px;border-radius:3px}</style>
</head><body><div><h1>404</h1>
<p>Community <code>{{ slug }}</code> not found.</p>
<p style="margin-top:12px">Available:<br>{{ available }}</p>
</div></body></html>"""


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/create-link")
def create_link():
    slug = request.args.get("community", "").strip().lower()
    communities = load_communities()
    all_slugs = [slugify(_name(c)) for c in communities]

    if not slug:
        return render_template_string(NOT_FOUND, slug="(none)", available=", ".join(all_slugs)), 404

    community = next((c for c in communities if slugify(_name(c)) == slug), None)
    if not community:
        return render_template_string(NOT_FOUND, slug=slug, available="<br>".join(all_slugs)), 404

    name = _name(community)
    units_raw = community.get("properties") or []
    hero_image = _hero_img(community)
    gallery_images = _gallery(community)
    description = _desc(community)
    builder = _clean(community.get("builder")) or "Branthaven"
    location = _clean(community.get("location")) or ""
    status = _clean(community.get("status")) or ""
    price_range = _clean(community.get("price_range")) or ""
    contact_phone = _clean(community.get("contact_phone")) or ""
    features = [f for f in (community.get("features") or []) if _clean(f)]
    property_types = [p for p in (community.get("property_types") or []) if _clean(p)]

    # Categorize images
    image_cats = _categorize_images(gallery_images, name)

    units = []
    for idx, u in enumerate(units_raw):
        img = _unit_img(u, community)
        # If no image, rotate through gallery
        if not img and gallery_images:
            img = gallery_images[idx % len(gallery_images)]
        units.append({
            "address": _clean(u.get("address")),
            "floorplan": _clean(u.get("floorplan")),
            "price": _clean(u.get("price")),
            "status": _clean(u.get("status")),
            "bedrooms": _clean(u.get("bedrooms")),
            "bathrooms": _clean(u.get("bathrooms")),
            "sqft": _clean(u.get("sqft")),
            "garage": _clean(u.get("garage")),
            "description": _clean(u.get("description")),
            "image_url": img,
        })

    _email(
        f"🔗 Link Opened: {name}",
        f"<html><body><h2>{name}</h2><p>Status: {status}<br>Location: {location}<br>"
        f"Units: {len(units)}<br>Time: {datetime.now()}<br>URL: {request.url}</p></body></html>"
    )

    return render_template_string(PAGE,
        name=name, location=location, builder=builder, status=status,
        price_range=price_range, contact_phone=contact_phone,
        features=features, property_types=property_types,
        community_url=community.get("url", "#"), slug=slug,
        units=units, hero_image=hero_image,
        gallery_images=gallery_images,
        exterior_images=image_cats.get("exterior", []),
        interior_images=image_cats.get("interior", []),
        amenity_images=image_cats.get("amenity", []),
        description=description, year=datetime.now().year,
    )


@app.route("/confirm-page", methods=["POST"])
def confirm_page():
    data = request.get_json(force=True, silent=True) or {}
    slug = slugify(data.get("slug", ""))
    html = data.get("html", "")
    if not slug or not html:
        return jsonify({"error": "Missing slug or html"}), 400

    html = re.sub(r'\s+data-editable="[^"]*"', '', html)
    html = re.sub(r'\s+data-editable-img="[^"]*"', '', html)
    html = re.sub(r'\s+contenteditable="[^"]*"', '', html)

    fp = Path(CONFIRMED_DIR) / f"{slug}.html"
    fp.write_text(html, encoding="utf-8")
    _email(f"✅ Published: {slug}", f"<p>Page published: {slug} ({len(html):,} bytes)</p>")
    return jsonify({"ok": True, "url": f"/p/{slug}"})


@app.route("/save-draft", methods=["POST"])
def save_draft():
    data = request.get_json(force=True, silent=True) or {}
    slug = slugify(data.get("slug", ""))
    html = data.get("html", "")
    if not slug or not html:
        return jsonify({"error": "Missing slug or html"}), 400
    d = Path(CONFIRMED_DIR) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.html").write_text(html, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/p/<slug>")
def serve_confirmed(slug):
    fp = Path(CONFIRMED_DIR) / f"{slugify(slug)}.html"
    if not fp.exists(): abort(404)
    return fp.read_text(encoding="utf-8")


@app.route("/upload-image", methods=["POST"])
def upload_image():
    if "image" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["image"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return jsonify({"error": "Bad type"}), 400
    slug = slugify(request.form.get("slug", "general"))
    d = Path(UPLOAD_DIR) / slug
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    f.save(str(d / name))
    return jsonify({"ok": True, "url": f"/uploads/{slug}/{name}"})


@app.route("/uploads/<path:fp>")
def serve_upload(fp):
    p = Path(UPLOAD_DIR) / fp
    if not p.exists(): abort(404)
    if not str(p.resolve()).startswith(str(Path(UPLOAD_DIR).resolve())): abort(403)
    return send_file(str(p.resolve()))


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(force=True, silent=True) or {}
    missing = [f for f in ["first_name", "last_name", "email", "community"] if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    lead = {
        "submitted_at": datetime.now().isoformat(),
        "community": data.get("community"),
        "community_slug": data.get("community_slug"),
        "name": f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
        "email": data.get("email"),
        "phone": data.get("phone", ""),
        "unit_interest": data.get("unit_interest", ""),
        "timeline": data.get("timeline", ""),
        "message": data.get("message", ""),
    }
    save_lead(lead)
    _email(f"🏠 New Lead: {lead['name']} → {lead['community']}",
           f"<p><b>{lead['name']}</b> ({lead['email']})<br>Community: {lead['community']}<br>"
           f"Phone: {lead['phone'] or '—'}<br>Interest: {lead['unit_interest'] or 'Any'}<br>"
           f"Timeline: {lead['timeline'] or '—'}<br>Message: {lead['message'] or '—'}</p>")
    return jsonify({"ok": True})


@app.route("/leads")
def leads():
    if request.args.get("token") != ADMIN_TOKEN: abort(403)
    if not os.path.exists(LEADS_FILE): return jsonify([])
    with open(LEADS_FILE, "r", encoding="utf-8") as f: return jsonify(json.load(f))


@app.route("/health")
def health():
    c = load_communities()
    return jsonify({
        "status": "ok",
        "communities": len(c),
        "confirmed": len(list(Path(CONFIRMED_DIR).glob("*.html"))),
        "slugs": [slugify(_name(x)) for x in c],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)),
            debug=os.getenv("DEBUG", "false").lower() == "true")
