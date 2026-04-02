import asyncio
import json
import os
import re
import sys
import time
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv
import requests
from playwright.async_api import async_playwright

load_dotenv()

# --- Force UTF-8 on Windows console ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Logging (ASCII only, UTF-8 file) ---
file_handler = logging.FileHandler("property_agent.log", encoding="utf-8")
console_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[file_handler, console_handler],
)
logger = logging.getLogger("PropertyAgent")


# --- Helpers ---
def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:80] or "unnamed"


def make_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def safe_json_dump(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, default=str, ensure_ascii=False)


def safe_json_load(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# =============================================================================
#  LOCAL DATA STORE -- all JSON files, no database
# =============================================================================
class LocalStore:
    """
    Stores everything in local JSON files:
        data/
            properties.json          -- main property list
            change_log.json          -- field-level changes
            crawl_history.json       -- every page visit
            site_map.json            -- learned URLs per domain
            images/                  -- downloaded images
            page_screenshots/        -- full page screenshots
    """

    def __init__(self, base_dir: str = "data"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.properties_file = self.base_dir / "properties.json"
        self.changes_file = self.base_dir / "change_log.json"
        self.crawl_file = self.base_dir / "crawl_history.json"
        self.sitemap_file = self.base_dir / "site_map.json"
        self.images_dir = self.base_dir / "images"
        self.screenshots_dir = self.base_dir / "page_screenshots"

        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Load existing data into memory
        self.properties: list[dict] = safe_json_load(self.properties_file) or []
        self.changes: list[dict] = safe_json_load(self.changes_file) or []
        self.crawl_history: list[dict] = safe_json_load(self.crawl_file) or []
        self.site_map: list[dict] = safe_json_load(self.sitemap_file) or []

    # --- save helpers ---
    def _save_properties(self):
        safe_json_dump(self.properties, self.properties_file)

    def _save_changes(self):
        safe_json_dump(self.changes, self.changes_file)

    def _save_crawl_history(self):
        safe_json_dump(self.crawl_history, self.crawl_file)

    def _save_site_map(self):
        safe_json_dump(self.site_map, self.sitemap_file)

    def save_all(self):
        self._save_properties()
        self._save_changes()
        self._save_crawl_history()
        self._save_site_map()

    # --- property upsert with change tracking ---
    def upsert_property(self, prop: dict) -> tuple:
        """
        Returns (action, property_index, changes_list)
        action = 'new' | 'updated' | 'unchanged'
        """
        now = datetime.now().isoformat()
        domain = urlparse(prop.get("url", "")).netloc
        name = (prop.get("name") or "").strip()
        location = (prop.get("location") or "").strip()

        if not name:
            return ("skipped", -1, [])

        # find existing by name + location + domain
        existing_idx = None
        for i, p in enumerate(self.properties):
            if (
                (p.get("name") or "").strip().lower() == name.lower()
                and (p.get("location") or "").strip().lower() == location.lower()
                and (p.get("source_domain") or "") == domain
            ):
                existing_idx = i
                break

        if existing_idx is not None:
            existing = self.properties[existing_idx]
            tracked = ["price", "status", "description", "image_url", "property_type"]
            changes = []

            for field in tracked:
                old_val = (existing.get(field) or "").strip()
                new_val = (prop.get(field) or "").strip()
                if old_val != new_val and new_val:
                    change_record = {
                        "property_name": name,
                        "property_location": location,
                        "field": field,
                        "old_value": old_val,
                        "new_value": new_val,
                        "changed_at": now,
                    }
                    changes.append(change_record)
                    existing[field] = new_val

            if changes:
                existing["last_updated"] = now
                existing["last_seen"] = now
                self.changes.extend(changes)
                logger.info(
                    "  ~ Updated: %s -- changed %s",
                    name, [c["field"] for c in changes]
                )
                return ("updated", existing_idx, changes)

            # unchanged -- just bump last_seen
            existing["last_seen"] = now
            return ("unchanged", existing_idx, [])

        # brand new property
        new_record = {
            "id": len(self.properties) + 1,
            "name": name,
            "location": location,
            "price": prop.get("price", ""),
            "status": prop.get("status", ""),
            "url": prop.get("url", ""),
            "image_url": prop.get("image_url", ""),
            "local_images": [],
            "thumbnail_path": "",
            "description": prop.get("description", ""),
            "property_type": prop.get("property_type", ""),
            "builder": prop.get("builder", ""),
            "source_domain": domain,
            "first_seen": now,
            "last_seen": now,
            "last_updated": now,
            "is_active": True,
            "images": [],
        }
        self.properties.append(new_record)
        idx = len(self.properties) - 1

        logger.info(
            "  + NEW: %s | %s | %s",
            name, location, prop.get("price", "N/A")
        )
        return ("new", idx, [])

    def attach_images(self, prop_idx: int, image_records: list[dict]):
        """Attach downloaded image info to a property."""
        if prop_idx < 0 or prop_idx >= len(self.properties):
            return
        p = self.properties[prop_idx]
        existing_paths = set(p.get("local_images") or [])

        for img in image_records:
            lp = img.get("local_path", "")
            if lp and lp not in existing_paths:
                existing_paths.add(lp)
                if "images" not in p:
                    p["images"] = []
                p["images"].append(img)

        p["local_images"] = list(existing_paths)
        if existing_paths and not p.get("thumbnail_path"):
            p["thumbnail_path"] = list(existing_paths)[0]

    # --- crawl history ---
    def log_crawl(self, url, c_hash, n_props, screenshot_path=None):
        self.crawl_history.append({
            "url": url,
            "content_hash": c_hash,
            "screenshot_path": screenshot_path or "",
            "crawled_at": datetime.now().isoformat(),
            "properties_found": n_props,
        })

    def get_page_hash(self, url) -> str | None:
        for entry in reversed(self.crawl_history):
            if entry.get("url") == url:
                return entry.get("content_hash")
        return None

    # --- site map ---
    def save_site_map_url(self, domain, url, page_type, priority=0.5):
        for entry in self.site_map:
            if entry.get("url") == url:
                entry["page_type"] = page_type
                entry["last_crawled"] = datetime.now().isoformat()
                entry["priority"] = priority
                return
        self.site_map.append({
            "domain": domain,
            "url": url,
            "page_type": page_type,
            "last_crawled": datetime.now().isoformat(),
            "priority": priority,
        })

    def get_learned_urls(self, domain) -> list[dict]:
        return [
            e for e in self.site_map
            if e.get("domain") == domain and e.get("priority", 0) >= 0.5
        ]

    # --- reporting ---
    def get_daily_report(self):
        today = datetime.now().date().isoformat()
        new_today = [
            p for p in self.properties
            if (p.get("first_seen") or "").startswith(today)
        ]
        changes_today = [
            c for c in self.changes
            if (c.get("changed_at") or "").startswith(today)
        ]
        total_images = sum(len(p.get("local_images") or []) for p in self.properties)
        return {
            "new_properties": new_today,
            "changes": changes_today,
            "total_properties": len(self.properties),
            "total_images": total_images,
        }

    def export_json(self, path="detailed_properties.json"):
        safe_json_dump(self.properties, path)
        logger.info("Exported %d properties -> %s", len(self.properties), path)

    def get_storage_stats(self) -> dict:
        total_images = sum(len(p.get("local_images") or []) for p in self.properties)
        # calculate image folder size
        img_size = 0
        for root, dirs, files in os.walk(self.images_dir):
            for f in files:
                img_size += os.path.getsize(os.path.join(root, f))
        return {
            "total_properties": len(self.properties),
            "active_properties": sum(1 for p in self.properties if p.get("is_active")),
            "total_images": total_images,
            "total_image_size_mb": round(img_size / 1024 / 1024, 2),
            "total_crawls": len(self.crawl_history),
            "total_changes": len(self.changes),
        }


# =============================================================================
#  IMAGE DOWNLOADER
# =============================================================================
class ImageDownloader:

    def __init__(self, store: LocalStore):
        self.store = store
        self.images_dir = store.images_dir
        self.screenshots_dir = store.screenshots_dir
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            )
        })

    def _get_extension(self, url: str, content_type: str = "") -> str:
        path = urlparse(url).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"):
            if path.endswith(ext):
                return ext
        ct = content_type.lower()
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
        if "png" in ct:
            return ".png"
        if "webp" in ct:
            return ".webp"
        if "gif" in ct:
            return ".gif"
        return ".jpg"

    def download_image(self, url: str, domain: str, property_name: str,
                       index: int) -> str | None:
        prop_slug = slugify(property_name)
        folder = self.images_dir / slugify(domain) / prop_slug
        folder.mkdir(parents=True, exist_ok=True)

        try:
            resp = self._session.get(url, timeout=30, stream=True)
            if resp.status_code != 200:
                return None

            ext = self._get_extension(url, resp.headers.get("Content-Type", ""))
            filename = f"img_{index:03d}{ext}"
            filepath = folder / filename

            # skip if already downloaded (same size)
            if filepath.exists():
                local_size = filepath.stat().st_size
                remote_size = int(resp.headers.get("Content-Length", 0))
                if remote_size and abs(local_size - remote_size) < 100:
                    return str(filepath)

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)

            size_kb = filepath.stat().st_size / 1024
            if size_kb < 1:
                filepath.unlink()
                return None

            logger.info("      [img] %s (%.0f KB)", filename, size_kb)
            return str(filepath)

        except Exception as e:
            logger.debug("      image download failed %s: %s", url, e)
            return None

    def download_property_images(self, image_urls: list[str], domain: str,
                                  property_name: str) -> list[dict]:
        results = []
        for i, url in enumerate(image_urls):
            local = self.download_image(url, domain, property_name, i)
            results.append({
                "remote_url": url,
                "local_path": local or "",
                "alt_text": "",
                "context": "",
            })
        return [r for r in results if r["local_path"]]

    async def save_screenshot(self, page, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            domain_slug = slugify(parsed.netloc)
            path_slug = slugify(parsed.path or "home")
            date_str = datetime.now().strftime("%Y-%m-%d")
            filename = f"{domain_slug}_{path_slug}_{date_str}.png"
            filepath = self.screenshots_dir / filename

            await page.screenshot(path=str(filepath), full_page=True)
            size_kb = filepath.stat().st_size / 1024
            logger.info("    [screenshot] %.0f KB", size_kb)
            return str(filepath)

        except Exception as e:
            logger.debug("    screenshot failed: %s", e)
            return None


# =============================================================================
#  LLM CLIENT
# =============================================================================
class LLMClient:

    def __init__(self, api_key: str, mock: bool = False):
        self.api_key = api_key
        self.mock = mock
        self.call_count = 0

    def _call(self, system: str, user: str, retries: int = 2):
        self.call_count += 1
        if self.mock:
            return None

        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://localhost:3000",
                        "X-Title": "Property Intelligence Agent",
                    },
                    json={
                        "model": "mistralai/mistral-small-creative",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user[:15000]},
                        ],
                    },
                    timeout=90,
                )

                if resp.status_code != 200:
                    logger.error("LLM API error %d: %s", resp.status_code, resp.text[:300])
                    if attempt < retries:
                        time.sleep(3)
                        continue
                    return None

                raw = resp.json()["choices"][0]["message"]["content"].strip()
                logger.debug("LLM raw (first 500): %s", raw[:500])

                # strip markdown fences
                if "```json" in raw:
                    raw = raw.split("```json", 1)[1]
                if "```" in raw:
                    raw = raw.split("```")[0]
                raw = raw.strip()

                # find JSON array or object
                if not raw.startswith(("[", "{")):
                    # try to extract JSON from the response
                    arr_start = raw.find("[")
                    obj_start = raw.find("{")
                    if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
                        raw = raw[arr_start:]
                    elif obj_start >= 0:
                        raw = raw[obj_start:]

                return json.loads(raw)

            except json.JSONDecodeError:
                logger.warning(
                    "LLM returned non-JSON (attempt %d/%d). First 200 chars: %s",
                    attempt + 1, retries + 1, repr(raw[:200])
                )
                if attempt < retries:
                    time.sleep(2)
                    continue
                return None

            except Exception as e:
                logger.error("LLM call error: %s", e)
                if attempt < retries:
                    time.sleep(2)
                    continue
                return None

        return None

    def classify_page(self, text: str, url: str) -> dict:
        if self.mock:
            return {
                "type": "listing_page",
                "has_properties": True,
                "should_extract": True,
            }

        system = (
            "You analyse real-estate web pages. "
            'Return ONLY valid JSON (no extra text): '
            '{"type": "listing_page" or "detail_page" or '
            '"community_page" or "navigation_page" or "irrelevant", '
            '"has_properties": true/false, '
            '"should_extract": true/false, '
            '"summary": "brief description"}. '
        )
        result = self._call(system, f"URL: {url}\n\n{text[:5000]}")
        if isinstance(result, dict):
            return result
        return {
            "type": "unknown",
            "has_properties": False,
            "should_extract": False,
        }

    def pick_links(self, links: list[dict], source_url: str) -> list[dict]:
        if self.mock:
            kw = (
                "communit home residen property listing collection "
                "neighbourhood neighborhood project floorplan model"
            ).split()
            out = []
            for link in links:
                blob = (link.get("text", "") + " " + link.get("href", "")).lower()
                if any(k in blob for k in kw):
                    out.append({
                        "url": link["href"],
                        "reason": "keyword match",
                        "priority": 0.7,
                    })
            return out[:15]

        lines = "\n".join(
            f'- "{l.get("text","").strip()[:80]}" -> {l["href"]}'
            for l in links[:120]
        )
        system = (
            f"You are a web-navigation agent for real-estate sites.\n"
            f"Source page: {source_url}\n"
            "Pick links most likely to lead to property listings, "
            "community pages, pricing, or floor-plans.\n"
            'Return ONLY a JSON array: [{"url":"...","reason":"...","priority":0.0-1.0}]. '
            "Only relevant links. No extra text."
        )
        result = self._call(system, lines)
        if isinstance(result, list):
            return result
        return []

    def extract_properties(self, content: str, url: str) -> list[dict]:
        if self.mock:
            domain = urlparse(url).netloc
            return [{
                "name": f"Mock Property ({domain})",
                "location": "GTA, ON",
                "price": "$999,000",
                "status": "Available",
                "url": url,
                "image_url": "",
                "all_image_urls": [],
                "description": "Mock entry for testing",
                "property_type": "Detached",
                "builder": domain,
            }]

        system = (
            f"Extract property listings from this scraped page ({url}).\n"
            "Return ONLY a JSON array. Each object:\n"
            "  name, location, price (string), status, "
            f'url ("{url}"), '
            "image_url (primary image URL), "
            "all_image_urls (array of ALL related image URLs for this property), "
            "description, property_type, builder.\n"
            "Match images to properties using alt text and context.\n"
            "If nothing found return []. No extra text, just the JSON array."
        )
        result = self._call(system, content)
        if isinstance(result, list):
            return result
        return []


