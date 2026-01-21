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
        """Saves KeyPoints AND Descriptors to disk for geometric verification."""
        templates = []
        counts = []
        
        print(f"[MATCHER] Starting enrollment for {name} with {len(raw_frames)} raw scans...")

        for i, raw in enumerate(raw_frames):
            img_arr = np.array(list(raw), dtype=np.uint8).reshape((50, 103))
            img = self._preprocess(img_arr)
            kp, des = self.sift.detectAndCompute(img, None)
            
            # --- LOGGING ADDED HERE ---
            num_features = len(kp)
            counts.append(num_features)
            print(f"[MATCHER] Frame {i+1}: Found {num_features} features")

            # We need at least a few points to make a valid template
            if des is not None and num_features > 5:
                # Pack KeyPoints for saving
                packed_kp = [(p.pt, p.size, p.angle, p.response, p.octave, p.class_id) for p in kp]
                templates.append((packed_kp, des))
        
        # --- SUMMARY STATS ---
        if counts:
            print(f"[MATCHER] STATS for {name}: MAX={max(counts)}, AVG={sum(counts)/len(counts):.1f}")

        if templates:
            safe_name = name.replace("/", "_") # Sanitize
            np.save(os.path.join(self.enroll_dir, f"{safe_name}.npy"), np.array(templates, dtype=object))
            print(f"[MATCHER] Successfully saved {len(templates)} templates for {safe_name}")
            return True
        return False

    def verify_finger(self, raw_frame):
        """Returns (matched_name, score) using RANSAC Geometric Verification"""
        img_arr = np.array(list(raw_frame), dtype=np.uint8).reshape((50, 103))
        img = self._preprocess(img_arr)
        kp_live, des_live = self.sift.detectAndCompute(img, None)
        
        # Need at least 4 points to calculate Homography (Geometry)
        if des_live is None or len(kp_live) < 4: return None, 0

        best_score = 0
        best_match = None

        for filename in os.listdir(self.enroll_dir):
            if not filename.endswith(".npy"): continue
            
            name = filename.replace(".npy", "")
            try:
                enrolled_templates = np.load(os.path.join(self.enroll_dir, filename), allow_pickle=True)
            except: continue
            
            for template_data in enrolled_templates:
                # Sanity check: ensure we have both KeyPoints and Descriptors (Handles corrupt/old files)
                if len(template_data) != 2: continue
                
                packed_kp_stored, des_stored = template_data
                
                # Reconstruct the KeyPoint objects from the saved tuples
                kp_stored = [cv2.KeyPoint(x=pt[0], y=pt[1], size=sz, angle=ang, response=resp, octave=oct, class_id=cid) 
                             for (pt, sz, ang, resp, oct, cid) in packed_kp_stored]

                if des_stored is None or len(des_stored) < 2: continue
                
                # 1. Standard KNN Match (The "Bag of Features" check)
                try:
                    matches = self.matcher.knnMatch(des_live, des_stored, k=2)
                except: continue
                
                good_matches = []
                for m, n in matches:
                    if m.distance < 0.75 * n.distance:
                        good_matches.append(m)

                # 2. Geometric Verification (RANSAC)
                # We strictly require 4+ matching points to define a valid geometric transformation
                if len(good_matches) > 4:
                    src_pts = np.float32([kp_live[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
                    dst_pts = np.float32([kp_stored[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

                    # cv2.findHomography attempts to map points from Live -> Stored
                    # RANSAC discards points that don't fit the map (outliers)
                    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 1.5)
                    
                    if mask is not None:
                        # The score is the number of INLIERS (points that geometrically align)
                        inliers = np.sum(mask)
                        if inliers > best_score:
                            best_score = inliers
                            best_match = name

        # Score Threshold
        # With RANSAC, a score of 8-10 means 8-10 points matched AND fit the same physical shape.
        # This is much harder to fake than 8 random points.
        if best_score > 25: 
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