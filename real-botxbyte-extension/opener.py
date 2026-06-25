import pyautogui
import time
import subprocess

# Adds a 0.5-second pause after every PyAutoGUI call. 
pyautogui.PAUSE = 0.5

def load_unpacked_extension():
    print("Finding the existing Chrome window and bringing it to the front...")
    
    # Use wmctrl to focus the already open Chrome window
    try:
        subprocess.run(['wmctrl', '-a', 'Chrome'], check=True)
    except FileNotFoundError:
        print("Error: wmctrl is not installed. Run 'sudo apt install wmctrl'")
        return
    except subprocess.CalledProcessError:
        print("Error: Could not find an open Chrome window. Please open Chrome first!")
        return

    # Give the window 1 second to fully come to the foreground
    time.sleep(1)
    
    print("Chrome focused! Starting key sequence...")

    # 1. Open a new tab in the existing window (Ctrl+T) and focus address bar (Ctrl+L)
    pyautogui.hotkey('ctrl', 't')
    pyautogui.hotkey('ctrl', 'l')

    # 2. Type the extensions URL and press Enter
    pyautogui.write('chrome://extensions')
    pyautogui.press('enter')

    # Wait an extra 2 seconds for the extensions page to fully render
    time.sleep(2)

    # 3. Focus the address bar again (Alt+D), hit Esc to drop focus into the page body
    pyautogui.hotkey('alt', 'd')
    pyautogui.press('esc')

    # 4. Press Tab 3 times to navigate to the Developer Mode toggle
    pyautogui.press('tab', presses=3, interval=0.2)

    # 5. Press Space to toggle Developer Mode ON
    pyautogui.press('space')

    # Wait a moment for the new menu buttons to appear
    time.sleep(0.5)

    # 6. Press Tab one more time to highlight "Load unpacked"
    pyautogui.press('tab')

    # 7. Press Space to click it and open the file dialog
    pyautogui.press('space')
    
    # --- FILE DIALOG AUTOMATION ---
    print("Waiting for the file dialog to open...")
    time.sleep(2) # Give the Ubuntu file chooser time to appear

    # 8. Click the 'Downloads' folder
    pyautogui.click(x=444, y=302)
    print("Clicked Downloads!")

    # Wait for the contents of the Downloads folder to render on screen
    time.sleep(1)

    # 9. Click the specific extension folder
    pyautogui.click(x=776, y=208)
    print("Clicked the extension folder!")

    # Wait for the selection to register
    time.sleep(1)

    # 10. Click the 'Select Folder' or 'Open' button to finish
    pyautogui.click(x=1441, y=928)
    print("Clicked Open! Extension should now be loaded.")

if __name__ == "__main__":
    load_unpacked_extension()


#python3 -c "import pyautogui, time; print('Hover over Downloads in 3 seconds...'); time.sleep(3); print(pyautogui.position())"  --> this command use to know the coordinates of the mouse pointer on the screen. You can hover over any element and get its coordinates printed in the terminal.