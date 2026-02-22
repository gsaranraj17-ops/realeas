# Realtor Agentic Scraper

This is a Python script that scrapes property listings from Realtor.com (specifically the Ontario section) using Playwright for browser automation and an LLM (Large Language Model) to extract specific details like amenities and listing dates.

## Features

- **Pagination:** Iterates through multiple pages of listings.
- **Date Filtering:** Allows users to specify a target date (e.g., `YYYY-MM-DD`). The scraper will only keep properties listed on that date.
- **Agentic Extraction:** Uses an LLM (via OpenRouter or Gemini API) to analyze the full text of property pages to identify listing dates and amenities.
- **Smart Stop Logic:** When filtering by date, the scraper assumes results are sorted chronologically (newest first). If it encounters a listing older than the target date, it stops scraping to save resources.
- **Robust Fallback:** If the LLM is unavailable or fails, it falls back to regex-based extraction.

## Prerequisites

1.  **Python 3.8+**
2.  **Playwright:** Requires installation of browser binaries.
3.  **API Keys (Optional but Recommended):**
    - `OPENROUTER_API_KEY`: For using OpenRouter models.
    - `GEMINI_API_KEY`: For using Google Gemini models.

## Installation

1.  Clone the repository.
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Install Playwright browsers:
    ```bash
    playwright install chromium
    ```
4.  Create a `.env` file and add your API keys:
    ```
    OPENROUTER_API_KEY=your_key_here
    GEMINI_API_KEY=your_key_here
    ```

## Usage

### Basic Scraping (All Pages)
Run the script without arguments to scrape listing pages (up to the safety limit):
```bash
python realtor_scraper.py
```

### Scraping for a Specific Date
To scrape properties listed on a specific date (e.g., October 27, 2023):
```bash
python realtor_scraper.py --date 2023-10-27
```
The script will skip newer listings and stop once it finds listings older than the target date.

## Output

The script saves the scraped data to `detailed_properties.json` in the current directory.

## Limitations

- The script currently has a hardcoded safety limit of 3 pages (`MAX_PAGES`) to prevent infinite loops during testing. Modify the script to increase this if needed.
- It relies on the structure of Realtor.com, which may change over time.
