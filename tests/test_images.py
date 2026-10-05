"""Image transform parity with the browser uploader.

This does not compare against a hand-copied formula -- it extracts the
arithmetic out of the shipped js/replay-upload.js and runs it in node, so if
the site's crop or width cap ever changes, this test fails instead of the
OCR corpus quietly drifting.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader.images import (
    MAX_W, _jsround, transform, transform_geometry, inspect, looks_like_screenshot,
    pillarbox_bounds, BAR_MIN_ASPECT, BAR_LUMA, BAR_ROW_STRIDE,
)

# The browser-parity tests compare images.py against the website's own upload
# code (js/replay-upload.js), which lives in the site's repository, not here.
# The site's deploy gate clones this repo and runs them with GD_SITE_JS set,
# so a change on the site that would break this app's uploads fails there.
SITE_JS = os.environ.get("GD_SITE_JS", "")

SIZES = [(1920, 1080), (2560, 1440), (3840, 2160), (1600, 900), (1366, 768),
         (3440, 1440), (1800, 1013), (1801, 1013), (800, 600), (5120, 2880)]
# (W, H, bar_x0, bar_w): ultrawide frames after the black side bars are found
BARRED = [(5120, 1440, 883, 3354), (3840, 1080, 960, 1920), (5120, 1440, 0, 5120)]


def extract_js_formula():
    """Pull the crop/scale arithmetic out of the site's shrinkImage()."""
    with open(SITE_JS, "r", encoding="utf-8") as f:
        src = f.read()
    want = [
        r"var cx = (bx\.x0 \+ Math\.round\(bx\.w \* [\d.]+\));",
        r"var cy = (Math\.round\(img\.naturalHeight \* [\d.]+\));",
        r"var cw = (Math\.round\(bx\.w \* [\d.]+\));",
        r"var ch = (Math\.round\(img\.naturalHeight \* [\d.]+\));",
        r"var scale = (Math\.min\(1, Math\.max\(maxW / cw, \(maxW \* 9 / 16\) / ch\)\));",
        r"cv2\.width = (Math\.round\(cw \* scale\));",
        r"cv2\.height = (Math\.round\(ch \* scale\));",
    ]
    out = []
    for pat in want:
        m = re.search(pat, src)
        if not m:
            return None
        out.append(m.group(1))
    return out


class TestBrowserParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SITE_JS):
            raise unittest.SkipTest("set GD_SITE_JS to the site's js/replay-upload.js to run")
        cls.node = shutil.which("node")
        cls.formula = extract_js_formula()

    def test_the_site_still_has_the_shape_we_replicate(self):
        self.assertIsNotNone(
            self.formula,
            "js/replay-upload.js shrinkImage() no longer matches the arithmetic "
            "this app replicates -- re-check images.py before shipping")

    def test_width_cap_is_still_1800(self):
        with open(SITE_JS, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("shrinkImage(s, 1800, 0.85)", src,
                      "the site's width cap / quality changed; update images.py")
        self.assertEqual(MAX_W, 1800)

    def test_output_dimensions_match_node(self):
        if not self.node:
            self.skipTest("node not available")
        cx_e, cy_e, cw_e, ch_e, sc_e, w_e, h_e = self.formula
        cases = [[w, h, 0, w] for (w, h) in SIZES] + [list(b) for b in BARRED]
        js = """
const cases = %s;
const out = cases.map(([W,H,X0,BW]) => {
  const img = {naturalWidth: W, naturalHeight: H};
  const bx = {x0: X0, w: BW};
  const maxW = 1800;
  const cx = %s, cy = %s, cw = %s, ch = %s;
  const scale = %s;
  const cv2 = {};
  cv2.width = %s; cv2.height = %s;
  return [cx, cy, cw, ch, cv2.width, cv2.height];
});
console.log(JSON.stringify(out));
""" % (json.dumps(cases), cx_e, cy_e, cw_e, ch_e, sc_e, w_e, h_e)
        res = subprocess.run([self.node, "-e", js], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        ref = json.loads(res.stdout)
        for (w, h, x0, bw), expect in zip(cases, ref):
            got = list(transform_geometry(w, h, MAX_W, x0, bw))
            self.assertEqual(got, expect, "geometry drift at %dx%d bars %d+%d"
                             % (w, h, x0, bw))

    def test_bar_constants_match_site(self):
        with open(SITE_JS, "r", encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("function pillarboxBounds"):src.index("function shrinkImage")]
        self.assertIn("if (W / H < %s) return full;" % BAR_MIN_ASPECT, fn)
        self.assertIn("0.114 * px[i + 2] > %d)" % BAR_LUMA, fn)
        self.assertIn("y += %d)" % BAR_ROW_STRIDE, fn)
        self.assertIn("Math.round(H * 16 / 9)", fn)
        self.assertIn("if (x1 - x0 > W * 0.9) return full;", fn)
        self.assertIn("var bx = pillarboxBounds(img);", src)

    def test_js_rounding_semantics(self):
        # Python's round() is banker's rounding; JS Math.round is half-up.
        # Getting this wrong is a silent one-pixel drift on half the images.
        self.assertEqual([_jsround(x) for x in (0.5, 1.5, 2.5, 3.5)], [1, 2, 3, 4])


class TestPillarbox(unittest.TestCase):

    def _frame(self, w, h, lit_w, bg=(40, 58, 74)):
        from PIL import Image
        im = Image.new("RGB", (w, h), (10, 10, 10))
        x0 = (w - lit_w) // 2
        im.paste(Image.new("RGB", (lit_w, h), bg), (x0, 0))
        return im, x0

    def test_ultrawide_bars_are_trimmed_to_the_pixel(self):
        im, x0 = self._frame(5120, 1440, 3360)
        self.assertEqual(pillarbox_bounds(im), (x0, 3360))

    def test_narrow_picture_widens_to_16_9(self):
        im, _ = self._frame(5120, 1440, 1200)
        x0, bw = pillarbox_bounds(im)
        self.assertEqual(bw, 2560)
        self.assertEqual(x0, 1280)

    def test_16_9_frame_is_never_trimmed(self):
        # a dark scene with black edges on an ordinary display must not crop
        im, _ = self._frame(3840, 2160, 2800)
        self.assertEqual(pillarbox_bounds(im), (0, 3840))

    def test_all_black_frame_is_left_alone(self):
        im, _ = self._frame(5120, 1440, 0)
        self.assertEqual(pillarbox_bounds(im), (0, 5120))

    def test_ultrawide_output_keeps_a_16_9_height(self):
        # 937246: width-only cap gave 1600x469; the reader needs ~1000 px
        _, _, _, _, ow, oh = transform_geometry(5120, 1440, MAX_W, 883, 3354)
        self.assertGreaterEqual(oh, 1012)


class TestTransform(unittest.TestCase):

    def _img(self, w, h, path):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (w, h), (12, 26, 38))
        d = ImageDraw.Draw(im)
        for i in range(0, w, 97):
            d.line([(i, 0), (i, h)], fill=(255, 209, 102), width=1)
        d.rectangle([w - 40, h - 40, w - 5, h - 5], fill=(255, 255, 255))
        im.save(path)
        return path

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_4k_becomes_1800_wide_jpeg(self):
        p = self._img(3840, 2160, os.path.join(self.tmp, "a.png"))
        data, ext, info = transform(p)
        self.assertEqual(ext, "jpg")
        self.assertEqual((info["out_w"], info["out_h"]), (1800, 1056))
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(im.size, (1800, 1056))
            self.assertEqual(im.format, "JPEG")

    def test_small_capture_is_cropped_but_not_upscaled(self):
        p = self._img(1366, 768, os.path.join(self.tmp, "b.png"))
        data, ext, info = transform(p)
        self.assertEqual((info["out_w"], info["out_h"]),
                         transform_geometry(1366, 768)[4:])
        self.assertLess(info["out_w"], 1366)     # cropped
        self.assertGreater(info["out_w"], 1200)  # not shrunk further

    def test_transform_always_produces_jpg_even_from_webp(self):
        # The browser's fallback can ship a .webp the server's OCR glob
        # ignores. Ours cannot.
        from PIL import Image
        p = os.path.join(self.tmp, "c.webp")
        Image.new("RGB", (2560, 1440), (30, 30, 30)).save(p, format="WEBP")
        data, ext, _ = transform(p)
        self.assertEqual(ext, "jpg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_inspect_flags_small_and_cropped(self):
        p = self._img(1280, 720, os.path.join(self.tmp, "d.png"))
        self.assertTrue(any("wide" in w for w in inspect(p)))
        p2 = self._img(1920, 1080, os.path.join(self.tmp, "e.png"))
        self.assertEqual(inspect(p2, display_aspect=1920 / 1080.0), [])
        p3 = self._img(1900, 400, os.path.join(self.tmp, "f.png"))
        self.assertTrue(any("cropped region" in w
                            for w in inspect(p3, display_aspect=1920 / 1080.0)))

    def test_looks_like_screenshot_rejects_icons(self):
        from PIL import Image
        p = os.path.join(self.tmp, "icon.png")
        Image.new("RGB", (64, 64), (0, 0, 0)).save(p)
        self.assertFalse(looks_like_screenshot(p))
        p2 = self._img(1920, 1080, os.path.join(self.tmp, "g.png"))
        self.assertTrue(looks_like_screenshot(p2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
