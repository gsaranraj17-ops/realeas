import asyncio
import json
import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from dotenv import load_dotenv
import requests
from playwright.async_api import async_playwright
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SmartScraper")

CRAWL_DELAY = float(os.getenv("CRAWL_DELAY", 2))
DATA_DIR = os.getenv("DATA_DIR", "data")

TARGET_KEYWORDS = [
    "coming soon", "quickstart", "quick start", "quick-start",
    "pre-construction", "preconstruction",
]
REJECT_KEYWORDS = [
    "now selling", "sold out", "sold", "move-in ready", "move in ready",
    "occupied", "closed", "complete", "now available", "available now",
    "ready to move", "move-in", "movein",
]
SKIP_IMAGE_KEYWORDS = [
    "logo", "icon", "cookie", "favicon", "sprite", "social",
    "pixel", "tracking", "badge", "arrow", "button", "avatar",
    "headshot", "career", "desk-work", "woman-kitchen",
    "uwsc_sales", "integrity.", "pride.", "quality.", "/value.",
]


def is_target_status(s: str) -> bool:
    if not s:
        return False
    s = s.lower().strip()
    if any(r in s for r in REJECT_KEYWORDS):
        return False
    return any(kw in s for kw in TARGET_KEYWORDS)


def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text)[:80] or "unnamed"


