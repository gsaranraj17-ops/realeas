import os
import json
import re
import requests
import argparse
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import dotenv

dotenv.load_dotenv()

def parse_relative_date(text):
    """
    Parses relative dates like "2 days ago", "just now", "yesterday".
    Returns a YYYY-MM-DD string or None.
    """
    text = text.lower()
    today = datetime.now()

    if "just now" in text or "today" in text or "hours ago" in text or "minutes ago" in text:
        return today.strftime("%Y-%m-%d")

    if "yesterday" in text:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    match = re.search(r'(\d+)\s+days?\s+ago', text)
    if match:
        days = int(match.group(1))
        return (today - timedelta(days=days)).strftime("%Y-%m-%d")

    match = re.search(r'(\d+)\s+weeks?\s+ago', text)
    if match:
        weeks = int(match.group(1))
        return (today - timedelta(weeks=weeks)).strftime("%Y-%m-%d")

    return None

def parse_absolute_date(text):
    """
    Parses absolute dates like "Oct 27, 2023", "2023-10-27".
    Returns YYYY-MM-DD string or None.
    """
    # YYYY-MM-DD
    match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if match:
        return match.group(1)

    # Month DD, YYYY (e.g., Oct 27, 2023 or October 27, 2023)
    match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
    if match:
        try:
            date_str = f"{match.group(1)} {match.group(2)} {match.group(3)}"
            return datetime.strptime(date_str, "%B %d %Y").strftime("%Y-%m-%d")
        except:
            try:
                return datetime.strptime(date_str, "%b %d %Y").strftime("%Y-%m-%d")
            except:
                pass
    return None

def analyze_listing_with_llm(page_text, target_date=None):
    """
    Uses an LLM to analyze the full page text.
    Extracts amenities and the listing date.
    Decides the action ('keep', 'skip', 'stop') based on the target_date.

    Returns:
    {
        'amenities': [...],
        'listing_date': 'YYYY-MM-DD' or None,
        'action': 'keep' | 'skip' | 'stop' | 'unknown'
    }
    """
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    result = {"amenities": [], "listing_date": None, "action": "unknown"}

    if openrouter_key:
        print("  [Agent] OpenRouter API key detected. analyzing full page with LLM...")
        try:
            system_prompt = (
                f"You are a real estate scraping assistant. The user is looking for properties listed specifically on {target_date if target_date else 'ANY DATE'}.\n"
                "The list is sorted by date descending (newest first).\n"
                "Your task:\n"
                "1. Extract a list of amenities.\n"
                "2. Extract the listing date (or 'listed on' date). Convert relative dates (e.g., '2 days ago') to YYYY-MM-DD format based on today.\n"
                "3. Determine the action:\n"
                "   - If listing_date MATCHES target_date: 'keep'\n"
                "   - If listing_date is NEWER than target_date: 'skip'\n"
                "   - If listing_date is OLDER than target_date: 'stop'\n"
                "   - If target_date is not provided: 'keep'\n"
                "   - If listing_date cannot be determined: 'unknown'\n"
                "Return ONLY a JSON object with keys: 'amenities', 'listing_date', 'action'. Do not include markdown."
            )

            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost:3000",
                    "X-Title": "Realtor Agentic Scraper"
                },
                json={
                    "model": "mistralai/mistral-small-creative",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": page_text[:10000]} # Limit text length for token limits
                    ]
                },
                timeout=15
            )
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                content = content.replace("```json", "").replace("```", "").strip()
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        result.update(parsed)
                        return result
                except:
                    pass
            else:
                 print(f"  [Agent] OpenRouter API Error: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"  [Agent] OpenRouter Extraction Failed: {e}")

    elif gemini_key:
        print("  [Agent] Gemini API key detected. analyzing full page with LLM...")
        try:
            url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={gemini_key}"

            prompt_text = (
                f"Analyze this property listing. Target Date: {target_date if target_date else 'None'}. "
                "Extract 'amenities' (list) and 'listing_date' (YYYY-MM-DD). "
                "Decide 'action': 'keep' (match), 'skip' (newer), 'stop' (older), or 'unknown'. "
                "Assume list is sorted newest first. Return JSON."
                f"\n\nPage Text: {page_text[:10000]}"
            )

            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "response_schema": {
                        "type": "object",
                        "properties": {
                            "amenities": {"type": "array", "items": {"type": "string"}},
                            "listing_date": {"type": "string"},
                            "action": {"type": "string", "enum": ["keep", "skip", "stop", "unknown"]}
                        }
                    }
                }
            }

            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()

            parsed = json.loads(response.json()['candidates'][0]['content']['parts'][0]['text'])
            result.update(parsed)
            return result

        except Exception as e:
            print(f"  [Agent] Gemini Extraction Failed: {e}")

    else:
        # Fallback Logic if no LLM
        pass

    # Fallback / Simulated Agent Logic
    keywords = ['pool', 'gym', 'fireplace', 'garage', 'waterfront', 'balcony', 'garden', 'parking', 'sauna']
    desc_lower = page_text.lower()
    for word in keywords:
        if word in desc_lower:
            result['amenities'].append(word.capitalize())

    # Fallback date extraction
    abs_date = parse_absolute_date(page_text)
    extracted_date = abs_date if abs_date else parse_relative_date(page_text)

    result['listing_date'] = extracted_date

    # Determine Action manually if LLM failed
    if target_date and extracted_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            extracted_dt = datetime.strptime(extracted_date, "%Y-%m-%d")

            if extracted_dt > target_dt:
                result['action'] = 'skip'
            elif extracted_dt < target_dt:
                result['action'] = 'stop'
            else:
                result['action'] = 'keep'
        except:
            result['action'] = 'unknown'
    elif target_date:
        result['action'] = 'unknown' # Date not found
    else:
        result['action'] = 'keep' # No target date, keep everything

    return result


