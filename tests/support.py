"""Locate the launcher module. Tests import it, they do not copy it."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "launcher"))

import satoru  # noqa: E402  (path juggling has to come first)


def has_tomllib():
    try:
        import tomllib  # noqa: F401
        return True
    except ImportError:
        return False


# ----------------------------------------------------------------------------
# a synthetic .exe with an icon resource, for testing satoru's PE parser
# without a real game file. Only what the parser reads is real: DOS stub,
# PE + optional header (PE32 or PE32+), one .rsrc section holding a proper
# RT_GROUP_ICON / RT_ICON resource tree. Everything else about a real exe
# (code, relocations, imports) is left out because nothing reads it.

RT_ICON = 3
RT_GROUP_ICON = 14


def _res_dir_header(id_entries):
    return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, id_entries)


def build_pe_with_icon(bits=32, small=(16, 16, 20), big=(48, 48, 40)):
    """A minimal PE32 or PE32+ file whose .rsrc carries two icon sizes.

    `small` and `big` are (width, height, byte-count-of-fake-image-data).
    Returns (file_bytes, expected_big_image_bytes) — the bytes the parser
    is expected to pick, since the larger of the two by area must win.
    """
    assert bits in (32, 64)
    RSRC_RVA = 0x3000

    # ---- lay out the resource-section blob at fixed offsets -------------
    OFF_ROOT = 0
    OFF_ICON_NAMES = 32          # RT_ICON: name/id level (2 entries)
    OFF_GROUP_NAMES = 64         # RT_GROUP_ICON: name/id level (1 entry)
    OFF_LANG_ICON_SMALL = 88
    OFF_LANG_ICON_BIG = 112
    OFF_LANG_GROUP = 136
    OFF_DATA_ICON_SMALL = 160
    OFF_DATA_ICON_BIG = 176
    OFF_DATA_GROUP = 192
    OFF_GROUPDIR_BYTES = 208
    GROUPDIR_LEN = 6 + 14 * 2
    OFF_IMG_SMALL = OFF_GROUPDIR_BYTES + GROUPDIR_LEN
    OFF_IMG_BIG = OFF_IMG_SMALL + small[2]
    BLOB_LEN = OFF_IMG_BIG + big[2]

    blob = bytearray(BLOB_LEN)

    def put(offset, data):
        blob[offset:offset + len(data)] = data

    # root: two types, RT_ICON(3) and RT_GROUP_ICON(14), ascending by id
    put(OFF_ROOT, _res_dir_header(2))
    put(OFF_ROOT + 16, struct.pack("<II", RT_ICON, 0x80000000 | OFF_ICON_NAMES))
    put(OFF_ROOT + 24, struct.pack("<II", RT_GROUP_ICON, 0x80000000 | OFF_GROUP_NAMES))

    # RT_ICON name/id level: ids 101 (small) and 102 (big)
    put(OFF_ICON_NAMES, _res_dir_header(2))
    put(OFF_ICON_NAMES + 16, struct.pack("<II", 101, 0x80000000 | OFF_LANG_ICON_SMALL))
    put(OFF_ICON_NAMES + 24, struct.pack("<II", 102, 0x80000000 | OFF_LANG_ICON_BIG))

    # RT_GROUP_ICON name/id level: one name, id 1
    put(OFF_GROUP_NAMES, _res_dir_header(1))
    put(OFF_GROUP_NAMES + 16, struct.pack("<II", 1, 0x80000000 | OFF_LANG_GROUP))

    # language level, one language each, pointing at data entries
    put(OFF_LANG_ICON_SMALL, _res_dir_header(1))
    put(OFF_LANG_ICON_SMALL + 16, struct.pack("<II", 0x409, OFF_DATA_ICON_SMALL))
    put(OFF_LANG_ICON_BIG, _res_dir_header(1))
    put(OFF_LANG_ICON_BIG + 16, struct.pack("<II", 0x409, OFF_DATA_ICON_BIG))
    put(OFF_LANG_GROUP, _res_dir_header(1))
    put(OFF_LANG_GROUP + 16, struct.pack("<II", 0x409, OFF_DATA_GROUP))

    # data entries: OffsetToData is a real RVA, not blob-relative
    put(OFF_DATA_ICON_SMALL, struct.pack("<IIII", RSRC_RVA + OFF_IMG_SMALL, small[2], 0, 0))
    put(OFF_DATA_ICON_BIG, struct.pack("<IIII", RSRC_RVA + OFF_IMG_BIG, big[2], 0, 0))
    put(OFF_DATA_GROUP, struct.pack("<IIII", RSRC_RVA + OFF_GROUPDIR_BYTES, GROUPDIR_LEN, 0, 0))

    # GRPICONDIR: header + two GRPICONDIRENTRY
    groupdir = struct.pack("<HHH", 0, 1, 2)
    groupdir += struct.pack("<BBBBHHIH", small[0], small[1], 0, 0, 1, 32, small[2], 101)
    groupdir += struct.pack("<BBBBHHIH", big[0], big[1], 0, 0, 1, 32, big[2], 102)
    put(OFF_GROUPDIR_BYTES, groupdir)

    small_img = (b"S" * small[2])
    big_img = (b"B" * big[2])
    put(OFF_IMG_SMALL, small_img)
    put(OFF_IMG_BIG, big_img)

    blob = bytes(blob)

    # ---- wrap it in a PE ---------------------------------------------------
    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)

    if bits == 32:
        machine = 0x14C
        magic = 0x10B
        dd_off = 96
    else:
        machine = 0x8664
        magic = 0x20B
        dd_off = 112

    optional = bytearray(dd_off + 16 * 8)
    struct.pack_into("<H", optional, 0, magic)
    struct.pack_into("<II", optional, dd_off + 2 * 8, RSRC_RVA, len(blob))

    file_header = struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, len(optional), 0x0102)

    headers_before_section = bytes(dos) + b"PE\0\0" + file_header + bytes(optional)
    praw = len(headers_before_section) + 40  # + the one section header

    section = struct.pack(
        "<8sIIIIIIHH",
        b".rsrc\0\0\0", len(blob), RSRC_RVA, len(blob), praw, 0, 0, 0, 0,
    ) + struct.pack("<I", 0x40000040)
    # struct.pack with trailing Characteristics appended separately above;
    # section header is 40 bytes total: 8+4*5+2*2+4 = 40.

    data = headers_before_section + section + blob
    assert len(data) == praw + len(blob)
    return bytes(data), big_img