# ═════════════════════════════════════════════════════════════════════════════
#  LOCAL STORAGE
# ═════════════════════════════════════════════════════════════════════════════
class LocalStore:
    def __init__(self, base_dir=DATA_DIR):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.base_dir / "communities.json"
        self.images_dir = self.base_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.communities = self._load()

    def _load(self):
        if self.file.exists():
            with open(self.file, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def save(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self.communities, f, indent=4, ensure_ascii=False)

    def add_community(self, data: dict):
        name = data.get("community_name", "").strip()
        url = data.get("url", "")
        for c in self.communities:
            if c.get("community_name") == name and c.get("url") == url:
                c.update(data)
                c["last_seen"] = datetime.now().isoformat()
                return
        data["id"] = len(self.communities) + 1
        data["first_seen"] = datetime.now().isoformat()
        data["last_seen"] = datetime.now().isoformat()
        data["source_domain"] = urlparse(url).netloc
        self.communities.append(data)
        ti = len(data.get("all_images", []))
        for p in data.get("properties", []):
            ti += len(p.get("property_images", []))
        logger.info("  ✓ SAVED: %s | %s | %d units | %d images",
                     name, data.get("status"), len(data.get("properties", [])), ti)

    def export(self, path="detailed_properties.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.communities, f, indent=4, ensure_ascii=False)
        logger.info("✓ Exported %d communities to %s", len(self.communities), path)


# ═════════════════════════════════════════════════════════════════════════════
#  IMAGE DOWNLOADER
# ═════════════════════════════════════════════════════════════════════════════
class ImageDownloader:
    def __init__(self, images_dir: Path):
        self.images_dir = images_dir
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def download(self, url, community_slug, domain_slug, subfolder, index):
        if not url or any(kw in url.lower() for kw in SKIP_IMAGE_KEYWORDS):
            return None
        if url.lower().endswith(".svg"):
            return None
        folder = self.images_dir / domain_slug / community_slug
        if subfolder:
            folder = folder / subfolder
        folder.mkdir(parents=True, exist_ok=True)
        try:
            resp = self.session.get(url, timeout=30, stream=True)
            if resp.status_code != 200:
                return None
            ext = ".jpg"
            ct = resp.headers.get("Content-Type", "").lower()
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            fp = folder / f"img_{index:03d}{ext}"
            with open(fp, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            if fp.stat().st_size < 2048:
                fp.unlink()
                return None
            return str(fp).replace("\\", "/")
        except Exception:
            return None


# ═════════════════════════════════════════════════════════════════════════════
#  LLM AGENT — minimal calls, link-based filtering
# ═════════════════════════════════════════════════════════════════════════════
class LLMAgent:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)
        self.calls = 0

    def _call(self, system: str, user: str, max_tokens: int = 4096):
        self.calls += 1
        last_err = None
        for attempt in range(4):
            try:
                resp = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user[:28000]},
                    ],
                    temperature=0,
                    max_tokens=max_tokens,
                    timeout=60,
                )
                raw = resp.choices[0].message.content.strip()
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0].strip()
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0].strip()
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("LLM #%d JSON error: %s", self.calls, e)
                return None
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                logger.warning("LLM #%d attempt %d: %s — retry in %ds",
                                self.calls, attempt + 1, e, wait)
                time.sleep(wait)
        logger.error("LLM #%d failed after 4 attempts: %s", self.calls, last_err)
        return None

    def identify_targets_from_links(self, all_links: list[dict]) -> list[dict]:
        """Legacy method — kept for compatibility."""
        return []

    def identify_targets_from_links_and_text(self, page_text: str, links_text: str, base_url: str) -> list[dict]:
        """ONE call: give the LLM the communities listing page text + links.
        It reads the section headers (COMING SOON, QUICKSTART, NOW AVAILABLE, SOLD OUT)
        and returns which community links fall under COMING SOON or QUICKSTART."""

        system = f"""You are reading a real-estate builder's communities listing page.
The page has sections with headers like "NOW AVAILABLE", "COMING SOON", "QUICKSTART", "SOLD OUT".
Under each section header are community cards with links.

Your job: identify which community links are under the "COMING SOON" or "QUICKSTART" sections.

From the page text and links below, return ONLY communities that appear under:
- "COMING SOON" section → status = "COMING SOON"
- "QUICKSTART" section → status = "QUICKSTART"

EXCLUDE communities under:
- "NOW AVAILABLE" or "NOW SELLING" sections
- "SOLD OUT" section
- "MOVE IN READY" section
- Footer links, navigation links, privacy policy, accessibility, contact pages

For each target, return the community detail page URL (not register/contact links).
Base URL: {base_url}

Return JSON array:
[{{"url": "full url to community detail page", "name": "community name", "status": "COMING SOON or QUICKSTART"}}]

Return [] if none found."""

        user = f"PAGE TEXT:\n{page_text[:8000]}\n\nLINKS ON PAGE:\n{links_text[:5000]}"
        result = self._call(system, user, max_tokens=2048)
        if not isinstance(result, list):
            return []

        validated = []
        seen = set()
        for item in result:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").split("#")[0].rstrip("/")
            status = (item.get("status") or "").upper().strip()
            name = item.get("name", "")
            if not url or url in seen:
                continue
            # Must be COMING SOON or QUICKSTART
            if status not in ("COMING SOON", "QUICKSTART"):
                continue
            # Skip non-community pages
            path = urlparse(url).path.rstrip("/").lower()
            if path in ("/", "", "/communities", "/quickstart", "/contact",
                         "/privacy-policy", "/accessibility", "/about"):
                continue
            seen.add(url)
            validated.append({"url": url, "name": name, "status": status})
        return validated

    def extract_community(self, text: str, url: str) -> dict | None:
        system = f"""Extract ALL data from this Coming Soon / QuickStart / Pre-Construction community page.

Return JSON:
{{
    "community_name": "Development Name",
    "location": "City, Province/State",
    "builder": "Builder/Developer Name",
    "status": "COMING SOON / QUICKSTART / PRE-CONSTRUCTION",
    "url": "{url}",
    "description": "Full description",
    "price_range": "From $XXX to $XXX or starting from $XXX",
    "completion_date": "Expected completion if mentioned",
    "total_units": "Total units if mentioned",
    "features": ["feature1", "feature2"],
    "amenities": ["amenity1", "amenity2"],
    "property_types": ["Detached", "Townhome", "Condo"],
    "contact_phone": "Phone if shown",
    "properties": [
        {{
            "address": "Address / Lot # / Unit #",
            "floorplan": "Model name",
            "price": "Price or Inquire",
            "status": "Unit status",
            "bedrooms": "X",
            "bathrooms": "X",
            "sqft": "XXX",
            "garage": "X car",
            "description": "Description",
            "features": ["feature1"]
        }}
    ]
}}

Extract EVERY unit/lot/home shown. If none listed, properties=[].
Only extract what's on the page — do not invent data."""

        result = self._call(system, text[:22000])
        if isinstance(result, dict) and result.get("community_name"):
            return result
        return None

    def extract_property(self, text: str, url: str) -> dict | None:
        system = f"""Extract ALL details from this individual property/unit page.

Return JSON:
{{
    "address": "Full address or Unit/Lot number",
    "floorplan": "Floor plan or model name",
    "price": "Price",
    "status": "Availability status",
    "bedrooms": "Number",
    "bathrooms": "Number",
    "sqft": "Square footage",
    "garage": "Garage info",
    "lot_size": "Lot size",
    "stories": "Number of stories",
    "description": "Full description",
    "features": ["All features, upgrades, finishes"],
    "specifications": {{}}
}}

Extract ALL details. Do not invent data. URL: {url}"""

        result = self._call(system, text[:22000])
        if isinstance(result, dict):
            return result
        return None

    def find_property_links(self, links: list[dict], page_text: str, community_url: str) -> list[str]:
        """Find links to individual property/unit detail pages within a community."""
        community_path = urlparse(community_url).path.rstrip("/")
        system = f"""From these links on a community page, find links to INDIVIDUAL property/unit/lot detail pages.

The current community page is: {community_url}

INCLUDE:
- Links to specific units, lots, homes that are SUB-PAGES of this community
- URLs that extend the current community path (e.g. {community_path}/lot-38)
- "View Details", "Learn More", "View Home" for specific properties

EXCLUDE:
- Links to OTHER communities (e.g. /communities/greenwich, /communities/pier-house — these are NOT property pages)
- The community page itself: {community_url}
- PDF files (any URL ending in .pdf)
- Links with # fragments (same-page anchors)
- Menu/navigation links to other sections of the site
- Contact, register, email, phone links
- Gallery links, "Download" links
- Any link whose URL path does NOT start with {community_path}

Return JSON array of absolute URLs. Max 25. Return [] if no property detail pages exist."""

        lines = [f"{l['url']} | {l['text']}" for l in links if not l['url'].lower().endswith('.pdf')]
        user = f"Community page text:\n{page_text[:2000]}\n\nLinks:\n" + "\n".join(lines)
        result = self._call(system, user, max_tokens=1024)
        if not isinstance(result, list):
            return []
        community_base = community_url.split("#")[0].rstrip("/")
        cleaned = []
        seen = set()
        for link in result:
            if not isinstance(link, str):
                continue
            clean = link.split("#")[0].rstrip("/")
            if (clean and clean not in seen and clean != community_base
                    and not clean.lower().endswith('.pdf')):
                # Hard filter: only keep links that are sub-paths of the community URL
                clean_path = urlparse(clean).path.rstrip("/")
                if clean_path.startswith(community_path + "/") or clean_path.startswith(community_path.replace("/communities/", "/quickstart/") + "/"):
                    seen.add(clean)
                    cleaned.append(clean)
                else:
                    logger.info("    ✗ Rejected property link (not a sub-page): %s", clean)
        return cleaned

    def filter_images(self, images: list[dict], name: str, url: str) -> list[dict]:
        """Filter images to keep only ones relevant to this specific community/property."""
        if not images or len(images) <= 3:
            return images

        entries = []
        for i, img in enumerate(images):
            entries.append(f"{i}: {img.get('src','')} | alt={img.get('alt','')}")

        system = f"""Filter images for the real-estate community/property: "{name}"
URL: {url}

KEEP: renderings, exterior/interior photos of THIS community's homes,
      floor plans, site plans, gallery images, lot-specific photos.
      Check the URL path — images with the community name or lot numbers in the path are relevant.

REJECT:
- Generic site-wide images that appear on every page (e.g. /uploads/2021/03/Value.jpg, /uploads/2020/09/Quality.png)
- Stock lifestyle photos (people at desks, woman on phone, generic kitchen/bathroom not specific to this community)
- Company branding, headshots, career images
- Images from OTHER communities (different community name in URL path)
- Sales centre photos from other projects (e.g. UWSC_, Casa_, Fifty_, Homestead_)

Return JSON array of integer indices to KEEP. Example: [0, 2, 5]"""

        user = "\n".join(entries)
        result = self._call(system, user, max_tokens=1024)
        if not isinstance(result, list):
            return images
        filtered = [images[i] for i in result if isinstance(i, int) and 0 <= i < len(images)]
        return filtered if filtered else images


