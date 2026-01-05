import cv2
import numpy as np
import os

class FingerprintMatcher:
    def __init__(self, enroll_dir="/var/lib/open-fprintd/egis"):
        self.enroll_dir = enroll_dir
        if not os.path.exists(enroll_dir):
            try:
                os.makedirs(enroll_dir)
            except PermissionError:
                print(f"[MATCHER] ERROR: Cannot create {enroll_dir}. Run as root.")

        self.sift = cv2.SIFT_create()
        self.matcher = cv2.BFMatcher()

    def _preprocess(self, img_array):
        # Ensure dimensions match your 0575 sensor (50x103)
        img = cv2.normalize(img_array, None, 0, 255, cv2.NORM_MINMAX).astype('uint8')
        img = cv2.equalizeHist(img)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        return img

    def enroll_finger(self, name, raw_frames):
        """Saves sift descriptors to disk."""
        descriptors_list = []
        for raw in raw_frames:
            img_arr = np.array(list(raw), dtype=np.uint8).reshape((50, 103))
            img = self._preprocess(img_arr)
            kp, des = self.sift.detectAndCompute(img, None)
            if des is not None:
                descriptors_list.append(des)
        
        if descriptors_list:
            safe_name = name.replace("/", "_") # Sanitize
            dtype_obj = np.array(descriptors_list, dtype=object)
            np.save(os.path.join(self.enroll_dir, f"{safe_name}.npy"), dtype_obj)
            print(f"[MATCHER] Enrolled: {safe_name}")
            return True
        return False

    def verify_finger(self, raw_frame):
        """Returns (matched_name, score)"""
        img_arr = np.array(list(raw_frame), dtype=np.uint8).reshape((50, 103))
        img = self._preprocess(img_arr)
        kp, des = self.sift.detectAndCompute(img, None)
        
        if des is None: return None, 0

        best_score = 0
        best_match = None

        for filename in os.listdir(self.enroll_dir):
            if not filename.endswith(".npy"): continue
            
            name = filename.replace(".npy", "")
            try:
                enrolled_templates = np.load(os.path.join(self.enroll_dir, filename), allow_pickle=True)
            except: continue
            
            for template_des in enrolled_templates:
                if template_des is None or len(template_des) < 2: continue
                try:
                    matches = self.matcher.knnMatch(des, template_des, k=2)
                except: continue
                
                good_points = 0
                for m, n in matches:
                    if m.distance < 0.75 * n.distance:
                        good_points += 1
                
                if good_points > best_score:
                    best_score = good_points
                    best_match = name

        # Score Threshold (Adjust as needed)
        if best_score > 10: 
            return best_match, best_score
        return None, 0

    def get_enrolled_fingers(self, username):
        """Finds files starting with 'username_'"""
        fingers = []
        prefix = f"{username}_"
        for filename in os.listdir(self.enroll_dir):
            if filename.startswith(prefix) and filename.endswith(".npy"):
                # "user_right-index-finger.npy" -> "right-index-finger"
                fingers.append(filename[len(prefix):-4])
        return fingers

    def delete_user_fingers(self, username):
        prefix = f"{username}_"
        for filename in os.listdir(self.enroll_dir):
            if filename.startswith(prefix):
                os.remove(os.path.join(self.enroll_dir, filename))