import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import egis_driver
import fingerprint_matcher  # <--- Now importing your matcher
import time
import threading

# --- Configuration ---
DEVICE_IFACE = 'io.github.uunicorn.Fprint.Device'
MANAGER_IFACE = 'net.reactivated.Fprint.Manager'
MANAGER_OBJ = '/net/reactivated/Fprint/Manager'
MANAGER_BUS = 'net.reactivated.Fprint'

# We store prints locally in the driver folder to avoid permission headaches
ENROLL_DIR = "./enrolled_prints"

class EgisBridge(dbus.service.Object):
    def __init__(self, bus):
        self.bus = bus
        self.path = "/org/reactivated/Fprint/Device/Egis"
        dbus.service.Object.__init__(self, bus, self.path)
        
        print("[BRIDGE] Initializing Driver...")
        self.driver = egis_driver.EgisDriver()
        
        print(f"[BRIDGE] Initializing Matcher (Storage: {ENROLL_DIR})...")
        self.matcher = fingerprint_matcher.FingerprintMatcher(enroll_dir=ENROLL_DIR)
        
        self.scanning = False
        self.enroll_scans = []  
        
        self._register_with_manager()

    def _register_with_manager(self):
        try:
            manager_proxy = self.bus.get_object(MANAGER_BUS, MANAGER_OBJ)
            manager = dbus.Interface(manager_proxy, MANAGER_IFACE)
            manager.RegisterDevice(self.path)
            print("[BRIDGE] Successfully registered with open-fprintd!")
        except Exception as e:
            print(f"[BRIDGE] Failed to register: {e}")

    # --- DBus Methods ---

    @dbus.service.method(DEVICE_IFACE, in_signature='ss', out_signature='')
    def VerifyStart(self, username, finger_name):
        print(f"[BRIDGE] Verify Requested for user: {username}")
        self.scanning = True
        # If finger_name is empty/any, default to right-index
        target_finger = finger_name if finger_name else "right-index-finger"
        threading.Thread(target=self._scan_loop, args=("verify", username, target_finger)).start()

    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def VerifyStop(self):
        print("[BRIDGE] Verify Stopped")
        self.scanning = False

    @dbus.service.method(DEVICE_IFACE, in_signature='ss', out_signature='')
    def EnrollStart(self, username, finger_name):
        print(f"[BRIDGE] Enroll Requested for user: {username}")
        self.enroll_scans = [] 
        self.scanning = True
        # If finger_name is empty, default to right-index
        target_finger = finger_name if finger_name else "right-index-finger"
        threading.Thread(target=self._scan_loop, args=("enroll", username, target_finger)).start()

    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def EnrollStop(self):
        print("[BRIDGE] Enroll Stopped")
        self.scanning = False
        
    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        print("[BRIDGE] Cancel Requested")
        self.scanning = False

    @dbus.service.method(DEVICE_IFACE, in_signature='s', out_signature='as')
    def ListEnrolledFingers(self, username):
        fingers = self.matcher.get_enrolled_fingers(username)
        print(f"[BRIDGE] Listing fingers for {username}: {fingers}")
        return fingers

    @dbus.service.method(DEVICE_IFACE, in_signature='s', out_signature='')
    def DeleteEnrolledFingers(self, username):
        print(f"[BRIDGE] Deleting prints for {username}")
        self.matcher.delete_user_fingers(username)

    # --- Logic Core ---
    
    def _wait_for_finger_release(self):
        """Blocks until the sensor is clear to prevent double-scanning."""
        # Simple debounce
        time.sleep(0.5) 
        print("[BRIDGE] Waiting for finger release...")
        while self.scanning:
            if self.driver.check_sensor_clear():
                print("[BRIDGE] Sensor clear.")
                return
            time.sleep(0.1)

    def _scan_loop(self, mode, username, finger_name):
        print(f"[BRIDGE] Starting {mode} loop for {username} ({finger_name})...")
        
        while self.scanning:
            if not self.driver.check_sensor_clear():
                print("[BRIDGE] Finger detected! Capturing...")
                
                img, contrast = self.driver.get_live_frame()
                if img is None: continue 

                print(f"[BRIDGE] Captured frame. Contrast: {contrast:.2f}")

                if mode == "enroll":
                    self._handle_enroll(img, username, finger_name)
                elif mode == "verify":
                    self._handle_verify(img, username)
                
                self._wait_for_finger_release()
            
            time.sleep(0.05)

    def _handle_enroll(self, img, username, finger_name):
        self.enroll_scans.append(img)
        count = len(self.enroll_scans)
        print(f"[BRIDGE] Enroll Progress: {count}/5")

        if count < 5:
            self.EnrollStatus("enroll-stage-passed", False)
        else:
            # Construct the unique ID for the matcher (e.g. jayabbhi_right-index-finger)
            unique_name = f"{username}_{finger_name}"
            
            print(f"[BRIDGE] Processing enrollment for {unique_name}...")
            success = self.matcher.enroll_finger(unique_name, self.enroll_scans)
            
            if success:
                print("[BRIDGE] Enrollment Successful!")
                self.EnrollStatus("enroll-completed", True)
            else:
                print("[BRIDGE] Enrollment Failed (Low quality?)")
                self.EnrollStatus("enroll-failed", True)
                
            self.scanning = False

    def _handle_verify(self, img, username):
        # 1. Ask matcher to find the best match in the database
        match_name, score = self.matcher.verify_finger(img)
        
        if match_name:
            print(f"[BRIDGE] Best Match: {match_name} (Score: {score})")
            
            # 2. Check if the matched finger belongs to the requested user
            # Matcher returns "username_fingername"
            if match_name.startswith(username + "_"):
                print("[BRIDGE] AUTHENTICATED!")
                self.VerifyStatus("verify-match", True)
                self.scanning = False
            else:
                print(f"[BRIDGE] Match found, but belongs to wrong user ({match_name})")
                self.VerifyStatus("verify-no-match", False)
        else:
            print("[BRIDGE] No match found.")
            self.VerifyStatus("verify-no-match", False)

    # --- Signals ---
    @dbus.service.signal(DEVICE_IFACE, signature='sb')
    def VerifyStatus(self, result, done):
        pass

    @dbus.service.signal(DEVICE_IFACE, signature='sb')
    def EnrollStatus(self, result, done):
        pass

if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName("io.github.uunicorn.Fprint.Device.Egis", bus)
    device = EgisBridge(bus)
    loop = GLib.MainLoop()
    loop.run()
