#!/usr/bin/env python3
"""
Sustainable Dynamic Character Discovery and AI-Cropped Icon Pipeline.
Polls GameBanana categories, fetches official avatar icons or transparent splash art,
crops high-resolution 256x256 headshots, and generates versioned manifest.json.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from PIL import Image
import requests

# Root category mappings per game
DEFAULT_ROOT_CATEGORIES: Dict[str, int] = {
    "Endfield": 42770,
    "NTE": 37906,
    "ZZZ": 30305,
    "Genshin": 18140,
}

GAME_IDS: Dict[str, int] = {
    "Endfield": 21842,
    "NTE": 23012,
    "ZZZ": 19482,
    "Genshin": 8552,
}

MEDIAWIKI_ENDPOINTS: Dict[str, str] = {
    "Endfield": "https://endfield.fandom.com/api.php",
    "ZZZ": "https://zenless-zone-zero.fandom.com/api.php",
    "Genshin": "https://genshin-impact.fandom.com/api.php",
}


@dataclass
class CategoryProbeRecord:
    id_row: int
    name: str
    icon_url: str
    is_obsolete: bool = False
    item_count: int = 0


def sanitize_internal_name(display_name: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", display_name).strip().lower()
    return re.sub(r"[-\s]+", "_", cleaned)


def calculate_smart_face_crop(
    face_box: Tuple[int, int, int, int], img_w: int, img_h: int, expansion: float = 2.6
) -> Tuple[int, int, int, int]:
    """
    Computes a 1:1 square crop area around a detected face box [x, y, w, h].
    Expands the box to max(w, h) * expansion and biases slightly toward eye level.
    Clamps within image boundaries to avoid artificial transparent padding.
    """
    x, y, w, h = face_box
    cx = x + w / 2.0
    cy = y + h / 2.0

    s = int(round(max(w, h) * expansion))
    s = min(s, min(img_w, img_h))

    target_cy = int(round(cy - 0.15 * s))
    target_cx = int(round(cx))

    half_s = s // 2
    x1 = target_cx - half_s
    y1 = target_cy - half_s

    # Clamp inside image boundaries so we don't introduce empty space above head
    if y1 < 0:
        y1 = 0
    elif y1 + s > img_h:
        y1 = img_h - s

    if x1 < 0:
        x1 = 0
    elif x1 + s > img_w:
        x1 = img_w - s

    return (x1, y1, x1 + s, y1 + s)


def crop_and_resize_face(
    image: Image.Image,
    face_box: Tuple[int, int, int, int],
    target_size: Tuple[int, int] = (256, 256),
    expansion: float = 2.6,
) -> Image.Image:
    """
    Crops the face area with padding if boundaries exceed image dimensions,
    and returns a clean target_size RGBA image.
    """
    rgba_img = image.convert("RGBA")
    img_w, img_h = rgba_img.size

    x1, y1, x2, y2 = calculate_smart_face_crop(face_box, img_w, img_h, expansion=expansion)
    crop_w = x2 - x1
    crop_h = y2 - y1

    # Create empty canvas with transparent background
    canvas = Image.new("RGBA", (crop_w, crop_h), (0, 0, 0, 0))

    # Calculate overlapping source rectangle
    src_x1 = max(0, x1)
    src_y1 = max(0, y1)
    src_x2 = min(img_w, x2)
    src_y2 = min(img_h, y2)

    # Destination coordinates on canvas
    dst_x = src_x1 - x1
    dst_y = src_y1 - y1

    if src_x2 > src_x1 and src_y2 > src_y1:
        region = rgba_img.crop((src_x1, src_y1, src_x2, src_y2))
        canvas.paste(region, (dst_x, dst_y))

    return canvas.resize(target_size, Image.Resampling.LANCZOS)


def detect_face_in_image(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """
    Detects face bounding box using OpenCV cascade if available,
    filtering for detections in upper 60% of image height.
    Falls back to upper-center portrait framing if no face is detected.
    """
    img_w, img_h = image.size

    try:
        import cv2
        import numpy as np

        # Convert to numpy grayscale
        cv_img = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)

        # Attempt OpenCV frontal face detection
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if os.path.exists(cascade_path):
            detector = cv2.CascadeClassifier(cascade_path)
            faces = detector.detectMultiScale(cv_img, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
            if len(faces) > 0:
                # Filter for upper 60% of image height and select largest
                candidates = [
                    (int(x), int(y), int(w), int(h))
                    for (x, y, w, h) in faces
                    if y < 0.6 * img_h
                ]
                if candidates:
                    candidates.sort(key=lambda b: b[2] * b[3], reverse=True)
                    return candidates[0]
    except Exception:
        pass

    # Fallback to upper 30% center box
    w = int(img_w * 0.35)
    h = int(img_h * 0.25)
    x = (img_w - w) // 2
    y = int(img_h * 0.10)
    return (x, y, w, h)


def diff_categories_against_manifest(
    categories: List[CategoryProbeRecord],
    manifest: Dict[str, Any],
    game_name: str,
) -> List[CategoryProbeRecord]:
    """
    Identifies GameBanana categories that are not yet recorded in the manifest.
    """
    game_data = manifest.get("games", {}).get(game_name, {})
    characters = game_data.get("characters", [])

    existing_category_ids = {
        c.get("gameBananaCategoryId") for c in characters if c.get("gameBananaCategoryId")
    }
    existing_internal_names = {
        c.get("internalName") for c in characters if c.get("internalName")
    }

    new_categories: List[CategoryProbeRecord] = []
    for cat in categories:
        if cat.is_obsolete:
            continue
        internal_name = sanitize_internal_name(cat.name)
        if cat.id_row in existing_category_ids or internal_name in existing_internal_names:
            continue
        new_categories.append(cat)

    return new_categories


def generate_manifest_record(
    name: str,
    category_id: int,
    game_name: str,
    png_bytes: bytes,
    quality: str = "official_avatar",
) -> Dict[str, Any]:
    internal_name = sanitize_internal_name(name)
    sha256 = hashlib.sha256(png_bytes).hexdigest()
    image_path = f"Images/{game_name}/{internal_name}.png"

    slug = re.sub(r"[^\w]", "", name.lower())
    words = [w.lower() for w in re.split(r"[\s_-]+", name.strip()) if len(w) >= 2]
    keys = list(dict.fromkeys([internal_name, name.lower(), slug] + words))

    return {
        "internalName": internal_name,
        "displayName": name,
        "gameBananaCategoryId": category_id,
        "imagePath": image_path,
        "imageUrl": f"https://raw.githubusercontent.com/MotefiBranch/JASM-Assets/main/{image_path}",
        "sha256": sha256,
        "assetQuality": quality,
        "keys": keys,
    }


def probe_gamebanana_categories(
    root_category_id: int, timeout: int = 10, session: Optional[requests.Session] = None
) -> List[CategoryProbeRecord]:
    """
    Fast gatekeeper probe querying GameBanana v11 API. Returns in <5 seconds.
    """
    url = f"https://gamebanana.com/apiv11/Mod/Categories?_idCategoryRow={root_category_id}&_sSort=a_to_z"
    s = session or requests.Session()

    try:
        resp = s.get(url, timeout=timeout, headers={"User-Agent": "JASM-AssetPipeline/1.0"})
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []

        records: List[CategoryProbeRecord] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            id_row = item.get("_idRow")
            name = item.get("_sName")
            icon_url = item.get("_sIconUrl", "")
            is_obsolete = item.get("_bIsObsolete", False)
            item_count = item.get("_nItemCount", 0)

            if id_row and name:
                records.append(
                    CategoryProbeRecord(
                        id_row=int(id_row),
                        name=str(name),
                        icon_url=str(icon_url or ""),
                        is_obsolete=bool(is_obsolete),
                        item_count=int(item_count or 0),
                    )
                )
        return records
    except Exception as ex:
        print(f"Error querying GameBanana root {root_category_id}: {ex}", file=sys.stderr)
        return []


def fetch_official_avatar_wiki(
    game_name: str, character_name: str, session: Optional[requests.Session] = None
) -> Optional[Tuple[bytes, str]]:
    """
    Tries to fetch official avatar icon from MediaWiki API.
    Returns (bytes, quality_tier) or None.
    """
    endpoint = MEDIAWIKI_ENDPOINTS.get(game_name)
    if not endpoint:
        return None

    s = session or requests.Session()
    first_name = re.split(r"[\s_-]+", character_name.strip())[0] if character_name else character_name

    candidates: List[str] = []
    if game_name == "ZZZ":
        candidates.extend([
            f"Agent {character_name} Icon.png",
            f"Agent {first_name} Icon.png",
            f"Agent {character_name} Portrait.png",
        ])
    elif game_name == "Endfield":
        candidates.extend([
            f"Operator {character_name} Icon.png",
            f"{character_name} Avatar.png",
        ])

    candidates.extend([
        f"{character_name} Icon.png",
        f"{first_name} Icon.png",
        f"Character {character_name} Icon.png",
        f"{character_name}.png",
    ])

    unique_candidates = list(dict.fromkeys(candidates))
    titles_param = "|".join([f"File:{t}" for t in unique_candidates])

    try:
        params = {
            "action": "query",
            "titles": titles_param,
            "prop": "imageinfo",
            "iiprop": "url|size",
            "format": "json",
        }
        resp = s.get(endpoint, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            pages = data.get("query", {}).get("pages", {})
            best_bytes: Optional[bytes] = None
            max_size = 0
            for page in pages.values():
                imageinfo = page.get("imageinfo")
                if imageinfo and len(imageinfo) > 0:
                    img_url = imageinfo[0].get("url")
                    size = imageinfo[0].get("size", 0)
                    if img_url:
                        img_resp = s.get(img_url, timeout=10)
                        if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                            if size > max_size:
                                max_size = size
                                best_bytes = img_resp.content
            if best_bytes:
                return (best_bytes, "official_avatar")
    except Exception:
        pass

    return None


def align_gb_to_highres(
    gb_bytes: bytes,
    hr_bytes: bytes,
    min_size: int = 250,
    max_size: int = 1200,
    min_score_threshold: float = 0.80,
) -> Tuple[Optional[Image.Image], float]:
    """
    Reverse-engineers the exact headshot crop from a low-resolution GameBanana category thumbnail
    by performing multi-scale normalized cross-correlation against the high-resolution master art.
    Returns (256x256 Image, score) if correlation >= min_score_threshold, else (None, score).
    """
    try:
        import cv2
        import numpy as np

        gb_rgba = cv2.imdecode(np.frombuffer(gb_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
        hr_rgba = cv2.imdecode(np.frombuffer(hr_bytes, np.uint8), cv2.IMREAD_UNCHANGED)

        if gb_rgba is None or hr_rgba is None:
            return None, 0.0

        hr_h, hr_w = hr_rgba.shape[:2]
        upper_h = int(hr_h * 0.65)
        hr_upper = hr_rgba[0:upper_h, :]

        gb_bgr = gb_rgba[:, :, :3]
        gb_a = gb_rgba[:, :, 3] if gb_rgba.shape[2] == 4 else np.full(gb_rgba.shape[:2], 255, dtype=np.uint8)
        hr_bgr = hr_upper[:, :, :3]
        hr_a = hr_upper[:, :, 3] if hr_upper.shape[2] == 4 else np.full(hr_upper.shape[:2], 255, dtype=np.uint8)

        mask = (gb_a > 30).astype(np.uint8) * 255
        sh, sw = hr_upper.shape[:2]

        best_score = -1.0
        best_crop = None

        # Coarse sweep
        for crop_size in range(min_size, min(max_size, min(sh, sw)), 10):
            scale = 40.0 / crop_size
            new_w = int(round(sw * scale))
            new_h = int(round(sh * scale))
            if new_w < 40 or new_h < 40:
                continue
            scaled_bgr = cv2.resize(hr_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
            scaled_a = cv2.resize(hr_a, (new_w, new_h), interpolation=cv2.INTER_AREA)

            res_bgr = cv2.matchTemplate(scaled_bgr, gb_bgr, cv2.TM_CCORR_NORMED, mask=mask)
            res_a = cv2.matchTemplate(scaled_a, gb_a, cv2.TM_CCORR_NORMED)

            res_bgr = np.nan_to_num(res_bgr, nan=0.0, posinf=0.0, neginf=0.0)
            res_a = np.nan_to_num(res_a, nan=0.0, posinf=0.0, neginf=0.0)

            res_total = 0.6 * res_bgr + 0.4 * res_a
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res_total)

            if max_val > best_score:
                best_score = max_val
                orig_x = int(round(max_loc[0] / scale))
                orig_y = int(round(max_loc[1] / scale))
                best_crop = (orig_x, orig_y, crop_size)

        if best_crop is None or best_score < min_score_threshold:
            return None, best_score

        # Fine sweep (step 1px)
        bx, by, bs = best_crop
        for crop_size in range(max(min_size, bs - 15), min(max_size, bs + 16), 1):
            scale = 40.0 / crop_size
            new_w = int(round(sw * scale))
            new_h = int(round(sh * scale))
            if new_w < 40 or new_h < 40:
                continue
            scaled_bgr = cv2.resize(hr_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
            scaled_a = cv2.resize(hr_a, (new_w, new_h), interpolation=cv2.INTER_AREA)

            res_bgr = cv2.matchTemplate(scaled_bgr, gb_bgr, cv2.TM_CCORR_NORMED, mask=mask)
            res_a = cv2.matchTemplate(scaled_a, gb_a, cv2.TM_CCORR_NORMED)

            res_bgr = np.nan_to_num(res_bgr, nan=0.0, posinf=0.0, neginf=0.0)
            res_a = np.nan_to_num(res_a, nan=0.0, posinf=0.0, neginf=0.0)

            res_total = 0.6 * res_bgr + 0.4 * res_a
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res_total)

            if max_val > best_score:
                best_score = max_val
                orig_x = int(round(max_loc[0] / scale))
                orig_y = int(round(max_loc[1] / scale))
                best_crop = (orig_x, orig_y, crop_size)

        pil_hr = Image.open(BytesIO(hr_bytes)).convert("RGBA")
        x, y, s = best_crop

        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(pil_hr.width, x + s)
        y2 = min(pil_hr.height, y + s)

        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        canvas.paste(pil_hr.crop((x1, y1, x2, y2)), (x1 - x, y1 - y))

        out_256 = canvas.resize((256, 256), Image.Resampling.LANCZOS)
        return out_256, best_score
    except Exception:
        return None, 0.0


def fetch_image_and_process(
    cat: CategoryProbeRecord,
    game_name: str,
    session: Optional[requests.Session] = None,
) -> Optional[Tuple[bytes, str]]:
    """
    Progressive quality fetch:
    Tier 3: Official wiki avatar reverse-aligned with GameBanana headshot framing
    Tier 2: Official wiki portrait cropped with face detection
    Tier 1: GameBanana category thumbnail
    """
    s = session or requests.Session()

    # Tier 3 & 2: Official wiki fetch
    wiki_result = fetch_official_avatar_wiki(game_name, cat.name, s)
    if wiki_result:
        img_bytes, quality = wiki_result
        try:
            # Automatic default: transparent lineup against GameBanana headshot
            if cat.icon_url:
                gb_resp = s.get(cat.icon_url, timeout=10)
                if gb_resp.status_code == 200 and len(gb_resp.content) > 100:
                    aligned_img, score = align_gb_to_highres(gb_resp.content, img_bytes)
                    if aligned_img is not None and score >= 0.80:
                        out_buf = BytesIO()
                        aligned_img.save(out_buf, format="PNG")
                        return (out_buf.getvalue(), "aligned_headshot")

            # Fallback within wiki art: face detection or resize
            pil_img = Image.open(BytesIO(img_bytes)).convert("RGBA")
            w, h = pil_img.size
            if abs(w - h) > 20 or h > 300:
                face_box = detect_face_in_image(pil_img)
                if face_box:
                    cropped = crop_and_resize_face(pil_img, face_box, (256, 256), expansion=1.5)
                else:
                    cropped = pil_img.resize((256, 256), Image.Resampling.LANCZOS)
            else:
                cropped = pil_img.resize((256, 256), Image.Resampling.LANCZOS)

            out_buf = BytesIO()
            cropped.save(out_buf, format="PNG")
            return (out_buf.getvalue(), "official_avatar")
        except Exception:
            pass

    # Tier 1 fallback: GB category icon
    if cat.icon_url:
        try:
            resp = s.get(cat.icon_url, timeout=10)
            if resp.status_code == 200 and len(resp.content) > 200:
                pil_img = Image.open(BytesIO(resp.content)).convert("RGBA")
                face_box = detect_face_in_image(pil_img)
                if face_box:
                    cropped = crop_and_resize_face(pil_img, face_box, (256, 256))
                else:
                    cropped = pil_img.resize((256, 256), Image.Resampling.LANCZOS)
                out_buf = BytesIO()
                cropped.save(out_buf, format="PNG")
                return (out_buf.getvalue(), "thumbnail")
        except Exception:
            pass

    return None


def run_gatekeeper_probe(
    manifest_path: str,
    games: Optional[List[str]] = None,
) -> Tuple[int, Dict[str, List[CategoryProbeRecord]]]:
    """
    Executes fast probe. Returns (diff_count, diff_dict).
    Completes in <5s when no new characters are found.
    """
    start_time = time.time()
    manifest: Dict[str, Any] = {"version": 1, "games": {}}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as ex:
            print(f"Warning: Failed to parse {manifest_path}: {ex}", file=sys.stderr)

    target_games = games or list(DEFAULT_ROOT_CATEGORIES.keys())
    session = requests.Session()
    diffs: Dict[str, List[CategoryProbeRecord]] = {}
    total_diff = 0

    for game in target_games:
        root_id = DEFAULT_ROOT_CATEGORIES.get(game)
        if not root_id:
            continue
        categories = probe_gamebanana_categories(root_id, timeout=10, session=session)
        new_cats = diff_categories_against_manifest(categories, manifest, game)
        if new_cats:
            diffs[game] = new_cats
            total_diff += len(new_cats)

    elapsed = time.time() - start_time
    print(f"Gatekeeper probe completed in {elapsed:.2f}s. Discovered {total_diff} new character categories.")
    return total_diff, diffs


def sync_manifest_and_assets(
    manifest_path: str,
    images_dir: str,
    games: Optional[List[str]] = None,
) -> int:
    """
    Full pipeline run: fetches assets for new categories, updates manifest.json and writes images.
    """
    manifest: Dict[str, Any] = {"version": 1, "updatedAt": "", "games": {}}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    target_games = games or list(DEFAULT_ROOT_CATEGORIES.keys())
    session = requests.Session()
    total_added = 0

    for game in target_games:
        root_id = DEFAULT_ROOT_CATEGORIES.get(game)
        if not root_id:
            continue

        if game not in manifest["games"]:
            manifest["games"][game] = {
                "gameId": GAME_IDS.get(game, 0),
                "charactersRootCategoryId": root_id,
                "characters": [],
            }

        categories = probe_gamebanana_categories(root_id, timeout=10, session=session)
        new_cats = diff_categories_against_manifest(categories, manifest, game)

        for cat in new_cats:
            result = fetch_image_and_process(cat, game, session)
            if not result:
                continue
            png_bytes, quality = result

            game_img_dir = os.path.join(images_dir, game)
            os.makedirs(game_img_dir, exist_ok=True)

            internal_name = sanitize_internal_name(cat.name)
            img_filename = f"{internal_name}.png"
            img_full_path = os.path.join(game_img_dir, img_filename)

            with open(img_full_path, "wb") as f:
                f.write(png_bytes)

            record = generate_manifest_record(cat.name, cat.id_row, game, png_bytes, quality)
            manifest["games"][game]["characters"].append(record)
            total_added += 1
            print(f"Added {game} character: {cat.name} ({quality})")

    if total_added > 0:
        manifest["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"Updated {manifest_path} with {total_added} additions.")

    return total_added


def main() -> int:
    parser = argparse.ArgumentParser(description="Character Asset Scraper and Crop Pipeline")
    parser.add_argument("--probe-only", action="store_true", help="Run fast gatekeeper check only")
    parser.add_argument("--game", type=str, help="Single game filter (Endfield, NTE, ZZZ, Genshin)")
    parser.add_argument("--manifest", type=str, default="manifest.json", help="Path to manifest.json")
    parser.add_argument("--images-dir", type=str, default="Images", help="Directory for output PNGs")

    args = parser.parse_args()

    games = [args.game] if args.game else None

    if args.probe_only:
        diff_count, _ = run_gatekeeper_probe(args.manifest, games)
        # Exit 0 if clean, exit 2 if new characters exist
        return 2 if diff_count > 0 else 0

    added = sync_manifest_and_assets(args.manifest, args.images_dir, games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
