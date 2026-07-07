"""
URL Scraper — Fresh Chromium per request.

Each scrape request:
  1. Launches a fresh ungoogled-chromium instance with real-botxbyte-extension + cf-autoclick
  2. server.py (bridge) connects to extension via WebSocket
  3. Sends the scrape workflow to the extension
  4. Extension navigates, handles CF/cookies, extracts data
  5. Returns result, then kills the chromium instance

No memory leaks — each request gets a clean browser that's destroyed after use.
Slower but 100% reliable. Scale by running more workflow instances.

Exposes same API:
  POST /url-scraper-service/api/v1/scrape/
  GET  /url-scraper-service/api/v1/health/
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request as flask_request

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).parent.resolve()
WORKFLOW_TEMPLATE_PATH = SCRIPT_DIR / "url-scraper.json"
EXTENSION_DIR = SCRIPT_DIR / "real-botxbyte-extension"
VENDOR_DIR = SCRIPT_DIR / "vendor"
CHROME_BIN = VENDOR_DIR / "ungoogled-chromium" / "chrome"
CF_AUTOCLICK_DIR = VENDOR_DIR / "cf-autoclick"

DEFAULT_PORT = int(os.getenv("SCRAPER_PORT", "8814"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))  # max parallel scrapes
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "90"))  # per-request timeout
DISPLAY = os.getenv("DISPLAY", ":1")

app = Flask(__name__)

# Thread pool for concurrent scraping
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

# Stats
_stats = {
    "total_processed": 0,
    "total_errors": 0,
    "active": 0,
    "started_at": time.time(),
}


# --------------------------------------------------------------------------- #
# Workflow Builder
# --------------------------------------------------------------------------- #

def build_scrape_workflow(target_url: str, selectors: List[Dict[str, Any]]) -> dict:
    """Build workflow JSON for the extension."""
    with open(WORKFLOW_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = json.load(f)

    if not selectors:
        selectors = [
            {"name": "source_title", "selector": "title", "js_query": "document.title", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_content", "selector": "article, .article-body, .post-content, .entry-content, [role='main'] p, main p", "js_query": "(() => { const s = document.querySelector('article') || document.querySelector('.article-body') || document.querySelector('.post-content') || document.querySelector('.entry-content') || document.querySelector('[role=\"main\"]') || document.querySelector('main'); return s ? s.innerText : document.body.innerText.substring(0, 50000); })()", "is_multiple_value": False, "remove_selector": ["script", "style", "nav", "header", "footer", "aside", "ins", "iframe"], "is_external_link": False, "is_internal_link": False},
            {"name": "source_author", "selector": "meta[name='author']", "js_query": "document.querySelector('meta[name=\"author\"]')?.content || document.querySelector('[rel=\"author\"]')?.textContent?.trim() || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_published_date", "selector": "meta[property='article:published_time']", "js_query": "document.querySelector('meta[property=\"article:published_time\"]')?.content || document.querySelector('time[datetime]')?.getAttribute('datetime') || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_featured_image", "selector": "meta[property='og:image']", "js_query": "document.querySelector('meta[property=\"og:image\"]')?.content || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
            {"name": "source_excerpt", "selector": "meta[name='description']", "js_query": "document.querySelector('meta[name=\"description\"]')?.content || document.querySelector('meta[property=\"og:description\"]')?.content || ''", "is_multiple_value": False, "remove_selector": [], "is_external_link": False, "is_internal_link": False},
        ]

    template["variables"] = {
        "target_url": target_url,
        "selectors_json": json.dumps(selectors),
    }
    template["options"] = {
        "new_tab": True,
        "close_tab": True,
        "close_tab_on_error": True,
    }
    return template


# --------------------------------------------------------------------------- #
# Fresh Chrome + Extension Scrape
# --------------------------------------------------------------------------- #

def scrape_with_fresh_chrome(target_url: str, selectors: List[Dict[str, Any]]) -> dict:
    """
    Launch fresh chromium → start server.py → send workflow → get result → kill everything.
    
    This is the core function. Each call is fully isolated.
    """
    profile_dir = tempfile.mkdtemp(prefix="chrome_scrape_")
    chrome_proc = None
    server_proc = None
    ws_port = _find_free_port()
    http_port = _find_free_port()

    try:
        # 1. Build extensions list
        extensions = []
        if EXTENSION_DIR.exists():
            extensions.append(str(EXTENSION_DIR))
        if CF_AUTOCLICK_DIR.exists():
            extensions.append(str(CF_AUTOCLICK_DIR))

        if not extensions:
            return {"success": False, "error": "No extensions found in vendor/"}

        ext_arg = ",".join(extensions)

        # 2. Start server.py (bridge) with custom ports
        server_env = os.environ.copy()
        server_env["WS_PORT"] = str(ws_port)
        server_env["HTTP_PORT"] = str(http_port)
        server_env["MAX_TABS"] = "2"
        server_env["MAX_QUEUE"] = "5"
        server_env["DISPLAY"] = DISPLAY

        server_script = EXTENSION_DIR / "server.py"
        
        # Create a wrapper that patches missing imports before running server.py
        wrapper_code = f"""
