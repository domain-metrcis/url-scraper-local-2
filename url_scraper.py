"""
URL Scraper Push-based Worker — local HTTP server that receives scrape requests.

Exposes the same API as url-scraper-service:
  POST /url-scraper-service/api/v1/scrape/

Callers push scrape tasks to this server. Each request navigates a browser
to the target URL, executes the url-scraper.json workflow JS, and returns
extracted data synchronously.

Usage:
    python url_scraper.py [--port 8814] [--workers 3] [--chrome /path/to/chrome]
                          [--extension /path/to/ext]
"""

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKFLOW_JSON_PATH = os.path.join(SCRIPT_DIR, "url-scraper.json")

# Per-worker profile tracking
_CREATED_PROFILES: List[str] = []
_PROFILES_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Workflow JS loader
# --------------------------------------------------------------------------- #

def load_workflow_js(spec_path: str) -> str:
    """Load the evaluate script from url-scraper.json workflow."""
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    actions = spec.get("actions", [])
    for action in reversed(actions):
        if action.get("type") == "evaluate" and action.get("set_variable") == "scraped_data":
            return action["script"]
    evals = [a for a in actions if a.get("type") == "evaluate"]
    if evals:
        return evals[-1]["script"]
    raise RuntimeError(f"{spec_path} has no evaluate actions")


def load_page_info_js(spec_path: str) -> str:
    """Load the page_info evaluate script."""
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    for action in spec.get("actions", []):
        if action.get("type") == "evaluate" and action.get("set_variable") == "page_info":
            return action["script"]
    return "return JSON.stringify({ title: document.title, url: window.location.href, domain: window.location.hostname })"


# --------------------------------------------------------------------------- #
# Browser helpers
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_chrome_binary() -> Optional[str]:
    import platform
    system = platform.system()
    if system == "Linux":
        candidates = [
            os.path.join(SCRIPT_DIR, "vendor", "ungoogled-chromium", "chrome"),
            "/usr/bin/ungoogled-chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
        ]
    elif system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = []
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def detect_chrome_major(chrome_binary: Optional[str]) -> Optional[int]:
    import subprocess, re
    if not chrome_binary:
        return None
    try:
        out = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=5)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _create_profile(worker_id: int, extensions: Optional[List[str]] = None) -> str:
    profile_id = f"urlscraper_w{worker_id}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(tempfile.gettempdir(), profile_id)
    os.makedirs(dest, exist_ok=True)
    if extensions:
        ext_base = os.path.join(dest, "Extensions")
        os.makedirs(ext_base, exist_ok=True)
        for ext_path in extensions:
            if ext_path and os.path.isdir(ext_path):
                ext_name = os.path.basename(os.path.realpath(ext_path))
                ext_dest = os.path.join(ext_base, ext_name)
                shutil.copytree(ext_path, ext_dest, dirs_exist_ok=True)
    with _PROFILES_LOCK:
        _CREATED_PROFILES.append(dest)
    return dest


def _remove_profile(path: Optional[str]) -> None:
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    with _PROFILES_LOCK:
        try:
            _CREATED_PROFILES.remove(path)
        except ValueError:
            pass


def _cleanup_all():
    with _PROFILES_LOCK:
        paths = list(_CREATED_PROFILES)
        _CREATED_PROFILES.clear()
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


atexit.register(_cleanup_all)


def build_driver(worker_id: int, headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int], extension_path: Optional[str] = None,
                 cf_autoclick_path: Optional[str] = None):
    import undetected_chromedriver as uc

    ext_sources = [p for p in [cf_autoclick_path, extension_path] if p and os.path.isdir(p)]
    profile = _create_profile(worker_id, extensions=ext_sources)

    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--remote-debugging-port={_free_port()}")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")

    ext_base = os.path.join(profile, "Extensions")
    if os.path.isdir(ext_base):
        ext_dirs = [os.path.join(ext_base, d) for d in os.listdir(ext_base)
                    if os.path.isdir(os.path.join(ext_base, d))]
        if ext_dirs:
            joined = ",".join(ext_dirs)
            opts.add_argument(f"--load-extension={joined}")
            opts.add_argument(f"--disable-extensions-except={joined}")

    if chrome_binary:
        opts.binary_location = chrome_binary

    vendored_chromedriver = os.path.join(SCRIPT_DIR, "vendor", "ungoogled-chromium", "chromedriver")
    driver_kwargs = dict(
        options=opts, headless=headless, use_subprocess=True,
        version_main=version_main, user_data_dir=profile,
    )
    if os.path.isfile(vendored_chromedriver):
        driver_kwargs["driver_executable_path"] = vendored_chromedriver

    driver = uc.Chrome(**driver_kwargs)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(60)
    return driver, profile


