import usb.core
import usb.util
import time
import numpy as np
import os
import sys
from PIL import Image

# --- Hardware Constants ---
VENDOR_ID = 0x1c7a
PRODUCT_ID = 0x0575
ENDPOINT_OUT = 0x01
ENDPOINT_IN = 0x82
IMG_WIDTH = 103
IMG_HEIGHT = 50 

class EgisDriver:
    def __init__(self):
        self.dev = self._find_device()
        print("[DRIVER] Device found. Waiting 1.0s for stability...")
        time.sleep(1.0) 
        
        self._initialize_sensor()
        
        # Calibration defaults
        self.noise_floor = 13.0
        self.touch_threshold = 31.0
        self._calibrate_baseline()
    
    def _find_device(self):
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if not dev:
            raise ValueError("Egis Sensor not found!")
        
        if dev.is_kernel_driver_active(0):
            try: dev.detach_kernel_driver(0)
            except: pass
        
        dev.set_configuration()
        return dev

    def _send_hex(self, hex_str, read_resp=True):
        cmd = bytes.fromhex(hex_str)
        try:
            self.dev.write(ENDPOINT_OUT, cmd)
            if read_resp:
                return self.dev.read(ENDPOINT_IN, 64, timeout=1000)
        except usb.core.USBError:
            pass
        return None

    def _initialize_sensor(self):
        print("[DRIVER] Initializing Hardware Sequence...")
        # 1. Factory Reset & Patching
        patches = [
            "45 47 49 53 60 00 06", "45 47 49 53 60 01 06", "45 47 49 53 60 40 06",
            "45 47 49 53 61 0a f4", "45 47 49 53 61 0c 44", "45 47 49 53 61 40 00",
            "45 47 49 53 60 40 00", "45 47 49 53 71 02 02 01 0c", "45 47 49 53 61 0c 22",
            "45 47 49 53 61 0b 03", "45 47 49 53 61 0a fc"
        ]
        for p in patches: self._send_hex(p)
        
        # 2. Unlock
        self._send_hex("45 47 49 53 60 00 fc")
        self._send_hex("45 47 49 53 60 01 fc")
        self._send_hex("45 47 49 53 60 41 fc")

        # 3. Init Blob
        init_cmds = [
            "45 47 49 53 97 00 00",
            "45 47 49 53 60 00 00", "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
            "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
            "45 47 49 53 60 01 00", "45 47 49 53 61 0a fd", "45 47 49 53 61 35 02",
            "45 47 49 53 61 80 00", "45 47 49 53 60 80 00", "45 47 49 53 61 0a fc",
            "45 47 49 53 63 01 02 0f 03", "45 47 49 53 61 0c 22", "45 47 49 53 61 09 83",
            "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 0a f4",
            "45 47 49 53 61 0c 44", "45 47 49 53 61 50 03", "45 47 49 53 60 50 03",
        ]
        for c in init_cmds: 
            self._send_hex(c)
            time.sleep(0.002)
        
        # 4. Final Setup
        final_cmds = [
            "45 47 49 53 60 40 ec", "45 47 49 53 61 0c 22", "45 47 49 53 61 0b 03",
            "45 47 49 53 61 0a fc", "45 47 49 53 60 40 fc",
            "45 47 49 53 63 09 0b 83 24 00 44 0f 08 20 20 01 05 12",
            "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 23 00",
            "45 47 49 53 61 24 33", "45 47 49 53 61 20 00", "45 47 49 53 61 21 66",
            "45 47 49 53 60 00 66", "45 47 49 53 60 01 66",
        ]
        for c in final_cmds: self._send_hex(c)
        print("[DRIVER] Hardware Ready.")

    def _rearm(self):
        # The critical sequence to reset sensor state between frames
        self._send_hex("45 47 49 53 61 2d 20")
        self._send_hex("45 47 49 53 60 00 20")
        self._send_hex("45 47 49 53 60 01 20")
        self._send_hex("45 47 49 53 63 2c 02 00 57")
        self._send_hex("45 47 49 53 60 2d 02")
        self._send_hex("45 47 49 53 62 67 03")
        self._send_hex("45 47 49 53 63 2c 02 00 13")
        self._send_hex("45 47 49 53 60 00 02")

    def get_frame_stats(self):
        """
        Rearms, captures a single frame, and returns (contrast, raw_data).
        Contrast < 2.0 usually means empty. Contrast > 5.0 usually means finger.
        """
        self._rearm()
        
        # Trigger Capture
        self.dev.write(ENDPOINT_OUT, bytes.fromhex("45 47 49 53 64 14 ec"))
        
        try:
            # Read Image Data
            data = self.dev.read(ENDPOINT_IN, 10000, timeout=1500)
            
            # Drain Histogram/Metadata (important to keep pipe clean)
            try: self.dev.read(ENDPOINT_IN, 512, timeout=20)
            except: pass

            if len(data) > 5000:
                # Pad/Trim
                target = IMG_WIDTH * IMG_HEIGHT
                if len(data) < target: 
                    data += bytes(target - len(data))
                else: 
                    data = data[:target]
                
                arr = np.array(list(data), dtype=np.uint8)
                contrast = np.std(arr)
                return contrast, data, arr
            
        except usb.core.USBError:
            pass
            
        return 0.0, None, None

    def _calibrate_baseline(self):
        print("[DRIVER] Calibrating noise floor...")
        readings = []
        for _ in range(5):
            c, _, _ = self.get_frame_stats()
            if c > 0: readings.append(c)
        
        if readings:
            self.noise_floor = sum(readings) / len(readings)
            self.touch_threshold = self.noise_floor + 12.0
            print(f"[DRIVER] Baseline: {self.noise_floor:.2f} | Threshold: {self.touch_threshold:.2f}")

