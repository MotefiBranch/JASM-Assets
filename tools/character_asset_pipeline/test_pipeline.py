import hashlib
import json
import os
import unittest
from io import BytesIO
from PIL import Image

try:
    from tools.character_asset_pipeline.crop_character_icons import (
        calculate_smart_face_crop,
        crop_and_resize_face,
        diff_categories_against_manifest,
        generate_manifest_record,
        CategoryProbeRecord,
    )
except ImportError:
    from crop_character_icons import (
        calculate_smart_face_crop,
        crop_and_resize_face,
        diff_categories_against_manifest,
        generate_manifest_record,
        CategoryProbeRecord,
    )


class TestCharacterAssetPipeline(unittest.TestCase):
    def test_smart_framing_head_shoulders(self):
        # Face bounding box: x=100, y=100, w=100, h=100 in 1000x1000 image
        box = (100, 100, 100, 100)
        img_w, img_h = 1000, 1000
        crop_box = calculate_smart_face_crop(box, img_w, img_h)

        # Desired crop size S = max(100, 100) * 2.6 = 260
        # Face center: cx = 150, cy = 150
        # Target cy = cy - 0.15 * S = 150 - 39 = 111
        # Crop width and height should be equal (1:1 aspect ratio)
        crop_w = crop_box[2] - crop_box[0]
        crop_h = crop_box[3] - crop_box[1]
        self.assertEqual(crop_w, crop_h)
        self.assertEqual(crop_w, 260)

        # Verify eye bias: the center of the crop is shifted upwards relative to face center
        crop_center_y = (crop_box[1] + crop_box[3]) / 2.0
        self.assertLess(crop_center_y, 150)

    def test_gatekeeper_exits_fast_when_no_diff(self):
        # Local manifest contains category 46924 (Camille)
        manifest = {
            "version": 1,
            "games": {
                "Endfield": {
                    "charactersRootCategoryId": 42770,
                    "characters": [
                        {
                            "internalName": "camille",
                            "displayName": "Camille",
                            "gameBananaCategoryId": 46924,
                            "assetQuality": "official_avatar"
                        }
                    ]
                }
            }
        }

        # Categories fetched from GameBanana match existing manifest
        categories = [
            CategoryProbeRecord(id_row=46924, name="Camille", icon_url="https://images.gamebanana.com/ico/camille.png")
        ]

        diff = diff_categories_against_manifest(categories, manifest, "Endfield")
        self.assertEqual(len(diff), 0)

    def test_gatekeeper_detects_new_categories(self):
        manifest = {
            "version": 1,
            "games": {
                "Endfield": {
                    "charactersRootCategoryId": 42770,
                    "characters": []
                }
            }
        }

        categories = [
            CategoryProbeRecord(id_row=46924, name="Camille", icon_url="https://images.gamebanana.com/ico/camille.png")
        ]

        diff = diff_categories_against_manifest(categories, manifest, "Endfield")
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0].name, "Camille")

    def test_crop_transparent_render_produces_256x256_rgba(self):
        # Create dummy transparent 800x1200 RGBA image
        img = Image.new("RGBA", (800, 1200), (0, 0, 0, 0))
        # Face at x=350, y=200, w=100, h=100
        face_box = (350, 200, 100, 100)

        cropped = crop_and_resize_face(img, face_box, target_size=(256, 256))

        self.assertEqual(cropped.size, (256, 256))
        self.assertEqual(cropped.mode, "RGBA")

    def test_manifest_schema_and_sha256(self):
        # Generate dummy 256x256 PNG
        img = Image.new("RGBA", (256, 256), (255, 0, 0, 255))
        buf = BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        expected_sha = hashlib.sha256(png_bytes).hexdigest()

        record = generate_manifest_record(
            name="Camille",
            category_id=46924,
            game_name="Endfield",
            png_bytes=png_bytes,
            quality="official_avatar"
        )

        self.assertEqual(record["internalName"], "camille")
        self.assertEqual(record["displayName"], "Camille")
        self.assertEqual(record["gameBananaCategoryId"], 46924)
        self.assertEqual(record["imagePath"], "Images/Endfield/camille.png")
        self.assertIn("https://raw.githubusercontent.com/MotefiBranch/JASM-Assets/main/Images/Endfield/camille.png", record["imageUrl"])
        self.assertEqual(record["sha256"], expected_sha)
        self.assertEqual(record["assetQuality"], "official_avatar")


if __name__ == "__main__":
    unittest.main()
