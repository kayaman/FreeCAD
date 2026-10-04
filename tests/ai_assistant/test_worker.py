# SPDX-License-Identifier: LGPL-2.1-or-later
import io
from pathlib import Path
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/Mod/AIAssistant"))
from freecad_ai.validation import content_digest
from freecad_ai.worker import BEGIN, END, PROTOCOL, WorkerError, decode_message, encode_message


def archive(data, timestamp):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr(zipfile.ZipInfo("Persistence.xml", date_time=timestamp), data)
    return buffer.getvalue()


class DigestTests(unittest.TestCase):
    def test_identical_values_dumped_at_different_times_match(self):
        first = archive(b"<Property value='40'/>", (2026, 10, 3, 21, 0, 0))
        later = archive(b"<Property value='40'/>", (2026, 10, 3, 21, 0, 4))
        self.assertNotEqual(first, later)
        self.assertEqual(content_digest(first), content_digest(later))

    def test_changed_values_differ(self):
        when = (2026, 10, 3, 21, 0, 0)
        self.assertNotEqual(content_digest(archive(b"40", when)),
                            content_digest(archive(b"41", when)))

    def test_non_archive_content_is_hashed_directly(self):
        self.assertNotEqual(content_digest(b"a"), content_digest(b"b"))


class FramingTests(unittest.TestCase):
    def test_roundtrip_ignores_banners(self):
        payload = {"protocol": PROTOCOL, "ok": True, "delta": {"added": []}}
        output = b"FreeCAD 1.1 banner\n" + encode_message(payload) + b"\nFreeCAD exits\n"
        self.assertEqual(decode_message(output), payload)

    def test_missing_incomplete_or_foreign_results_fail(self):
        for output in (b"no result", BEGIN + b"{}", BEGIN + b"[1]" + END,
                       BEGIN + b'{"protocol": 99}' + END, BEGIN + b"broken" + END):
            with self.assertRaises(WorkerError):
                decode_message(output)

    def test_size_limit(self):
        with self.assertRaises(WorkerError):
            decode_message(encode_message({"protocol": PROTOCOL, "x": "y" * 100}), limit=50)


if __name__ == "__main__":
    unittest.main()
