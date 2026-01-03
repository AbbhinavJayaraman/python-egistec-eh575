#!/usr/bin/env python3
import time
import sys
import threading
import os
from gi.repository import GLib
from pydbus import SystemBus
from pydbus.generic import signal

# --- MODULES ---
# Ensure we can import from the current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from egis_driver import EgisDriver
from fingerprint_matcher import FingerprintMatcher 

# --- DBUS XML DEFINITION ---
# We use .strip() to ensure NO whitespace exists before <node>
XML_INTERFACE = """<node>
  <interface name="io.github.uunicorn.Fprint.Device">
    <method name="Claim">
      <arg direction="in" type="s" name="username"/>
    </method>
    <method name="Release">
    </method>
    <method name="VerifyStart">
      <arg direction="in" type="s" name="username"/>
      <arg direction="in" type="s" name="finger_name"/>
    </method>
    <method name="EnrollStart">
      <arg direction="in" type="s" name="username"/>
      <arg direction="in" type="s" name="finger_name"/>
    </method>
    <method name="Cancel">
    </method>
    <method name="ListEnrolledFingers">
      <arg direction="in" type="s" name="username"/>
      <arg direction="out" type="as" name="fingers"/>
    </method>
    <method name="DeleteEnrolledFingers">
      <arg direction="in" type="s" name="username"/>
    </method>
    
    <signal name="VerifyStatus">
      <arg type="s" name="result"/>
      <arg type="b" name="done"/>
    </signal>
    <signal name="VerifyFingerSelected">
      <arg type="s" name="finger"/>
    </signal>
    <signal name="EnrollStatus">
      <arg type="s" name="result"/>
      <arg type="b" name="done"/>
    </signal>
  </interface>
</node>
"""

class EgisService:

    # Force removal of newlines at start/end
    dbus = XML_INTERFACE.strip()

    # --- ADD THESE LINES ---
    VerifyStatus = signal()
    VerifyFingerSelected = signal()
    EnrollStatus = signal()

    def __init__(self):
        print("[Service] Initializing Hardware...")
        # Verify XML is clean
        if not self.dbus.startswith("<node>"):
            print(f"[CRITICAL] XML format error! Starts with: {repr(self.dbus[:10])}")
            sys.exit(1)

        try:
            self.driver = EgisDriver()
        except Exception as e:
            print(f"[Error] Failed to initialize driver: {e}")
            sys.exit(1)
            
        storage_path = "/var/lib/open-fprintd/egis"
        self.matcher = FingerprintMatcher(enroll_dir=storage_path)
        
        self.scan_thread = None
        self.cancel_scan = False
        self.claimed_user = None

    def Claim(self, username):
        print(f"[Service] Claimed by {username}")
        self.claimed_user = username

    def Release(self):
        print("[Service] Released")
        self._stop_scan_thread()
        self.claimed_user = None

    def ListEnrolledFingers(self, username):
        print(f"[Service] Listing fingers for {username}")
        fingers = self.matcher.get_enrolled_fingers(username)
        print(f" -> Found: {fingers}")
        return fingers

    def DeleteEnrolledFingers(self, username):
        print(f"[Service] Deleting fingers for {username}")
        self.matcher.delete_user_fingers(username)

    def VerifyStart(self, username, finger_name):
        print(f"[Service] Verify requested for {username}")
        self._start_thread(self._verify_loop, (username, finger_name))

    def EnrollStart(self, username, finger_name):
        print(f"[Service] Enroll requested for {username} (finger: {finger_name})")
        self._start_thread(self._enroll_loop, (username, finger_name))

    def Cancel(self):
        print("[Service] Cancel requested")
        self._stop_scan_thread()

    def _verify_loop(self, username, finger_name):
        print("[Verify] Starting loop...")
        while not self.cancel_scan:
            frame = self.driver.capture_frame(timeout_sec=0.5)
            if frame:
                match_name, score = self.matcher.verify_finger(frame)
                expected_prefix = f"{username}_"
                
                if match_name and match_name.startswith(expected_prefix) and score > 15:
                    print(f"MATCH! Finger: {match_name}, Score: {score}")
                    self.VerifyStatus("verify-match", True)
                    return
                elif match_name:
                     print(f"Mismatch: Saw {match_name} but wanted {username}")
                     self.VerifyStatus("verify-no-match", False)
                else:
                    self.VerifyStatus("verify-no-match", False)
            time.sleep(0.01)

    def _enroll_loop(self, username, finger_name):
        STAGES = 5
        completed = 0
        samples = []

        print(f"[Enroll] Starting enrollment for {finger_name}...")
        while completed < STAGES and not self.cancel_scan:
            print(f"[Enroll] Waiting for finger (Stage {completed+1})...")
            frame = self.driver.capture_frame(timeout_sec=2.0)
            if frame:
                samples.append(frame)
                completed += 1
                print(f"[Enroll] Stage {completed} captured.")
                self.EnrollStatus("enroll-stage-passed", False)
                time.sleep(1.0) 
            
        if not self.cancel_scan and completed == STAGES:
            print("[Enroll] All stages complete. Saving...")
            full_name = f"{username}_{finger_name}"
            self.matcher.enroll_finger(full_name, samples)
            self.EnrollStatus("enroll-completed", True)
        elif self.cancel_scan:
             print("[Enroll] Cancelled.")
        else:
            self.EnrollStatus("enroll-failed", True)

    def _start_thread(self, target, args):
        self._stop_scan_thread()
        self.cancel_scan = False
        self.scan_thread = threading.Thread(target=target, args=args)
        self.scan_thread.daemon = True
        self.scan_thread.start()

    def _stop_scan_thread(self):
        self.cancel_scan = True
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=2.0)

if __name__ == "__main__":
    if not os.path.exists("/var/lib/open-fprintd/egis"):
        try:
            os.makedirs("/var/lib/open-fprintd/egis")
        except PermissionError:
            print("Error: Run as root to create /var/lib/open-fprintd/egis")
            sys.exit(1)

    bus = SystemBus()
    MY_DBUS_PATH = "/org/reactivated/Fprint/Device/Egis575"
    
    try:
        service = EgisService()
        bus.publish("org.reactivated.Fprint.Driver.Egis575", (MY_DBUS_PATH, service))
        print("[Main] DBus Service Published locally.")
    except Exception as e:
        print(f"Failed to publish DBus service: {e}")
        sys.exit(1)
    
    print("[Main] Registering with Open-Fprintd Manager...")
    try:
        # Use the correct Bus Name. 
        # We also explicitly specify the Object Path where the Manager lives.
        manager = bus.get("net.reactivated.Fprint", "/net/reactivated/Fprint/Manager")
        manager.RegisterDevice(MY_DBUS_PATH)
        print("[Main] Plugin Registered! Ready for GNOME.")
    except Exception as e:
        print(f"[Error] Could not register with Manager: {e}")
        print("Ensure 'open-fprintd' service is running!")
        sys.exit(1)

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
