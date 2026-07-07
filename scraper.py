"""
Minimal URL Scraper — Fresh Chrome per request via CDP.

Files:
  - scraper.py (this file) — Flask API + Chrome lifecycle
  - url-scraper.json — Workflow schema (reference only, JS is embedded here)

Each request:
  1. Launch fresh ungoogled-chromium with cf-autoclick extension
  2. Connect via CDP (Chrome DevTools Protocol)  
  3. Navigate to URL, wait for page load
  4. Execute JS to extract data using selectors
  5. Kill chrome, delete profile
  
API:
  POST /url-scraper-service/api/v1/scrape/
  GET  /url-scraper-service/api/v1/health/
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import socket
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import requests as http_requests
from flask import Flask, jsonify, request as flask_request

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).parent.resolve()

# Paths — override with env vars
CHROME_BIN = os.getenv("CHROME_BIN", str(SCRIPT_DIR / "vendor" / "ungoogled-chromium" / "chrome"))
CF_AUTOCLICK_DIR = os.getenv("CF_AUTOCLICK_DIR", str(SCRIPT_DIR / "vendor" / "cf-autoclick"))
DISPLAY = os.getenv("DISPLAY", ":0")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
SCRAPE_TIMEOUT = int(os.getenv("SCRAPE_TIMEOUT", "60"))
PORT = int(os.getenv("SCRAPER_PORT", "8814"))

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

_stats = {"processed": 0, "errors": 0, "active": 0, "started_at": time.time()}


# --------------------------------------------------------------------------- #
# Helper: find free port
# --------------------------------------------------------------------------- #

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------- #
# Default selectors (when none provided)
# --------------------------------------------------------------------------- #

DEFAULT_SELECTORS = [
    {"name": "source_title", "selector": "title", "js_query": "document.title", "is_multiple_value": False, "remove_selector": []},
    {"name": "source_content", "selector": "article", "js_query": "(() => { let el = document.querySelector('article') || document.querySelector('.article-body, .post-content, .entry-content, [role=main], main'); return el ? el.innerText : document.body.innerText.substring(0,50000); })()", "is_multiple_value": False, "remove_selector": ["script","style","nav","header","footer","aside","ins","iframe"]},
    {"name": "source_author", "selector": "meta[name='author']", "js_query": "document.querySelector('meta[name=\"author\"]')?.content || ''", "is_multiple_value": False, "remove_selector": []},
    {"name": "source_published_date", "selector": "meta[property='article:published_time']", "js_query": "document.querySelector('meta[property=\"article:published_time\"]')?.content || document.querySelector('time[datetime]')?.getAttribute('datetime') || ''", "is_multiple_value": False, "remove_selector": []},
    {"name": "source_featured_image", "selector": "meta[property='og:image']", "js_query": "document.querySelector('meta[property=\"og:image\"]')?.content || ''", "is_multiple_value": False, "remove_selector": []},
    {"name": "source_excerpt", "selector": "meta[name='description']", "js_query": "document.querySelector('meta[name=\"description\"]')?.content || document.querySelector('meta[property=\"og:description\"]')?.content || ''", "is_multiple_value": False, "remove_selector": []},
]


# --------------------------------------------------------------------------- #
# Build extraction JS from selectors
# --------------------------------------------------------------------------- #

def build_extraction_js(selectors: list) -> str:
    """Build JavaScript that extracts data using the provided selectors."""
    selectors_json = json.dumps(selectors)
    return f"""
    (() => {{
        const selectors = {selectors_json};
        const results = [];
        for (const sel of selectors) {{
            const result = {{ name: sel.name, selector: sel.selector, value: null }};
            try {{
                if (sel.remove_selector && sel.remove_selector.length > 0) {{
                    sel.remove_selector.forEach(rs => {{
                        document.querySelectorAll(rs).forEach(el => el.remove());
                    }});
                }}
                if (sel.js_query) {{
                    try {{ result.value = eval(sel.js_query); }} catch(e) {{}}
                }}
                if (!result.value) {{
                    if (sel.is_multiple_value) {{
                        const els = document.querySelectorAll(sel.selector);
                        result.value = Array.from(els).map(el => 
                            el.getAttribute('content') || el.getAttribute('href') || el.getAttribute('src') || el.innerText.trim()
                        );
                    }} else {{
                        const el = document.querySelector(sel.selector);
                        if (el) result.value = el.getAttribute('content') || el.getAttribute('href') || el.getAttribute('src') || el.innerText.trim();
                    }}
                }}
            }} catch(e) {{ result.error = e.message; }}
            results.push(result);
        }}
        return JSON.stringify(results);
    }})()
    """


# --------------------------------------------------------------------------- #
# Core: scrape with fresh chrome via CDP
# --------------------------------------------------------------------------- #

def scrape_url(target_url: str, selectors: list) -> dict:
    """Launch chrome, navigate, extract, kill. Returns parsed result."""
    
    if not selectors:
        selectors = DEFAULT_SELECTORS
    
    profile_dir = tempfile.mkdtemp(prefix="scrape_")
    cdp_port = _free_port()
    chrome_proc = None
    
    try:
        # 1. Launch chrome
        chrome_args = [
            CHROME_BIN,
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={cdp_port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--headless=new",
        ]
        
        # Add cf-autoclick extension if exists
        if os.path.isdir(CF_AUTOCLICK_DIR):
            chrome_args.append(f"--load-extension={CF_AUTOCLICK_DIR}")
            # Can't use headless with extensions, switch to headed
            chrome_args = [a for a in chrome_args if a != "--headless=new"]
        
        env = os.environ.copy()
        env["DISPLAY"] = DISPLAY
        
        chrome_proc = subprocess.Popen(
            chrome_args, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        
        # 2. Wait for CDP to be ready
        cdp_base = f"http://127.0.0.1:{cdp_port}"
        ready = False
        for _ in range(15):
            time.sleep(1)
            try:
                r = http_requests.get(f"{cdp_base}/json/version", timeout=2)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
        
        if not ready:
            return {"success": False, "error": "Chrome CDP not ready after 15s"}
        
        # 3. Get a page target (or create new tab)
        tabs = http_requests.get(f"{cdp_base}/json", timeout=5).json()
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        
        if not page_tabs:
            # Create a new tab
            r = http_requests.put(f"{cdp_base}/json/new?about:blank", timeout=5)
            tabs = http_requests.get(f"{cdp_base}/json", timeout=5).json()
            page_tabs = [t for t in tabs if t.get("type") == "page"]
        
        if not page_tabs:
            return {"success": False, "error": "No page target available"}
        
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]
        
        # 4. Connect via WebSocket and navigate
        import websocket
        ws = websocket.create_connection(ws_url, timeout=SCRAPE_TIMEOUT)
        msg_id = 1
        
        def send_cdp(method, params=None):
            nonlocal msg_id
            msg = {"id": msg_id, "method": method, "params": params or {}}
            ws.send(json.dumps(msg))
            msg_id += 1
            # Wait for response with matching id
            while True:
                resp = json.loads(ws.recv())
                if resp.get("id") == msg_id - 1:
                    return resp
                # Also handle events (just skip)
        
        # Enable Page events
        send_cdp("Page.enable")
        
        # Navigate
        send_cdp("Page.navigate", {"url": target_url})
        
        # Wait for load (simple approach: just wait)
        time.sleep(8)
        
        # 5. Extract data using JS
        extraction_js = build_extraction_js(selectors)
        result = send_cdp("Runtime.evaluate", {
            "expression": extraction_js,
            "returnByValue": True,
        })
        
        scraped_raw = result.get("result", {}).get("result", {}).get("value", "[]")
        
        # 6. Get page HTML
        html_result = send_cdp("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        })
        page_html = html_result.get("result", {}).get("result", {}).get("value", "")
        
        # 7. Get page info
        info_result = send_cdp("Runtime.evaluate", {
            "expression": "JSON.stringify({title: document.title, url: window.location.href, domain: window.location.hostname})",
            "returnByValue": True,
        })
        page_info_raw = info_result.get("result", {}).get("result", {}).get("value", "{}")
        
        ws.close()
        
        # 8. Parse results
        try:
            scraped_data = json.loads(scraped_raw) if isinstance(scraped_raw, str) else scraped_raw
        except Exception:
            scraped_data = []
        
        try:
            page_info = json.loads(page_info_raw) if isinstance(page_info_raw, str) else page_info_raw
        except Exception:
            page_info = {}
        
        # Build variables dict
        variables = {}
        for item in (scraped_data if isinstance(scraped_data, list) else []):
            if isinstance(item, dict) and item.get("name"):
                variables[item["name"]] = item.get("value")
        
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
            }
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    finally:
        # ALWAYS cleanup
        if chrome_proc and chrome_proc.poll() is None:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            except Exception:
                chrome_proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Flask API
# --------------------------------------------------------------------------- #

@app.route("/url-scraper-service/api/v1/scrape/", methods=["POST"])
def scrape():
    _stats["active"] += 1
    try:
        body = flask_request.get_json(force=True)
        target_url = body.get("target_url", "")
        selectors = body.get("selectors", [])
        
        if not target_url:
            return jsonify({"success": False, "error": "target_url required"}), 400
        
        future = executor.submit(scrape_url, target_url, selectors)
        result = future.result(timeout=SCRAPE_TIMEOUT + 30)
        
        if result.get("success"):
            _stats["processed"] += 1
        else:
            _stats["errors"] += 1
        
        return jsonify(result)
    except Exception as e:
        _stats["errors"] += 1
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        _stats["active"] -= 1


@app.route("/url-scraper-service/api/v1/health/", methods=["GET"])
def health():
    return jsonify({
        "service": "url-scraper",
        "status": "healthy",
        "architecture": "fresh-chrome-cdp-per-request",
        "stats": {
            "processed": _stats["processed"],
            "errors": _stats["errors"],
            "active": _stats["active"],
            "max_concurrent": MAX_CONCURRENT,
            "uptime": int(time.time() - _stats["started_at"]),
        },
        "chrome": CHROME_BIN,
        "extension": CF_AUTOCLICK_DIR,
    })


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    parser.add_argument("--timeout", type=int, default=SCRAPE_TIMEOUT)
    parser.add_argument("--chrome", type=str, default=CHROME_BIN)
    parser.add_argument("--extension", type=str, default=CF_AUTOCLICK_DIR)
    args = parser.parse_args()
    
    CHROME_BIN = args.chrome
    CF_AUTOCLICK_DIR = args.extension
    MAX_CONCURRENT = args.max_concurrent
    SCRAPE_TIMEOUT = args.timeout
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
    
    print(f"🚀 URL Scraper starting on port {args.port}")
    print(f"   Chrome: {CHROME_BIN}")
    print(f"   Extension: {CF_AUTOCLICK_DIR}")
    print(f"   Concurrent: {MAX_CONCURRENT}")
    print(f"   Timeout: {SCRAPE_TIMEOUT}s")
    
    app.run(host="0.0.0.0", port=args.port, threaded=True)
