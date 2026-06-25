import os
import subprocess
import time
import glob

import pytesseract
from Xlib import X, display as xdisplay
from PIL import Image

# ---- XWayland setup ----
if not os.environ.get("DISPLAY"):
    os.environ["DISPLAY"] = ":1"

if not os.environ.get("XAUTHORITY"):
    auth_files = glob.glob("/run/user/1000/.mutter-Xwaylandauth.*")
    if auth_files:
        os.environ["XAUTHORITY"] = auth_files[0]

subprocess.run(["xhost", "+local:"], capture_output=True)

CHROME_BIN = "/usr/bin/google-chrome"
CHROME_PROFILE = "Profile 3"
EXTENSION_PATH = "/home/sanket777/Desktop/Botxbyte/real-botxbyte-extension"


# ---- XWayland screenshot (captures Chrome X11 window directly) ----

def screenshot_chrome(wid):
    """Capture a screenshot of the Chrome XWayland window via python-xlib."""
    d = xdisplay.Display(os.environ["DISPLAY"])
    win = d.create_resource_object("window", int(wid))
    geom = win.get_geometry()
    raw = win.get_image(0, 0, geom.width, geom.height, X.ZPixmap, 0xFFFFFFFF)
    img = Image.frombytes("RGB", (geom.width, geom.height), raw.data, "raw", "BGRX")
    d.close()
    return img


# ---- xdotool wrappers ----

def xdo_key(wid, keys):
    """Send key combo to a specific X11 window."""
    subprocess.run(["xdotool", "key", "--window", str(wid), "--clearmodifiers", keys], check=True)


def xdo_type(wid, text):
    """Type text into a specific X11 window."""
    subprocess.run(["xdotool", "type", "--window", str(wid), "--clearmodifiers", "--delay", "30", text], check=True)


def xdo_click(x, y):
    """Move mouse and click at absolute screen coordinates."""
    subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], check=True)
    time.sleep(0.1)
    subprocess.run(["xdotool", "click", "1"], check=True)


# ---- OCR helpers ----

def find_text(target, img):
    """Find single word on screenshot. Returns (center_x, center_y) or None."""
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    target_lower = target.lower()
    for i, word in enumerate(data["text"]):
        if target_lower in word.lower():
            x = data["left"][i] + data["width"][i] // 2
            y = data["top"][i] + data["height"][i] // 2
            return (x, y)
    return None


