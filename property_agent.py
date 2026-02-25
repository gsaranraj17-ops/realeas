import asyncio
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import requests
from playwright.async_api import async_playwright

# Load environment variables
load_dotenv()

class PropertyAgent:
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY") 
        if not self.api_key:
            print("Warning: OPENROUTER_API_KEY or OPENAI_API_KEY not found in environment.")
        
        self.data_file = "detailed_properties.json"
        self.visited_urls = set()
        self.mock_llm = os.getenv("MOCK_LLM", "false").lower() == "true"

    async def search_google(self, queries=None):
        """
        Searches Google for property sites or returns a list of target URLs.
        """
        # Default provided URLs
        target_urls = [
            "https://mattamyhomes.com/ontario/gta", # More specific to ensure content
            "https://www.fieldgatehomes.com/our-communities/",
        ]

        if queries:
            print(f"Searching Google for: {queries}")
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                for query in queries:
                    try:
                        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
                        await page.goto(search_url)
                        await page.wait_for_selector('div.g', timeout=5000)
                        
                        # Extract links from search results
                        links = await page.locator('div.g a').evaluate_all("els => els.map(el => el.href)")
                        
                        # Filter links to ensure they are relevant and not ads/google links
                        for link in links:
                            if "google" not in link and link.startswith("http"):
                                target_urls.append(link)
                                
                    except Exception as e:
                        print(f"Error searching for {query}: {e}")
                
                await browser.close()

        # Remove duplicates
        return list(set(target_urls))

    async def navigate_and_extract(self, urls):
        """
        Navigates to the URLs and extracts property details using LLM.
        """
        extracted_properties = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            for url in urls:
                if url in self.visited_urls:
                    continue
                self.visited_urls.add(url)
                
                print(f"Navigating to: {url}")
                try:
                    await page.goto(url, timeout=60000)
                    # Scroll down to trigger lazy loading
                    for _ in range(5):
                        await page.mouse.wheel(0, 1000)
                        await page.wait_for_timeout(1000)

                    # Extract text content
                    text_content = await page.evaluate("document.body.innerText")
                    text_content = text_content.replace('\n', ' ').strip()
                    
                    # Extract images (filter out small icons)
                    images = await page.evaluate("""() => {
                        return Array.from(document.images).filter(img => img.naturalWidth > 200).map(img => {
                            return {
                                src: img.src,
                                alt: img.alt,
                                parentText: img.parentElement ? img.parentElement.innerText.substring(0, 100).replace(/\\n/g, ' ') : ''
                            }
                        }).filter(img => img.src && img.src.startsWith('http'));
                    }""")
                    
                    # Combine text and image info for LLM context
                    combined_content = f"Page Text:\n{text_content[:10000]}\n\nDetected Images:\n"
                    for img in images[:20]: # Limit to top 20 images to save tokens
                        combined_content += f"- Image: {img['src']} (Alt: {img['alt']}, Context: {img['parentText']})\n"

                    print(f"Extracting data from {url}...")
                    properties = self.extract_with_llm(combined_content, url)
                    if properties:
                        extracted_properties.extend(properties)
                        
                except Exception as e:
                    print(f"Error processing {url}: {e}")
            
            await browser.close()
        return extracted_properties

    def extract_with_llm(self, text_content, url):
        """
        Uses LLM to parse text content and extract property details.
        """
        system_prompt = f"""
        You are a real estate data extraction agent.
        Extract the available property listings from the text content provided by the user, which was scraped from a website ({url}).
        Return a JSON array of objects. Each object should have:
        - "name": Name of the community or property.
        - "location": City, Address, or Region.
        - "price": Price range or specific price (as string).
        - "status": e.g., "Available", "Coming Soon", "Sold Out".
        - "url": The source URL ({url}).
        - "image_url": Select the most relevant image URL from the 'Detected Images' list for this property.
        - "description": A brief description if available.

        If no properties are found, return an empty array [].
        Do not include any markdown formatting (like ```json), just the raw JSON string.
        """

        if self.mock_llm:
            print("Using Mock LLM...")
            return [
                {
                    "name": "Mock Community 1",
                    "location": "Toronto, ON",
                    "price": "$1,000,000",
                    "status": "Available",
                    "url": url,
                    "image_url": "https://example.com/mock1.jpg",
                    "description": "A beautiful mock community."
                },
                {
                    "name": "Mock Community 2",
                    "location": "Ottawa, ON",
                    "price": "$800,000",
                    "status": "Coming Soon",
                    "url": url,
                    "image_url": "https://example.com/mock2.jpg",
                    "description": "Another mock community."
                }
            ]

        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost:3000", # Required by OpenRouter for free/low-tier usage tracking
                    "X-Title": "Realtor Agentic Scraper"
                },
                json={
                    "model": "mistralai/mistral-small-creative", # Or "openai/gpt-3.5-turbo"
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant that outputs JSON. " + system_prompt},
                        {"role": "user", "content": text_content[:15000]} # Limit text length for token limits. text_content here is actually combined_content from the caller
                    ]
                }
            )
            
            if response.status_code != 200:
                print(f"LLM API Error: {response.status_code} - {response.text}")
                return []

            content = response.json()['choices'][0]['message']['content'].strip()
            
            # Remove markdown code blocks if present
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            
            return json.loads(content)
        except Exception as e:
            print(f"LLM Extraction Error: {e}")
            return []

    def load_data(self):
        """
        Loads existing data from the JSON file.
        """
        if os.path.exists(self.data_file):
            with open(self.data_file, 'r') as f:
                return json.load(f)
        return []

    def save_data(self, data):
        """
        Saves data to the JSON file.
        """
        with open(self.data_file, 'w') as f:
            json.dump(data, f, indent=4)

    def update_data(self, new_data):
        """
        Compares new data with existing data and updates the file.
        """
        existing_data = self.load_data()
        existing_keys = {(p.get('name'), p.get('location')) for p in existing_data}
        
        added_count = 0
        for item in new_data:
            key = (item.get('name'), item.get('location'))
            if key not in existing_keys:
                item['date_found'] = datetime.now().isoformat()
                existing_data.append(item)
                existing_keys.add(key)
                added_count += 1
            else:
                # Optional: Update existing record if needed (e.g. price change)
                pass
                
        if added_count > 0:
            print(f"Added {added_count} new properties.")
            self.save_data(existing_data)
        else:
            print("No new properties found.")

    async def run(self):
        print("Starting Property Agent...")

        # Use fixed known URLs (stable)
        urls = [
            "https://mattamyhomes.com/ontario/gta",
            "https://www.fieldgatehomes.com/our-communities/",
        ]

        print(f"Visiting {len(urls)} URLs.")

        new_properties = await self.navigate_and_extract(urls)

        print(f"Extracted {len(new_properties)} properties.")

        if new_properties:
            self.update_data(new_properties)
        else:
            print("No properties extracted.")

if __name__ == "__main__":
    agent = PropertyAgent()
    asyncio.run(agent.run())
