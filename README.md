# Property Agent

A Python agent that searches for and extracts property listings from Canadian real estate developer websites using Playwright and LLMs (via OpenRouter).

## Setup

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    playwright install chromium
    ```

2.  Set up environment variables:
    Create a `.env` file or set the variable in your shell:
    ```bash
    export OPENROUTER_API_KEY="your_api_key_here"
    ```

## Usage

Run the agent:
```bash
python property_agent.py
```

To run with Mock LLM (for testing without API key):
```bash
MOCK_LLM=true python property_agent.py
```

## Features

-   **Search & Discovery**: Starts with a list of known developers (Mattamy, Fieldgate) and can be extended to search Google.
-   **Agentic Extraction**: Uses an LLM to parse unstructured web content into structured JSON data.
-   **Incremental Updates**: Saves data to `detailed_properties.json` and only appends new properties found in subsequent runs.
-   **Images**: Extracts image URLs if available.

## Scheduling

To run this daily, you can use `cron` (Linux/Mac) or Task Scheduler (Windows).
Example cron job (runs daily at 8 AM):
```bash
0 8 * * * cd /path/to/repo && python property_agent.py >> agent.log 2>&1
```
