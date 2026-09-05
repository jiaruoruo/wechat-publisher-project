"""tests for models/image_sizes.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.image_sizes import closest_size, DALLE_VALID_SIZES, DASHSCOPE_VALID_SIZES


class TestClosestSize(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(closest_size("1024x1024", DALLE_VALID_SIZES, "DALL-E 3"), "1024x1024")

    def test_cover_ratio_maps_to_wide(self):
        # 900x383 ≈ 2.35:1，最接近的 DALL-E 尺寸是 1792x1024（1.75:1）
        self.assertEqual(closest_size("900x383", DALLE_VALID_SIZES, "DALL-E 3"), "1792x1024")

    def test_dashscope_4_3_maps_to_square(self):
        # 1024x768 = 4:3，dashscope 最接近的是 1024*1024（1:1）
        self.assertEqual(closest_size("1024x768", DASHSCOPE_VALID_SIZES, "DashScope"), "1024*1024")

    def test_asterisk_format_accepted(self):
        self.assertEqual(closest_size("1024*1024", DASHSCOPE_VALID_SIZES, "DashScope"), "1024*1024")

    def test_invalid_size_falls_back_to_first(self):
        self.assertEqual(closest_size("abc", DALLE_VALID_SIZES, "DALL-E 3"), "1024x1024")

    def test_zero_height_falls_back_to_first(self):
        self.assertEqual(closest_size("100x0", DALLE_VALID_SIZES, "DALL-E 3"), "1024x1024")


if __name__ == "__main__":
    unittest.main()
