"""
Mattamy GTA – Property Landing Page Server
==========================================
pip install flask python-dotenv openai

Run:  python app.py
URL:  http://<EC2_IP>/create-link?community=seaton-whitevale

Features:
  - Edit mode: viewers can inline-edit text and swap images
  - Confirm Link: finalises the page as a static snapshot
  - Confirmed pages served at /p/<slug>
"""

import json
import os
import re
import smtplib
import hashlib
import uuid
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

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE   = os.getenv("DATA_FILE",  "detailed_properties.json")
DATA_DIR    = os.getenv("DATA_DIR",   "data")
LEADS_FILE  = os.getenv("LEADS_FILE", "leads.json")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme")
CONFIRMED_DIR = os.getenv("CONFIRMED_DIR", "confirmed_pages")  # where finalised HTML lives
UPLOAD_DIR    = os.getenv("UPLOAD_DIR",    "uploads")           # user-uploaded images

EMAIL_SENDER   = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL", EMAIL_SENDER)

# Ensure directories exist
os.makedirs(CONFIRMED_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:80] or "unnamed"


def load_communities() -> list[dict]:
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_local_images(community: dict) -> list[str]:
    urls = []
    for unit in community.get("properties") or []:
        local = unit.get("local_image", "")
        if not local:
            continue
        candidates = [
            Path(__file__).parent / local,
            Path(local),
        ]
        for p in candidates:
            if p.exists():
                urls.append(f"/images/{local}")
                break
    return urls


