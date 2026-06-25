#!/usr/bin/env python3
import asyncio, json, uuid, os, zipfile, subprocess, shutil, tempfile, sys
from pathlib import Path
import aiohttp
from aiohttp import web, ClientSession
import websockets
import boto3
from faster_whisper import WhisperModel

# Log file - capture all print() output to this file
LOG_FILE = Path(__file__).parent / "server.log"

class TeeWriter:
    """Write to both a file and the original stream."""
    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file
        self.file = open(log_file, "a", buffering=1)  # line-buffered

    def write(self, data):
        self.original.write(data)
        self.file.write(data)

    def flush(self):
        self.original.flush()
        self.file.flush()

# Redirect stdout and stderr to also write to log file
sys.stdout = TeeWriter(sys.__stdout__, LOG_FILE)
sys.stderr = TeeWriter(sys.__stderr__, LOG_FILE)

# Config
WS_PORT = 8765
HTTP_PORT = 8766
MAX_TABS = int(os.getenv("MAX_TABS", "10"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "20"))
PROFILES_DIR = Path("./profiles")
EXTENSION_PATH = Path(__file__).parent.resolve()

# Wasabi config (S3-compatible)
WASABI_ENDPOINT = os.getenv("WASABI_ENDPOINT_URL", "https://s3.us-central-1.wasabisys.com")
WASABI_ACCESS_KEY = os.getenv("WASABI_ACCESS_KEY_ID", "")
WASABI_SECRET_KEY = os.getenv("WASABI_SECRET_ACCESS_KEY", "")
WASABI_BUCKET = os.getenv("BROWSER_PROFILE_BUCKET", "browser_profiles")
WORKSPACE_ID = os.getenv("WORKSPACE_ID", "default")

ws_conn = None
pending = {}
chrome_processes = {}
tab_semaphore = None  # initialized in main()
active_tab_count = 0
queued_task_count = 0

# Whisper config
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
_whisper_model = None

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        print(f"🧠 Loading Whisper model: {WHISPER_MODEL_NAME} on {WHISPER_DEVICE} ({WHISPER_COMPUTE})")
        _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        print("✅ Whisper model loaded")
    return _whisper_model

def get_s3_client():
    if not WASABI_ACCESS_KEY or not WASABI_SECRET_KEY:
        return None
    return boto3.client("s3", endpoint_url=WASABI_ENDPOINT, 
                        aws_access_key_id=WASABI_ACCESS_KEY,
                        aws_secret_access_key=WASABI_SECRET_KEY)

# === Profile Management ===

def download_profile(profile_id: str) -> dict:
    """Download profile from Wasabi"""
    s3 = get_s3_client()
    if not s3:
        return {"success": False, "error": "Wasabi not configured"}
    
    s3_key = f"workspaces/{WORKSPACE_ID}/profiles/{profile_id}.zip"
    local_zip = PROFILES_DIR / f"{profile_id}.zip"
    profile_path = PROFILES_DIR / profile_id
    
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        print(f"📥 Downloading: {s3_key}")
        s3.download_file(WASABI_BUCKET, s3_key, str(local_zip))
        
        # Extract
        if profile_path.exists():
            shutil.rmtree(profile_path)
        with zipfile.ZipFile(local_zip, 'r') as z:
            z.extractall(profile_path)
        local_zip.unlink()
        
        print(f"✅ Profile ready: {profile_path}")
        return {"success": True, "path": str(profile_path)}
    except Exception as e:
        return {"success": False, "error": str(e)}