def find_phrase(phrase, img):
    """Find multi-word phrase on screenshot. Returns center of bounding box."""
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = phrase.lower().split()
    entries = [(i, data["text"][i].lower()) for i in range(len(data["text"])) if data["text"][i].strip()]

    for idx in range(len(entries) - len(words) + 1):
        match = all(words[j] in entries[idx + j][1] for j in range(len(words)))
        if match:
            indices = [entries[idx + j][0] for j in range(len(words))]
            left = min(data["left"][i] for i in indices)
            top = min(data["top"][i] for i in indices)
            right = max(data["left"][i] + data["width"][i] for i in indices)
            bottom = max(data["top"][i] + data["height"][i] for i in indices)
            return ((left + right) // 2, (top + bottom) // 2)
    return None


def wait_and_find(target, chrome_wid, timeout=15, phrase=False):
    """Retry OCR until target text is found or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        img = screenshot_chrome(chrome_wid)
        pos = find_phrase(target, img) if phrase else find_text(target, img)
        if pos:
            print(f"  Found '{target}' at {pos}")
            return pos
        time.sleep(1)
    print(f"  WARNING: Could not find '{target}' on screen after {timeout}s")
    return None


def click_text(target, chrome_wid, timeout=15, phrase=False):
    """Find text via OCR and click it. Coordinates are relative to window, need offset."""
    pos = wait_and_find(target, chrome_wid, timeout, phrase)
    if pos:
        # Get window position on screen to convert to absolute coords
        result = subprocess.run(
            ["xdotool", "getwindowgeometry", str(chrome_wid)],
            capture_output=True, text=True,
        )
        # Parse "Position: X,Y (screen: 0)"
        for line in result.stdout.split("\n"):
            if "Position" in line:
                coords = line.split(":")[1].split("(")[0].strip()
                wx, wy = [int(c) for c in coords.split(",")]
                abs_x = wx + pos[0]
                abs_y = wy + pos[1]
                print(f"  Clicking at screen ({abs_x}, {abs_y})")
                xdo_click(abs_x, abs_y)
                return True
    return False


# ---- Chrome helpers ----

def get_chrome_wid():
    """Find the main Chrome X11 window (largest geometry)."""
    result = subprocess.run(
        ["xdotool", "search", "--name", "Chrome"],
        capture_output=True, text=True,
    )
    wids = [w.strip() for w in result.stdout.strip().split("\n") if w.strip()]

    best_wid = None
    best_area = 0
    for wid in wids:
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", wid],
            capture_output=True, text=True,
        )
        for line in geo.stdout.split("\n"):
            if "Geometry" in line:
                size = line.split(":")[1].strip()
                w, h = [int(x) for x in size.split("x")]
                if w * h > best_area:
                    best_area = w * h
                    best_wid = wid
    return best_wid


def focus_chrome(wid):
    """Activate the Chrome window."""
    subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)], capture_output=True)
    time.sleep(0.5)


def kill_chrome():
    subprocess.run(["pkill", "-f", "google-chrome"], capture_output=True)
    time.sleep(2)


def launch_chrome_x11():
    subprocess.Popen(
        [CHROME_BIN, "--ozone-platform=x11", "--start-maximized",
         f"--profile-directory={CHROME_PROFILE}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(5)


# ---- Main flow ----

def load_extension():
    # Step 0: Restart Chrome under XWayland
    print("[0] Closing existing Chrome...")
    kill_chrome()
    print("[0] Launching Chrome under XWayland...")
    launch_chrome_x11()

    # Find and focus Chrome window
    print("[0] Finding Chrome window...")
    wid = get_chrome_wid()
    if not wid:
        print("  ERROR: Chrome window not found! Aborting.")
        return
    print(f"  Chrome window ID: {wid}")
    focus_chrome(wid)

    # Step 1: Navigate to chrome://extensions
    print("[1] Navigating to chrome://extensions...")
    xdo_key(wid, "ctrl+l")
    time.sleep(0.5)
    xdo_type(wid, "chrome://extensions")
    time.sleep(0.3)
    xdo_key(wid, "Return")
    time.sleep(3)

    # Step 2: Enable Developer Mode (find via OCR)
    print("[2] Looking for Developer mode toggle...")
    pos = wait_and_find("Developer", wid)
    if pos:
        # Toggle is to the right of the "Developer mode" text
        result = subprocess.run(
            ["xdotool", "getwindowgeometry", str(wid)],
            capture_output=True, text=True,
        )
        for line in result.stdout.split("\n"):
            if "Position" in line:
                coords = line.split(":")[1].split("(")[0].strip()
                wx, wy = [int(c) for c in coords.split(",")]
                xdo_click(wx + pos[0] + 150, wy + pos[1])
        time.sleep(1)

    # Step 3: Click "Load unpacked" (find via OCR)
    print("[3] Looking for 'Load unpacked' button...")
    click_text("Load unpacked", wid, phrase=True)
    time.sleep(2)

    # Step 4: In the file picker, type the path
    print("[4] Typing extension path in file dialog...")
    xdo_key(wid, "ctrl+l")
    time.sleep(0.5)
    xdo_type(wid, EXTENSION_PATH)
    time.sleep(0.5)
    xdo_key(wid, "Return")
    time.sleep(1.5)

    # Step 5: Click "Open" button (find via OCR)
    print("[5] Looking for 'Open' button...")
    if not click_text("Open", wid):
        xdo_key(wid, "Return")
    time.sleep(2)

    print("\nExtension loaded successfully!")


if __name__ == "__main__":
    print("Starting extension loader in 3 seconds...")
    print("Press Ctrl+C to abort.")
    time.sleep(3)
    load_extension()