# ═════════════════════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ═════════════════════════════════════════════════════════════════════════════
class PopupHandler:
    def __init__(self, page):
        self.page = page

    async def setup(self):
        self.page.context.on("page", lambda p: asyncio.create_task(self._close(p)))
        await self.page.add_init_script("""
            window.open = () => null;
            window.alert = () => {};
            window.confirm = () => true;
        """)

    async def _close(self, popup):
        try:
            await popup.close()
        except Exception:
            pass

    async def dismiss(self):
        for sel in ['button:has-text("Accept")', 'button:has-text("Accept All")',
                     'button:has-text("Agree")', '#onetrust-accept-btn-handler']:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=2000)
                    await self.page.wait_for_timeout(400)
                    break
            except Exception:
                continue
        for sel in ['button[aria-label*="close" i]', 'button.close',
                     '.modal-close', '[data-dismiss="modal"]']:
            try:
                for b in await self.page.query_selector_all(sel):
                    if await b.is_visible():
                        await b.click(timeout=1000)
                        await self.page.wait_for_timeout(200)
            except Exception:
                continue
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass


async def load_page(page, url: str, ph: PopupHandler) -> bool:
    if url.lower().endswith('.pdf'):
        logger.info("    Skipping PDF: %s", url)
        return False
    for attempt in range(2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)
            await ph.dismiss()
            return True
        except Exception as e:
            if attempt == 0:
                logger.warning("    Retry loading %s: %s", url, e)
                await asyncio.sleep(2)
            else:
                logger.error("    Failed to load %s: %s", url, e)
    return False


