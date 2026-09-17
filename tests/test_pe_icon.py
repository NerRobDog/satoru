"""Reading a Windows exe's own icon, in pure stdlib.

A pack may point at the game's exe (`[paths] icon_exe`) so its .app gets the
game's own icon instead of the generic one. The exe carries a whole family of
sizes under RT_GROUP_ICON / RT_ICON; satoru keeps only the largest, because
that is the one macOS's own `sips` uses when handed an .ico with several
images anyway.

No real game file is needed here: `support.build_pe_with_icon` builds a
minimal PE32 or PE32+ with a synthetic resource tree.
"""
import struct
import tempfile
import unittest

from support import satoru, build_pe_with_icon


def _write(tmp_bytes):
    fh = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
    fh.write(tmp_bytes)
    fh.close()
    return fh.name


class PickingTheBiggestIcon(unittest.TestCase):
    def _check(self, bits):
        data, expected_big = build_pe_with_icon(bits=bits)
        path = _write(data)
        try:
            ico = satoru.pe_best_icon_ico(path)
        finally:
            import os
            os.remove(path)
        self.assertIsNotNone(ico, "a well-formed resource tree must yield an .ico")
        reserved, itype, count = struct.unpack_from("<HHH", ico, 0)
        self.assertEqual((reserved, itype, count), (0, 1, 1),
                          "a single-image ICONDIR header")
        bw, bh, colors, res, planes, bitcount, byte_count, offset = \
            struct.unpack_from("<BBBBHHII", ico, 6)
        self.assertEqual((bw, bh), (48, 48), "must pick the larger of the two sizes")
        self.assertEqual(byte_count, len(expected_big))
        self.assertEqual(ico[offset:offset + byte_count], expected_big,
                          "the image bytes must be copied verbatim")

    def test_pe32(self):
        self._check(32)

    def test_pe32_plus(self):
        self._check(64)

    def test_the_256_sentinel_is_read_as_256(self):
        # width/height 0 in a GRPICONDIRENTRY means 256, not 0 - a naive
        # reader that compares raw bytes would treat 0 as tiny and lose it.
        data, expected_big = build_pe_with_icon(bits=32, small=(16, 16, 8),
                                                 big=(0, 0, 12))
        path = _write(data)
        try:
            ico = satoru.pe_best_icon_ico(path)
        finally:
            import os
            os.remove(path)
        bw, bh = struct.unpack_from("<BB", ico, 6)
        self.assertEqual((bw, bh), (0, 0), "256 is stored back as the 0 sentinel")


class MalformedOrMissingInput(unittest.TestCase):
    def test_not_a_pe_file_returns_none(self):
        path = _write(b"not an exe at all, just some bytes")
        try:
            self.assertIsNone(satoru.pe_best_icon_ico(path))
        finally:
            import os
            os.remove(path)

    def test_missing_file_returns_none(self):
        self.assertIsNone(satoru.pe_best_icon_ico("/no/such/file.exe"))

    def test_pe_with_no_resources_returns_none(self):
        data, _ = build_pe_with_icon(bits=32)
        # DOS stub (0x40) + "PE\0\0" (4) + IMAGE_FILE_HEADER (20) puts the
        # optional header at 0x44; its data directory starts at +96, and the
        # resource entry (index 2) is 16 bytes into that - zero it out so
        # the file has no resource directory at all, same as a real exe
        # built without any icon.
        opt_off = 0x40 + 4 + 20
        resource_entry_off = opt_off + 96 + 16
        zeroed = bytearray(data)
        zeroed[resource_entry_off:resource_entry_off + 8] = b"\0" * 8
        path = _write(bytes(zeroed))
        try:
            self.assertIsNone(satoru.pe_best_icon_ico(path))
        finally:
            import os
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
