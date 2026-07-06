"""
URL Scraper Push-based Worker — local HTTP server that receives scrape requests.

Exposes the same API as url-scraper-service:
  POST /url-scraper-service/api/v1/scrape/

Architecture: Single browser, single active tab, sequential request queue.
All scrape requests are queued and processed one at a time to guarantee each
page gets full browser focus and renders completely. No tab-focus issues.

Usage:
    python url_scraper.py [--port 8814] [--headless] [--chrome /path/to/chrome]
                          [--extension /path/to/ext]
"""

import argparse
import atexit
import json
import os
import queue
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

# Profile tracking
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


def _create_profile(extensions: Optional[List[str]] = None) -> str:
    profile_id = f"urlscraper_{uuid.uuid4().hex[:8]}"
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


def build_driver(headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int], extension_path: Optional[str] = None,
                 cf_autoclick_path: Optional[str] = None):
    import undetected_chromedriver as uc

    ext_sources = [p for p in [cf_autoclick_path, extension_path] if p and os.path.isdir(p)]
    profile = _create_profile(extensions=ext_sources)

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

def scrape_url(driver, target_url: str, selectors_json: str, wait_for: str = "") -> Dict[str, Any]:
    """Navigate to target_url and run the url-scraper workflow JS.
    
    Args:
        wait_for: CSS selector to wait for before capturing page (for JS-rendered pages).
                  If set, waits up to 15s for element matching this selector to appear.

    Optimized for speed with aggressive early-exit:
    - Skip CF wait if no challenge detected (saves 20s)
    - Exit content wait immediately when content found (saves 5-10s)
    - Minimal fixed waits (0.5s instead of 2-3s)
    - Typical time: 8-12s per URL (vs 25s before)
    """
    t0 = time.time()
    result: Dict[str, Any] = {"target_url": target_url, "status": "error"}

    try:
        # Navigate with retry (2 attempts for speed)
        page_loaded = False
        for nav_attempt in range(2):
            try:
                driver.get(target_url)
                page_loaded = True
                break
            except Exception:
                try:
                    current = driver.current_url
                    if current and current != "about:blank" and "data:" not in current:
                        page_loaded = True
                        break
                except Exception:
                    pass
                if nav_attempt < 1:
                    time.sleep(2)

        if not page_loaded:
            try:
                driver.set_page_load_timeout(15)
                driver.get(target_url)
                page_loaded = True
            except Exception:
                pass
            finally:
                driver.set_page_load_timeout(30)

        # Verify not stuck on about:blank
        try:
            current_url = driver.current_url
            if current_url == "about:blank" or not current_url:
                result["error"] = "Navigation failed — page did not load"
                return result
        except Exception:
            pass

        # Wait for body + readyState (fast poll)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                if driver.execute_script("return !!document.querySelector('body') && document.readyState !== 'loading'"):
                    break
            except Exception:
                pass
            time.sleep(0.3)

        # Quick CF check — only wait if challenge actually present
        is_cf = False
        try:
            is_cf = driver.execute_script("""
                var title = document.title || '';
                var body = document.body ? document.body.innerText.substring(0, 500) : '';
                var text = (title + ' ' + body).toLowerCase();
                return text.indexOf('just a moment') >= 0 || text.indexOf('checking your browser') >= 0 
                    || text.indexOf('verify you are human') >= 0 || text.indexOf('attention required') >= 0;
            """)
        except Exception:
            pass

        if is_cf:
            print(f"  [cf] Challenge detected, waiting...")
            cf_deadline = time.time() + 20
            while time.time() < cf_deadline:
                time.sleep(2)
                try:
                    still_cf = driver.execute_script("""
                        var text = ((document.title||'') + ' ' + (document.body?document.body.innerText.substring(0,500):'')).toLowerCase();
                        return text.indexOf('just a moment')>=0 || text.indexOf('checking your browser')>=0 || text.indexOf('verify you are human')>=0;
                    """)
                    if not still_cf:
                        break
                except Exception:
                    break
            time.sleep(1)
        else:
            time.sleep(1)  # Brief settle for normal pages

        # wait_for: wait for a specific CSS selector to appear (for JS-rendered listing pages)
        if wait_for:
            print(f"  [wait_for] Waiting for '{wait_for}' to appear...")
            wf_deadline = time.time() + 15
            while time.time() < wf_deadline:
                try:
                    found = driver.execute_script(f"""
                        var els = document.querySelectorAll('{wait_for}');
                        return els.length >= 3;
                    """)
                    if found:
                        print(f"  [wait_for] Elements found!")
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                print(f"  [wait_for] Timeout waiting for '{wait_for}'")

        # Early JSON-LD check — for JS-heavy sites (Next.js/React), content may only
        # exist in structured data, not in DOM selectors. Extract early to avoid timeout.
        _jsonld_extracted = None
        try:
            _jsonld_extracted = driver.execute_script("""
                var scripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (var i = 0; i < scripts.length; i++) {
                    try {
                        var data = JSON.parse(scripts[i].textContent);
                        if (data.articleBody && data.articleBody.length > 100) {
                            return {headline: data.headline||'', articleBody: data.articleBody,
                                    author: (data.author&&data.author.name)||'', datePublished: data.datePublished||'',
                                    image: (data.image&&data.image[0]&&data.image[0].url)||(typeof data.image==='string'?data.image:'')||'',
                                    description: data.description||''};
                        }
                        if (Array.isArray(data)) {
                            for (var j=0;j<data.length;j++) {
                                if (data[j].articleBody && data[j].articleBody.length > 100) {
                                    var d=data[j];
                                    return {headline: d.headline||'', articleBody: d.articleBody,
                                            author: (d.author&&d.author.name)||'', datePublished: d.datePublished||'',
                                            image: (d.image&&d.image[0]&&d.image[0].url)||(typeof d.image==='string'?d.image:'')||'',
                                            description: d.description||''};
                                }
                            }
                        }
                    } catch(e) {}
                }
                return null;
            """)
        except Exception:
            pass

        # If JSON-LD has content, use it directly (skip content wait + extraction)
        if _jsonld_extracted and _jsonld_extracted.get("articleBody"):
            print(f"  [json-ld] ✓ Fast extraction from JSON-LD ({len(_jsonld_extracted['articleBody'])} chars)")
            body = _jsonld_extracted["articleBody"]
            scraped_data_raw = json.dumps([
                {"name": "source_title", "selector": "json-ld", "value": _jsonld_extracted.get("headline", "")},
                {"name": "source_content", "selector": "json-ld", "value": "<p>" + body.replace("\n\n", "</p><p>").replace("\n", " ") + "</p>"},
                {"name": "source_author", "selector": "json-ld", "value": _jsonld_extracted.get("author", "")},
                {"name": "source_published_date", "selector": "json-ld", "value": _jsonld_extracted.get("datePublished", "")},
                {"name": "source_featured_image", "selector": "json-ld", "value": _jsonld_extracted.get("image", "")},
                {"name": "source_meta_description", "selector": "json-ld", "value": _jsonld_extracted.get("description", "")},
            ])
            page_info_js = load_page_info_js(WORKFLOW_JSON_PATH)
            page_info_raw = driver.execute_script(f"return (function() {{ {page_info_js} }})()")
            result["scraped_data"] = scraped_data_raw
            result["page_info"] = page_info_raw
            result["final_url"] = driver.current_url
            result["page_source"] = ""  # Skip page_source for JSON-LD extraction (saves memory)
            result["status"] = "completed"
            result["elapsed_seconds"] = round(time.time() - t0, 2)
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            return result

        # Smart content wait — exit IMMEDIATELY when content found (poll every 0.5s)
        try:
            content_deadline = time.time() + 8
            while time.time() < content_deadline:
                has_content = driver.execute_script("""
                    var el = document.querySelector('.articlecontent, article, .entry-content, .post-content, [itemprop="articleBody"], main, .content-main');
                    return el && el.innerText && el.innerText.length > 200;
                """)
                if has_content:
                    break
                time.sleep(0.5)
        except Exception:
            pass

        # Quick scroll + cookie dismiss (parallel, minimal wait)
        try:
            driver.execute_script("""
                window.scrollTo(0, document.body.scrollHeight);
                var s = ["button[class*='accept']","button[class*='Accept']","[id*='onetrust-accept']","button[id*='accept']"];
                for (var i=0;i<s.length;i++){try{var e=document.querySelector(s[i]);if(e){e.click();break;}}catch(x){}}
            """)
        except Exception:
            pass
        time.sleep(0.5)

        # Execute scraper workflow JS
        scraper_js = load_workflow_js(WORKFLOW_JSON_PATH)
        js_with_selectors = scraper_js.replace("${selectors_json}", selectors_json)
        scraped_data_raw = driver.execute_script(f"return (function() {{ {js_with_selectors} }})()")

        # Check if content was extracted
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

        # Retry ONLY if empty
        if not has_content:
            print(f"  [retry] Content empty, retrying in 3s...")
            time.sleep(3)
            scraped_data_raw = driver.execute_script(f"return (function() {{ {js_with_selectors} }})()")

        # JSON-LD fallback: if still no content, try extracting from structured data
        # (works for Next.js/React sites where content is in JSON-LD but not in DOM selectors)
        has_content_after_retry = False
        if scraped_data_raw:
            parsed_retry = json.loads(scraped_data_raw) if isinstance(scraped_data_raw, str) else scraped_data_raw
            if isinstance(parsed_retry, list):
                for item in parsed_retry:
                    if isinstance(item, dict) and item.get("name") == "source_content":
                        val = item.get("value", "")
                        if val and len(str(val)) > 200:
                            has_content_after_retry = True
                            break

        if not has_content_after_retry:
            print(f"  [json-ld] Trying JSON-LD extraction...")
            try:
                jsonld_data = driver.execute_script("""
                    var scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (var i = 0; i < scripts.length; i++) {
                        try {
                            var data = JSON.parse(scripts[i].textContent);
                            if (data.articleBody && data.articleBody.length > 100) {
                                return JSON.stringify([
                                    {name: "source_title", selector: "json-ld", value: data.headline || ""},
                                    {name: "source_content", selector: "json-ld", value: "<p>" + data.articleBody.replace(/\\n\\n/g, "</p><p>").replace(/\\n/g, " ") + "</p>"},
                                    {name: "source_author", selector: "json-ld", value: (data.author && data.author.name) || ""},
                                    {name: "source_published_date", selector: "json-ld", value: data.datePublished || ""},
                                    {name: "source_featured_image", selector: "json-ld", value: (data.image && data.image[0] && data.image[0].url) || (typeof data.image === 'string' ? data.image : "")},
                                    {name: "source_meta_description", selector: "json-ld", value: data.description || ""}
                                ]);
                            }
                            // Handle array of JSON-LD
                            if (Array.isArray(data)) {
                                for (var j = 0; j < data.length; j++) {
                                    if (data[j].articleBody && data[j].articleBody.length > 100) {
                                        var d = data[j];
                                        return JSON.stringify([
                                            {name: "source_title", selector: "json-ld", value: d.headline || ""},
                                            {name: "source_content", selector: "json-ld", value: "<p>" + d.articleBody.replace(/\\n\\n/g, "</p><p>").replace(/\\n/g, " ") + "</p>"},
                                            {name: "source_author", selector: "json-ld", value: (d.author && d.author.name) || ""},
                                            {name: "source_published_date", selector: "json-ld", value: d.datePublished || ""},
                                            {name: "source_featured_image", selector: "json-ld", value: (d.image && d.image[0] && d.image[0].url) || (typeof d.image === 'string' ? d.image : "")},
                                            {name: "source_meta_description", selector: "json-ld", value: d.description || ""}
                                        ]);
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                    return null;
                """)
                if jsonld_data:
                    scraped_data_raw = jsonld_data
                    print(f"  [json-ld] ✓ Extracted content from JSON-LD structured data")
            except Exception:
                pass
        # Get page info
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
# Sequential Browser Worker
# --------------------------------------------------------------------------- #

