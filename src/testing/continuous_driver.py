import usb.core
import usb.util
import time
import numpy as np
from PIL import Image
import os
import sys

# --- Config ---
VENDOR_ID = 0x1c7a
PRODUCT_ID = 0x0575
ENDPOINT_OUT = 0x01
ENDPOINT_IN = 0x82
IMG_WIDTH = 103
IMG_HEIGHT = 50 
IMG_SIZE = 5147 

def send_hex(dev, hex_str, read_resp=True, tag="CMD"):
    cmd = bytes.fromhex(hex_str)
    try:
        dev.write(ENDPOINT_OUT, cmd)
        if read_resp:
            # Always read 64 bytes to clear the buffer of status responses
            return dev.read(ENDPOINT_IN, 64, timeout=1000)
    except usb.core.USBError as e:
        # print(f"[{tag}] USB Error: {e}")
        pass
    return None

def initialize_sensor(dev):
    print("\n[INIT] Starting Initialization Sequence...")
    
    # 1. FACTORY RESET & PATCHING
    # CRITICAL FIX: read_resp=True ensures we don't leave junk in the USB buffer
    patches = [
        "45 47 49 53 60 00 06", "45 47 49 53 60 01 06", "45 47 49 53 60 40 06",
        "45 47 49 53 61 0a f4", "45 47 49 53 61 0c 44", "45 47 49 53 61 40 00",
        "45 47 49 53 60 40 00", "45 47 49 53 71 02 02 01 0c", "45 47 49 53 61 0c 22",
        "45 47 49 53 61 0b 03", "45 47 49 53 61 0a fc"
    ]
    for p in patches: send_hex(dev, p, read_resp=True)

    # 2. UNLOCK (Key FC)
    send_hex(dev, "45 47 49 53 60 00 fc", tag="RESET_FC")
    send_hex(dev, "45 47 49 53 60 01 fc", tag="CHAL_FC")
    send_hex(dev, "45 47 49 53 60 41 fc", tag="UNLOCK_FC")

    # 3. MASSIVE INITIALIZATION BLOB (Packets 59-224)
    init_cmds = [
        "45 47 49 53 97 00 00",
        # Sending multiple status checks to flush the state
        "45 47 49 53 60 00 00", "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
        "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
        "45 47 49 53 60 01 00", "45 47 49 53 61 0a fd", "45 47 49 53 61 35 02",
        "45 47 49 53 61 80 00", "45 47 49 53 60 80 00", "45 47 49 53 61 0a fc",
        "45 47 49 53 63 01 02 0f 03", "45 47 49 53 61 0c 22", "45 47 49 53 61 09 83",
        "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 0a f4",
        "45 47 49 53 61 0c 44", "45 47 49 53 61 50 03", "45 47 49 53 60 50 03",
    ]
    for cmd in init_cmds:
        send_hex(dev, cmd, read_resp=True)
        time.sleep(0.002)

    # 4. FINAL SETUP
    final_cmds = [
        "45 47 49 53 60 40 ec", "45 47 49 53 61 0c 22", "45 47 49 53 61 0b 03",
        "45 47 49 53 61 0a fc", "45 47 49 53 60 40 fc",
        "45 47 49 53 63 09 0b 83 24 00 44 0f 08 20 20 01 05 12",
        "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 23 00",
        "45 47 49 53 61 24 33", "45 47 49 53 61 20 00", "45 47 49 53 61 21 66",
        "45 47 49 53 60 00 66", "45 47 49 53 60 01 66",
    ]
    for cmd in final_cmds:
        send_hex(dev, cmd, read_resp=True)
    
    print("[INIT] Sequence Complete. Sensor Ready.")

