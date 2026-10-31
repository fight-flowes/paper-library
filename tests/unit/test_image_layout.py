import struct
import pytest

from paper_library.web.images import image_dimensions


@pytest.mark.parametrize('data,expected',[
    (b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',1200,800),(1200,800)),
    (b'GIF89a'+struct.pack('<HH',320,240),(320,240)),
    (b'\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x07\x08'+struct.pack('>HH',600,900),(900,600)),
    (b'RIFF'+b'\x00'*4+b'WEBPVP8X'+b'\x00'*8+(639).to_bytes(3,'little')+(479).to_bytes(3,'little'),(640,480)),
    (b'not an image',None),
    (b'\xff\xd8\xff\xc0\x00',None),
    (b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',0,800),None),
])
def test_raster_headers_reserve_space_and_reject_invalid_data(tmp_path,data,expected):
    path=tmp_path/'image.png';path.write_bytes(data)
    assert image_dimensions(path)==expected
    path.unlink();assert image_dimensions(path) is None


def test_image_layout_attributes_do_not_modify_saved_segments():
    from paper_library.web.markdown import render_markdown
    source='English text ![figure](images/image.png) with $x+1$.\n'
    original=render_markdown(source,'/asset',translation_slots=True)
    sized=render_markdown(source,'/asset',translation_slots=True,image_size=lambda _:(600,400))
    assert 'width="600" height="400"' in sized.html
    assert original.segments==sized.segments