# --------------------------------------------------------------------------- #
# Scrape logic
# --------------------------------------------------------------------------- #

def scrape_url(driver, target_url: str, selectors_json: str) -> Dict[str, Any]:
    """Navigate to target_url and run the url-scraper workflow JS."""
    t0 = time.time()
    result: Dict[str, Any] = {"target_url": target_url, "status": "error"}

    try:
        try:
            driver.get(target_url)
        except Exception:
            pass

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if driver.execute_script("return !!document.querySelector('body')"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        time.sleep(3)

        # Wait for content to render (dynamic JS pages need more time)
        # Try to detect when main content is loaded
        try:
            content_deadline = time.time() + 10
            while time.time() < content_deadline:
                has_content = driver.execute_script("""
                    var el = document.querySelector('.articlecontent, article, .entry-content, .post-content, [itemprop="articleBody"], main');
                    return el && el.innerText && el.innerText.length > 200;
                """)
                if has_content:
                    break
                time.sleep(1)
        except Exception:
            pass

        time.sleep(2)

        # Dismiss cookie banners
        try:
            driver.execute_script("""
                var s = ["button[class*='accept']","button[class*='Accept']",
                    "[id*='onetrust-accept']","button[id*='accept']"];
                for (var i=0;i<s.length;i++){try{var e=document.querySelector(s[i]);if(e){e.click();break;}}catch(x){}}
            """)
        except Exception:
            pass

        time.sleep(1)
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(2)

        scraper_js = load_workflow_js(WORKFLOW_JSON_PATH)
        js_with_selectors = scraper_js.replace("${selectors_json}", selectors_json)
        scraped_data_raw = driver.execute_script(f"return (function() {{ {js_with_selectors} }})()")

        # Check if content was extracted — if empty, wait more and retry (page may still be loading)
        has_content = False
        if scraped_data_raw:
            parsed_check = json.loads(scraped_data_raw) if isinstance(scraped_data_raw, str) else scraped_data_raw
            if isinstance(parsed_check, list):
                for item in parsed_check:
                    if isinstance(item, dict) and item.get("name") == "source_content":
                        val = item.get("value", "")
                        if val and len(str(val)) > 200:
                            has_content = True
                            break

        if not has_content:
            # Retry after additional wait — page JS may still be rendering
            time.sleep(5)
            try:
                driver.execute_script("window.scrollTo(0, 0)")
            except Exception:
                pass
            time.sleep(2)
            scraped_data_raw = driver.execute_script(f"return (function() {{ {js_with_selectors} }})()")

        page_info_js = load_page_info_js(WORKFLOW_JSON_PATH)
        page_info_raw = driver.execute_script(f"return (function() {{ {page_info_js} }})()")

        result["scraped_data"] = scraped_data_raw
        result["page_info"] = page_info_raw
        result["final_url"] = driver.current_url
        result["page_source"] = driver.page_source
        result["status"] = "completed"

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["elapsed_seconds"] = round(time.time() - t0, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()

    return result


# --------------------------------------------------------------------------- #
# Browser pool
# --------------------------------------------------------------------------- #

class BrowserPool:
    """Thread-safe pool of browser instances. Each scrape request gets its own
    dedicated browser instance — no tab sharing (Selenium is not thread-safe
    for multi-tab on a single driver).
    
    Pool pre-warms browsers and recycles them after N uses to prevent memory leaks."""

    def __init__(self, size: int, headless: bool, chrome_bin: Optional[str],
                 version_main: Optional[int], extension_path: Optional[str],
                 cf_autoclick_path: Optional[str]):
        self._max_size = size
        self._headless = headless
        self._chrome_bin = chrome_bin
        self._version_main = version_main
        self._extension_path = extension_path
        self._cf_autoclick_path = cf_autoclick_path
        self._semaphore = threading.Semaphore(size)
        self._lock = threading.Lock()
        self._drivers: List[tuple] = []  # [(driver, profile, uses)]
        self._next_id = 0
        self._recycle_after = 30  # Restart browser after N scrapes

    def _create(self) -> tuple:
        wid = self._next_id
        self._next_id += 1
        driver, profile = build_driver(
            wid, self._headless, self._chrome_bin, self._version_main,
            self._extension_path, self._cf_autoclick_path,
        )
        return driver, profile, 0

    def acquire(self) -> tuple:
        self._semaphore.acquire()
        with self._lock:
            if self._drivers:
                return self._drivers.pop()
        try:
            return self._create()
        except Exception as e:
            self._semaphore.release()
            raise RuntimeError(f"Failed to create browser: {e}")

    def release(self, entry: tuple, broken: bool = False):
        driver, profile, uses = entry
        uses += 1
        if broken or uses >= self._recycle_after:
            # Kill and cleanup
            try:
                driver.quit()
            except Exception:
                pass
            _remove_profile(profile)
        else:
            # Navigate to blank to free memory, then return to pool
            try:
                driver.get("about:blank")
            except Exception:
                try:
                    driver.quit()
                except Exception:
                    pass
                _remove_profile(profile)
                self._semaphore.release()
                return
            with self._lock:
                self._drivers.append((driver, profile, uses))
        self._semaphore.release()

    def shutdown(self):
        with self._lock:
            for driver, profile, _ in self._drivers:
                try:
                    driver.quit()
                except Exception:
                    pass
                _remove_profile(profile)
            self._drivers.clear()
        _cleanup_all()


# --------------------------------------------------------------------------- #
# HTTP Server (Push endpoint)
# --------------------------------------------------------------------------- #

def create_app(pool: BrowserPool):
    """Create Flask app with /url-scraper-service/api/v1/scrape/ endpoint."""
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/url-scraper-service/api/v1/scrape/", methods=["POST"])
    def scrape():
        body = request.get_json(force=True)
        target_url = body.get("target_url", "")
        selectors = body.get("selectors", [])
        if not target_url:
            return jsonify({"success": False, "error_message": "target_url required"}), 400

        # If no selectors provided, use defaults to extract content
        if not selectors:
            selectors = [
                {"name": "source_title", "selector": "h1, title", "attribute": "text"},
                {"name": "source_content", "selector": "article, .entry-content, .post-content, .article-body, main, #content, body", "attribute": "html"},
                {"name": "source_author", "selector": ".author, [rel='author'], .byline, .pst-by_lnk", "attribute": "text"},
                {"name": "source_published_date", "selector": "time[datetime], .date, .published", "attribute": "text"},
                {"name": "source_meta_description", "selector": "meta[name='description']", "attribute": "content"},
                {"name": "source_canonical_url", "selector": "link[rel='canonical']", "attribute": "href"},
                {"name": "source_featured_image", "selector": "meta[property='og:image']", "attribute": "content"},
            ]

        # Build selectors_json in the format the workflow expects
        selectors_json = json.dumps([
            {
                "name": s.get("name", ""),
                "selector": s.get("selector", ""),
                "js_query": s.get("js_query", ""),
                "is_multiple_value": s.get("is_multiple_value", False),
                "remove_selector": s.get("remove_selector", []),
                "custom": s.get("custom", False),
                "is_external_link": s.get("is_external_link", False),
                "is_internal_link": s.get("is_internal_link", False),
            }
            for s in selectors
        ])

        entry = pool.acquire()
        driver, profile, uses = entry
        broken = False
        try:
            result = scrape_url(driver, target_url, selectors_json)
        except Exception as e:
            broken = True
            result = {"target_url": target_url, "status": "error", "error": str(e)}
        finally:
            pool.release((driver, profile, uses), broken=broken)

        # Parse scraped_data into variables
        variables = {}
        scraped_data = []
        if result.get("scraped_data"):
            raw = result["scraped_data"]
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                scraped_data = parsed
                for item in parsed:
                    if isinstance(item, dict) and item.get("name"):
                        variables[item["name"]] = item.get("value")

        # Add raw page_source as page_html in scraped_data (for AI selector detection)
        page_source = result.get("page_source", "")
        if page_source:
            # Truncate to 100K to keep response fast over the tunnel
            truncated_source = page_source[:100000] if len(page_source) > 100000 else page_source
            scraped_data.insert(0, {"name": "page_html", "selector": "html", "value": truncated_source})
            variables["page_html"] = truncated_source

        if result.get("page_info"):
            raw = result["page_info"]
            info = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(info, dict):
                variables["__page_title"] = info.get("title", "")
                variables["__page_url"] = info.get("url", "")

        return jsonify({
            "success": True,
            "data": {
                "target_url": target_url,
                "final_url": result.get("final_url", target_url),
                "http_status": 200,
                "elapsed_ms": int(result.get("elapsed_seconds", 0) * 1000),
                "variables": variables,
                "scraped_data": scraped_data,
                "screenshot_b64": None,
                "detected_selectors": [],
            },
            "message": "Scrape completed",
        })

    @app.route("/url-scraper-service/api/v1/health/", methods=["GET"])
    @app.route("/health/", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "service": "url-scraper-local"})

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "url-scraper-local",
            "version": "1.0.0",
            "docs": "POST /url-scraper-service/api/v1/scrape/",
        })

    return app


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(
        description="URL Scraper — Push-based local worker. Runs an HTTP server "
                    "that accepts scrape requests (same API as url-scraper-service).",
    )
    p.add_argument("--port", type=int, default=8814)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--workers", type=int, default=5, help="Max concurrent tabs (parallel scrapes)")
    p.add_argument("--chrome", help="Path to chrome/chromium binary")
    p.add_argument("--extension", help="Path to real-botxbyte-extension folder")
    args = p.parse_args()

    # Signal handlers
    def _handler(signum, _frame):
        _cleanup_all()
        sys.exit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass

    # Resolve extensions
    extension_path: Optional[str] = None
    if args.extension:
        extension_path = os.path.abspath(os.path.expanduser(args.extension))
    else:
        vendored = os.path.join(SCRIPT_DIR, "vendor", "real-botxbyte-extension")
        if os.path.isfile(os.path.join(vendored, "manifest.json")):
            extension_path = vendored

    cf_autoclick_path: Optional[str] = None
    vendored_cf = os.path.join(SCRIPT_DIR, "vendor", "cf-autoclick")
    if os.path.isfile(os.path.join(vendored_cf, "manifest.json")):
        cf_autoclick_path = vendored_cf

    chrome_bin = args.chrome or find_chrome_binary()
    version_main = detect_chrome_major(chrome_bin)

    print(f"[*] URL Scraper - Parallel Browser Pool (HTTP Server)")
    print(f"[*] Listening: http://{args.host}:{args.port}")
    print(f"[*] Endpoint: POST /url-scraper-service/api/v1/scrape/")
    print(f"[*] Chrome: {chrome_bin}")
    print(f"[*] CF-Autoclick: {cf_autoclick_path or 'none'}")
    print(f"[*] Extension: {extension_path or 'none'}")
    print(f"[*] Pool size: {args.workers} browser instances")
    print(f"[*] Workflow: {WORKFLOW_JSON_PATH}")
    print(f"[*] Architecture: {args.workers} isolated browsers, thread-safe")
    print()

    pool = BrowserPool(
        size=args.workers, headless=args.headless, chrome_bin=chrome_bin,
        version_main=version_main, extension_path=extension_path,
        cf_autoclick_path=cf_autoclick_path,
    )
    atexit.register(pool.shutdown)

    app = create_app(pool)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
