import os
import json
import re
import requests
import argparse
from playwright.sync_api import sync_playwright
import dotenv

dotenv.load_dotenv()

def parse_description_with_llm(description):
    """
    Simulates an agentic LLM step.
    If OPENROUTER_API_KEY or GEMINI_API_KEY is present, it uses the LLM to extract amenities and date.
    Otherwise, it uses a fallback deterministic method to extract amenities and tries to find a date.
    Returns a dict: {'amenities': [...], 'date': 'YYYY-MM-DD' or None}
    """
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    result = {"amenities": [], "date": None}

    if openrouter_key:
        print("  [Agent] OpenRouter API key detected. Extracting amenities and date with LLM...")
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost:3000", # Required by OpenRouter, dummy value
                    "X-Title": "Realtor Agentic Scraper"
                },
                json={
                    "model": "mistralai/mistral-small-creative", # Using a free/cheap model default
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a real estate assistant. Extract a list of amenities and the listing date (or 'listed on' date) from the property description provided. Return ONLY a JSON object with keys 'amenities' (list of strings) and 'date' (string in YYYY-MM-DD format, or null if not found). Do not include markdown formatting or extra text."
                        },
                        {
                            "role": "user",
                            "content": description
                        }
                    ]
                },
                timeout=10
            )
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                # Clean up potential markdown code blocks
                content = content.replace("```json", "").replace("```", "").strip()
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        result['amenities'] = parsed.get('amenities', [])
                        result['date'] = parsed.get('date')
                        return result
                except:
                    pass
            else:
                 print(f"  [Agent] OpenRouter API Error: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"  [Agent] OpenRouter Extraction Failed: {e}")

    elif gemini_key:
        print("  [Agent] Gemini API key detected. Extracting amenities and date...")
        try:
            url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={gemini_key}"

            payload = {
                "contents": [{
                    "parts": [{
                        "text": f"Extract property amenities and listing date from text. Return JSON object with keys 'amenities' (array of strings) and 'date' (YYYY-MM-DD string or null). Description: {description}"
                    }]
                }],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "response_schema": {
                        "type": "object",
                        "properties": {
                            "amenities": {"type": "array", "items": {"type": "string"}},
                            "date": {"type": "string"}
                        }
                    }
                }
            }

            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()

            data = response.json()
            raw_content = data['candidates'][0]['content']['parts'][0]['text']

            parsed = json.loads(raw_content)
            result['amenities'] = parsed.get('amenities', [])
            result['date'] = parsed.get('date')
            return result

        except Exception as e:
            print(f"  [Agent] Gemini Extraction Failed: {e}")

    else:
        # print("  [Agent] No LLM API key found. Using fallback extraction logic.")
        pass

    # Fallback / Simulated Agent Logic
    # print("  [Agent] Falling back to keyword extraction.")
    keywords = ['pool', 'gym', 'fireplace', 'garage', 'waterfront', 'balcony', 'garden', 'parking', 'sauna']

    desc_lower = description.lower()
    for word in keywords:
        if word in desc_lower:
            result['amenities'].append(word.capitalize())

    # Fallback date extraction (simple regex for YYYY-MM-DD)
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', description)
    if date_match:
        result['date'] = date_match.group(1)
    else:
        # Try finding "Listed on Month DD, YYYY"
        # Since standard library doesn't have advanced parsing, we keep it simple.
        pass

    return result

def scrape_realtor_agentic(target_date=None):
    results = []

    with sync_playwright() as p:
        print("[Agent] Launching browser...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()

        url = "https://www.realtor.com/international/ca/ontario/"
        print(f"[Agent] Navigating to {url}...")
        page.goto(url, timeout=60000)
        print(f"[Agent] Page title: {page.title()}")

        page_num = 1
        MAX_PAGES = 3 # Safety limit for demonstration

        while True:
            print(f"--- Processing Page {page_num} ---")

            # Simulate human behavior: waiting and scrolling
            page.wait_for_timeout(2000)
            print("[Agent] Scrolling to load listings...")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            # Extract listing URLs
            print("[Agent] Extracting listing links...")
            # Get all links that match the pattern
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
                        description = new_page.locator('body').inner_text()
                        data['full_text_sample'] = description[:500] + "..."

                        # Extract amenities and date
                        extraction_result = parse_description_with_llm(description)
                        data['extracted_amenities'] = extraction_result['amenities']
                        data['extracted_date'] = extraction_result['date']

                    except:
                        data['description'] = "No description found"
                        data['extracted_amenities'] = []
                        data['extracted_date'] = None

                    # Date Filtering Logic
                    if target_date:
                        if data['extracted_date']:
                            if data['extracted_date'] != target_date:
                                print(f"  [Agent] Date mismatch: Found {data['extracted_date']}, expected {target_date}. Skipping.")
                                new_page.close()
                                continue
                            else:
                                print(f"  [Agent] Date match: {data['extracted_date']}. Keeping.")
                        else:
                            print(f"  [Agent] Date not found in description. Keeping property but flagging.")
                            data['date_warning'] = "Date not found, verification needed."

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

                    # Keep top 5 distinct high-quality looking images
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

            # Pagination Logic
            if page_num >= MAX_PAGES:
                print(f"[Agent] Reached max page limit ({MAX_PAGES}). Stopping.")
                break

            print("[Agent] Checking for Next page...")
            try:
                # Assuming standard pagination, let's look for an anchor with "Next" or an arrow
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
