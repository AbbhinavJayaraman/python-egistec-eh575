import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import egis_driver
import fingerprint_matcher 
import time
import threading
import egis_config # Loads your ENROLL_STAGES and MATCH_THRESHOLD

# --- Configuration ---
DEVICE_IFACE = 'io.github.uunicorn.Fprint.Device'
MANAGER_IFACE = 'net.reactivated.Fprint.Manager'
MANAGER_OBJ = '/net/reactivated/Fprint/Manager'
MANAGER_BUS = 'net.reactivated.Fprint'

# Keeping storage local to avoid permission hell in /var/lib
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
        self.scan_thread = None
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

    # --- Thread Safety (Ported from Service) ---
    def _stop_scan(self):
        """Stops any running scan and waits for the thread to die."""
        if self.scanning:
            print("[BRIDGE] Stopping active scan...")
            self.scanning = False
        
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=2.0)
            if self.scan_thread.is_alive():
                print("[BRIDGE] WARNING: Thread did not exit cleanly!")
            else:
                print("[BRIDGE] Thread stopped.")

    def _start_scan(self, target_func, args):
        """Safely kills old thread before starting new one."""
        self._stop_scan()
        self.scanning = True
        self.scan_thread = threading.Thread(target=target_func, args=args)
        self.scan_thread.start()

    # --- DBus Methods ---

    @dbus.service.method(DEVICE_IFACE, in_signature='ss', out_signature='')
    def VerifyStart(self, username, finger_name):
        print(f"[BRIDGE] Verify Requested for user: {username}")
        target_finger = finger_name if finger_name else "right-index-finger"
        self._start_scan(self._scan_loop, ("verify", username, target_finger))

    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def VerifyStop(self):
        print("[BRIDGE] Verify Stopped")
        self._stop_scan()

    @dbus.service.method(DEVICE_IFACE, in_signature='ss', out_signature='')
    def EnrollStart(self, username, finger_name):
        print(f"[BRIDGE] Enroll Requested for user: {username}")
        self.enroll_scans = [] 
        target_finger = finger_name if finger_name else "right-index-finger"
        self._start_scan(self._scan_loop, ("enroll", username, target_finger))

    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def EnrollStop(self):
        print("[BRIDGE] Enroll Stopped")
        self._stop_scan()
        
    @dbus.service.method(DEVICE_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        print("[BRIDGE] Cancel Requested")
        self._stop_scan()

    @dbus.service.method(DEVICE_IFACE, in_signature='s', out_signature='as')
    def ListEnrolledFingers(self, username):
        fingers = self.matcher.get_enrolled_fingers(username)
        print(f"[BRIDGE] Listing fingers for {username}: {fingers}")
        return fingers

    @dbus.service.method(DEVICE_IFACE, in_signature='s', out_signature='')
    def DeleteEnrolledFingers(self, username):
        print(f"[BRIDGE] Deleting prints for {username}")
        self.matcher.delete_user_fingers(username)

    # --- Logic Core (The "Robust" Version) ---
    
    def _wait_for_finger_release(self):
        """Blocks until sensor is CLEAR for 2 consecutive frames."""
        print("[BRIDGE] Waiting for finger release...")
        time.sleep(0.3) # Initial debounce
        
        consecutive_clears = 0
        while self.scanning:
            if self.driver.check_sensor_clear():
                consecutive_clears += 1
                if consecutive_clears >= 2:
                    print("[BRIDGE] Sensor clear. Ready.")
                    return
            else:
                consecutive_clears = 0
            time.sleep(0.1)

    def _scan_loop(self, mode, username, finger_name):
        print(f"[BRIDGE] Starting {mode} loop for {username} ({finger_name})...")
        
        # Initial lift check before we even start capturing
        self._wait_for_finger_release()

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
                
                # Force lift before next scan
                if self.scanning:
                    self._wait_for_finger_release()
            
            time.sleep(0.05)

    def _handle_enroll(self, img, username, finger_name):
        self.enroll_scans.append(img)
        count = len(self.enroll_scans)
        
        # Uses your new config file
        try: target = egis_config.ENROLL_STAGES
        except: target = 15

        print(f"[BRIDGE] Enroll Progress: {count}/{target}")

        if count < target:
            self.EnrollStatus("enroll-stage-passed", False)
        else:
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
        match_name, score = self.matcher.verify_finger(img)
        
        # Uses your new config file
        try: min_score = egis_config.MATCH_THRESHOLD
        except: min_score = 15

        if match_name and score >= min_score:
            print(f"[BRIDGE] Best Match: {match_name} (Score: {score})")
            if match_name.startswith(username + "_"):
                print("[BRIDGE] AUTHENTICATED!")
                self.VerifyStatus("verify-match", True)
                self.scanning = False
            else:
                print(f"[BRIDGE] Wrong user! ({match_name})")
                self.VerifyStatus("verify-no-match", False)
        else:
            print(f"[BRIDGE] Rejected. (Score: {score}/{min_score})")
            self.VerifyStatus("verify-no-match", False)

    # --- Signals ---
    @dbus.service.signal(DEVICE_IFACE, signature='sb')
    def VerifyStatus(self, result, done):
        pass

    @dbus.service.signal(DEVICE_IFACE, signature='s')
    def VerifyFingerSelected(self, finger):
        pass 

    @dbus.service.signal(DEVICE_IFACE, signature='sb')
    def EnrollStatus(self, result, done):
        pass

if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    # We must claim a bus name so open-fprintd knows who we are
    # Note: We must own the name defined in the PolicyKit file
    name = dbus.service.BusName("io.github.uunicorn.Fprint.Device.Egis", bus)
    
    device = EgisBridge(bus)
    loop = GLib.MainLoop()
    loop.run()