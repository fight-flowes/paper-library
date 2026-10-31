"""Read existing raster image headers to reserve layout space; never opens PDFs."""
from functools import lru_cache
import struct


def image_dimensions(path):
    try:
        stat = path.stat()
        return _dimensions(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, struct.error):
        return None


@lru_cache(maxsize=2048)
def _dimensions(path, modified, size):
    with open(path, 'rb') as stream:
        data = stream.read(32)
        dimensions = None
        if data.startswith(b'\x89PNG\r\n\x1a\n') and data[12:16] == b'IHDR' and len(data) >= 24:
            dimensions = struct.unpack('>II', data[16:24])
        elif data[:6] in {b'GIF87a', b'GIF89a'} and len(data) >= 10:
            dimensions = struct.unpack('<HH', data[6:10])
        elif data.startswith(b'\xff\xd8'):
            stream.seek(2)
            while stream.tell() < min(size, 1024 * 1024):
                if stream.read(1) != b'\xff':
                    break
                marker = stream.read(1)
                while marker == b'\xff':
                    marker = stream.read(1)
                if not marker or marker in {b'\xda', b'\xd9'}:
                    break
                if marker == b'\x01' or b'\xd0' <= marker <= b'\xd7':
                    continue
                length = struct.unpack('>H', stream.read(2))[0]
                if length < 2 or stream.tell() + length - 2 > size:
                    break
                if marker in {bytes([n]) for n in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf)}:
                    height, width = struct.unpack('>HH', stream.read(5)[1:5])
                    dimensions = (width, height)
                    break
                stream.seek(length - 2, 1)
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if data[12:16] == b'VP8X' and len(data) >= 30:
                dimensions = (int.from_bytes(data[24:27], 'little') + 1, int.from_bytes(data[27:30], 'little') + 1)
            elif data[12:16] == b'VP8L' and len(data) >= 25 and data[20] == 0x2f:
                bits = int.from_bytes(data[21:25], 'little')
                dimensions = ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
            elif data[12:16] == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a' and len(data) >= 30:
                width, height = struct.unpack('<HH', data[26:30]);dimensions = (width & 0x3fff, height & 0x3fff)
        if dimensions and all(0 < n <= 100000 for n in dimensions):
            return dimensions
    return None
