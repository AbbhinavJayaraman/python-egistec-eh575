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
        """
        Uses Partial Affine Transform to find if a sub-region matches perfectly.
        Robust to rotation and translation.
        """
        img_arr = np.array(list(raw_frame), dtype=np.uint8).reshape((50, 103))
        img = self._preprocess(img_arr)
        kp_live, des_live = self.sift.detectAndCompute(img, None)
        
        if des_live is None or len(kp_live) < 4: return None, 0

        best_score = 0
        best_match = None

        # Iterate through the bank of templates
        for filename in os.listdir(self.enroll_dir):
            if not filename.endswith(".npy"): continue
            
            name = filename.replace(".npy", "")
            try:
                # Load templates: List of (packed_kp, descriptors)
                templates = np.load(os.path.join(self.enroll_dir, filename), allow_pickle=True)
            except: continue
            
            for (packed_kp_stored, des_stored) in templates:
                if des_stored is None or len(des_stored) < 3: continue
                
                # 1. Relaxed Feature Matching
                try:
                    matches = self.matcher.knnMatch(des_live, des_stored, k=2)
                except: continue
                
                good_matches = []
                for m, n in matches:
                    # Looser ratio (0.85) allows for more partial matches
                    if m.distance < 0.85 * n.distance:
                        good_matches.append(m)

                # We need fewer points for Affine (3 points defines a triangle)
                if len(good_matches) >= 3:
                    src_pts = np.float32([kp_live[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
                    # Unpack the stored keypoints (index 0 is the pt tuple)
                    dst_pts = np.float32([packed_kp_stored[m.trainIdx][0] for m in good_matches]).reshape(-1, 1, 2)

                    # --- THE MAGIC FIX ---
                    # estimateAffinePartial2D finds the best [Rotation + Shift + Scale]
                    # It does NOT allow perspective warping.
                    # It returns a 2x3 matrix (M) and a mask of inliers.
                    M, mask = cv2.estimateAffinePartial2D(src_pts, dst_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0)
                    
                    if mask is not None:
                        # Count how many points fit this rigid overlay
                        inliers = np.sum(mask)
                        
                        # LOGIC: If 15+ points fit a perfect rotation/shift, it's you.
                        # It doesn't matter if the other 50 points are off-screen.
                        if inliers > best_score:
                            best_score = inliers
                            best_match = name
                            # Optimization: If we found a solid patch, stop searching this user
                            if best_score > 30: break

        # Threshold: 15 aligned points is statistically very hard to fake 
        # with a rigid transform constraint.
        if best_score > 15: 
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