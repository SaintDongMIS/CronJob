"""tobim_copy_images_gps：SSE 解析與 complete 計數（無網路）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from tobim_copy_images_gps import (  # noqa: E402
    _copied_counts_from_raw,
    _counts_from_complete,
    _parse_sse_payload,
)


class TestSseParsing(unittest.TestCase):
    def test_complete_without_progress_payload_is_still_parseable(self) -> None:
        small = '{"type":"complete","copiedImages":[],"gpsEntries":[]}'
        data = _parse_sse_payload(small)
        assert data is not None
        images, gps = _counts_from_complete(data)
        self.assertEqual(images, 0)
        self.assertEqual(gps, 0)

    def test_oversized_complete_falls_back_to_raw_count(self) -> None:
        raw = (
            '{"type":"complete","copiedImages":["a.jpg","b.jpg"],'
            '"gpsEntries":["g1","g2","g3"]}'
        )
        data = _parse_sse_payload(raw)
        assert data is not None
        self.assertEqual(data.get("type"), "complete")
        images, gps = _counts_from_complete(data)
        self.assertEqual(images, 2)
        self.assertEqual(gps, 3)

    def test_truncated_complete_json_keeps_raw_marker(self) -> None:
        raw = '{"type":"complete","copiedImages":["x.jpg","y.jpg"'
        data = _parse_sse_payload(raw)
        assert data is not None
        self.assertIn("_raw", data)
        images, _gps = _copied_counts_from_raw(data["_raw"])
        self.assertEqual(images, 2)


if __name__ == "__main__":
    unittest.main()