def upload_profile(profile_id: str) -> dict:
    """Upload profile to Wasabi"""
    s3 = get_s3_client()
    if not s3:
        return {"success": False, "error": "Wasabi not configured"}
    
    profile_path = PROFILES_DIR / profile_id
    if not profile_path.exists():
        return {"success": False, "error": "Profile not found"}
    
    s3_key = f"workspaces/{WORKSPACE_ID}/profiles/{profile_id}.zip"
    local_zip = PROFILES_DIR / f"{profile_id}.zip"
    
    try:
        print(f"📦 Zipping: {profile_path}")
        with zipfile.ZipFile(local_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            for file in profile_path.rglob('*'):
                if file.is_file():
                    z.write(file, file.relative_to(profile_path))
        
        print(f"📤 Uploading: {s3_key}")
        s3.upload_file(str(local_zip), WASABI_BUCKET, s3_key)
        local_zip.unlink()
        
        return {"success": True, "s3_key": s3_key}
    except Exception as e:
        return {"success": False, "error": str(e)}

def create_profile(profile_id: str) -> dict:
    """Create empty profile directory"""
    profile_path = PROFILES_DIR / profile_id
    profile_path.mkdir(parents=True, exist_ok=True)
    return {"success": True, "path": str(profile_path)}

# === Chrome Browser ===

def launch_chrome(profile_id: str) -> dict:
    """Launch Chrome with profile and extension"""
    profile_path = PROFILES_DIR / profile_id
    
    if not profile_path.exists():
        profile_path.mkdir(parents=True, exist_ok=True)
    
    # Find Chrome
    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    ]
    chrome_bin = next((p for p in chrome_paths if Path(p).exists()), None)
    
    if not chrome_bin:
        return {"success": False, "error": "Chrome not found"}
    
    args = [
        chrome_bin,
        f"--user-data-dir={profile_path}",
        f"--load-extension={EXTENSION_PATH}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chrome_processes[profile_id] = proc
        print(f"🚀 Chrome launched: PID {proc.pid}, Profile: {profile_id}")
        return {"success": True, "pid": proc.pid, "profile": profile_id}
    except Exception as e:
        return {"success": False, "error": str(e)}

def close_chrome(profile_id: str) -> dict:
    """Close Chrome for profile"""
    if profile_id in chrome_processes:
        chrome_processes[profile_id].terminate()
        del chrome_processes[profile_id]
        return {"success": True}
    return {"success": False, "error": "Chrome not running"}

# === WebSocket Handler ===

async def ws_handler(ws):
    global ws_conn
    ws_conn = ws
    print("✅ Extension connected")
    try:
        async for msg in ws:
            data = json.loads(msg)
            if data.get('id') in pending:
                pending[data['id']].set_result(data)
    finally:
        ws_conn = None

# === HTTP Endpoints ===

async def workflow_handler(req):
    global active_tab_count, queued_task_count

    if not ws_conn:
        return web.json_response({"error": "Extension not connected"}, status=503)

    # Reject if queue is full (tasks waiting beyond the active tab slots)
    if queued_task_count >= MAX_QUEUE:
        return web.json_response({
            "success": False,
            "error": "Queue is full. Please wait and try again.",
            "queue": {"active_tabs": active_tab_count, "max_tabs": MAX_TABS,
                      "queued": queued_task_count, "max_queue": MAX_QUEUE}
        }, status=429)

    workflow = await req.json()
    task_id = workflow.pop("task_id", None)
    webhook_url = workflow.pop("webhook_url", None)
    startup_webhook_url = workflow.pop("startup_webhook_url", None)

    # Extract ImageKit config from options before sending to extension
    imagekit_config = None
    if workflow.get("options"):
        ik_private = workflow["options"].pop("imagekit_private_key", None)
        ik_public = workflow["options"].pop("imagekit_public_key", None)
        ik_url = workflow["options"].pop("imagekit_url_endpoint", None)
        is_screenshot = workflow["options"].pop("is_screenshot", False)
        is_image_generation = workflow["options"].pop("is_image_generation", False)
        if (is_screenshot or is_image_generation) and ik_private:
            imagekit_config = {
                "imagekit_private_key": ik_private,
                "imagekit_public_key": ik_public,
                "imagekit_url_endpoint": ik_url,
            }

    req_id = str(uuid.uuid4())

    if task_id and webhook_url:
        # Async mode: reserve a queue slot, return 202 immediately
        queued_task_count += 1
        print(f"📋 Task {task_id} added to queue ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} active)")
        asyncio.create_task(_wait_and_callback(req_id, task_id, webhook_url, startup_webhook_url, imagekit_config, workflow))
        return web.json_response({
            "success": True, "task_id": task_id,
            "message": f"Workflow queued ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} tabs in use)"
        }, status=202)
    else:
        # Legacy sync mode: reserve a queue slot, wait for tab slot, then execute
        queued_task_count += 1
        print(f"⏳ Sync workflow queued ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} active)")
        await tab_semaphore.acquire()
        queued_task_count -= 1
        active_tab_count += 1
        print(f"🔒 Tab slot acquired for sync workflow ({active_tab_count}/{MAX_TABS} in use)")
        try:
            pending[req_id] = asyncio.Future()
            await ws_conn.send(json.dumps({"id": req_id, "action": "startWorkflow", "workflow": workflow}))
            result = await asyncio.wait_for(pending[req_id], timeout=3000)

            # Process generated image upload to ImageKit before returning
            ext_variables = result.get("variables", {})
            if result.get("success") and imagekit_config:
                generated_image_url = await _process_generated_image(ext_variables, imagekit_config, "sync")
                if generated_image_url:
                    result["variables"] = ext_variables
                    result["generated_image_url"] = generated_image_url

            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"error": "Timeout", "message": "Workflow did not complete within 3000 seconds"}, status=504)
        finally:
            pending.pop(req_id, None)
            tab_semaphore.release()
            active_tab_count -= 1
            print(f"🔓 Tab slot released for sync workflow ({active_tab_count}/{MAX_TABS} in use)")


