"""Generate the original two-glyph TrueType fixture using only the standard library.

No font is copied: .notdef is empty and U+0041 is one triangular contour.
Prints base64; this generator is not imported by the browser fixture runtime.
"""
import base64
import struct


def u16(*values):
    return struct.pack('>' + 'H' * len(values), *values)


def s16(*values):
    return struct.pack('>' + 'h' * len(values), *values)


def u32(*values):
    return struct.pack('>' + 'I' * len(values), *values)


def checksum(data):
    data += b'\0' * (-len(data) % 4)
    return sum(struct.unpack('>' + 'I' * (len(data) // 4), data)) & 0xffffffff


def build():
    # Original coordinates: (0,0), (300,700), (600,0).
    empty = s16(0, 0, 0, 0, 0)
    triangle = (s16(1, 0, 0, 600, 700) + u16(2, 0) + bytes([1, 1, 1])
                + s16(0, 300, 300, 0, 700, -700) + b'\0')
    labels = {1: 'Original Hub Fixture', 2: 'Regular', 4: 'Original Hub Fixture Regular',
              6: 'OriginalHubFixture-Regular'}
    records, strings = b'', b''
    for identity, label in sorted(labels.items()):
        raw = label.encode('utf-16-be')
        records += u16(3, 1, 0x409, identity, len(raw), len(strings))
        strings += raw
    tables = {
        'head': (u32(0x10000, 0x10000, 0, 0x5f0f3cf5) + u16(3, 1000)
                 + b'\0' * 16 + s16(0, 0, 600, 700) + u16(0, 8) + s16(2, 0, 0)),
        'hhea': (u32(0x10000) + s16(800, -200, 0) + u16(600)
                 + s16(0, 0, 600, 1, 0, 0, 0, 0, 0, 0, 0) + u16(2)),
        'maxp': u32(0x10000) + u16(2, 3, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),
        'hmtx': u16(600, 0, 600, 0),
        'glyf': empty + triangle,
        'loca': u16(0, len(empty) // 2, (len(empty) + len(triangle)) // 2),
        'cmap': (u16(0, 1, 3, 1) + u32(12) + u16(4, 32, 0, 4, 4, 1, 0)
                 + u16(65, 0xffff, 0, 65, 0xffff) + s16(-64, 1) + u16(0, 0)),
        'name': u16(0, len(labels), 6 + 12 * len(labels)) + records + strings,
        'post': u32(0x30000, 0) + s16(0, 0) + u32(0, 0, 0, 0, 0),
        'OS/2': (u16(0) + s16(600) + u16(400, 5, 0) + s16(*([0] * 11))
                 + bytes(10) + u32(1, 0, 0, 0) + b'EHFX' + u16(64, 65, 65)
                 + s16(800, -200, 0) + u16(800, 200)),
    }
    count = len(tables)
    offset = 12 + count * 16
    directory, payload = b'', b''
    head_offset = None
    for name, data in sorted(tables.items()):
        directory += name.encode() + u32(checksum(data), offset, len(data))
        if name == 'head':
            head_offset = offset
        padded = data + bytes(-len(data) % 4)
        payload += padded
        offset += len(padded)
    font = bytearray(u32(0x10000) + u16(count, 128, 3, count * 16 - 128) + directory + payload)
    struct.pack_into('>I', font, head_offset + 8, (0xb1b0afba - checksum(bytes(font))) & 0xffffffff)
    assert checksum(bytes(font)) == 0xb1b0afba
    return bytes(font)


if __name__ == '__main__':
    print(base64.b64encode(build()).decode())