class ScrapeRequest:
    """A scrape request with a result event for synchronous response."""
    def __init__(self, target_url: str, selectors_json: str, wait_for: str = ""):
        self.target_url = target_url
        self.selectors_json = selectors_json
        self.wait_for = wait_for
        self.result: Optional[Dict[str, Any]] = None
        self.event = threading.Event()


class BrowserInstance:
    """A single isolated browser with its own queue and worker thread."""

    def __init__(self, instance_id: int, headless: bool, chrome_bin: Optional[str],
                 version_main: Optional[int], extension_path: Optional[str],
                 cf_autoclick_path: Optional[str]):
        self.id = instance_id
        self._headless = headless
        self._chrome_bin = chrome_bin
        self._version_main = version_main
        self._extension_path = extension_path
        self._cf_autoclick_path = cf_autoclick_path
        self._driver = None
        self._profile = None
        self._total_scrapes = 0
        self._recycle_after = 100
        self._queue: queue.Queue = queue.Queue()
        self._running = True
        self._total_processed = 0
        self._total_errors = 0
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _ensure_browser(self):
        if self._driver is not None:
            try:
                _ = self._driver.title
                return
            except Exception:
                self._kill_browser()
        print(f"[Browser-{self.id}] Starting...")
        self._driver, self._profile = build_driver(
            self._headless, self._chrome_bin, self._version_main,
            self._extension_path, self._cf_autoclick_path,
        )
        self._total_scrapes = 0
        print(f"[Browser-{self.id}] Ready")

    def _kill_browser(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
        self._driver = None
        if self._profile:
            _remove_profile(self._profile)
            self._profile = None

    def _worker_loop(self):
        print(f"[Browser-{self.id}] Worker thread started")
        while self._running:
            try:
                req: ScrapeRequest = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._ensure_browser()
                qsize = self._queue.qsize()
                print(f"[Browser-{self.id}] Processing: {req.target_url[:70]} (queue: {qsize})")

                # Per-scrape timeout: kill browser after 45s
                scrape_timed_out = False
                def _timeout_handler():
                    nonlocal scrape_timed_out
                    scrape_timed_out = True
                    print(f"  [Browser-{self.id}] Timeout 45s — killing browser")
                    self._kill_browser()

                timer = threading.Timer(45.0, _timeout_handler)
                timer.start()
                try:
                    result = scrape_url(self._driver, req.target_url, req.selectors_json, req.wait_for)
                finally:
                    timer.cancel()

                if scrape_timed_out:
                    req.result = {
                        "target_url": req.target_url,
                        "status": "error",
                        "error": "Scrape timed out after 45s",
                        "elapsed_seconds": 45,
                    }
                    self._total_errors += 1
                else:
                    req.result = result
                    self._total_processed += 1
                    self._total_scrapes += 1
                    emoji = "✓" if result.get("status") == "completed" else "✗"
                    print(f"  [Browser-{self.id}] [{emoji}] {result.get('elapsed_seconds', 0)}s — {req.target_url[:60]}")

            except Exception as e:
                print(f"  [Browser-{self.id}] Error: {e}")
                req.result = {
                    "target_url": req.target_url,
                    "status": "error",
                    "error": str(e),
                    "elapsed_seconds": 0,
                }
                self._total_errors += 1
                self._kill_browser()
            finally:
                req.event.set()
                self._queue.task_done()

            if self._total_scrapes >= self._recycle_after:
                print(f"[Browser-{self.id}] Recycling after {self._total_scrapes} scrapes")
                self._kill_browser()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def submit(self, req: ScrapeRequest):
        self._queue.put(req)

    def shutdown(self):
        self._running = False
        self._kill_browser()


class MultiBrowserPool:
    """Multiple isolated browser instances with round-robin distribution.
    
    Each browser runs in its own thread with its own queue. Requests are
    distributed to the browser with the shortest queue (least busy).
    
    3 browsers × 1 tab each = 3 parallel scrapes, each with full focus.
    No tab-switching issues. 3x throughput vs single browser.
    """

    def __init__(self, num_instances: int, headless: bool, chrome_bin: Optional[str],
                 version_main: Optional[int], extension_path: Optional[str],
                 cf_autoclick_path: Optional[str]):
        self._instances: List[BrowserInstance] = []
        for i in range(num_instances):
            instance = BrowserInstance(
                i, headless, chrome_bin, version_main,
                extension_path, cf_autoclick_path,
            )
            self._instances.append(instance)
        print(f"[Pool] Started {num_instances} browser instances")

    def submit(self, target_url: str, selectors_json: str, timeout: float = 90, wait_for: str = "") -> Dict[str, Any]:
        """Submit request to least-busy browser and wait for result."""
        req = ScrapeRequest(target_url, selectors_json, wait_for)

        # Pick browser with shortest queue
        best = min(self._instances, key=lambda b: b.queue_size)
        best.submit(req)

        if not req.event.wait(timeout=timeout):
            return {
                "target_url": target_url,
                "status": "error",
                "error": f"Scrape timed out after {timeout}s (queue congestion)",
                "elapsed_seconds": timeout,
            }
        return req.result

    def get_stats(self) -> Dict[str, Any]:
        total_processed = sum(b._total_processed for b in self._instances)
        total_errors = sum(b._total_errors for b in self._instances)
        queues = [b.queue_size for b in self._instances]
        return {
            "instances": len(self._instances),
            "queue_sizes": queues,
            "total_queue": sum(queues),
            "total_processed": total_processed,
            "total_errors": total_errors,
            "browsers_alive": [b._driver is not None for b in self._instances],
        }

    def shutdown(self):
        for b in self._instances:
            b.shutdown()
        _cleanup_all()


# --------------------------------------------------------------------------- #
# HTTP Server
# --------------------------------------------------------------------------- #

def create_app(worker: MultiBrowserPool):
    """Create Flask app with /url-scraper-service/api/v1/scrape/ endpoint."""
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/url-scraper-service/api/v1/scrape/", methods=["POST"])
    def scrape():
        body = request.get_json(force=True)
        target_url = body.get("target_url", "")
        selectors = body.get("selectors", [])
        wait_for = body.get("wait_for", "")  # CSS selector to wait for before capture
        if not target_url:
            return jsonify({"success": False, "error_message": "target_url required"}), 400

        # Default selectors if none provided
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

        # Build selectors_json
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

        # Submit to sequential queue and wait
        result = worker.submit(target_url, selectors_json, timeout=90, wait_for=wait_for)

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

        # Add page_html for AI selector detection
        page_source = result.get("page_source", "")
        if page_source:
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
        stats = worker.get_stats()
        return jsonify({
            "status": "healthy",
            "service": "url-scraper-local",
            "architecture": "multi-browser-pool",
            "stats": stats,
        })

    @app.route("/", methods=["GET"])
    def root():
        stats = worker.get_stats()
        return jsonify({
            "service": "url-scraper-local",
            "version": "3.0.0",
            "architecture": "multi-browser-pool (3 browsers, 1 tab each, parallel)",
            "docs": "POST /url-scraper-service/api/v1/scrape/",
            "stats": stats,
        })

    return app


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(
        description="URL Scraper v3 — Multi-browser pool. "
                    "3 isolated browsers, 1 tab each, parallel processing. Full focus per page.",
    )
    p.add_argument("--port", type=int, default=8814)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--workers", type=int, default=3, help="Number of parallel browser instances (default 3)")
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

    num_browsers = args.workers  # Default 3 browser instances

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  URL Scraper v3.0 — Multi-Browser Pool                   ║")
    print(f"╠══════════════════════════════════════════════════════════╣")
    print(f"║  Endpoint: POST /url-scraper-service/api/v1/scrape/     ║")
    print(f"║  Mode: {num_browsers} browsers, 1 tab each, parallel processing       ║")
    print(f"║  Guarantee: Every page gets full browser focus           ║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print(f"")
    print(f"  Listen:       http://{args.host}:{args.port}")
    print(f"  Browsers:     {num_browsers} parallel instances")
    print(f"  Chrome:       {chrome_bin}")
    print(f"  CF-Autoclick: {cf_autoclick_path or 'none'}")
    print(f"  Extension:    {extension_path or 'none'}")
    print(f"  Workflow:     {WORKFLOW_JSON_PATH}")
    print(f"  Recycle:      every 100 scrapes per browser")
    print(f"  Throughput:   ~{num_browsers * 6}-{num_browsers * 10} URLs/min")
    print(f"")

    worker = MultiBrowserPool(
        num_instances=num_browsers,
        headless=args.headless, chrome_bin=chrome_bin,
        version_main=version_main, extension_path=extension_path,
        cf_autoclick_path=cf_autoclick_path,
    )
    atexit.register(worker.shutdown)

    app = create_app(worker)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
