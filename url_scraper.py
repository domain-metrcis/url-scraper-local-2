"""
URL Scraper — Extension-based worker using real-botxbyte-extension.

Exposes the same API as url-scraper-service:
  POST /url-scraper-service/api/v1/scrape/
  GET  /url-scraper-service/api/v1/health/

Architecture:
  1. Receives scrape request (target_url + selectors)
  2. Builds a workflow JSON from the url-scraper.json template
  3. Sends workflow to server.py (port 8766) which forwards to extension via WebSocket
  4. Extension opens a new tab, navigates, handles CF/cookies, extracts data
  5. Returns result synchronously (server.py manages tab semaphore + queue)

No CDP, no undetected-chromedriver, no crashes.
The extension handles CF turnstile, cookie consent, CSP stripping natively.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request as flask_request

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).parent.resolve()
WORKFLOW_TEMPLATE_PATH = SCRIPT_DIR / "url-scraper.json"
EXTENSION_SERVER_URL = os.getenv("EXTENSION_SERVER_URL", "http://localhost:8766")
DEFAULT_PORT = int(os.getenv("SCRAPER_PORT", "8814"))
REQUEST_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT", "120"))  # seconds

app = Flask(__name__)

# Stats
_stats = {
    "total_processed": 0,
    "total_errors": 0,
    "total_queue": 0,
    "started_at": time.time(),
}


# --------------------------------------------------------------------------- #
# Workflow Builder
# --------------------------------------------------------------------------- #

def _load_workflow_template() -> dict:
    """Load the url-scraper.json workflow template."""
    with open(WORKFLOW_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_scrape_workflow(target_url: str, selectors: List[Dict[str, Any]]) -> dict:
    """
    Build a workflow JSON for the extension to execute.
    
    If selectors are provided, uses them for extraction.
    If empty, uses default selectors that extract page_html + basic meta.
    """
    template = _load_workflow_template()
    
    # Default selectors when none provided — extract full page HTML + meta
    if not selectors:
        selectors = [
            {"name": "source_title", "selector": "title", "js_query": "document.title", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_content", "selector": "article, .article-body, .post-content, .entry-content, [role='main'] p, main p", "js_query": "(() => { const s = document.querySelector('article') || document.querySelector('.article-body') || document.querySelector('.post-content') || document.querySelector('.entry-content') || document.querySelector('[role=\"main\"]') || document.querySelector('main'); return s ? s.innerText : document.body.innerText.substring(0, 50000); })()", "is_multiple_value": False, "remove_selector": ["script", "style", "nav", "header", "footer", "aside", "ins", "iframe"], "is_external_link": False, "is_internal_link": False},
            {"name": "source_author", "selector": "meta[name='author'], [rel='author'], .author-name", "js_query": "document.querySelector('meta[name=\"author\"]')?.content || document.querySelector('[rel=\"author\"]')?.textContent?.trim() || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_published_date", "selector": "meta[property='article:published_time'], time[datetime]", "js_query": "document.querySelector('meta[property=\"article:published_time\"]')?.content || document.querySelector('time[datetime]')?.getAttribute('datetime') || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_featured_image", "selector": "meta[property='og:image']", "js_query": "document.querySelector('meta[property=\"og:image\"]')?.content || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_excerpt", "selector": "meta[name='description'], meta[property='og:description']", "js_query": "document.querySelector('meta[name=\"description\"]')?.content || document.querySelector('meta[property=\"og:description\"]')?.content || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
        ]

    # Set variables in the workflow
    template["variables"] = {
        "target_url": target_url,
        "selectors_json": json.dumps(selectors),
    }
    
    # Ensure options are set for new tab + close on completion
    template["options"] = {
        "new_tab": True,
        "close_tab": True,
        "close_tab_on_error": True,
    }
    
    return template


# --------------------------------------------------------------------------- #
# Extension Communication
# --------------------------------------------------------------------------- #

def send_workflow_to_extension(workflow: dict, timeout: int = REQUEST_TIMEOUT) -> dict:
    """
    Send workflow to server.py (synchronous mode).
    Returns the extension's result dict.
    """
    try:
        resp = requests.post(
            f"{EXTENSION_SERVER_URL}/workflow",
            json=workflow,
            timeout=timeout,
        )
        if resp.status_code == 429:
            return {"success": False, "error": "Queue full (429). Scraper is overloaded."}
        if resp.status_code == 503:
            return {"success": False, "error": "Extension not connected (503)."}
        if resp.status_code >= 400:
            return {"success": False, "error": f"server.py returned HTTP {resp.status_code}: {resp.text[:200]}"}
        return resp.json()
    except requests.Timeout:
        return {"success": False, "error": f"Timeout after {timeout}s waiting for extension"}
    except requests.ConnectionError:
        return {"success": False, "error": f"Cannot connect to extension server at {EXTENSION_SERVER_URL}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# Result Parsing
# --------------------------------------------------------------------------- #

def parse_extension_result(result: dict, target_url: str) -> dict:
    """
    Parse extension workflow result into the standard scraper API response format.
    
    Extension returns:
      {"success": true, "variables": {"scraped_data": "...", "page_info": "..."}}
    
    We convert to:
      {"success": true, "data": {"variables": {...}, "scraped_data": [...]}}
    """
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error", "Workflow failed"),
            "data": {},
        }
    
    ext_vars = result.get("variables", {})
    
    # Parse scraped_data from JSON string
    scraped_data_raw = ext_vars.get("scraped_data", "[]")
    try:
        if isinstance(scraped_data_raw, str):
            scraped_data = json.loads(scraped_data_raw)
        else:
            scraped_data = scraped_data_raw
    except (json.JSONDecodeError, TypeError):
        scraped_data = []
    
    # Parse page_info
    page_info_raw = ext_vars.get("page_info", "{}")
    try:
        if isinstance(page_info_raw, str):
            page_info = json.loads(page_info_raw)
        else:
            page_info = page_info_raw
    except (json.JSONDecodeError, TypeError):
        page_info = {}
    
    # Build variables dict (same format as current scraper returns)
    variables = {}
    for item in scraped_data:
        if isinstance(item, dict) and item.get("name"):
            variables[item["name"]] = item.get("value")
    
    # Add page_html (full page source from evaluate)
    # The extension stores page source in the evaluate result
    page_html = ext_vars.get("page_html", "")
    if not page_html:
        # Construct from scraped content if available
        content = variables.get("source_content", "")
        if content and len(content) > 100:
            page_html = f"<html><body>{content}</body></html>"
    variables["page_html"] = page_html
    
    # Add page_info fields
    if page_info:
        variables["__page_title"] = page_info.get("title", "")
        variables["__page_url"] = page_info.get("url", target_url)
    
    return {
        "success": True,
        "data": {
            "variables": variables,
            "scraped_data": scraped_data,
            "page_info": page_info,
        },
    }


# --------------------------------------------------------------------------- #
# Flask Endpoints
# --------------------------------------------------------------------------- #

@app.route("/url-scraper-service/api/v1/scrape/", methods=["POST"])
def scrape():
    """
    Main scrape endpoint — compatible with article-innovator orchestration-service.
    
    Request:
      {"target_url": "https://...", "selectors": [...], "stop_on_error": false}
    
    Response:
      {"success": true, "data": {"variables": {...}, "scraped_data": [...]}}
    """
    _stats["total_queue"] += 1
    
    try:
        body = flask_request.get_json(force=True)
        target_url = body.get("target_url", "")
        selectors = body.get("selectors", [])
        
        if not target_url:
            return jsonify({"success": False, "error": "target_url is required"}), 400
        
        # Build workflow
        workflow = build_scrape_workflow(target_url, selectors)
        
        # Send to extension via server.py
        result = send_workflow_to_extension(workflow)
        
        # Parse into standard format
        response = parse_extension_result(result, target_url)
        
        if response["success"]:
            _stats["total_processed"] += 1
        else:
            _stats["total_errors"] += 1
        
        return jsonify(response)
    
    except Exception as e:
        _stats["total_errors"] += 1
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        _stats["total_queue"] = max(0, _stats["total_queue"] - 1)


@app.route("/url-scraper-service/api/v1/health/", methods=["GET"])
def health():
    """Health check endpoint."""
    # Check if extension server is reachable
    ext_status = "unknown"
    try:
        resp = requests.get(f"{EXTENSION_SERVER_URL}/status", timeout=3)
        if resp.status_code == 200:
            ext_status = "connected"
        else:
            ext_status = "disconnected"
    except Exception:
        ext_status = "unreachable"
    
    return jsonify({
        "service": "url-scraper-extension",
        "status": "healthy" if ext_status == "connected" else "degraded",
        "architecture": "extension-based",
        "extension_server": ext_status,
        "extension_server_url": EXTENSION_SERVER_URL,
        "stats": {
            "total_processed": _stats["total_processed"],
            "total_errors": _stats["total_errors"],
            "total_queue": _stats["total_queue"],
            "uptime_seconds": int(time.time() - _stats["started_at"]),
        },
    })


@app.route("/url-scraper-service/api/v1/metrics/", methods=["GET"])
def metrics():
    """Prometheus-style metrics."""
    return jsonify({
        "scraper_requests_total": _stats["total_processed"] + _stats["total_errors"],
        "scraper_success_total": _stats["total_processed"],
        "scraper_errors_total": _stats["total_errors"],
        "scraper_queue_current": _stats["total_queue"],
    })


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="URL Scraper (Extension-based)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--extension-server", type=str, default=EXTENSION_SERVER_URL, 
                        help="Extension server.py URL")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT,
                        help="Timeout for scrape requests (seconds)")
    args = parser.parse_args()
    
    global EXTENSION_SERVER_URL, REQUEST_TIMEOUT
    EXTENSION_SERVER_URL = args.extension_server
    REQUEST_TIMEOUT = args.timeout
    
    print(f"🚀 URL Scraper (Extension-based) starting on port {args.port}")
    print(f"   Extension server: {EXTENSION_SERVER_URL}")
    print(f"   Timeout: {REQUEST_TIMEOUT}s")
    print(f"   Workflow template: {WORKFLOW_TEMPLATE_PATH}")
    print()
    
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