def main():
    try:
        # Initialize
        driver = EgisDriver()
        
        target_scans = 10
        current_scan = 0
        
        # Create output dir
        if not os.path.exists("test_scans"):
            os.makedirs("test_scans")
            
        print(f"\n--- Starting Interactive Test ({target_scans} scans) ---")

        while current_scan < target_scans:
            print(f"\n[SCAN {current_scan + 1}/{target_scans}]")
            
            # 1. ENSURE CLEAR
            # We loop until contrast drops below threshold
            print(">> Checking sensor... (Please LIFT finger)")
            while True:
                contrast, _, _ = driver.get_frame_stats()
                # print(f"   Debug: Clean Check Contrast: {contrast:.2f} (Needs < {driver.touch_threshold:.2f})")
                
                if contrast < driver.touch_threshold:
                    # Sensor is clear
                    break
                # Optional: slight delay to not hammer USB too hard
                # time.sleep(0.1) 

            print(">> Sensor Clear. READY.")
            print(">> Please TOUCH the sensor now...")

            # 2. WAIT FOR TOUCH
            # Loop until contrast spikes above threshold
            while True:
                contrast, data, arr = driver.get_frame_stats()
                
                if contrast > driver.touch_threshold:
                    print(f">> Touch Detected! (Contrast: {contrast:.2f})")
                    
                    # SAVE IMAGE
                    filename = f"test_scans/scan_{current_scan + 1}.png"
                    img = Image.fromarray(arr.reshape((IMG_HEIGHT, IMG_WIDTH)), 'L')
                    img.save(filename)
                    print(f"   Saved to {filename}")
                    
                    current_scan += 1
                    break

            # 3. ENFORCE LIFT
            # Don't proceed to next scan until finger is removed
            print(">> Please LIFT your finger to continue.")
            while True:
                contrast, _, _ = driver.get_frame_stats()
                if contrast < driver.touch_threshold:
                    print(">> Finger Lifted.")
                    break

    except KeyboardInterrupt:
        print("\nAborted.")
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    main()