async def smart_scroll(page, rounds=8):
    for i in range(rounds):
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(700)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


async def get_text(page) -> str:
    try:
        return await page.evaluate("document.body.innerText")
    except Exception:
        return ""


async def get_links(page) -> list[dict]:
    """Get all links as [{url, text}], filtering out anchors and junk."""
    try:
        raw = await page.evaluate("""() => {
            const pageBase = location.origin + location.pathname;
            const results = [];
            const seen = new Set();
            document.querySelectorAll('a[href]').forEach(a => {
                const hrefAttr = a.getAttribute('href') || '';
                if (hrefAttr.startsWith('#') || hrefAttr.startsWith('javascript:')
                    || hrefAttr.startsWith('mailto:') || hrefAttr.startsWith('tel:')) return;
                const url = a.href.split('#')[0];
                if (!url || url === pageBase || url === pageBase + '/' || seen.has(url)) return;
                seen.add(url);
                const text = (a.innerText || a.getAttribute('aria-label')
                              || a.getAttribute('title') || '').trim().substring(0, 120);
                results.push({url, text});
            });
            return results;
        }""")
        return raw
    except Exception:
        return []


async def get_section_tagged_links(page) -> list[dict]:
    """Extract links grouped by their section header on the page.
    Returns [{url, text, section}] where section is the nearest preceding
    heading text (e.g. 'COMING SOON', 'NOW AVAILABLE', 'QUICKSTART', 'SOLD OUT')."""
    try:
        raw = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            // Walk through all elements in DOM order
            let currentSection = '';
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT, null, false
            );
            let node;
            while (node = walker.nextNode()) {
                const tag = node.tagName.toLowerCase();
                // Detect section headers (h1-h6, or elements with large bold text)
                if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
                    const hText = node.innerText.trim().toUpperCase();
                    if (hText.includes('COMING SOON') || hText === 'COMING SOON') {
                        currentSection = 'COMING SOON';
                    } else if (hText.includes('QUICKSTART') || hText.includes('QUICK START')) {
                        currentSection = 'QUICKSTART';
                    } else if (hText.includes('NOW AVAILABLE') || hText.includes('NOW SELLING')) {
                        currentSection = 'NOW AVAILABLE';
                    } else if (hText.includes('SOLD OUT')) {
                        currentSection = 'SOLD OUT';
                    } else if (hText.includes('MOVE IN READY') || hText.includes('MOVE-IN READY')) {
                        currentSection = 'MOVE IN READY';
                    }
                }
                // Also check for section-like text in non-heading elements
                if (tag === 'p' || tag === 'span' || tag === 'div') {
                    const t = node.innerText.trim().toUpperCase();
                    if (t === 'COMING SOON' || t === 'QUICKSTART' || t === 'QUICK START'
                        || t === 'NOW AVAILABLE' || t === 'SOLD OUT') {
                        currentSection = t === 'QUICK START' ? 'QUICKSTART' : t;
                    }
                }
                // Collect links
                if (tag === 'a' && node.href) {
                    const hrefAttr = node.getAttribute('href') || '';
                    if (hrefAttr.startsWith('#') || hrefAttr.startsWith('javascript:')
                        || hrefAttr.startsWith('mailto:') || hrefAttr.startsWith('tel:')) continue;
                    const url = node.href.split('#')[0];
                    if (url && !seen.has(url)) {
                        seen.add(url);
                        const text = (node.innerText || node.getAttribute('aria-label')
                                      || node.getAttribute('title') || '').trim().substring(0, 200);
                        results.push({url, text, section: currentSection});
                    }
                }
            }
            return results;
        }""")
        return raw
    except Exception:
        return []


async def get_content_images(page) -> list[dict]:
    """Get ALL visible images from the page. Only skip by URL keywords — no DOM position filtering
    since many builder sites put images in unusual containers."""
    try:
        return await page.evaluate("""() => {
            const skipUrl = [
                'logo','icon','cookie','favicon','sprite','social','pixel',
                'tracking','badge','arrow','button','avatar','/value.',
            ];
            const imgs = [], seen = new Set();
            
            // All <img> tags
            document.querySelectorAll('img').forEach(i => {
                if (i.naturalWidth > 200 && i.naturalHeight > 100 && i.src && !seen.has(i.src)) {
                    const s = i.src.toLowerCase();
                    if (!s.endsWith('.svg') && !skipUrl.some(k => s.includes(k))) {
                        seen.add(i.src);
                        imgs.push({src:i.src, width:i.naturalWidth,
                                   height:i.naturalHeight, alt:i.alt||''});
                    }
                }
            });
            
            // CSS background images on any element with significant size
            document.querySelectorAll('*').forEach(el => {
                if (el.offsetWidth < 200 || el.offsetHeight < 100) return;
                const bg = getComputedStyle(el).backgroundImage;
                if (bg && bg !== 'none' && bg.includes('url(')) {
                    const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
                    if (m && m[1] && !seen.has(m[1])) {
                        const s = m[1].toLowerCase();
                        if (!s.endsWith('.svg') && !skipUrl.some(k => s.includes(k))
                            && s.startsWith('http')) {
                            seen.add(m[1]);
                            imgs.push({src:m[1], width:el.offsetWidth,
                                       height:el.offsetHeight, alt:''});
                        }
                    }
                }
            });
            
            return imgs.sort((a,b) => (b.width*b.height)-(a.width*a.height));
        }""")
    except Exception:
        return []


# ═════════════════════════════════════════════════════════════════════════════
#  SCRAPER — simple 2-step approach
# ═════════════════════════════════════════════════════════════════════════════
class Scraper:
    """
    Step 1: Visit /communities page, get text + links.
            ONE LLM call to identify which communities are under COMING SOON / QUICKSTART sections.
            Also visit /quickstart to find quickstart sub-pages by URL.
    Step 2: Visit only those target URLs, scrape details + images.
    """

    def __init__(self, agent: LLMAgent, store: LocalStore, downloader: ImageDownloader):
        self.agent = agent
        self.store = store
        self.dl = downloader

    async def run(self, seed_url: str, browser):
        domain = urlparse(seed_url).netloc
        base = f"{urlparse(seed_url).scheme}://{domain}"

        logger.info("\n" + "=" * 70)
        logger.info("STEP 1 — Finding Coming Soon & QuickStart communities")
        logger.info("=" * 70)

        targets = await self._find_targets(seed_url, base, domain, browser)

        if not targets:
            logger.info("  No coming soon / quickstart communities found")
            return

        logger.info("\n  ★ Found %d targets:", len(targets))
        for t in targets:
            logger.info("    %s | %s | %s", t["status"], t["name"], t["url"])

        logger.info("\n" + "=" * 70)
        logger.info("STEP 2 — Scraping %d target communities", len(targets))
        logger.info("=" * 70)

        await self._scrape_targets(targets, browser)

    async def _find_targets(self, seed_url, base, domain, browser) -> list[dict]:
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = await ctx.new_page()
        ph = PopupHandler(page)
        await ph.setup()

        targets = {}  # url -> {url, name, status}

        try:
            # 1) Visit the communities listing page
            communities_url = seed_url.rstrip("/")
            # Normalize to /communities if seed is the homepage
            if urlparse(communities_url).path.rstrip("/") in ("", "/"):
                communities_url = base + "/communities"

            logger.info("  Loading communities page: %s", communities_url)
            if await load_page(page, communities_url, ph):
                await smart_scroll(page, rounds=5)

                page_text = await get_text(page)
                page_links = await get_links(page)

                # Format links for LLM
                link_lines = []
                for l in page_links:
                    if urlparse(l["url"]).netloc == domain:
                        link_lines.append(f'{l["url"]} | {l["text"][:100]}')

                # ONE LLM call: parse the page sections
                logger.info("  Asking LLM to identify targets from page sections...")
                llm_targets = self.agent.identify_targets_from_links_and_text(
                    page_text, "\n".join(link_lines), base
                )
                for t in llm_targets:
                    url = t["url"].split("#")[0].rstrip("/")
                    if url not in targets and urlparse(url).netloc == domain:
                        targets[url] = t
                        logger.info("    ★ %s | %s", t["status"], t["name"])

            await asyncio.sleep(CRAWL_DELAY)

            # 2) Visit /quickstart to find quickstart sub-pages by URL pattern
            qs_url = base + "/quickstart"
            logger.info("  Loading quickstart page: %s", qs_url)
            if await load_page(page, qs_url, ph):
                await smart_scroll(page, rounds=4)
                qs_links = await get_links(page)
                for l in qs_links:
                    url = l["url"].split("#")[0].rstrip("/")
                    path = urlparse(url).path.rstrip("/").lower()
                    if (urlparse(url).netloc == domain
                            and "/quickstart/" in path
                            and path != "/quickstart"
                            and url not in targets):
                        name = l["text"].strip() or path.split("/")[-1].replace("-", " ").title()
                        # Clean name
                        for rm in ["CALL NOW", "EMAIL NOW", "REGISTER", "CONTACT", "DOWNLOAD"]:
                            name = name.replace(rm, "").replace(rm.lower(), "")
                        name = " ".join(name.split()).strip(" ·-–—/\\|")
                        if len(name) < 3:
                            name = path.split("/")[-1].replace("-", " ").title()
                        targets[url] = {"url": url, "name": name, "status": "QUICKSTART"}
                        logger.info("    ★ QUICKSTART | %s", name)

        finally:
            await page.close()
            await ctx.close()

        return list(targets.values())


    async def _scrape_targets(self, targets: list[dict], browser):
        """Visit each target community page, extract details + images."""
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = await ctx.new_page()
        ph = PopupHandler(page)
        await ph.setup()

        # Deduplicate targets by community name (keep first occurrence)
        seen_names = set()
        unique_targets = []
        for t in targets:
            name_key = t["name"].lower().strip()
            if name_key not in seen_names:
                seen_names.add(name_key)
                unique_targets.append(t)
        targets = unique_targets

        try:
            for idx, target in enumerate(targets, 1):
                url = target["url"]
                logger.info("\n" + "=" * 70)
                logger.info("[%d/%d] %s | %s", idx, len(targets), target["name"], target["status"])
                logger.info("  URL: %s", url)
                logger.info("=" * 70)

                try:
                    if not await load_page(page, url, ph):
                        continue

                    await smart_scroll(page, rounds=10)
                    text = await get_text(page)

                    # Extract community data
                    logger.info("  → Extracting details...")
                    data = self.agent.extract_community(text, url)
                    if not data:
                        logger.warning("  ✗ Failed to extract data")
                        continue

                    # Validate status — use original target status if LLM extraction changed it
                    extracted_status = data.get("status", "")
                    original_status = target.get("status", "")
                    if not is_target_status(extracted_status):
                        # If the LLM changed the status but we know it's a target, use original
                        if is_target_status(original_status):
                            data["status"] = original_status
                            logger.info("  → Overriding extracted status '%s' with original '%s'",
                                         extracted_status, original_status)
                        else:
                            logger.warning("  ✗ Status '%s' not coming soon/quickstart — skip", extracted_status)
                            continue
                    # Preserve quickstart status — don't let LLM downgrade to "COMING SOON"
                    if "quickstart" in original_status.lower() and "quickstart" not in data["status"].lower():
                        data["status"] = "QUICKSTART"

                    domain_slug = slugify(urlparse(url).netloc)
                    comm_slug = slugify(data.get("community_name", ""))

                    # ── Images ────────────────────────────────────────────
                    logger.info("  → Collecting images...")
                    images = await get_content_images(page)
                    logger.info("  → %d raw images found, filtering...", len(images))
                    images = self.agent.filter_images(images, data.get("community_name", ""), url)
                    logger.info("  → %d images after filtering", len(images))

                    downloaded = []
                    for i, img in enumerate(images[:25]):
                        local = self.dl.download(img["src"], comm_slug, domain_slug, "", i)
                        if local:
                            downloaded.append({
                                "url": img["src"], "local_path": local,
                                "alt": img.get("alt", ""),
                                "width": img.get("width"), "height": img.get("height"),
                            })
                    data["all_images"] = downloaded
                    logger.info("  ✓ %d images downloaded", len(downloaded))

                    # ── Property sub-pages ────────────────────────────────
                    logger.info("  → Looking for individual property pages...")
                    page_links = await get_links(page)
                    prop_urls = self.agent.find_property_links(page_links, text, url)
                    logger.info("  → %d property links found", len(prop_urls))

                    properties = data.get("properties", [])

                    for pi, purl in enumerate(prop_urls[:20], 1):
                        logger.info("    [%d/%d] %s", pi, min(len(prop_urls), 20), purl)
                        try:
                            if not await load_page(page, purl, ph):
                                continue
                            await smart_scroll(page, rounds=5)
                            ptxt = await get_text(page)
                            pdata = self.agent.extract_property(ptxt, purl)
                            if not pdata:
                                logger.info("      ✗ No data")
                                continue

                            # Property images
                            pimgs = await get_content_images(page)
                            pname = pdata.get("address") or pdata.get("floorplan") or f"unit_{pi}"
                            pimgs = self.agent.filter_images(pimgs, pname, purl)
                            psub = slugify(pname)
                            pdl = []
                            for i, img in enumerate(pimgs[:10]):
                                local = self.dl.download(img["src"], comm_slug, domain_slug, psub, i)
                                if local:
                                    pdl.append({"url": img["src"], "local_path": local, "alt": img.get("alt", "")})

                            pdata["property_images"] = pdl
                            pdata["property_url"] = purl
                            if pdl:
                                pdata["local_image"] = pdl[0]["local_path"]
                            properties.append(pdata)
                            logger.info("      ✓ %s | %d images", pname, len(pdl))
                            await asyncio.sleep(CRAWL_DELAY)
                        except Exception as e:
                            logger.warning("      ERROR: %s", e)

                    data["properties"] = properties

                    # Assign community images to properties that have none
                    # For quickstart pages, community images are often lot-specific
                    comm_images = data.get("all_images", [])
                    if comm_images:
                        for prop in properties:
                            if prop.get("property_images"):
                                continue  # already has images from sub-page
                            # Try to match by lot/unit number in the image URL
                            addr = (prop.get("address") or "").lower()
                            floorplan = (prop.get("floorplan") or "").lower()
                            matched = []
                            for img in comm_images:
                                img_url = img.get("url", "").lower()
                                # Match lot number: "lot 38" matches "lot38" in URL
                                for term in [addr, floorplan]:
                                    # Extract numbers from address like "Unit 38" or "Lot 42"
                                    nums = re.findall(r'\d+', term)
                                    for num in nums:
                                        if f"lot{num}" in img_url or f"lot-{num}" in img_url or f"unit{num}" in img_url:
                                            matched.append(img)
                                            break
                            if matched:
                                prop["property_images"] = matched
                                prop["image_url"] = matched[0]["url"]
                                if matched[0].get("local_path"):
                                    prop["local_image"] = matched[0]["local_path"]
                            elif comm_images:
                                # Fallback: assign first community image
                                prop["image_url"] = comm_images[0]["url"]
                                if comm_images[0].get("local_path"):
                                    prop["local_image"] = comm_images[0]["local_path"]

                    self.store.add_community(data)

                    tpi = sum(len(p.get("property_images", [])) for p in properties)
                    logger.info("\n  ✓ DONE: %d properties, %d+%d images",
                                 len(properties), len(downloaded), tpi)
                    await asyncio.sleep(CRAWL_DELAY)

                except Exception as e:
                    logger.error("  ERROR: %s", e)

        finally:
            await page.close()
            await ctx.close()


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════
async def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: Set OPENAI_API_KEY in .env")
        return

    script_dir = Path(__file__).parent
    seed_file = script_dir / "seed_urls.json"
    if seed_file.exists():
        with open(seed_file, "r") as f:
            seeds = json.load(f)
        logger.info("Loaded %d seed URLs from %s", len(seeds), seed_file)
    else:
        seeds = ["https://www.branthaven.com/communities"]
        logger.info("No seed_urls.json — using default")

    logger.info("=" * 70)
    logger.info("SMART PROPERTY SCRAPER")
    logger.info("  Step 1: Collect links → LLM picks targets in ONE call")
    logger.info("  Step 2: Scrape only coming soon / quickstart pages")
    logger.info("=" * 70)

    store = LocalStore()
    agent = LLMAgent(api_key)
    dl = ImageDownloader(store.images_dir)
    scraper = Scraper(agent, store, dl)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-popup-blocking", "--no-first-run", "--disable-extensions"],
        )
        for seed in seeds:
            logger.info("\n\n" + "=" * 70)
            logger.info("SITE: %s", seed)
            logger.info("=" * 70)
            try:
                await scraper.run(seed, browser)
            except Exception as e:
                logger.error("Site failed: %s — %s", seed, e)
        await browser.close()

    store.save()
    store.export()

    logger.info("\n" + "=" * 70)
    logger.info("COMPLETE — %d communities, %d LLM calls",
                 len(store.communities), agent.calls)
    logger.info("=" * 70)
    for c in store.communities:
        props = c.get("properties", [])
        ci = len(c.get("all_images", []))
        pi = sum(len(p.get("property_images", [])) for p in props)
        logger.info("  ★ %s | %s | %d units | %d+%d imgs",
                     c.get("community_name"), c.get("status"), len(props), ci, pi)


if __name__ == "__main__":
    asyncio.run(main())