async def _process_generated_image(ext_variables: dict, imagekit_config: dict, task_id: str) -> str:
    """Upload generated image to ImageKit. Returns the ImageKit URL or None."""
    if not imagekit_config:
        return None

    generated_image_url = None

    # Primary: use file path from download_and_upload action (file on disk)
    gen_img_path = ext_variables.get("generated_image_file_path")
    if gen_img_path and isinstance(gen_img_path, str) and os.path.isfile(gen_img_path):
        try:
            import base64 as b64_mod
            with open(gen_img_path, "rb") as f:
                file_bytes = f.read()
            gen_img_b64 = b64_mod.b64encode(file_bytes).decode()
            generated_image_url = await _upload_to_imagekit(gen_img_b64, imagekit_config, f"{task_id or 'unknown'}_genimg")
            if generated_image_url:
                print(f"🖼️ Generated image uploaded (file path) for task {task_id}: {generated_image_url}")
                ext_variables.pop("generated_image_file_path", None)
                ext_variables.pop("generated_image_src", None)
                ext_variables["generated_image_url"] = generated_image_url
        except Exception as e:
            print(f"❌ ImageKit upload error (file path) for task {task_id}: {e}")

    # Fallback: use browser-captured base64 if available
    gen_img_b64 = ext_variables.get("generated_image_base64")
    if gen_img_b64 and isinstance(gen_img_b64, str) and len(gen_img_b64) > 100:
        try:
            generated_image_url = await _upload_to_imagekit(gen_img_b64, imagekit_config, f"{task_id or 'unknown'}_genimg")
            if generated_image_url:
                print(f"🖼️ Generated image uploaded (browser base64) for task {task_id}: {generated_image_url}")
                ext_variables.pop("generated_image_base64", None)
                ext_variables.pop("generated_image_src", None)
                ext_variables["generated_image_url"] = generated_image_url
        except Exception as e:
            print(f"❌ ImageKit upload error (browser base64) for task {task_id}: {e}")

    # URL-based upload: pass source URL directly to ImageKit (ImageKit fetches it)
    if not generated_image_url:
        gen_img_src = ext_variables.get("generated_image_src")
        if gen_img_src and isinstance(gen_img_src, str) and gen_img_src.startswith("http"):
            try:
                generated_image_url = await _upload_to_imagekit(gen_img_src, imagekit_config, f"{task_id or 'unknown'}_genimg")
                if generated_image_url:
                    print(f"🖼️ Generated image uploaded (URL passthrough) for task {task_id}: {generated_image_url}")
                    ext_variables.pop("generated_image_src", None)
                    ext_variables["generated_image_url"] = generated_image_url
            except Exception as e:
                print(f"❌ ImageKit URL upload error for task {task_id}: {e}")

            # Fallback: download with headers and upload as base64
            if not generated_image_url:
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                        "Referer": gen_img_src.split("/")[0] + "//" + gen_img_src.split("/")[2] + "/",
                    }
                    async with ClientSession() as dl_session:
                        async with dl_session.get(gen_img_src, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as dl_resp:
                            if dl_resp.status == 200:
                                import base64 as b64_mod
                                img_bytes = await dl_resp.read()
                                gen_img_b64 = b64_mod.b64encode(img_bytes).decode()
                                generated_image_url = await _upload_to_imagekit(gen_img_b64, imagekit_config, f"{task_id or 'unknown'}_genimg")
                                if generated_image_url:
                                    print(f"🖼️ Generated image uploaded (URL download) for task {task_id}: {generated_image_url}")
                                    ext_variables.pop("generated_image_src", None)
                                    ext_variables["generated_image_url"] = generated_image_url
                            else:
                                print(f"❌ Failed to download generated image for task {task_id}: HTTP {dl_resp.status}")
                except Exception as e:
                    print(f"❌ ImageKit generated image upload error (URL fallback) for task {task_id}: {e}")

    return generated_image_url


async def _wait_and_callback(req_id: str, task_id: str, webhook_url: str, startup_webhook_url: str = None, imagekit_config: dict = None, workflow: dict = None):
    """Wait for a tab slot, dispatch workflow to extension, and POST result to webhook(s)."""
    global active_tab_count, queued_task_count

    # Wait for a tab slot before dispatching
    print(f"⏳ Task {task_id} waiting for tab slot ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} active)")
    await tab_semaphore.acquire()
    queued_task_count -= 1
    active_tab_count += 1
    print(f"🔒 Tab slot acquired for task {task_id} ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} active)")

    try:
        # Check extension is still connected after waiting
        if not ws_conn:
            callback_data = {
                "task_status": "failed",
                "output_data": {
                    "success": False,
                    "error": "Extension disconnected while waiting for tab slot",
                },
            }
            await _post_webhooks(callback_data, task_id, webhook_url, startup_webhook_url)
            return

        # Dispatch to extension now that we have a slot
        pending[req_id] = asyncio.Future()
        await ws_conn.send(json.dumps({"id": req_id, "action": "startWorkflow", "workflow": workflow}))

        try:
            result = await asyncio.wait_for(pending[req_id], timeout=3000)
            is_success = result.get("success", False)
            ext_variables = result.get("variables", {})

            # Upload screenshot to ImageKit if configured (even on workflow failure)
            screenshot_url = None
            if imagekit_config:
                screenshot_b64 = ext_variables.get("screenshot_base64") or ext_variables.get("screenshot")
                if screenshot_b64 and isinstance(screenshot_b64, str) and len(screenshot_b64) > 100:
                    try:
                        screenshot_url = await _upload_to_imagekit(screenshot_b64, imagekit_config, task_id or "unknown")
                        if screenshot_url:
                            print(f"📸 Screenshot uploaded for task {task_id}: {screenshot_url}")
                            ext_variables.pop("screenshot_base64", None)
                            ext_variables.pop("screenshot", None)
                    except Exception as e:
                        print(f"❌ ImageKit upload error for task {task_id}: {e}")

            # Upload generated image to ImageKit if configured and image was captured
            generated_image_url = None
            if is_success:
                generated_image_url = await _process_generated_image(ext_variables, imagekit_config, task_id or "unknown")

            callback_data = {
                "task_status": "completed" if is_success else "failed",
                "output_data": {
                    "success": is_success,
                    "variables": ext_variables,
                    "error": result.get("error"),
                },
            }
            if screenshot_url:
                callback_data["output_data"]["screenshot_url"] = screenshot_url
            if generated_image_url:
                callback_data["output_data"]["generated_image_url"] = generated_image_url
        except asyncio.TimeoutError:
            callback_data = {
                "task_status": "failed",
                "output_data": {
                    "success": False,
                    "error": "Workflow timed out after 3000 seconds",
                },
            }
        except Exception as e:
            callback_data = {
                "task_status": "failed",
                "output_data": {
                    "success": False,
                    "error": str(e),
                },
            }
        finally:
            pending.pop(req_id, None)
    finally:
        tab_semaphore.release()
        active_tab_count -= 1
        print(f"🔓 Tab slot released for task {task_id} ({queued_task_count}/{MAX_QUEUE} queued, {active_tab_count}/{MAX_TABS} active)")

    # POST result to webhook(s)
    await _post_webhooks(callback_data, task_id, webhook_url, startup_webhook_url)


async def _post_webhooks(callback_data: dict, task_id: str, webhook_url: str, startup_webhook_url: str = None):
    """POST callback data to webhook URL(s)."""
    async def _post_webhook(url, label):
        try:
            async with ClientSession() as session:
                print(f"📤 Sending {label} webhook for task {task_id} to: {url}")
                async with session.post(url, json=callback_data) as resp:
                    body = await resp.text()
                    print(f"📥 Webhook response ({label}) for task {task_id}: status={resp.status} body={body[:500]}")
        except Exception as e:
            print(f"❌ Webhook callback failed ({label}) for task {task_id}: {e}")

    tasks = [_post_webhook(webhook_url, "task")]
    if startup_webhook_url:
        tasks.append(_post_webhook(startup_webhook_url, "startup-task"))

    await asyncio.gather(*tasks)


async def _ensure_imagekit_folder(auth_str: str, folder_path: str) -> bool:
    """Create folder in ImageKit if it doesn't exist. Returns True on success."""
    # Split path into parent and folder name
    # e.g., "/screenshots/2026-04-18" -> parent="/screenshots", name="2026-04-18"
    parts = folder_path.strip('/').split('/')
    
    async with ClientSession() as session:
        # Create each level of the folder hierarchy
        current_parent = "/"
        for folder_name in parts:
            try:
                async with session.post(
                    'https://api.imagekit.io/v1/folder',
                    json={
                        "folderName": folder_name,
                        "parentFolderPath": current_parent
                    },
                    headers={
                        'Authorization': f'Basic {auth_str}',
                        'Content-Type': 'application/json'
                    }
                ) as resp:
                    if resp.status in (200, 201):
                        print(f"📁 Created ImageKit folder: {current_parent}{folder_name}")
                    elif resp.status == 409:
                        # Folder already exists - this is fine
                        pass
                    else:
                        body = await resp.text()
                        print(f"⚠️ ImageKit folder creation response: status={resp.status} body={body[:200]}")
            except Exception as e:
                print(f"⚠️ ImageKit folder creation error: {e}")
            
            # Update parent for next iteration
            current_parent = f"{current_parent}{folder_name}/"
    
    return True


async def _upload_to_imagekit(base64_data: str, imagekit_config: dict, task_id: str) -> str:
    """Upload base64 screenshot to ImageKit and return the public URL."""
    import base64 as b64_mod
    from datetime import datetime

    private_key = imagekit_config.get("imagekit_private_key", "")
    auth_str = b64_mod.b64encode(f"{private_key}:".encode()).decode()
    file_name = f"screenshot_{task_id}_{uuid.uuid4().hex[:8]}.png"
    date_str = datetime.now().strftime('%Y-%m-%d')
    folder = f"/screenshots/{date_str}"

    print(f"📁 ImageKit upload - folder: {folder}, fileName: {file_name}")

    # Ensure folder exists before uploading
    await _ensure_imagekit_folder(auth_str, folder)

    async with ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field('file', base64_data)
        form.add_field('fileName', file_name)
        form.add_field('folder', folder)
        form.add_field('useUniqueFileName', 'true')

        async with session.post(
            'https://upload.imagekit.io/api/v1/files/upload',
            data=form,
            headers={'Authorization': f'Basic {auth_str}'}
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                print(f"✅ ImageKit response: filePath={result.get('filePath')}, folder={result.get('folder')}")
                return result.get('url', '')
            else:
                body = await resp.text()
                print(f"❌ ImageKit upload failed for task {task_id}: status={resp.status} body={body[:500]}")
                return ""

async def profile_handler(req):
    """Profile management: GET/POST/DELETE"""
    profile_id = req.match_info.get('profile_id')
    
    if req.method == 'GET':
        # Download from Wasabi
        result = await asyncio.to_thread(download_profile, profile_id)
    elif req.method == 'POST':
        # Upload to Wasabi
        result = await asyncio.to_thread(upload_profile, profile_id)
    elif req.method == 'PUT':
        # Create empty profile
        result = create_profile(profile_id)
    elif req.method == 'DELETE':
        # Delete local profile
        profile_path = PROFILES_DIR / profile_id
        if profile_path.exists():
            shutil.rmtree(profile_path)
            result = {"success": True}
        else:
            result = {"success": False, "error": "Not found"}
    else:
        result = {"error": "Method not allowed"}
    
    return web.json_response(result)

async def chrome_handler(req):
    """Launch/close Chrome: POST to start, DELETE to stop"""
    profile_id = req.match_info.get('profile_id')

    if req.method == 'POST':
        result = launch_chrome(profile_id)
    elif req.method == 'DELETE':
        result = close_chrome(profile_id)
    else:
        result = {"error": "Method not allowed"}

    return web.json_response(result)

async def status_handler(req):
    """Get status including running workflows"""
    if not ws_conn:
        return web.json_response({
            "extension_connected": False,
            "running": 0,
            "workflows": [],
            "tabs": {"active": active_tab_count, "max": MAX_TABS, "available": MAX_TABS - active_tab_count},
            "queue": {"queued": queued_task_count, "max_queue": MAX_QUEUE, "available": MAX_QUEUE - queued_task_count},
            "chrome_processes": list(chrome_processes.keys()),
            "profiles": [p.name for p in PROFILES_DIR.iterdir()] if PROFILES_DIR.exists() else []
        })

    # Ask extension for workflow status
    req_id = str(uuid.uuid4())
    pending[req_id] = asyncio.Future()
    await ws_conn.send(json.dumps({"id": req_id, "action": "getStatus"}))

    try:
        result = await asyncio.wait_for(pending[req_id], timeout=5)
        return web.json_response({
            "extension_connected": True,
            "running": result.get("running", 0),
            "workflows": result.get("workflows", []),
            "tabs": {"active": active_tab_count, "max": MAX_TABS, "available": MAX_TABS - active_tab_count},
            "queue": {"queued": queued_task_count, "max_queue": MAX_QUEUE, "available": MAX_QUEUE - queued_task_count},
            "chrome_processes": list(chrome_processes.keys()),
            "profiles": [p.name for p in PROFILES_DIR.iterdir()] if PROFILES_DIR.exists() else []
        })
    except asyncio.TimeoutError:
        return web.json_response({"error": "Timeout getting status"}, status=504)
    finally:
        pending.pop(req_id, None)

async def server_logs_handler(req):
    """Return server.py logs from server.log"""
    if not LOG_FILE.exists():
        return web.Response(text="No logs yet", content_type="text/plain")

    try:
        lines = int(req.query.get("lines", "1000"))
    except ValueError:
        lines = 1000

    try:
        content = LOG_FILE.read_text(errors="replace")
        log_lines = content.split("\n")
        # Return last N lines
        tail = log_lines[-lines:] if len(log_lines) > lines else log_lines
        return web.Response(text="\n".join(tail), content_type="text/plain")
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def cancel_workflow_handler(req):
    """Cancel a running workflow"""
    workflow_id = req.match_info.get('workflow_id')

    if not ws_conn:
        return web.json_response({"error": "Extension not connected"}, status=503)

    req_id = str(uuid.uuid4())
    pending[req_id] = asyncio.Future()
    await ws_conn.send(json.dumps({"id": req_id, "action": "cancelWorkflow", "workflowId": workflow_id}))

    try:
        result = await asyncio.wait_for(pending[req_id], timeout=10)
        return web.json_response(result)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Timeout"}, status=504)
    finally:
        pending.pop(req_id, None)

async def terminal_handler(req):
    """Execute a shell command on the server and return stdout/stderr."""
    try:
        data = await req.json()
        command = data.get("command")
        timeout_secs = data.get("timeout", 120)
        task_id = data.get("task_id")
        webhook_url = data.get("webhook_url")

        if not command:
            return web.json_response({"success": False, "error": "Missing 'command'"}, status=400)

        print(f"🖥️  Executing terminal command: {command[:200]}...")

        if task_id and webhook_url:
            # Async mode: return 202 immediately, callback when done
            asyncio.create_task(_execute_terminal_and_callback(command, timeout_secs, task_id, webhook_url))
            return web.json_response({"success": True, "task_id": task_id, "message": "Terminal command dispatched"}, status=202)

        # Sync mode: wait for result and return it
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_secs
            )
            exit_code = proc.returncode
            result = {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout.decode(errors="replace") if stdout else "",
                "stderr": stderr.decode(errors="replace") if stderr else "",
            }
            print(f"✅ Command finished with exit_code={exit_code}")
            return web.json_response(result)
        except asyncio.TimeoutError:
            proc.kill()
            return web.json_response({
                "success": False,
                "error": f"Command timed out after {timeout_secs}s",
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
            }, status=504)

    except Exception as e:
        print(f"❌ Terminal error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def _execute_terminal_and_callback(command: str, timeout_secs: int, task_id: str, webhook_url: str):
    """Execute a terminal command asynchronously and POST result to webhook."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_secs
        )
        exit_code = proc.returncode
        callback_data = {
            "task_status": "completed" if exit_code == 0 else "failed",
            "output_data": {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout.decode(errors="replace") if stdout else "",
                "stderr": stderr.decode(errors="replace") if stderr else "",
            },
        }
        print(f"✅ Async terminal command finished with exit_code={exit_code}")
    except asyncio.TimeoutError:
        proc.kill()
        callback_data = {
            "task_status": "failed",
            "output_data": {
                "success": False,
                "error": f"Command timed out after {timeout_secs}s",
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
            },
        }
    except Exception as e:
        callback_data = {
            "task_status": "failed",
            "output_data": {
                "success": False,
                "error": str(e),
            },
        }

    # POST result to webhook
    try:
        async with ClientSession() as session:
            print(f"📤 Sending terminal webhook for task {task_id} to: {webhook_url}")
            async with session.post(webhook_url, json=callback_data) as resp:
                body = await resp.text()
                print(f"📥 Terminal webhook response for task {task_id}: status={resp.status} body={body[:500]}")
    except Exception as e:
        print(f"❌ Terminal webhook callback failed for task {task_id}: {e}")


async def transcribe_handler(req):
    """Download audio from URL and transcribe with Whisper."""
    try:
        data = await req.json()
        audio_url = data.get("audio_url")

        if not audio_url:
            return web.json_response({"success": False, "error": "Missing audio_url"}, status=400)

        print(f"🎙️ Transcribing audio from: {audio_url[:80]}...")

        tmp_path = None
        try:
            async with ClientSession() as session:
                async with session.get(audio_url) as resp:
                    if resp.status != 200:
                        return web.json_response({
                            "success": False,
                            "error": f"Failed to download audio: HTTP {resp.status}"
                        }, status=502)
                    audio_data = await resp.read()

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            def _transcribe():
                model = get_whisper_model()
                segments, info = model.transcribe(
                    tmp_path,
                    language="en",
                    vad_filter=True,
                    beam_size=5,
                    temperature=0.0,
                )
                lines = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
                return " ".join(lines).strip()

            text = await asyncio.to_thread(_transcribe)

            print(f"✅ Transcription result: '{text}'")
            return web.json_response({"success": True, "text": text})

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def port_proxy_handler(req):
    """Proxy requests from /{port}/... to localhost:{port}/..."""
    port = req.match_info['port']
    try:
        port_int = int(port)
        if port_int < 1 or port_int > 65535:
            return web.json_response({"error": "Invalid port"}, status=400)
    except ValueError:
        return web.json_response({"error": "Invalid port"}, status=400)

    # Build the target URL: everything after /{port}
    tail = req.match_info.get('path', '')
    query_string = req.query_string
    target = f"http://localhost:{port_int}/{tail}"
    if query_string:
        target += f"?{query_string}"

    print(f"🔀 Proxying {req.method} /{port}/{tail} -> {target}")

    try:
        body = await req.read()
        async with ClientSession() as session:
            async with session.request(
                method=req.method,
                url=target,
                headers={k: v for k, v in req.headers.items()
                         if k.lower() not in ('host', 'transfer-encoding')},
                data=body if body else None,
                timeout=__import__('aiohttp').ClientTimeout(total=120),
            ) as resp:
                resp_body = await resp.read()
                return web.Response(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ('transfer-encoding', 'content-encoding')},
                    body=resp_body,
                )
    except Exception as e:
        print(f"❌ Proxy error for port {port_int}: {e}")
        return web.json_response(
            {"success": False, "error": f"Cannot reach localhost:{port_int} - {e}"},
            status=502,
        )

async def main():
    global tab_semaphore
    tab_semaphore = asyncio.Semaphore(MAX_TABS)

    await websockets.serve(ws_handler, "localhost", WS_PORT, max_size=20 * 1024 * 1024)
    print(f"🔌 ws://localhost:{WS_PORT}")
    
    app = web.Application()

    # Register routes under bare paths
    app.router.add_post('/workflow', workflow_handler)
    app.router.add_post('/transcribe', transcribe_handler)
    app.router.add_post('/terminal', terminal_handler)
    app.router.add_delete('/workflow/{workflow_id}', cancel_workflow_handler)
    app.router.add_route('*', '/profile/{profile_id}', profile_handler)
    app.router.add_route('*', '/chrome/{profile_id}', chrome_handler)
    app.router.add_get('/status', status_handler)
    app.router.add_get('/server-logs', server_logs_handler)

    # Port proxy: /{port} and /{port}/{path} -> localhost:{port}/{path}
    app.router.add_route('*', '/{port:\\d+}', port_proxy_handler)
    app.router.add_route('*', '/{port:\\d+}/{path:.*}', port_proxy_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, 'localhost', HTTP_PORT).start()
    
    print(f"🌐 http://localhost:{HTTP_PORT}")
    print(f"🔒 Tab limit: {MAX_TABS} concurrent workflows, Queue limit: {MAX_QUEUE} pending (set MAX_TABS / MAX_QUEUE env to change)")
    print("""
Endpoints:
  POST   /workflow              - Execute workflow (parallel supported)
  DELETE /workflow/{id}         - Cancel running workflow
  POST   /transcribe            - Transcribe audio URL with Whisper
  POST   /terminal              - Execute shell command and return output
  GET    /profile/{id}          - Download profile from Wasabi
  POST   /profile/{id}          - Upload profile to Wasabi  
  PUT    /profile/{id}          - Create empty profile
  DELETE /profile/{id}          - Delete local profile
  POST   /chrome/{id}           - Launch Chrome with profile
  DELETE /chrome/{id}           - Close Chrome
  GET    /status                - Get status + running workflows
  GET    /server-logs           - Get server.py logs
  *      /{port}/*              - Proxy to localhost:{port} (docker services)

Examples:
  curl -X PUT http://localhost:8766/profile/my-profile
  curl -X POST http://localhost:8766/chrome/my-profile
  curl -X POST http://localhost:8766/workflow -d @workflow.json
  curl http://localhost:8766/status  # check running workflows
  curl -X DELETE http://localhost:8766/workflow/{id}  # cancel workflow
""")
    
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