def scrape_realtor_agentic(target_date=None):
    results = []

    # Validate target date format early
    if target_date:
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            print(f"[Agent] Invalid date format: {target_date}. Expected YYYY-MM-DD.")
            return []

    with sync_playwright() as p:
        print("[Agent] Launching browser...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()

        # Added sort=date-desc to URL
        url = "https://www.realtor.com/international/ca/ontario/?sort=date-desc"
        print(f"[Agent] Navigating to {url}...")
        page.goto(url, timeout=60000)
        print(f"[Agent] Page title: {page.title()}")

        page_num = 1
        MAX_PAGES = 50 # Safety limit

        stop_searching = False

        while not stop_searching:
            print(f"--- Processing Page {page_num} ---")

            # Simulate human behavior: waiting and scrolling
            page.wait_for_timeout(2000)
            print("[Agent] Scrolling to load listings...")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            # Extract listing URLs
            print("[Agent] Extracting listing links...")
            links = page.locator('a[href^="/international/ca/"]').all()

            property_urls = []
            for link in links:
                href = link.get_attribute('href')
                if href and href.count('-') > 3: # Simple heuristic
                    full_url = "https://www.realtor.com" + href
                    if full_url not in property_urls:
                        property_urls.append(full_url)

            print(f"[Agent] Found {len(property_urls)} potential property links on page {page_num}.")

            # Limit to first 3 for demonstration on each page
            target_urls = property_urls[:3]

            for i, prop_url in enumerate(target_urls):
                print(f"\n[Agent] Visiting property {i+1}/{len(target_urls)} on page {page_num}: {prop_url}")
                try:
                    new_page = context.new_page()
                    new_page.goto(prop_url, timeout=60000)
                    new_page.wait_for_load_state("domcontentloaded")

                    # Simulate reading behavior
                    new_page.evaluate("window.scrollTo(0, 500)")
                    new_page.wait_for_timeout(1000)

                    # Expand description if possible
                    try:
                        read_more = new_page.get_by_text("Read more", exact=False)
                        if read_more.is_visible():
                            read_more.click()
                            new_page.wait_for_timeout(500)
                    except:
                        pass

                    # Extract Data
                    data = {}
                    data['url'] = prop_url

                    # Title / Address
                    try:
                        data['address'] = new_page.locator('h1').first.inner_text().strip()
                    except:
                        data['address'] = "Unknown Address"

                    # Price
                    try:
                        price_element = new_page.locator('[class*="Price"]').first
                        if price_element.is_visible():
                            data['price'] = price_element.inner_text().strip()
                        else:
                            raise Exception("Price element not found")
                    except:
                        # Fallback to regex from body
                        body_text = new_page.locator('body').inner_text()
                        price_match = re.search(r'(USD|CAD)\s*\$[\d,]+', body_text)
                        if price_match:
                            data['price'] = price_match.group(0)
                        else:
                            data['price'] = "Unknown Price"

                    # Key Facts (Bed/Bath)
                    data['facts'] = {}
                    try:
                        text_content = new_page.locator('body').inner_text()
                        bed_match = re.search(r'(\d+)\s*Bed', text_content, re.IGNORECASE)
                        if bed_match:
                            data['facts']['bedrooms'] = bed_match.group(1)

                        bath_match = re.search(r'(\d+)\s*Bath', text_content, re.IGNORECASE)
                        if bath_match:
                            data['facts']['bathrooms'] = bath_match.group(1)

                        sqft_match = re.search(r'([\d,]+)\s*sq\s*ft', text_content, re.IGNORECASE)
                        if sqft_match:
                            data['facts']['sqft'] = sqft_match.group(1)
                    except Exception as e:
                        print(f"  [Agent] Error extracting facts: {e}")

                    # Description & Agentic Extraction
                    try:
                        # Get full page text for LLM analysis
                        full_page_text = new_page.locator('body').inner_text()
                        data['full_text_sample'] = full_page_text[:500] + "..."

                        # Call Agent
                        analysis_result = analyze_listing_with_llm(full_page_text, target_date)

                        data['extracted_amenities'] = analysis_result.get('amenities', [])
                        data['extracted_date'] = analysis_result.get('listing_date')
                        action = analysis_result.get('action', 'unknown')

                        print(f"  [Agent] Analysis Result: Date={data['extracted_date']}, Action={action}")

                        if target_date:
                            if action == 'stop':
                                print(f"  [Agent] Stop condition met (older listing found). Stopping search.")
                                stop_searching = True
                                new_page.close()
                                break
                            elif action == 'skip':
                                print(f"  [Agent] Skipping property (newer listing).")
                                new_page.close()
                                continue
                            elif action == 'keep':
                                print(f"  [Agent] Keeping property (date match).")
                            else: # unknown
                                print(f"  [Agent] Date unknown. Keeping property but flagging.")
                                data['date_warning'] = "Date not found or ambiguous."

                    except Exception as e:
                        print(f"  [Agent] Error in LLM analysis: {e}")
                        data['description'] = "Error analyzing page"
                        data['extracted_amenities'] = []

                    # Images
                    print("  [Agent] Extracting images...")
                    new_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    new_page.wait_for_timeout(1000)

                    image_elements = new_page.locator('img').all()
                    images = []
                    for img in image_elements:
                        src = img.get_attribute('src')
                        if src and ('jpg' in src or 'jpeg' in src or 'png' in src) and 'logo' not in src:
                            if src not in images:
                                images.append(src)

                    data['images'] = images[:5]
                    print(f"  [Agent] Found {len(images)} images, saving top 5.")

                    results.append(data)
                    new_page.close()

                except Exception as e:
                    print(f"  [Agent] Failed to process {prop_url}: {e}")
                    try:
                        new_page.close()
                    except:
                        pass

            if stop_searching:
                break

            # Pagination Logic
            if page_num >= MAX_PAGES:
                print(f"[Agent] Reached max page limit ({MAX_PAGES}). Stopping.")
                break

            print("[Agent] Checking for Next page...")
            try:
                next_btn = page.locator("li.pagination-next a, a[rel='next'], a:has-text('Next')").first

                if next_btn.is_visible():
                    print(f"[Agent] Navigating to page {page_num + 1}...")
                    with page.expect_navigation(timeout=60000):
                        next_btn.click()
                    page_num += 1
                else:
                    print("[Agent] Next button not found or not visible. End of listings.")
                    break
            except Exception as e:
                print(f"[Agent] Error during pagination: {e}")
                break

        browser.close()

    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentic Realtor Scraper")
    parser.add_argument("--date", type=str, help="Filter properties by date (YYYY-MM-DD)", default=None)
    args = parser.parse_args()

    if args.date:
        print(f"[Agent] Filtering properties for date: {args.date}")

    data = scrape_realtor_agentic(target_date=args.date)

    with open("detailed_properties.json", "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n[Agent] Scraping complete. Found {len(data)} properties. Data saved to detailed_properties.json")
