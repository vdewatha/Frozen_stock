import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("kraken_ranges", Path(__file__).parents[1] / "scripts/download_kraken_hourly.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RangeTests(unittest.TestCase):
    def test_selective_zip_read_crc_and_total_budget(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("XBTUSD_60.csv", "1,2,3,4,5,6,7\n")
            archive.writestr("other.csv", "other")
        data = buffer.getvalue()
        def request(url, *, limit, byte_range=None):
            start, end = map(int, byte_range.split("-"))
            result = data[start:end + 1]
            self.assertLessEqual(len(result), limit)
            return 206, {"content-range": f"bytes {start}-{end}/{len(data)}"}, result
        with patch.object(module, "request", side_effect=request):
            remote = module.RemoteZip("https://example.invalid")
            with zipfile.ZipFile(remote) as archive:
                self.assertEqual(archive.read("XBTUSD_60.csv"), b"1,2,3,4,5,6,7\n")
            remote.transferred = 127 * 1024 * 1024
            remote.tail = None
            remote.seek(0)
            with self.assertRaises(ValueError): remote.read(1)

    def test_ignored_range_and_wrong_offsets_fail_closed(self):
        with patch.object(module, "request", return_value=(200, {}, b"x")):
            with self.assertRaises(ValueError): module.RemoteZip("https://example.invalid")
        with patch.object(module, "request", side_effect=[
            (206, {"content-range": "bytes 0-0/100"}, b"x"),
            (206, {"content-range": "bytes 1-2/100"}, b"xx")]):
            remote = module.RemoteZip("https://example.invalid")
            with self.assertRaises(ValueError): remote.read(2)
