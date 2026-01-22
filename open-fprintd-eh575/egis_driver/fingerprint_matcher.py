import os
import subprocess
import tempfile
import numpy as np
import cv2
import shutil

class FingerprintMatcher:
    def __init__(self, enroll_dir="/var/lib/open-fprintd/egis"):
        self.enroll_dir = enroll_dir
        if not os.path.exists(enroll_dir):
            try:
                os.makedirs(enroll_dir)
            except PermissionError:
                print(f"[MATCHER] ERROR: Cannot create {enroll_dir}. Run as root.")

    def _preprocess(self, img_array):
        # NIST tools work best with high-contrast, clean grayscale images
        img = cv2.normalize(img_array, None, 0, 255, cv2.NORM_MINMAX).astype('uint8')
        
        # CLAHE is critical here to cut through the "haze"
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
        img = clahe.apply(img)
        
        return img

    def enroll_finger(self, name, raw_frames):
        """
        Converts ~100 raw frames into 100 tiny .xyt files (minutiae maps).
        This is the "Shotgun" approach, but optimized for speed.
        """
        print(f"[NIST] Processing {len(raw_frames)} frames for {name}...")
        
        safe_name = name.replace("/", "_")
        user_dir = os.path.join(self.enroll_dir, safe_name)
        
        # WIPE old data. Mixed templates are the enemy of accuracy.
        if os.path.exists(user_dir):
            shutil.rmtree(user_dir)
        os.makedirs(user_dir)

        saved_count = 0
        
        for i, raw in enumerate(raw_frames):
            img_arr = np.array(list(raw), dtype=np.uint8).reshape((50, 103))
            img = self._preprocess(img_arr)
            
            # Extract minutiae using mindtct
            xyt_content = self._run_mindtct(img)
            
            # Quality Filter: Only save if we found at least 8 minutiae points.
            if xyt_content:
                # The file has a header; points start after.
                point_count = len(xyt_content.strip().split('\n'))
                if point_count > 8:
                    fname = os.path.join(user_dir, f"frame_{i:03d}.xyt")
                    with open(fname, "w") as f:
                        f.write(xyt_content)
                    saved_count += 1

        print(f"[NIST] Saved {saved_count} robust templates for {safe_name}")
        return saved_count > 0

    def verify_finger(self, raw_frame):
        """
        Matches live frame against ALL enrolled NIST templates instantly.
        """
        img_arr = np.array(list(raw_frame), dtype=np.uint8).reshape((50, 103))
        img = self._preprocess(img_arr)
        
        # 1. Get Live Minutiae
        live_xyt = self._run_mindtct(img)
        if not live_xyt: return None, 0

        best_score = 0
        best_match = None

        # 2. Iterate through all users
        for user_folder_name in os.listdir(self.enroll_dir):
            user_path = os.path.join(self.enroll_dir, user_folder_name)
            if not os.path.isdir(user_path): continue

            # Gather all .xyt files for this user
            gallery_files = [
                os.path.join(user_path, f) 
                for f in os.listdir(user_path) 
                if f.endswith(".xyt")
            ]
            
            if not gallery_files: continue

            # 3. The "Shotgun" Match
            # We pass ALL gallery files to bozorth3 in one command.
            score = self._run_bozorth(live_xyt, gallery_files)
            
            if score > best_score:
                best_score = score
                best_match = user_folder_name
                # If we hit a high confidence score, stop searching.
                if best_score > 40: break

        # NIST SCORING GUIDE (Bozorth3):
        # > 20: Likely Match (Good for small sensors)
        # > 40: High Confidence
        if best_score > 20: 
            return best_match, best_score
        
        return None, 0

    def _run_mindtct(self, img):
        """Runs `mindtct` to extract features from an image."""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp_img:
                cv2.imwrite(tmp_img.name, img)
                with tempfile.TemporaryDirectory() as tmp_dir:
                    base_path = os.path.join(tmp_dir, "out")
                    # Usage: mindtct <input_image> <output_base>
                    subprocess.run(["mindtct", tmp_img.name, base_path], 
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
                    
                    xyt_path = base_path + ".xyt"
                    if os.path.exists(xyt_path):
                        with open(xyt_path, "r") as f:
                            return f.read()
        except:
            return None
        return None

    def _run_bozorth(self, live_xyt_content, gallery_paths):
        """
        Runs `bozorth3` to compare one live print vs MANY gallery prints.
        """
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".xyt", delete=True) as tmp_live:
                tmp_live.write(live_xyt_content)
                tmp_live.flush()
                
                # Command: bozorth3 -p <live.xyt> <gallery1.xyt> ...
                cmd = ["bozorth3", "-p", tmp_live.name] + gallery_paths
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                
                # Output format: one score per line (or space separated)
                scores = []
                for token in result.stdout.split():
                    if token.isdigit():
                        scores.append(int(token))
                
                return max(scores) if scores else 0
        except:
            return 0

    def get_enrolled_fingers(self, username):
        fingers = []
        prefix = f"{username}_"
        for d in os.listdir(self.enroll_dir):
            if d.startswith(prefix) and os.path.isdir(os.path.join(self.enroll_dir, d)):
                fingers.append(d[len(prefix):])
        return fingers

    def delete_user_fingers(self, username):
        prefix = f"{username}_"
        for d in os.listdir(self.enroll_dir):
            if d.startswith(prefix):
                shutil.rmtree(os.path.join(self.enroll_dir, d))