def rearm_sensor(dev):
    """
    OPTIMIZED: Removed redundant unlock, added missing calibration packet.
    Matches 'betweenpackets.txt' exactly.
    """
    
    # 1. Maintenance & Reset (Transition to Mode 20)
    send_hex(dev, "45 47 49 53 61 2d 20", tag="PREP_RESET")
    send_hex(dev, "45 47 49 53 60 00 20", tag="RESET_HW")
    
    # 2. Check Mode 20 (Log Frame 45033)
    # The log uses '60 01 20' here, NOT '60 01 fc'. 
    # This confirms the reset worked without needing a full re-unlock.
    send_hex(dev, "45 47 49 53 60 01 20", tag="CHECK_20")
    
    # 3. Reload Config (Log Frame 45037)
    send_hex(dev, "45 47 49 53 63 2c 02 00 57", tag="REG_2C")
    send_hex(dev, "45 47 49 53 60 2d 02", tag="CMD_2D")

    # 4. CALIBRATION CHECK (Log Frame 45045)
    # Your script was missing this! It prevents drift over time.
    send_hex(dev, "45 47 49 53 62 67 03", tag="CALIB_CHECK")
    
    # 5. Wake for Capture (Log Frame 45049)
    send_hex(dev, "45 47 49 53 63 2c 02 00 13", tag="REG_2C_13")
    send_hex(dev, "45 47 49 53 60 00 02", tag="STATUS")

def main():
    print("--- Egis 0575 Continuous Driver (Optimized) ---")
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if not dev: return print("Device not found.")

    if dev.is_kernel_driver_active(0):
        try: dev.detach_kernel_driver(0)
        except: pass

    dev.set_configuration()
    
    # 1. BOOT DELAY
    print("[WAIT] Waiting 6.0s for firmware boot...")
    time.sleep(6.0)

    # 2. RUN FULL INIT
    initialize_sensor(dev)

    if not os.path.exists("continuous_scans"):
        os.makedirs("continuous_scans")

    # 3. CAPTURE LOOP
    print("\n[LOOP] Starting Capture Loop (Press Ctrl+C to stop)...")
    
    try:
        frame_idx = 1
        while True:
            # A. Re-Arm Sensor
            rearm_sensor(dev)
            
            # B. Trigger
            dev.write(ENDPOINT_OUT, bytes.fromhex("45 47 49 53 64 14 ec"))
            
            try:
                # C. Read Image
                raw_data = dev.read(ENDPOINT_IN, 10000, timeout=1500)
                
                # D. Clear Pipe (Histogram)
                # It is safe to use a short timeout here to "drain" the metadata
                try: dev.read(ENDPOINT_IN, 512, timeout=50)
                except: pass
                
                if len(raw_data) > 5000:
                    # Pad/Trim to exact size
                    target = IMG_WIDTH * IMG_HEIGHT
                    if len(raw_data) < target: 
                        raw_data += bytes(target - len(raw_data))
                    else: 
                        raw_data = raw_data[:target]
                    
                    img_arr = np.array(list(raw_data), dtype=np.uint8).reshape((IMG_HEIGHT, IMG_WIDTH))
                    contrast = np.std(img_arr)
                    
                    msg = f"Frame {frame_idx}: {len(raw_data)} bytes | Contrast: {contrast:.2f}"
                    
                    # Only save if there is something interesting (Contrast > 5)
                    if contrast > 5.0:
                        msg += " [FINGER DETECTED!]"
                        filename = f"continuous_scans/finger_{frame_idx}.png"
                        Image.fromarray(img_arr, 'L').save(filename)
                    
                    print(msg)
                else:
                    print(f"Frame {frame_idx}: Data too short ({len(raw_data)} bytes)")

            except Exception as e:
                print(f"Frame {frame_idx}: Capture Error: {e}")
            
            frame_idx += 1
            # OPTIMIZATION: Removed time.sleep(0.05). 
            # The USB Read is blocking, so it naturally limits the loop speed to the hardware frame rate.

    except KeyboardInterrupt:
        print("\n[STOP] Capture stopped by user.")

if __name__ == "__main__":
    main()