import sys, types
# Patch faster_whisper if not installed (optional dep for audio captcha)
try:
    import faster_whisper
except ImportError:
    mod = types.ModuleType('faster_whisper')
    mod.WhisperModel = lambda *a, **kw: None
    sys.modules['faster_whisper'] = mod
# Patch boto3 if not installed
try:
    import boto3
except ImportError:
    mod = types.ModuleType('boto3')
    mod.client = lambda *a, **kw: None
    sys.modules['boto3'] = mod
# Now run server.py
exec(open('{server_script}').read())
"""
        wrapper_file = Path(profile_dir) / "_server_wrapper.py"
        wrapper_file.write_text(wrapper_code)
        
        server_proc = subprocess.Popen(
            [sys.executable, str(wrapper_file)],
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(EXTENSION_DIR),
        )
        time.sleep(3)  # Let server.py start

        # Check if server.py crashed on startup
        if server_proc.poll() is not None:
            output = server_proc.stdout.read().decode(errors="replace")[:500]
            return {"success": False, "error": f"server.py crashed on startup: {output}"}

        # 3. Launch fresh chromium with extensions
        chrome_args = [
            str(CHROME_BIN),
            f"--user-data-dir={profile_dir}",
            f"--load-extension={ext_arg}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            f"--force-fieldtrials=WebRTC-Unretired/Enabled/",
        ]

        chrome_env = os.environ.copy()
        chrome_env["DISPLAY"] = DISPLAY

        chrome_proc = subprocess.Popen(
            chrome_args,
            env=chrome_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        
        # 4. Wait for extension to connect to server.py
        connected = False
        for _ in range(20):  # 20 seconds max wait
            time.sleep(1)
            if server_proc.poll() is not None:
                return {"success": False, "error": "server.py exited unexpectedly"}
            if chrome_proc.poll() is not None:
                return {"success": False, "error": "Chrome exited unexpectedly"}
            # Check if extension connected by hitting status endpoint
            try:
                resp = requests.get(f"http://localhost:{http_port}/status", timeout=2)
                if resp.status_code == 200:
                    status = resp.json()
                    # Extension connected means ws_conn is not None
                    connected = True
                    break
            except Exception:
                pass

        if not connected:
            # Try anyway — extension might connect during workflow
            pass

        # 5. Build and send workflow
        workflow = build_scrape_workflow(target_url, selectors)

        try:
            resp = requests.post(
                f"http://localhost:{http_port}/workflow",
                json=workflow,
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code == 429:
                return {"success": False, "error": "Extension queue full"}
            if resp.status_code == 503:
                return {"success": False, "error": "Extension not connected"}
            if resp.status_code >= 400:
                return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except requests.Timeout:
            return {"success": False, "error": f"Timeout after {SCRAPE_TIMEOUT}s"}
        except requests.ConnectionError:
            return {"success": False, "error": "Cannot connect to server.py"}

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        # 6. ALWAYS kill everything and cleanup
        if chrome_proc and chrome_proc.poll() is None:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            except Exception:
                chrome_proc.kill()

        if server_proc and server_proc.poll() is None:
            try:
                server_proc.terminate()
                server_proc.wait(timeout=5)
            except Exception:
                server_proc.kill()

        # Remove temp profile
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass


def _find_free_port() -> int:
    """Find a free TCP port."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------- #