def generate_description(community: dict) -> str:
    name     = community.get("name", "")
    location = community.get("location", "")
    builder  = community.get("builder", "Mattamy Homes")
    units    = community.get("properties") or []

    unit_lines = "\n".join(
        f"- {u.get('address','')} | {u.get('floorplan','')} | "
        f"{u.get('price','')} | {u.get('status','')} | {u.get('description','')}"
        for u in units
    )

    prompt = f"""You are a luxury real-estate copywriter for {builder}.
Write a compelling 2-paragraph marketing description for this new community.

Paragraph 1: evoke the lifestyle, neighbourhood feel, and location appeal of {name} in {location}, Ontario.
Paragraph 2: highlight the available homes — sizes, prices, and move-in timelines — in an enticing but factual way.

Keep the tone warm, aspirational, and specific. No generic filler. No headings or bullet points.

Community  : {name}
Location   : {location}, Ontario
Builder    : {builder}

Available units:
{unit_lines}

Output only the two paragraphs separated by a blank line."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.75,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        app.logger.warning("OpenAI call failed: %s", e)
        prices = [u.get("price","") for u in units if u.get("price") and u["price"] != "Inquire for Pricing"]
        low    = min(prices, default="")
        high   = max(prices, default="")
        price_range = f"{low} – {high}" if low and high and low != high else (low or high or "pricing on request")
        return (
            f"{name} is an exceptional new community by {builder} nestled in {location}, Ontario. "
            f"Thoughtfully designed for modern living, this development offers an ideal blend of "
            f"comfort, style, and convenience in one of the GTA's most sought-after locations.\n\n"
            f"With {len(units)} home{'s' if len(units) != 1 else ''} available — ranging from "
            f"{price_range} — {name} presents an outstanding opportunity to secure a brand-new "
            f"Mattamy home. Register your interest today for priority access and exclusive updates."
        )


def save_lead(lead: dict):
    leads = []
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r", encoding="utf-8") as f:
            leads = json.load(f)
    leads.append(lead)
    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, default=str)


def _send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, NOTIFY_EMAIL]):
        app.logger.warning("Email not configured — skipping notification.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, NOTIFY_EMAIL, msg.as_bytes())
        app.logger.info("Email sent: %s", subject)
    except Exception as e:
        app.logger.error("Email failed: %s", e)


# ── HTML Template (with Edit Mode) ───────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ name }} – Mattamy Homes</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300&family=Montserrat:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --navy:#0f1923;--navy2:#162030;
      --gold:#c9a050;--gold2:#e8c070;
      --cream:#f5f0e8;--warm:#ede8df;
      --text:#2a2a2a;--muted:#7a7a7a;
    }
    html{scroll-behavior:smooth}
    body{font-family:'Montserrat',sans-serif;background:var(--cream);color:var(--text);overflow-x:hidden}

    /* ═══ EDIT TOOLBAR ═══ */
    .edit-toolbar{
      position:fixed;top:0;left:0;right:0;z-index:1000;
      background:linear-gradient(135deg,#0f1923 0%,#1a2a3d 100%);
      border-bottom:2px solid var(--gold);
      padding:12px 32px;
      display:flex;align-items:center;justify-content:space-between;
      box-shadow:0 4px 24px rgba(0,0,0,.35);
      transform:translateY(0);transition:transform .3s;
    }
    .edit-toolbar.hidden{transform:translateY(-100%)}
    .tb-left{display:flex;align-items:center;gap:16px}
    .tb-logo{font-family:'Cormorant Garamond',serif;font-size:16px;color:var(--gold);font-weight:600;letter-spacing:1px}
    .tb-status{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;padding:4px 12px;border-radius:20px;font-weight:500}
    .tb-status.draft{background:rgba(201,160,80,.15);color:var(--gold2);border:1px solid rgba(201,160,80,.3)}
    .tb-status.editing{background:rgba(46,204,113,.15);color:#2ecc71;border:1px solid rgba(46,204,113,.3)}
    .tb-status.confirmed{background:rgba(46,204,113,.3);color:#a8e6c3;border:1px solid rgba(46,204,113,.5)}
    .tb-right{display:flex;align-items:center;gap:12px}
    .tb-btn{
      font-family:'Montserrat',sans-serif;font-size:10px;font-weight:600;
      letter-spacing:2px;text-transform:uppercase;
      padding:10px 22px;border-radius:3px;cursor:pointer;
      border:none;transition:all .2s;
    }
    .tb-btn-edit{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.25)}
    .tb-btn-edit:hover{border-color:var(--gold);color:var(--gold)}
    .tb-btn-edit.active{background:rgba(46,204,113,.15);border-color:#2ecc71;color:#2ecc71}
    .tb-btn-confirm{background:var(--gold);color:var(--navy)}
    .tb-btn-confirm:hover{background:var(--gold2);transform:translateY(-1px)}
    .tb-btn-confirm:disabled{opacity:.4;cursor:not-allowed;transform:none}
    .tb-hint{font-size:11px;color:rgba(255,255,255,.4);max-width:260px;line-height:1.4}
    body.has-toolbar{padding-top:56px}

    /* ═══ EDITABLE HIGHLIGHTS ═══ */
    body.edit-mode [data-editable]{
      outline:2px dashed rgba(201,160,80,.5);outline-offset:4px;
      cursor:text;transition:outline-color .2s,background .2s;
      border-radius:2px;
    }
    body.edit-mode [data-editable]:hover{outline-color:var(--gold);background:rgba(201,160,80,.06)}
    body.edit-mode [data-editable]:focus{outline-color:#2ecc71;outline-style:solid;background:rgba(46,204,113,.04)}
    body.edit-mode [data-editable-img]{position:relative;cursor:pointer}
    body.edit-mode [data-editable-img]::after{
      content:'✎ Click to change image';
      position:absolute;bottom:8px;right:8px;
      background:rgba(15,25,35,.85);color:var(--gold);
      font-size:10px;letter-spacing:1px;font-family:'Montserrat',sans-serif;
      padding:5px 12px;border-radius:3px;
      opacity:0;transition:opacity .3s;pointer-events:none;
    }
    body.edit-mode [data-editable-img]:hover::after{opacity:1}

    /* ═══ WELCOME MODAL ═══ */
    .modal-overlay{
      position:fixed;inset:0;z-index:2000;
      background:rgba(10,16,26,.8);
      backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
      display:flex;align-items:center;justify-content:center;
      opacity:1;transition:opacity .4s;
    }
    .modal-overlay.closing{opacity:0;pointer-events:none}
    .modal{
      background:linear-gradient(160deg,#fff 0%,#f9f6f0 100%);
      border-radius:8px;padding:48px;max-width:520px;width:90%;
      box-shadow:0 30px 80px rgba(0,0,0,.4);
      position:relative;
      animation:modalIn .5s cubic-bezier(.22,1,.36,1) both;
    }
    @keyframes modalIn{from{opacity:0;transform:translateY(30px) scale(.96)}to{opacity:1;transform:none}}
    .modal-badge{
      display:inline-block;font-size:9px;letter-spacing:3px;text-transform:uppercase;
      color:var(--gold);border:1px solid rgba(201,160,80,.3);
      padding:4px 14px;border-radius:20px;margin-bottom:20px;font-weight:600;
    }
    .modal h2{
      font-family:'Cormorant Garamond',serif;font-size:32px;font-weight:300;
      color:var(--navy);line-height:1.2;margin-bottom:20px;
    }
    .modal h2 em{color:var(--gold);font-style:italic}
    .modal p{font-size:13px;color:#555;line-height:1.85;margin-bottom:16px}
    .modal-steps{list-style:none;margin:20px 0 28px}
    .modal-steps li{
      display:flex;align-items:flex-start;gap:14px;
      padding:12px 0;border-bottom:1px solid rgba(0,0,0,.06);
      font-size:13px;color:#444;line-height:1.6;
    }
    .modal-steps li:last-child{border-bottom:none}
    .step-num{
      flex-shrink:0;width:28px;height:28px;
      background:var(--navy);color:var(--gold);
      font-size:11px;font-weight:600;
      display:flex;align-items:center;justify-content:center;
      border-radius:50%;
    }
    .modal-actions{display:flex;gap:12px;margin-top:8px}
    .modal-btn{
      flex:1;font-family:'Montserrat',sans-serif;font-size:11px;font-weight:600;
      letter-spacing:2px;text-transform:uppercase;padding:14px 20px;
      border-radius:3px;cursor:pointer;border:none;transition:all .2s;text-align:center;
    }
    .modal-btn-primary{background:var(--navy);color:var(--gold)}
    .modal-btn-primary:hover{background:#1a2a3d}
    .modal-btn-secondary{background:transparent;color:var(--navy);border:1px solid rgba(15,25,35,.2)}
    .modal-btn-secondary:hover{border-color:var(--navy)}

    /* ═══ IMAGE EDIT MODAL ═══ */
    .img-modal-overlay{
      position:fixed;inset:0;z-index:2000;
      background:rgba(10,16,26,.85);
      backdrop-filter:blur(6px);
      display:none;align-items:center;justify-content:center;
    }
    .img-modal-overlay.open{display:flex}
    .img-modal{
      background:#fff;border-radius:6px;padding:36px;max-width:520px;width:92%;
      box-shadow:0 20px 60px rgba(0,0,0,.4);
      animation:modalIn .4s cubic-bezier(.22,1,.36,1) both;
    }
    .img-modal h3{font-family:'Cormorant Garamond',serif;font-size:22px;color:var(--navy);margin-bottom:20px;font-weight:400}

    /* Tabs */
    .img-tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #eee}
    .img-tab{
      flex:1;padding:10px 16px;text-align:center;
      font-family:'Montserrat',sans-serif;font-size:10px;font-weight:600;
      letter-spacing:2px;text-transform:uppercase;
      color:var(--muted);cursor:pointer;border:none;background:none;
      border-bottom:2px solid transparent;margin-bottom:-2px;
      transition:color .2s,border-color .2s;
    }
    .img-tab:hover{color:var(--navy)}
    .img-tab.active{color:var(--navy);border-bottom-color:var(--gold)}
    .img-tab-icon{font-size:16px;display:block;margin-bottom:4px}
    .img-panel{display:none}
    .img-panel.active{display:block}

    /* URL tab */
    .img-modal label{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:6px}
    .img-modal input[type="text"]{
      width:100%;padding:11px 14px;border:1px solid #ddd;border-radius:3px;
      font-family:'Montserrat',sans-serif;font-size:13px;margin-bottom:6px;outline:none;
    }
    .img-modal input[type="text"]:focus{border-color:var(--gold)}

    /* Drop zone */
    .drop-zone{
      border:2px dashed rgba(201,160,80,.4);border-radius:6px;
      padding:32px 20px;text-align:center;cursor:pointer;
      transition:border-color .3s,background .3s;
      background:rgba(245,240,232,.5);
    }
    .drop-zone:hover,.drop-zone.dragover{
      border-color:var(--gold);background:rgba(201,160,80,.08);
    }
    .drop-zone-icon{font-size:36px;color:var(--gold);margin-bottom:10px;display:block}
    .drop-zone-text{font-size:12px;color:#666;line-height:1.7}
    .drop-zone-text strong{color:var(--navy);cursor:pointer}
    .drop-zone-text strong:hover{text-decoration:underline}
    .drop-zone-hint{font-size:10px;color:var(--muted);margin-top:6px;letter-spacing:.5px}
    .drop-zone input[type="file"]{display:none}

    /* Upload progress */
    .upload-progress{display:none;margin-top:12px}
    .upload-bar-track{height:4px;background:#eee;border-radius:2px;overflow:hidden}
    .upload-bar{height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));
                border-radius:2px;width:0%;transition:width .3s}
    .upload-status{font-size:10px;color:var(--muted);margin-top:6px;letter-spacing:1px;text-transform:uppercase}

    /* Thumbnails for multi-file */
    .upload-thumbs{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
    .upload-thumb{
      position:relative;width:72px;height:72px;border-radius:4px;overflow:hidden;
      border:2px solid transparent;cursor:pointer;transition:border-color .2s;
    }
    .upload-thumb img{width:100%;height:100%;object-fit:cover}
    .upload-thumb.selected{border-color:var(--gold)}
    .upload-thumb .thumb-remove{
      position:absolute;top:2px;right:2px;width:18px;height:18px;
      background:rgba(0,0,0,.7);color:#fff;border:none;border-radius:50%;
      font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;
      opacity:0;transition:opacity .2s;line-height:1;
    }
    .upload-thumb:hover .thumb-remove{opacity:1}

    /* Preview */
    .img-preview-wrap{position:relative;margin:14px 0;border-radius:4px;overflow:hidden;background:var(--warm);min-height:60px}
    .img-preview{width:100%;max-height:180px;object-fit:cover;display:block;border-radius:4px}
    .img-preview-empty{padding:28px;text-align:center;font-size:11px;color:var(--muted);letter-spacing:1px}

    .img-modal-actions{display:flex;gap:10px;margin-top:16px}
    .img-modal-actions .tb-btn{flex:1;text-align:center}

    /* ═══ TOAST ═══ */
    .toast{
      position:fixed;bottom:32px;left:50%;transform:translateX(-50%) translateY(80px);
      z-index:3000;background:var(--navy);color:#fff;
      padding:14px 28px;border-radius:4px;
      font-size:12px;letter-spacing:1px;
      box-shadow:0 8px 30px rgba(0,0,0,.3);
      opacity:0;transition:all .4s cubic-bezier(.22,1,.36,1);
      pointer-events:none;
    }
    .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    .toast .toast-icon{color:var(--gold);margin-right:8px}

    /* HERO */
    .hero{position:relative;height:90vh;min-height:520px;display:flex;align-items:flex-end;overflow:hidden}
    .hero-bg{position:absolute;inset:0;background-size:cover;background-position:center;
             animation:zoomIn 14s ease-out forwards}
    @keyframes zoomIn{from{transform:scale(1.06)}to{transform:scale(1.0)}}
    .hero-overlay{position:absolute;inset:0;
      background:linear-gradient(to top,rgba(10,16,26,.92) 0%,rgba(10,16,26,.35) 55%,rgba(10,16,26,.05) 100%)}
    .hero-content{position:relative;z-index:2;width:100%;max-width:1080px;margin:0 auto;
                  padding:0 48px 60px;animation:up .9s .2s both}
    @keyframes up{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:none}}
    .eyebrow{font-size:10px;letter-spacing:4px;color:var(--gold);text-transform:uppercase;margin-bottom:12px}
    .hero-title{font-family:'Cormorant Garamond',serif;font-size:clamp(44px,7vw,80px);
                font-weight:300;color:#fff;line-height:1.05;letter-spacing:-1px;margin-bottom:18px}
    .hero-title em{font-style:italic;color:var(--gold2)}
    .hero-pills{display:flex;gap:24px;flex-wrap:wrap}
    .pill{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:rgba(255,255,255,.6)}
    .pill b{color:#fff}
    .sep{width:1px;height:14px;background:rgba(255,255,255,.25);align-self:center}

    /* LAYOUT */
    .wrap{max-width:1080px;margin:0 auto;padding:0 48px}
    .s-label{font-size:10px;letter-spacing:4px;text-transform:uppercase;
             color:var(--gold);font-weight:500;margin-bottom:10px}
    .s-title{font-family:'Cormorant Garamond',serif;
             font-size:clamp(26px,4vw,40px);font-weight:300;
             color:var(--navy);line-height:1.15;letter-spacing:-.5px}

    /* DESCRIPTION */
    .desc-section{padding:80px 0 56px}
    .desc-grid{display:grid;grid-template-columns:1fr 2fr;gap:64px;align-items:start}
    .desc-text{font-size:15px;color:#444;line-height:1.9}
    .desc-text p+p{margin-top:18px}

    /* IMAGE GALLERY */
    .gallery-section{padding:16px 0 72px}
    .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
             gap:12px;margin-top:28px}
    .gallery img{width:100%;height:190px;object-fit:cover;border-radius:3px;display:block;
                 cursor:zoom-in;transition:transform .4s,box-shadow .4s}
    .gallery img:hover{transform:scale(1.03);box-shadow:0 10px 28px rgba(0,0,0,.18)}

    /* UNIT CARDS */
    .units-section{padding:0 0 80px}
    .units-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
                gap:22px;margin-top:32px}
    .card{background:#fff;border-radius:3px;overflow:hidden;box-shadow:0 2px 14px rgba(0,0,0,.06);
          transition:transform .3s,box-shadow .3s;
          opacity:0;transform:translateY(20px);animation:up .6s both}
    .card:hover{transform:translateY(-4px);box-shadow:0 14px 40px rgba(0,0,0,.12)}
    .card-img{position:relative;height:190px;overflow:hidden;background:var(--warm)}
    .card-img img{width:100%;height:100%;object-fit:cover;transition:transform .5s}
    .card:hover .card-img img{transform:scale(1.06)}
    .badge{position:absolute;top:12px;left:12px;font-size:9px;letter-spacing:2px;
           font-weight:600;text-transform:uppercase;padding:3px 9px;border-radius:2px}
    .b-ready{background:var(--gold);color:var(--navy)}
    .b-soon{background:#2d6a4f;color:#fff}
    .b-sold{background:#8b0000;color:#fff}
    .b-def{background:rgba(0,0,0,.5);color:#fff}
    .card-body{padding:20px 22px 24px}
    .card-addr{font-family:'Cormorant Garamond',serif;font-size:17px;color:var(--navy);margin-bottom:4px}
    .card-fp{font-size:11px;color:var(--muted);margin-bottom:8px}
    .card-desc{font-size:12px;color:#666;line-height:1.6;margin-bottom:12px}
    .card-price{font-family:'Cormorant Garamond',serif;font-size:24px;font-weight:600;color:var(--navy)}

    /* REGISTRATION */
    .reg-section{background:var(--navy);padding:96px 0}
    .reg-inner{display:grid;grid-template-columns:1fr 1fr;gap:72px;align-items:start}
    .reg-copy .s-label{color:var(--gold)}
    .reg-copy .s-title{color:#fff;margin-bottom:18px}
    .reg-copy p{font-size:14px;color:rgba(255,255,255,.5);line-height:1.85}
    .reg-form{background:rgba(255,255,255,.04);border:1px solid rgba(201,160,80,.18);
              border-radius:3px;padding:38px}
    .frow{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    .fg{margin-bottom:16px}
    .fg label{display:block;font-size:10px;letter-spacing:2px;text-transform:uppercase;
              color:rgba(255,255,255,.4);margin-bottom:7px}
    .fg input,.fg select,.fg textarea{
      width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);
      border-radius:3px;padding:11px 14px;color:#fff;
      font-family:'Montserrat',sans-serif;font-size:13px;outline:none;
      transition:border-color .2s,background .2s}
    .fg input::placeholder,.fg textarea::placeholder{color:rgba(255,255,255,.2)}
    .fg input:focus,.fg select:focus,.fg textarea:focus{border-color:var(--gold);background:rgba(255,255,255,.09)}
    .fg select option{background:var(--navy2);color:#fff}
    .fg textarea{resize:vertical;min-height:80px}
    .btn-submit{width:100%;background:var(--gold);color:var(--navy);border:none;
                padding:14px;font-family:'Montserrat',sans-serif;font-size:11px;
                font-weight:600;letter-spacing:3px;text-transform:uppercase;
                border-radius:3px;cursor:pointer;transition:background .2s,transform .15s;margin-top:4px}
    .btn-submit:hover{background:var(--gold2);transform:translateY(-1px)}
    .form-msg{display:none;padding:12px 16px;border-radius:3px;font-size:13px;
              margin-top:14px;text-align:center}
    .form-msg.ok{background:rgba(45,106,79,.3);border:1px solid rgba(45,106,79,.5);color:#a8e6c3}
    .form-msg.err{background:rgba(139,0,0,.3);border:1px solid rgba(139,0,0,.5);color:#ffaaaa}

    /* LIGHTBOX */
    #lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:999;
        align-items:center;justify-content:center;cursor:zoom-out}
    #lb.open{display:flex}
    #lb img{max-width:90vw;max-height:88vh;object-fit:contain;border-radius:3px}
    #lb-x{position:absolute;top:18px;right:24px;color:#fff;font-size:30px;
           cursor:pointer;font-weight:300;opacity:.6;transition:opacity .2s;line-height:1}
    #lb-x:hover{opacity:1}

    footer{background:#080e17;padding:28px 48px;text-align:center}
    footer p{font-size:11px;color:rgba(255,255,255,.2);letter-spacing:.5px}
    footer a{color:var(--gold);text-decoration:none}

    @media(max-width:768px){
      .hero-content,.wrap{padding-left:24px;padding-right:24px}
      .desc-grid,.reg-inner,.frow{grid-template-columns:1fr}
      .edit-toolbar{padding:10px 16px;flex-wrap:wrap;gap:8px}
      .tb-hint{display:none}
      .modal{padding:32px 24px}
      footer{padding:24px}
    }
  </style>
</head>
<body class="has-toolbar">

<!-- ═══ WELCOME MODAL ═══ -->
<div class="modal-overlay" id="welcomeModal">
  <div class="modal">
    <span class="modal-badge">Review &amp; Edit</span>
    <h2>Your page is <em>ready</em></h2>
    <p>This landing page has been generated for <strong>{{ name }}</strong>. You can review it as-is or make edits before publishing.</p>
    <ol class="modal-steps">
      <li>
        <span class="step-num">1</span>
        <span>Click <strong>Edit Page</strong> in the toolbar to enable editing. You can change any text directly on the page, and click any image to replace it with a new URL.</span>
      </li>
      <li>
        <span class="step-num">2</span>
        <span>Make your changes — headings, descriptions, prices, images — everything is editable.</span>
      </li>
      <li>
        <span class="step-num">3</span>
        <span>When satisfied, click <strong>Confirm Link</strong> to finalise. This saves a clean, shareable version of the page.</span>
      </li>
    </ol>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-secondary" onclick="closeModal()">Skip — Looks Good</button>
      <button class="modal-btn modal-btn-primary" onclick="closeModal();toggleEdit()">Start Editing</button>
    </div>
  </div>
</div>

<!-- ═══ EDIT TOOLBAR ═══ -->
<div class="edit-toolbar" id="editToolbar">
  <div class="tb-left">
    <span class="tb-logo">MATTAMY</span>
    <span class="tb-status draft" id="tbStatus">DRAFT</span>
    <span class="tb-hint" id="tbHint">Review content, then confirm to publish</span>
  </div>
  <div class="tb-right">
    <button class="tb-btn tb-btn-edit" id="btnEdit" onclick="toggleEdit()">✎ Edit Page</button>
    <button class="tb-btn tb-btn-confirm" id="btnConfirm" onclick="confirmPage()">✓ Confirm Link</button>
  </div>
</div>

<!-- ═══ IMAGE EDIT MODAL ═══ -->
<div class="img-modal-overlay" id="imgModal">
  <div class="img-modal">
    <h3>Replace Image</h3>

    <!-- Tabs -->
    <div class="img-tabs">
      <button class="img-tab active" data-tab="url" onclick="switchImgTab('url')">
        <span class="img-tab-icon">🔗</span> Paste URL
      </button>
      <button class="img-tab" data-tab="upload" onclick="switchImgTab('upload')">
        <span class="img-tab-icon">📁</span> Upload File
      </button>
    </div>

    <!-- URL Panel -->
    <div class="img-panel active" id="panelUrl">
      <label>Image URL</label>
      <input type="text" id="imgUrlInput" placeholder="https://example.com/new-image.jpg">
      <p style="font-size:10px;color:var(--muted);margin-top:4px;letter-spacing:.5px">Paste any public image URL — it will appear in the preview below</p>
    </div>

    <!-- Upload Panel -->
    <div class="img-panel" id="panelUpload">
      <div class="drop-zone" id="dropZone">
        <span class="drop-zone-icon">⇪</span>
        <p class="drop-zone-text">
          Drag &amp; drop images here<br>
          or <strong id="browseBtn">browse your files</strong>
        </p>
        <p class="drop-zone-hint">JPG, PNG, WebP — max 10 MB each</p>
        <input type="file" id="fileInput" accept="image/jpeg,image/png,image/webp,image/gif" multiple>
      </div>
      <div class="upload-progress" id="uploadProgress">
        <div class="upload-bar-track"><div class="upload-bar" id="uploadBar"></div></div>
        <p class="upload-status" id="uploadStatus">Uploading…</p>
      </div>
      <div class="upload-thumbs" id="uploadThumbs"></div>
    </div>

    <!-- Preview (shared) -->
    <div class="img-preview-wrap" id="previewWrap">
      <div class="img-preview-empty" id="previewEmpty">No image selected</div>
      <img id="imgPreview" class="img-preview" src="" alt="Preview" style="display:none">
    </div>

    <div class="img-modal-actions">
      <button class="tb-btn tb-btn-edit" onclick="cancelImgEdit()">Cancel</button>
      <button class="tb-btn tb-btn-confirm" id="btnApplyImg" onclick="applyImgEdit()">Apply</button>
    </div>
  </div>
</div>

<!-- ═══ TOAST ═══ -->
<div class="toast" id="toast"><span class="toast-icon">✓</span> <span id="toastMsg"></span></div>


<!-- ═══ HERO ═══ -->
<section class="hero">
  <div class="hero-bg" data-editable-img="hero" style="background-image:url('{{ hero_image_url }}')"></div>
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <p class="eyebrow" data-editable="eyebrow">{{ builder }} &nbsp;·&nbsp; {{ location }}</p>
    <h1 class="hero-title" data-editable="hero-title">
      {% set words = name.split() %}
      {{ words[0] }}<br><em>{{ words[1:] | join(' ') }}</em>
    </h1>
    <div class="hero-pills">
      <span class="pill"><b>{{ units|length }}</b> Unit{{ 's' if units|length != 1 else '' }}</span>
      <span class="sep"></span>
      <span class="pill">{{ location }}, Ontario</span>
      <span class="sep"></span>
      <span class="pill">{{ builder }}</span>
    </div>
  </div>
</section>


<!-- ═══ LLM DESCRIPTION ═══ -->
<section class="desc-section">
  <div class="wrap">
    <div class="desc-grid">
      <div>
        <p class="s-label">About</p>
        <h2 class="s-title" data-editable="about-title">{{ name }}</h2>
      </div>
      <div class="desc-text" data-editable="description">
        {% for para in description.split('\n\n') %}
          <p>{{ para }}</p>
        {% endfor %}
      </div>
    </div>
  </div>
</section>


<!-- ═══ LOCAL IMAGE GALLERY ═══ -->
{% if local_images %}
<section class="gallery-section">
  <div class="wrap">
    <p class="s-label">Gallery</p>
    <h2 class="s-title">Photos from Site</h2>
    <div class="gallery">
      {% for img_url in local_images %}
      <img src="{{ img_url }}" alt="{{ name }}" loading="lazy" data-editable-img="gallery-{{ loop.index0 }}">
      {% endfor %}
    </div>
  </div>
</section>
{% endif %}


<!-- ═══ AVAILABLE UNITS ═══ -->
<section class="units-section">
  <div class="wrap">
    <p class="s-label">Available Homes</p>
    <h2 class="s-title">Properties at {{ name }}</h2>
    <div class="units-grid">
      {% for unit in units %}
        {% set sl = unit.status | lower %}
        {% if 'ready' in sl or 'available' in sl %}{% set bc='b-ready' %}
        {% elif 'coming soon' in sl or 'launching' in sl %}{% set bc='b-soon' %}
        {% elif 'sold' in sl %}{% set bc='b-sold' %}
        {% else %}{% set bc='b-def' %}{% endif %}
      <div class="card" style="animation-delay:{{ loop.index0 * 0.08 }}s">
        <div class="card-img">
          {% if unit.image_url %}
          <img src="{{ unit.image_url }}" alt="{{ unit.address }}" loading="lazy" data-editable-img="unit-{{ loop.index0 }}">
          {% endif %}
          <span class="badge {{ bc }}" data-editable="badge-{{ loop.index0 }}">{{ unit.status or 'N/A' }}</span>
        </div>
        <div class="card-body">
          <h3 class="card-addr" data-editable="addr-{{ loop.index0 }}">{{ unit.address }}</h3>
          {% if unit.floorplan %}<p class="card-fp" data-editable="fp-{{ loop.index0 }}">{{ unit.floorplan }}</p>{% endif %}
          {% if unit.description %}<p class="card-desc" data-editable="desc-{{ loop.index0 }}">{{ unit.description.replace('\n',' · ') }}</p>{% endif %}
          <p class="card-price" data-editable="price-{{ loop.index0 }}">{{ unit.price or 'Inquire for Pricing' }}</p>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
</section>


<!-- ═══ REGISTRATION FORM ═══ -->
<section class="reg-section" id="register">
  <div class="wrap">
    <div class="reg-inner">
      <div class="reg-copy">
        <p class="s-label">Register Your Interest</p>
        <h2 class="s-title">Be the <em style="font-style:italic;color:var(--gold2)">First to Know</em></h2>
        <p style="margin-top:16px" data-editable="reg-copy">
          Register for priority access, detailed floor plans, pricing updates,
          and exclusive launch event invitations for {{ name }} in {{ location }}.
        </p>
      </div>

      <form class="reg-form" id="regForm">
        <input type="hidden" name="community"      value="{{ name }}">
        <input type="hidden" name="community_slug" value="{{ slug }}">
        <div class="frow">
          <div class="fg"><label>First Name *</label>
            <input type="text" name="first_name" placeholder="John" required></div>
          <div class="fg"><label>Last Name *</label>
            <input type="text" name="last_name" placeholder="Smith" required></div>
        </div>
        <div class="fg"><label>Email *</label>
          <input type="email" name="email" placeholder="john@example.com" required></div>
        <div class="fg"><label>Phone</label>
          <input type="tel" name="phone" placeholder="+1 (416) 000-0000"></div>
        <div class="frow">
          <div class="fg"><label>Unit Interest</label>
            <select name="unit_interest">
              <option value="">Any available</option>
              {% for unit in units %}
              <option value="{{ unit.address }}">{{ unit.address }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="fg"><label>Timeline</label>
            <select name="timeline">
              <option value="">Select...</option>
              <option>ASAP</option>
              <option>Within 3 months</option>
              <option>Within 6 months</option>
              <option>Just exploring</option>
            </select>
          </div>
        </div>
        <div class="fg"><label>Message</label>
          <textarea name="message" placeholder="Any questions or preferences..."></textarea></div>
        <button type="submit" class="btn-submit">Register Interest</button>
        <div class="form-msg" id="formMsg"></div>
      </form>
    </div>
  </div>
</section>


<!-- FOOTER -->
<footer>
  <p>&copy; {{ year }} {{ builder }} &nbsp;·&nbsp;
    <a href="{{ url }}" target="_blank">mattamyhomes.com</a>
    &nbsp;·&nbsp; Prices & availability subject to change.
  </p>
</footer>

<!-- LIGHTBOX -->
<div id="lb" onclick="closeLb()">
  <span id="lb-x" onclick="closeLb()">&times;</span>
  <img id="lb-img" src="" alt="">
</div>

<script>
  /* ═══ STATE ═══ */
  let editMode = false;
  let currentImgTarget = null;          // element being image-edited
  const SLUG = "{{ slug }}";

  /* ═══ WELCOME MODAL ═══ */
  function closeModal(){
    const m = document.getElementById('welcomeModal');
    m.classList.add('closing');
    setTimeout(()=> m.remove(), 400);
  }

  /* ═══ EDIT TOGGLE ═══ */
  function toggleEdit(){
    editMode = !editMode;
    document.body.classList.toggle('edit-mode', editMode);
    const btn = document.getElementById('btnEdit');
    const st  = document.getElementById('tbStatus');
    const hint= document.getElementById('tbHint');

    if(editMode){
      btn.classList.add('active');
      btn.innerHTML = '✎ Editing…';
      st.className  = 'tb-status editing';
      st.textContent= 'EDITING';
      hint.textContent = 'Click any highlighted area to edit';
      // Make text elements editable
      document.querySelectorAll('[data-editable]').forEach(el=>{
        el.contentEditable = 'true';
      });
      // Attach image click handlers
      document.querySelectorAll('[data-editable-img]').forEach(el=>{
        el._editClick = function(e){
          if(!editMode) return;
          e.preventDefault(); e.stopPropagation();
          openImgEdit(el);
        };
        el.addEventListener('click', el._editClick);
      });
      showToast('Edit mode ON — click any text or image to change it');
    } else {
      btn.classList.remove('active');
      btn.innerHTML = '✎ Edit Page';
      st.className  = 'tb-status draft';
      st.textContent= 'DRAFT';
      hint.textContent = 'Review content, then confirm to publish';
      // Disable editing
      document.querySelectorAll('[data-editable]').forEach(el=>{
        el.contentEditable = 'false';
      });
      document.querySelectorAll('[data-editable-img]').forEach(el=>{
        if(el._editClick) el.removeEventListener('click', el._editClick);
      });
      showToast('Edit mode OFF');
    }
  }

  /* ═══ IMAGE EDIT ═══ */
  let selectedImgUrl = '';        // the URL to apply (from URL tab or upload)
  let uploadedFiles  = [];        // {url, name, thumb} for uploaded images
  let activeTab      = 'url';

  function switchImgTab(tab){
    activeTab = tab;
    document.querySelectorAll('.img-tab').forEach(t=> t.classList.toggle('active', t.dataset.tab===tab));
    document.querySelectorAll('.img-panel').forEach(p=> p.classList.remove('active'));
    document.getElementById(tab==='url'?'panelUrl':'panelUpload').classList.add('active');
  }

  function showPreview(src){
    const img   = document.getElementById('imgPreview');
    const empty = document.getElementById('previewEmpty');
    if(src){
      img.src = src; img.style.display='block'; empty.style.display='none';
      selectedImgUrl = src;
    } else {
      img.style.display='none'; empty.style.display='block';
      selectedImgUrl = '';
    }
  }

  function openImgEdit(el){
    currentImgTarget = el;
    uploadedFiles = [];
    document.getElementById('uploadThumbs').innerHTML = '';
    switchImgTab('url');
    // Pre-fill current src
    let currentSrc = '';
    if(el.tagName === 'IMG'){
      currentSrc = el.src;
    } else {
      const bg = el.style.backgroundImage;
      currentSrc = bg.replace(/url\(['"]?/, '').replace(/['"]?\)/, '');
    }
    document.getElementById('imgUrlInput').value = currentSrc;
    showPreview(currentSrc);
    // Reset upload state
    const prog = document.getElementById('uploadProgress');
    prog.style.display = 'none';
    document.getElementById('uploadBar').style.width = '0%';
    document.getElementById('imgModal').classList.add('open');
  }

  // URL input → live preview
  document.getElementById('imgUrlInput').addEventListener('input', function(){
    showPreview(this.value.trim());
  });

  // ── File browse ──
  document.getElementById('browseBtn').addEventListener('click', ()=>{
    document.getElementById('fileInput').click();
  });
  document.getElementById('fileInput').addEventListener('change', function(){
    if(this.files.length) handleFiles(this.files);
    this.value = '';  // reset so same file can be re-selected
  });

  // ── Drag & Drop ──
  const dropZone = document.getElementById('dropZone');
  ['dragenter','dragover'].forEach(evt=>{
    dropZone.addEventListener(evt, e=>{ e.preventDefault(); dropZone.classList.add('dragover'); });
  });
  ['dragleave','drop'].forEach(evt=>{
    dropZone.addEventListener(evt, e=>{ e.preventDefault(); dropZone.classList.remove('dragover'); });
  });
  dropZone.addEventListener('drop', e=>{
    const files = e.dataTransfer.files;
    if(files.length) handleFiles(files);
  });

  // ── Upload handler ──
  async function handleFiles(fileList){
    const MAX_SIZE = 10 * 1024 * 1024;  // 10 MB
    const allowed  = ['image/jpeg','image/png','image/webp','image/gif'];
    const validFiles = Array.from(fileList).filter(f=>{
      if(!allowed.includes(f.type)){ showToast('Skipped ' + f.name + ' — unsupported type'); return false; }
      if(f.size > MAX_SIZE){ showToast('Skipped ' + f.name + ' — exceeds 10 MB'); return false; }
      return true;
    });
    if(!validFiles.length) return;

    const prog   = document.getElementById('uploadProgress');
    const bar    = document.getElementById('uploadBar');
    const status = document.getElementById('uploadStatus');
    prog.style.display = 'block';

    for(let i=0; i<validFiles.length; i++){
      const file = validFiles[i];
      status.textContent = `Uploading ${i+1}/${validFiles.length}: ${file.name}`;
      bar.style.width = ((i / validFiles.length) * 100) + '%';

      const formData = new FormData();
      formData.append('image', file);
      formData.append('slug', SLUG);

      try {
        const res = await fetch('/upload-image', { method:'POST', body: formData });
        const data = await res.json();
        if(res.ok && data.url){
          uploadedFiles.push({ url: data.url, name: file.name });
          addThumb(data.url, file.name, uploadedFiles.length - 1);
          // Auto-select latest uploaded
          showPreview(data.url);
          selectThumb(uploadedFiles.length - 1);
        } else {
          showToast('Upload failed: ' + (data.error || file.name));
        }
      } catch(err){
        showToast('Upload error: ' + err.message);
      }
      bar.style.width = (((i+1) / validFiles.length) * 100) + '%';
    }
    status.textContent = `${validFiles.length} file${validFiles.length>1?'s':''} uploaded`;
    setTimeout(()=>{ prog.style.display = 'none'; }, 2000);
  }

  // ── Thumbnails ──
  function addThumb(url, name, idx){
    const wrap = document.createElement('div');
    wrap.className = 'upload-thumb';
    wrap.dataset.idx = idx;
    wrap.innerHTML = `<img src="${url}" alt="${name}" title="${name}">
      <button class="thumb-remove" title="Remove">&times;</button>`;
    wrap.querySelector('img').addEventListener('click', ()=>{
      showPreview(url);
      selectThumb(idx);
    });
    wrap.querySelector('.thumb-remove').addEventListener('click', (e)=>{
      e.stopPropagation();
      wrap.remove();
      uploadedFiles[idx] = null;
      // If removed was selected, clear preview
      if(selectedImgUrl === url) showPreview('');
    });
    document.getElementById('uploadThumbs').appendChild(wrap);
  }
  function selectThumb(idx){
    document.querySelectorAll('.upload-thumb').forEach(t=>{
      t.classList.toggle('selected', parseInt(t.dataset.idx) === idx);
    });
  }

  function cancelImgEdit(){
    document.getElementById('imgModal').classList.remove('open');
    currentImgTarget = null;
    selectedImgUrl = '';
  }
  function applyImgEdit(){
    // Use selectedImgUrl (set by either URL input or upload thumbnail)
    const url = selectedImgUrl || document.getElementById('imgUrlInput').value.trim();
    if(!url){ cancelImgEdit(); return; }
    if(currentImgTarget.tagName === 'IMG'){
      currentImgTarget.src = url;
    } else {
      currentImgTarget.style.backgroundImage = `url('${url}')`;
    }
    cancelImgEdit();
    showToast('Image updated');
  }

  /* ═══ CONFIRM PAGE ═══ */
  async function confirmPage(){
    const btn = document.getElementById('btnConfirm');
    btn.disabled = true; btn.textContent = 'Saving…';

    // Turn off edit mode first
    if(editMode) toggleEdit();

    // Remove toolbar, modals, toast from the snapshot
    const toolbar = document.getElementById('editToolbar');
    const toast   = document.getElementById('toast');
    const imgMod  = document.getElementById('imgModal');
    toolbar.style.display = 'none';
    toast.style.display   = 'none';
    imgMod.style.display  = 'none';
    document.body.classList.remove('has-toolbar');

    // Remove all data-editable attributes and contentEditable
    document.querySelectorAll('[data-editable]').forEach(el=>{
      el.removeAttribute('contenteditable');
    });

    // Grab the full HTML
    const html = '<!DOCTYPE html>\n' + document.documentElement.outerHTML;

    // Restore toolbar
    toolbar.style.display = '';
    toast.style.display   = '';
    imgMod.style.display  = '';
    document.body.classList.add('has-toolbar');

    try {
      const res = await fetch('/confirm-page', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ slug: SLUG, html: html })
      });
      const data = await res.json();
      if(res.ok && data.url){
        const st = document.getElementById('tbStatus');
        st.className = 'tb-status confirmed';
        st.textContent = 'CONFIRMED';
        document.getElementById('tbHint').textContent = 'Page published successfully';
        btn.textContent = '✓ Confirmed';
        showToast('Page confirmed! Redirecting…');
        setTimeout(()=> window.location.href = data.url, 1500);
      } else {
        throw new Error(data.error || 'Save failed');
      }
    } catch(err){
      showToast('Error: ' + err.message);
      btn.disabled = false; btn.textContent = '✓ Confirm Link';
    }
  }

  /* ═══ TOAST ═══ */
  function showToast(msg){
    const t = document.getElementById('toast');
    document.getElementById('toastMsg').textContent = msg;
    t.classList.add('show');
    setTimeout(()=> t.classList.remove('show'), 3000);
  }

  /* ═══ LIGHTBOX ═══ */
  function openLb(src){
    if(editMode) return; // don't open lightbox in edit mode
    document.getElementById('lb-img').src=src;
    document.getElementById('lb').classList.add('open');
  }
  function closeLb(){document.getElementById('lb').classList.remove('open')}
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'){closeLb();cancelImgEdit();}
  });

  /* ═══ SCROLL REVEAL ═══ */
  const obs=new IntersectionObserver(entries=>{
    entries.forEach(e=>{if(e.isIntersecting){e.target.style.opacity='1';e.target.style.transform='none'}})
  },{threshold:0.1});
  document.querySelectorAll('.card').forEach(c=>obs.observe(c));

  /* ═══ REGISTRATION FORM ═══ */
  document.getElementById('regForm').addEventListener('submit',async function(e){
    e.preventDefault();
    const btn=this.querySelector('.btn-submit'),msg=document.getElementById('formMsg');
    btn.disabled=true;btn.textContent='Submitting…';msg.style.display='none';
    const data=Object.fromEntries(new FormData(this));
    try{
      const res=await fetch('/register',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      const body=await res.json();
      if(res.ok){
        msg.className='form-msg ok';msg.textContent='✓ Thank you! We will be in touch shortly.';
        this.reset();
        this.querySelector('[name=community]').value=data.community;
        this.querySelector('[name=community_slug]').value=data.community_slug;
      }else throw new Error(body.error||'Submission failed');
    }catch(err){msg.className='form-msg err';msg.textContent='✗ '+err.message;}
    finally{msg.style.display='block';btn.disabled=false;btn.textContent='Register Interest';}
  });
</script>
</body>
</html>"""


