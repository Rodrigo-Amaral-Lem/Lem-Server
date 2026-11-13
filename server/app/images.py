# Photograph validation shared by the upload endpoint and its tests.
# Image.verify() alone accepts truncated JPEG data, and a damaged PNG raises
# SyntaxError there. Decode the pixels once, so a stored photograph is always
# one that the PDF/ZIP export can render later.

from io import BytesIO
import struct
from PIL import Image

MAX_PIXELS = 40000000
IMAGE_ERRORS = (OSError, SyntaxError, ValueError, struct.error, Image.DecompressionBombError)


class InvalidImage(ValueError):
    """The uploaded bytes are not an acceptable, fully decodable JPEG/PNG."""


def inspect_image(data):
    """Return the MIME type of a complete JPEG/PNG or raise InvalidImage."""
    try:
        with Image.open(BytesIO(data), formats=["JPEG", "PNG"]) as image:
            kind = image.format
            if kind not in ("JPEG", "PNG"):
                raise InvalidImage("Fotografia deve ser JPEG ou PNG")
            if image.width * image.height > MAX_PIXELS:
                raise InvalidImage("Imagem excede 40 MP")
            image.verify()
        # verify() invalidates the object and does not decode image data.
        with Image.open(BytesIO(data), formats=[kind]) as image:
            if kind == "JPEG":
                image.draft("RGB", (160, 160))
            image.load()
    except InvalidImage:
        raise
    except IMAGE_ERRORS as error:
        raise InvalidImage("Imagem inválida") from error
    return "image/jpeg" if kind == "JPEG" else "image/png"
