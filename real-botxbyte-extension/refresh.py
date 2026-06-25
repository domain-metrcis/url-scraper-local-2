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

    
    pyautogui.click(x=399, y=504)
    print("Clicked Reload! Extension should now be reloaded.")

if __name__ == "__main__":
    load_unpacked_extension()



#python3 -c "import pyautogui, time; print('Hover over Downloads in 3 seconds...'); time.sleep(3); print(pyautogui.position())"  --> this command use to know the coordinates of the mouse pointer on the screen. You can hover over any element and get its coordinates printed in the terminal.