# Result Parser
# --------------------------------------------------------------------------- #

def parse_result(result: dict, target_url: str) -> dict:
    """Parse extension result into standard scraper response format."""
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error", "Workflow failed"),
            "data": {},
        }

    ext_vars = result.get("variables", {})

    # Parse scraped_data
    scraped_data_raw = ext_vars.get("scraped_data", "[]")
    try:
        scraped_data = json.loads(scraped_data_raw) if isinstance(scraped_data_raw, str) else scraped_data_raw
    except (json.JSONDecodeError, TypeError):
        scraped_data = []

    # Parse page_info
    page_info_raw = ext_vars.get("page_info", "{}")
    try:
        page_info = json.loads(page_info_raw) if isinstance(page_info_raw, str) else page_info_raw
    except (json.JSONDecodeError, TypeError):
        page_info = {}

    # Build variables
    variables = {}
    for item in (scraped_data if isinstance(scraped_data, list) else []):
        if isinstance(item, dict) and item.get("name"):
            variables[item["name"]] = item.get("value")

    # page_html for AI selector detection
    page_html = ext_vars.get("page_html", "")
    if not page_html:
        content = variables.get("source_content", "")
        if content and len(str(content)) > 100:
            page_html = f"<html><body>{content}</body></html>"
    variables["page_html"] = page_html

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
    """Main scrape endpoint."""
    _stats["active"] += 1
    try:
        body = flask_request.get_json(force=True)
        target_url = body.get("target_url", "")
        selectors = body.get("selectors", [])

        if not target_url:
            return jsonify({"success": False, "error": "target_url is required"}), 400

        # Run scrape in thread pool (blocks until done)
        future = executor.submit(scrape_with_fresh_chrome, target_url, selectors)
        result = future.result(timeout=SCRAPE_TIMEOUT + 30)

        response = parse_result(result, target_url)

        if response["success"]:
            _stats["total_processed"] += 1
        else:
            _stats["total_errors"] += 1

        return jsonify(response)

    except Exception as e:
        _stats["total_errors"] += 1
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        _stats["active"] -= 1


@app.route("/url-scraper-service/api/v1/health/", methods=["GET"])
def health():
    """Health check."""
    chrome_exists = CHROME_BIN.exists()
    ext_exists = EXTENSION_DIR.exists()

    return jsonify({
        "service": "url-scraper-fresh-chrome",
        "status": "healthy" if (chrome_exists and ext_exists) else "degraded",
        "architecture": "fresh-chromium-per-request",
        "stats": {
            "total_processed": _stats["total_processed"],
            "total_errors": _stats["total_errors"],
            "active_scrapes": _stats["active"],
            "max_concurrent": MAX_CONCURRENT,
            "uptime_seconds": int(time.time() - _stats["started_at"]),
        },
        "chrome_binary": str(CHROME_BIN),
        "chrome_exists": chrome_exists,
        "extension_exists": ext_exists,
    })


@app.route("/url-scraper-service/api/v1/metrics/", methods=["GET"])
def metrics():
    """Metrics endpoint."""
    return jsonify({
        "scraper_requests_total": _stats["total_processed"] + _stats["total_errors"],
        "scraper_success_total": _stats["total_processed"],
        "scraper_errors_total": _stats["total_errors"],
        "scraper_active_current": _stats["active"],
    })


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    global MAX_CONCURRENT, SCRAPE_TIMEOUT, executor

    parser = argparse.ArgumentParser(description="URL Scraper (Fresh Chrome per request)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    parser.add_argument("--timeout", type=int, default=SCRAPE_TIMEOUT)
    args = parser.parse_args()

    MAX_CONCURRENT = args.max_concurrent
    SCRAPE_TIMEOUT = args.timeout
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

    print(f"🚀 URL Scraper (Fresh Chrome) starting on port {args.port}")
    print(f"   Max concurrent: {MAX_CONCURRENT}")
    print(f"   Timeout: {SCRAPE_TIMEOUT}s")
    print(f"   Chrome: {CHROME_BIN}")
    print(f"   Extension: {EXTENSION_DIR}")
    print(f"   CF-Autoclick: {CF_AUTOCLICK_DIR}")
    print(f"   Display: {DISPLAY}")
    print()

    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