NOT_FOUND = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Not Found</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300&family=Montserrat&display=swap" rel="stylesheet">
<style>body{font-family:'Montserrat',sans-serif;background:#f5f0e8;display:flex;align-items:center;
justify-content:center;min-height:100vh;text-align:center;padding:40px}
h1{font-family:'Cormorant Garamond',serif;font-size:72px;font-weight:300;color:#0f1923}
p{color:#7a7a7a;font-size:14px;margin-top:10px;line-height:1.7}code{color:#c9a050;background:#ede8df;padding:2px 8px;border-radius:3px}</style>
</head><body><div>
<h1>404</h1>
<p>Community <code>{{ slug }}</code> not found.</p>
<p>Available communities:<br>{{ available }}</p>
</div></body></html>"""


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/create-link")
def create_link():
    slug        = request.args.get("community", "").strip().lower()
    communities = load_communities()
    all_slugs   = [slugify(c.get("name", "")) for c in communities]

    if not slug:
        return render_template_string(NOT_FOUND, slug="(none)",
                                      available=", ".join(all_slugs)), 404

    community = next((c for c in communities if slugify(c.get("name","")) == slug), None)

    if not community:
        app.logger.warning("Not found: '%s'. Available: %s", slug, all_slugs)
        return render_template_string(NOT_FOUND, slug=slug,
                                      available="<br>".join(all_slugs)), 404

    units          = community.get("properties") or []
    hero_image_url = units[0].get("image_url", "") if units else ""
    local_images   = get_local_images(community)
    description    = generate_description(community)

    app.logger.info("Serving [%s] | %d units | %d local imgs | desc=%d chars",
                    community.get("name"), len(units), len(local_images), len(description))

    # ── Notify: link was opened ───────────────────────────────────────────────
    page_url = request.url
    _send_email(
        subject=f"🔗 Link Opened: {community.get('name')} – {datetime.now().strftime('%b %d %H:%M')}",
        body=f"""
<html><body style="font-family:Arial,sans-serif;color:#222;padding:24px">
  <h2 style="color:#0f1923">Landing Page Accessed</h2>
  <table style="border-collapse:collapse;width:100%;max-width:480px">
    <tr><td style="padding:8px 0;color:#888;width:140px">Community</td>
        <td style="padding:8px 0"><b>{community.get('name')}</b></td></tr>
    <tr><td style="padding:8px 0;color:#888">Location</td>
        <td style="padding:8px 0">{community.get('location')}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Units</td>
        <td style="padding:8px 0">{len(units)}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Time</td>
        <td style="padding:8px 0">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
    <tr><td style="padding:8px 0;color:#888">URL</td>
        <td style="padding:8px 0"><a href="{page_url}" style="color:#c9a050">{page_url}</a></td></tr>
  </table>
  <p style="margin-top:24px">
    <a href="{page_url}" style="background:#0f1923;color:#c9a050;padding:10px 20px;
       text-decoration:none;border-radius:3px;font-size:12px;letter-spacing:1px">
      VIEW PAGE →
    </a>
  </p>
</body></html>"""
    )

    return render_template_string(
        PAGE,
        name           = community.get("name", ""),
        location       = community.get("location", ""),
        builder        = community.get("builder", "Mattamy Homes"),
        url            = community.get("url", "#"),
        slug           = slug,
        units          = units,
        hero_image_url = hero_image_url,
        local_images   = local_images,
        description    = description,
        year           = datetime.now().year,
    )


@app.route("/confirm-page", methods=["POST"])
def confirm_page():
    """
    Receives the edited HTML from the browser and saves it as a static file.
    POST JSON: { "slug": "seaton-whitevale", "html": "<!DOCTYPE html>..." }
    Returns:   { "ok": true, "url": "/p/seaton-whitevale" }
    """
    data = request.get_json(force=True, silent=True) or {}
    slug = slugify(data.get("slug", ""))
    html = data.get("html", "")

    if not slug or not html:
        return jsonify({"error": "Missing slug or html"}), 400

    # Clean the HTML: remove the edit toolbar, modals, and edit-related scripts
    # (The browser already hides them, but we strip residual markup for a clean file)
    html = re.sub(
        r'<!-- ═══ WELCOME MODAL ═══ -->.*?</div>\s*</div>',
        '', html, flags=re.DOTALL
    )
    # Remove data-editable and data-editable-img attributes
    html = re.sub(r'\s+data-editable="[^"]*"', '', html)
    html = re.sub(r'\s+data-editable-img="[^"]*"', '', html)
    html = re.sub(r'\s+contenteditable="[^"]*"', '', html)

    # Save with timestamp
    filepath = Path(CONFIRMED_DIR) / f"{slug}.html"
    filepath.write_text(html, encoding="utf-8")

    app.logger.info("Confirmed page saved: %s (%d bytes)", filepath, len(html))

    # ── Notify: page confirmed ────────────────────────────────────────────────
    confirmed_url = f"/p/{slug}"
    _send_email(
        subject=f"✅ Page Confirmed: {slug} – {datetime.now().strftime('%b %d %H:%M')}",
        body=f"""
<html><body style="font-family:Arial,sans-serif;color:#222;padding:24px">
  <h2 style="color:#0f1923">Landing Page Confirmed</h2>
  <table style="border-collapse:collapse;width:100%;max-width:480px">
    <tr><td style="padding:8px 0;color:#888;width:140px">Community</td>
        <td style="padding:8px 0"><b>{slug}</b></td></tr>
    <tr><td style="padding:8px 0;color:#888">Confirmed At</td>
        <td style="padding:8px 0">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
    <tr><td style="padding:8px 0;color:#888">File Size</td>
        <td style="padding:8px 0">{len(html):,} bytes</td></tr>
  </table>
  <p style="margin-top:24px">
    <a href="{confirmed_url}" style="background:#0f1923;color:#c9a050;padding:10px 20px;
       text-decoration:none;border-radius:3px;font-size:12px;letter-spacing:1px">
      VIEW CONFIRMED PAGE →
    </a>
  </p>
</body></html>"""
    )

    return jsonify({"ok": True, "url": confirmed_url})


@app.route("/p/<slug>")
def serve_confirmed(slug: str):
    """
    Serve the confirmed (finalised) static HTML page.
    GET /p/seaton-whitevale
    """
    slug = slugify(slug)
    filepath = Path(CONFIRMED_DIR) / f"{slug}.html"
    if not filepath.exists():
        abort(404)
    return filepath.read_text(encoding="utf-8")


@app.route("/upload-image", methods=["POST"])
def upload_image():
    """
    Accept a local file upload from the edit modal.
    POST multipart: image=<file>, slug=<community-slug>
    Returns: { "ok": true, "url": "/uploads/seaton-whitevale/abc123.jpg" }
    """
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    file = request.files["image"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Validate file type
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_ext:
        return jsonify({"error": f"File type {ext} not allowed"}), 400

    # Validate file size (10 MB max)
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({"error": "File exceeds 10 MB limit"}), 400

    # Organise by community slug
    slug = slugify(request.form.get("slug", "general"))
    save_dir = Path(UPLOAD_DIR) / slug
    save_dir.mkdir(parents=True, exist_ok=True)

    # Unique filename: short uuid + original extension
    unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
    save_path = save_dir / unique_name
    file.save(str(save_path))

    url = f"/uploads/{slug}/{unique_name}"
    app.logger.info("Image uploaded: %s (%d bytes)", url, size)

    return jsonify({"ok": True, "url": url, "filename": unique_name, "size": size})


@app.route("/uploads/<path:filepath>")
def serve_upload(filepath: str):
    """Serve user-uploaded images from the uploads directory."""
    full_path = Path(UPLOAD_DIR) / filepath
    if not full_path.exists():
        abort(404)
    # Security: ensure resolved path is within UPLOAD_DIR
    if not str(full_path.resolve()).startswith(str(Path(UPLOAD_DIR).resolve())):
        abort(403)
    return send_file(str(full_path.resolve()))


@app.route("/images/<path:filepath>")
def serve_image(filepath: str):
    full_path = Path(__file__).parent / filepath
    if not full_path.exists():
        abort(404)
    return send_file(str(full_path.resolve()))


@app.route("/register", methods=["POST"])
def register():
    data    = request.get_json(force=True, silent=True) or {}
    missing = [f for f in ["first_name","last_name","email","community"] if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    lead = {
        "submitted_at":  datetime.now().isoformat(),
        "community":     data.get("community"),
        "community_slug":data.get("community_slug"),
        "name":          f"{data.get('first_name','')} {data.get('last_name','')}".strip(),
        "email":         data.get("email"),
        "phone":         data.get("phone",""),
        "unit_interest": data.get("unit_interest",""),
        "timeline":      data.get("timeline",""),
        "message":       data.get("message",""),
    }
    save_lead(lead)
    app.logger.info("Lead: %s <%s> → %s", lead["name"], lead["email"], lead["community"])

    community_url = data.get("community_url", "")
    _send_email(
        subject=f"🏠 New Registration: {lead['name']} → {lead['community']}",
        body=f"""
<html><body style="font-family:Arial,sans-serif;color:#222;padding:24px">
  <h2 style="color:#0f1923">New Registration Received</h2>
  <table style="border-collapse:collapse;width:100%;max-width:480px">
    <tr><td style="padding:8px 0;color:#888;width:140px">Name</td>
        <td style="padding:8px 0"><b>{lead['name']}</b></td></tr>
    <tr><td style="padding:8px 0;color:#888">Email</td>
        <td style="padding:8px 0"><a href="mailto:{lead['email']}" style="color:#c9a050">{lead['email']}</a></td></tr>
    <tr><td style="padding:8px 0;color:#888">Phone</td>
        <td style="padding:8px 0">{lead['phone'] or '—'}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Community</td>
        <td style="padding:8px 0"><b>{lead['community']}</b></td></tr>
    <tr><td style="padding:8px 0;color:#888">Unit Interest</td>
        <td style="padding:8px 0">{lead['unit_interest'] or 'Any'}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Timeline</td>
        <td style="padding:8px 0">{lead['timeline'] or '—'}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Message</td>
        <td style="padding:8px 0">{lead['message'] or '—'}</td></tr>
    <tr><td style="padding:8px 0;color:#888">Submitted</td>
        <td style="padding:8px 0">{lead['submitted_at']}</td></tr>
  </table>
  {"<p style='margin-top:24px'><a href='" + community_url + "' style='background:#0f1923;color:#c9a050;padding:10px 20px;text-decoration:none;border-radius:3px;font-size:12px'>VIEW LISTING →</a></p>" if community_url else ""}
</body></html>"""
    )
    return jsonify({"ok": True}), 200


@app.route("/leads")
def leads():
    if request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    if not os.path.exists(LEADS_FILE):
        return jsonify([])
    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/health")
def health():
    communities = load_communities()
    confirmed   = list(Path(CONFIRMED_DIR).glob("*.html"))
    return jsonify({
        "status":          "ok",
        "communities":     len(communities),
        "confirmed_pages": len(confirmed),
        "confirmed_slugs": [p.stem for p in confirmed],
        "data_file":       DATA_FILE,
        "data_exists":     os.path.exists(DATA_FILE),
        "slugs":           [slugify(c.get("name","")) for c in communities],
    })


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