# =============================================================================
#  INTELLIGENT CRAWLER
# =============================================================================
class IntelligentCrawler:

    def __init__(
        self,
        llm: LLMClient,
        store: LocalStore,
        downloader: ImageDownloader,
        max_depth: int = 2,
        max_pages: int = 15,
        delay: float = 2.0,
    ):
        self.llm = llm
        self.store = store
        self.downloader = downloader
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.delay = delay

    async def crawl_site(self, seed_url: str, browser) -> list[dict]:
        domain = urlparse(seed_url).netloc
        visited: set[str] = set()
        pages_done = 0
        all_props: list[dict] = []

        # seed queue + learned URLs from past runs
        queue: list[tuple[str, int]] = [(seed_url, 0)]
        for learned in self.store.get_learned_urls(domain):
            if learned["url"] != seed_url:
                queue.append((learned["url"], 1))

        page = await browser.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            while queue and pages_done < self.max_pages:
                url, depth = queue.pop(0)
                norm = url.rstrip("/")
                if norm in visited or depth > self.max_depth:
                    continue
                if urlparse(url).netloc != domain:
                    continue

                visited.add(norm)
                pages_done += 1

                logger.info(
                    "  [depth %d/%d] (page %d/%d) %s",
                    depth, self.max_depth, pages_done, self.max_pages, url
                )

                try:
                    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    await self._lazy_scroll(page)

                    content = await self._grab_content(page)
                    c_hash = make_hash(content["text"][:5000])

                    # screenshot every page
                    screenshot_path = await self.downloader.save_screenshot(page, url)

                    # skip unchanged
                    prev_hash = self.store.get_page_hash(url)
                    if prev_hash == c_hash:
                        logger.info("    -- page unchanged, skipping extraction")
                        self.store.log_crawl(url, c_hash, 0, screenshot_path)
                        continue

                    # 1) classify
                    analysis = self.llm.classify_page(content["combined"][:5000], url)
                    ptype = analysis.get("type", "unknown")
                    should_extract = analysis.get("should_extract", False)
                    logger.info(
                        "    page_type=%s  should_extract=%s",
                        ptype, should_extract
                    )

                    self.store.save_site_map_url(
                        domain, url, ptype,
                        priority=0.85 if analysis.get("has_properties") else 0.3,
                    )

                    # 2) extract properties
                    props = []
                    if should_extract:
                        props = self.llm.extract_properties(content["combined"], url)
                        if props:
                            logger.info("    >> extracted %d properties", len(props))

                        # download images for each
                        for prop in props:
                            self._download_images_for_property(
                                prop, domain, content["images"]
                            )

                        all_props.extend(props)

                    self.store.log_crawl(url, c_hash, len(props), screenshot_path)

                    # 3) discover child links
                    if depth < self.max_depth:
                        links = await self._grab_links(page)
                        ranked = self.llm.pick_links(links, url)
                        ranked.sort(key=lambda x: x.get("priority", 0), reverse=True)
                        for item in ranked:
                            child = item.get("url", "")
                            if child and child.rstrip("/") not in visited:
                                queue.append((child, depth + 1))

                    await asyncio.sleep(self.delay)

                except Exception as exc:
                    logger.warning("    ERROR on %s: %s", url, exc)

        finally:
            await page.close()

        return all_props

    def _download_images_for_property(
        self, prop: dict, domain: str, page_images: list[dict]
    ):
        """Collect image URLs, download them, attach to prop dict."""
        image_urls: list[str] = []

        # from LLM
        if prop.get("image_url"):
            image_urls.append(prop["image_url"])
        for u in (prop.get("all_image_urls") or []):
            if u and u not in image_urls:
                image_urls.append(u)

        # match from page images by property name
        prop_name = (prop.get("name") or "").lower()
        if prop_name:
            for pimg in page_images:
                combined_text = (
                    (pimg.get("alt") or "") + " " + (pimg.get("ctx") or "")
                ).lower()
                src = pimg.get("src", "")
                if prop_name in combined_text and src not in image_urls:
                    image_urls.append(src)

        # download
        name = prop.get("name") or "unknown"
        downloaded = self.downloader.download_property_images(
            image_urls, domain, name
        )
        prop["_downloaded_images"] = downloaded
        local_paths = [d["local_path"] for d in downloaded if d.get("local_path")]
        prop["local_images"] = local_paths
        if local_paths:
            prop["thumbnail_local"] = local_paths[0]

    async def _lazy_scroll(self, page):
        for _ in range(5):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(700)
        await page.evaluate("window.scrollTo(0,0)")
        await page.wait_for_timeout(400)

    async def _grab_content(self, page) -> dict:
        text = await page.evaluate("document.body.innerText")
        text = text.replace("\n", " ").strip()

        images = await page.evaluate("""() =>
            Array.from(document.images)
                .filter(i => i.naturalWidth > 200)
                .slice(0, 30)
                .map(i => ({
                    src: i.src,
                    alt: i.alt || '',
                    ctx: (i.closest('a,div,section,article')
                          ?.innerText?.substring(0,150)
                          ?.replace(/\\n/g,' ')) || '',
                    width: i.naturalWidth,
                    height: i.naturalHeight
                }))
                .filter(i => i.src.startsWith('http'))
        """)

        combined = f"Page Text:\n{text[:10000]}\n\nImages:\n"
        for im in images:
            combined += (
                f"- {im['src']}  "
                f"alt=\"{im['alt']}\"  "
                f"size={im['width']}x{im['height']}  "
                f"ctx=\"{im['ctx']}\"\n"
            )

        return {"text": text, "images": images, "combined": combined}

    async def _grab_links(self, page) -> list[dict]:
        raw = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({
                    href: a.href,
                    text: a.innerText.trim().substring(0,100)
                }))
                .filter(l =>
                    l.href.startsWith('http')
                    && !l.href.includes('#')
                    && !/\\.(pdf|jpg|png|gif|svg|css|js|zip)$/i.test(l.href)
                )
        """)
        seen: set[str] = set()
        out = []
        for l in raw:
            n = l["href"].rstrip("/")
            if n not in seen:
                seen.add(n)
                out.append(l)
        return out


# =============================================================================
#  AGENT
# =============================================================================
class PropertyAgent:

    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")
        self.mock = os.getenv("MOCK_LLM", "false").lower() == "true"
        data_dir = os.getenv("DATA_DIR", "data")

        if not self.api_key and not self.mock:
            logger.warning("Set OPENROUTER_API_KEY in .env or use MOCK_LLM=true")

        self.store = LocalStore(base_dir=data_dir)
        self.llm = LLMClient(self.api_key, mock=self.mock)
        self.downloader = ImageDownloader(self.store)
        self.crawler = IntelligentCrawler(
            self.llm,
            self.store,
            self.downloader,
            max_depth=int(os.getenv("MAX_DEPTH", "2")),
            max_pages=int(os.getenv("MAX_PAGES", "15")),
            delay=float(os.getenv("CRAWL_DELAY", "2")),
        )
        self.seed_urls = self._load_seeds()

    @staticmethod
    def _load_seeds() -> list[str]:
        path = "seed_urls.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        defaults = [
            "https://mattamyhomes.com/ontario/gta",
            "https://www.fieldgatehomes.com/our-communities/",
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2)
        return defaults

    async def run_once(self) -> dict:
        logger.info("=" * 60)
        logger.info("CRAWL CYCLE START  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
        logger.info("=" * 60)

        all_props: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            for seed in self.seed_urls:
                logger.info("")
                logger.info("> Site: %s", seed)
                try:
                    props = await self.crawler.crawl_site(seed, browser)
                    all_props.extend(props)
                except Exception as e:
                    logger.error("  Site failed: %s", e)

            await browser.close()

        # persist
        stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "images": 0}
        for p in all_props:
            action, idx, _ = self.store.upsert_property(p)
            stats[action] = stats.get(action, 0) + 1

            # attach image records
            downloaded = p.get("_downloaded_images", [])
            if downloaded and idx >= 0:
                self.store.attach_images(idx, downloaded)
                stats["images"] += len(downloaded)

        # save everything to disk
        self.store.save_all()
        self.store.export_json()
        self._print_report(stats)
        return stats

    async def run_daily(self, hour: int = 8):
        logger.info("Daily mode -- scheduled for %02d:00 each day", hour)
        await self.run_once()

        while True:
            now = datetime.now()
            nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)

            wait = (nxt - now).total_seconds()
            logger.info("Next run: %s (in %.1f hours)", nxt.strftime("%Y-%m-%d %H:%M"), wait / 3600)
            await asyncio.sleep(wait)

            try:
                await self.run_once()
            except Exception as e:
                logger.error("Cycle failed: %s", e)

    def _print_report(self, stats: dict):
        report = self.store.get_daily_report()
        storage = self.store.get_storage_stats()

        logger.info("")
        logger.info("=" * 60)
        logger.info("RUN REPORT")
        logger.info("-" * 60)
        logger.info("  New properties ........ %d", stats.get("new", 0))
        logger.info("  Updated ............... %d", stats.get("updated", 0))
        logger.info("  Unchanged ............. %d", stats.get("unchanged", 0))
        logger.info("  Images downloaded ..... %d", stats.get("images", 0))
        logger.info("  LLM calls ............. %d", self.llm.call_count)
        logger.info("-" * 60)
        logger.info("STORAGE")
        logger.info("  Total properties ...... %d", storage["total_properties"])
        logger.info("  Active ................ %d", storage["active_properties"])
        logger.info("  Total images .......... %d", storage["total_images"])
        logger.info("  Image storage ......... %.1f MB", storage["total_image_size_mb"])
        logger.info("  Total crawls .......... %d", storage["total_crawls"])
        logger.info("  Total changes ......... %d", storage["total_changes"])

        if report["new_properties"]:
            logger.info("")
            logger.info("  NEW PROPERTIES TODAY:")
            for p in report["new_properties"]:
                logger.info(
                    "    * %s | %s | %s | %s",
                    p.get("name", "?"),
                    p.get("location", "?"),
                    p.get("price", "?"),
                    p.get("status", "?"),
                )

        if report["changes"]:
            logger.info("")
            logger.info("  CHANGES TODAY:")
            for c in report["changes"]:
                logger.info(
                    "    * %s: %s '%s' -> '%s'",
                    c.get("property_name", "?"),
                    c.get("field", "?"),
                    c.get("old_value", ""),
                    c.get("new_value", ""),
                )

        logger.info("=" * 60)
        logger.info("")


# =============================================================================
#  CLI
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Property Intelligence Agent")
    ap.add_argument("--mode", choices=["once", "daily"], default="once",
                    help="Run once or loop daily (default: once)")
    ap.add_argument("--hour", type=int, default=8,
                    help="Hour (0-23) for daily runs (default: 8)")
    ap.add_argument("--report", action="store_true",
                    help="Print today's report and exit")
    ap.add_argument("--export", action="store_true",
                    help="Export -> JSON and exit")
    ap.add_argument("--stats", action="store_true",
                    help="Print storage stats and exit")
    args = ap.parse_args()

    agent = PropertyAgent()

    if args.report:
        r = agent.store.get_daily_report()
        print(json.dumps(r, indent=2, default=str))
    elif args.export:
        agent.store.export_json()
    elif args.stats:
        s = agent.store.get_storage_stats()
        print(json.dumps(s, indent=2))
    elif args.mode == "daily":
        asyncio.run(agent.run_daily(hour=args.hour))
    else:
        asyncio.run(agent.run_once())


if __name__ == "__main__":
    main()
