import cv2
import numpy as np
import os
import time

class FingerprintMatcher:
    def __init__(self, enroll_dir="/var/lib/open-fprintd/egis"):
        self.enroll_dir = enroll_dir
        if not os.path.exists(enroll_dir):
            try:
                os.makedirs(enroll_dir)
            except PermissionError:
                print(f"[MATCHER] ERROR: Cannot create {enroll_dir}. Run as root.")

        # SIFT Configuration
        self.sift = cv2.SIFT_create()

        # FLANN Configuration (KD-Tree for SIFT)
        # Algorithm 1 = FLANN_INDEX_KDTREE
        index_params = dict(algorithm=1, trees=5)
        # Checks = 50 gives good precision/speed balance
        search_params = dict(checks=50)
        self.flann = cv2.FlannBasedMatcher(index_params, search_params)

        # Cache for Tree Lookup
        # self.descriptor_map maps a range of global indices back to a specific template
        self.descriptor_map = [] 
        self.cached_templates = {} 
        self.train_descriptors = None
        
        # Build the tree on startup
        self.rebuild_index()

    def _preprocess(self, img_array):
        """Standard preprocessing pipeline for SIFT"""
        img = cv2.normalize(img_array, None, 0, 255, cv2.NORM_MINMAX).astype('uint8')
        img = cv2.equalizeHist(img)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        return img

    def rebuild_index(self):
        """
        Loads ALL templates from disk and builds a single FLANN KD-Tree.
        This enables O(1) lookup instead of O(N) linear scanning.
        """
        print("[MATCHER] Rebuilding Global FLANN Index...")
        start_t = time.time()

        all_descriptors = []
        self.descriptor_map = [] 
        self.cached_templates = {}

        current_idx_offset = 0

        # Load every .npy file in the directory
        for filename in os.listdir(self.enroll_dir):
            if not filename.endswith(".npy"): continue
            
            try:
                # Load templates: List of (packed_kp, des)
                raw_data = np.load(os.path.join(self.enroll_dir, filename), allow_pickle=True)
                
                unpacked_templates = []
                for t_idx, (packed_kp, des) in enumerate(raw_data):
                    if des is None or len(des) < 2: continue
                    
                    # Unpack KeyPoints for RANSAC usage
                    kp = [cv2.KeyPoint(x=pt[0], y=pt[1], size=sz, angle=ang, response=resp, octave=oct, class_id=cid) 
                          for (pt, sz, ang, resp, oct, cid) in packed_kp]
                    
                    unpacked_templates.append((kp, des))

                    # Add descriptors to the Global Pile
                    all_descriptors.append(des)
                    
                    # Map global indices to this specific template
                    num_des = len(des)
                    self.descriptor_map.append({
                        "start": current_idx_offset,
                        "end": current_idx_offset + num_des,
                        "file": filename,
                        "idx": t_idx 
                    })
                    current_idx_offset += num_des
                
                self.cached_templates[filename] = unpacked_templates

            except Exception as e:
                print(f"[MATCHER] Failed to load {filename}: {e}")

        # Build the actual Tree
        if all_descriptors:
            self.train_descriptors = np.vstack(all_descriptors)
            self.flann.clear()
            self.flann.add([self.train_descriptors])
            self.flann.train()
            print(f"[MATCHER] Index built in {time.time()-start_t:.2f}s. Total Features: {current_idx_offset}")
        else:
            print("[MATCHER] Index is empty (no enrolled prints).")
            self.train_descriptors = None

    def enroll_finger(self, name, raw_frames):
        """
        Appends new scans to the existing user file instead of overwriting.
        This allows the user to 'add' to their print definition.
        """
        new_templates = []
        
        print(f"[MATCHER] Enrolling {name} (Appending {len(raw_frames)} scans)...")

        # 1. Process New Scans
        for raw in raw_frames:
            img_arr = np.array(list(raw), dtype=np.uint8).reshape((50, 103))
            img = self._preprocess(img_arr)
            kp, des = self.sift.detectAndCompute(img, None)
            
            if des is not None and len(kp) > 5:
                # Pack KeyPoints for serialization
                packed_kp = [(p.pt, p.size, p.angle, p.response, p.octave, p.class_id) for p in kp]
                new_templates.append((packed_kp, des))
        
        if not new_templates:
            return False

        # 2. Load Existing Scans (Append Mode)
        safe_name = name.replace("/", "_")
        file_path = os.path.join(self.enroll_dir, f"{safe_name}.npy")
        existing_data = []

        if os.path.exists(file_path):
            try:
                existing_data = list(np.load(file_path, allow_pickle=True))
                print(f"[MATCHER] Found {len(existing_data)} existing templates, appending...")
            except:
                print("[MATCHER] Existing file corrupt, starting fresh.")

        # 3. Save Combined Data
        final_data = existing_data + new_templates
        np.save(file_path, np.array(final_data, dtype=object))
        
        print(f"[MATCHER] Saved. Total templates for {name}: {len(final_data)}")
        
        # 4. Update the Tree Live
        self.rebuild_index()
        return True

    def verify_finger(self, raw_frame):
        """
        1. Query Global Tree -> Vote for best template.
        2. RANSAC -> Verify geometry of the winner.
        """
        if self.train_descriptors is None: return None, 0

        img_arr = np.array(list(raw_frame), dtype=np.uint8).reshape((50, 103))
        img = self._preprocess(img_arr)
        kp_live, des_live = self.sift.detectAndCompute(img, None)
        
        if des_live is None or len(kp_live) < 4: return None, 0

        # --- STEP 1: Global Voting ---
        # Find 2 nearest neighbors in the ENTIRE database
        matches = self.flann.knnMatch(des_live, k=2)
        
        good_matches = []
        for m, n in matches:
            if m.distance < 0.75 * n.distance:
                good_matches.append(m)

        if len(good_matches) < 4: return None, 0

        # Tally Votes: Which template owns these matched descriptors?
        candidate_votes = {}
        
        for m in good_matches:
            global_idx = m.trainIdx
            
            # Map global index -> (Filename, TemplateIdx)
            # Since map is ordered, we can just iterate. Fast enough for <500 templates.
            found_owner = None
            for entry in self.descriptor_map:
                if entry["start"] <= global_idx < entry["end"]:
                    found_owner = entry
                    break
            
            if found_owner:
                key = (found_owner["file"], found_owner["idx"])
                if key not in candidate_votes:
                    candidate_votes[key] = []
                
                # IMPORTANT: Convert Global Index to Local Index for RANSAC
                local_train_idx = global_idx - found_owner["start"]
                new_m = cv2.DMatch(m.queryIdx, local_train_idx, m.imgIdx, m.distance)
                candidate_votes[key].append(new_m)

        if not candidate_votes: return None, 0

        # --- STEP 2: Pick Winner & Verify ---
        # Sort by vote count
        sorted_candidates = sorted(candidate_votes.items(), key=lambda item: len(item[1]), reverse=True)
        
        # Check top candidate only (O(1) Check)
        best_candidate_key, best_candidate_matches = sorted_candidates[0]
        filename, t_idx = best_candidate_key
        
        # Retrieve KeyPoints from RAM cache
        if filename not in self.cached_templates: return None, 0
        kp_stored, des_stored = self.cached_templates[filename][t_idx]

        if len(best_candidate_matches) < 4: return None, 0

        # Prepare points for RANSAC
        src_pts = np.float32([kp_live[m.queryIdx].pt for m in best_candidate_matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp_stored[m.trainIdx].pt for m in best_candidate_matches]).reshape(-1, 1, 2)

        # Run Geometric Verification
        M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        
        if mask is not None:
            inliers = np.sum(mask)
            # Threshold: >25 inliers is usually a very strong match for SIFT
            if inliers > 25: 
                name = filename.replace(".npy", "")
                return name, inliers

        return None, 0

    def delete_specific_finger(self, username, finger_name):
        """Deletes a single finger and rebuilds the tree."""
        # Handle variations in naming (fprintd passes 'right-index-finger')
        # Our files are 'username_right-index-finger.npy'
        
        # Try exact match first
        filename = f"{username}_{finger_name}.npy"
        file_path = os.path.join(self.enroll_dir, filename)
        
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"[MATCHER] Deleted {filename}")
            self.rebuild_index()
            return True
        
        print(f"[MATCHER] Could not find {filename} to delete.")
        return False
        
    def get_enrolled_fingers(self, username):
        """Returns list of fingers for fprintd"""
        fingers = []
        prefix = f"{username}_"
        for filename in os.listdir(self.enroll_dir):
            if filename.startswith(prefix) and filename.endswith(".npy"):
                fingers.append(filename[len(prefix):-4])
        return fingers

    def delete_user_fingers(self, username):
        """Wipes all fingers for a user"""
        prefix = f"{username}_"
        deleted = False
        for filename in os.listdir(self.enroll_dir):
            if filename.startswith(prefix):
                os.remove(os.path.join(self.enroll_dir, filename))
                deleted = True
        
        if deleted:
            self.rebuild_index()