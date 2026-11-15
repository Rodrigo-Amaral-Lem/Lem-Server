# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Bounded evidence validation. Originals are never transcoded.

ffprobe is supplied by the server image. Upload admission serializes expensive
media inspection. Network protocols are disabled, input/output/time are bounded.
"""
import json
import subprocess
import tempfile
from pathlib import Path
from .images import inspect_image, InvalidImage

MAX_BYTES = 60 * 1024 * 1024
MAX_SECONDS = 120.5  # allow container timestamp rounding at the 120 second capture limit


def inspect_evidence(data):
    if not data or len(data) > MAX_BYTES:
        raise InvalidImage("Máximo 60 MiB por vídeo")
    if len(data) < 16 or data[4:8] != b"ftyp":
        if len(data) > 20 * 1024 * 1024:
            raise InvalidImage("Máximo 20 MiB por fotografia")
        return inspect_image(data)
    brand = data[8:12]
    mime = "video/3gpp" if brand.startswith(b"3g") else "video/mp4"
    try:
        with tempfile.TemporaryDirectory(prefix="lem-video-") as directory:
            source = Path(directory) / "evidence.mp4"
            # Close the writer before ffprobe opens the file (required on Windows).
            source.write_bytes(data)
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-show_entries", "format=duration,format_name:stream=codec_type,width,height,codec_name",
                 "-of", "json", str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                check=True,
            )
        if len(result.stdout) > 65536:
            raise ValueError("probe output")
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
        if (not 0 < duration <= MAX_SECONDS or len(videos) != 1
                or not 0 < videos[0].get("width", 0) * videos[0].get("height", 0) <= 8294400
                or videos[0].get("codec_name") not in {"h264", "hevc", "mpeg4", "h263"}
                or "mp4" not in info["format"].get("format_name", "")):
            raise ValueError("unsupported video")
    except FileNotFoundError as error:
        raise InvalidImage("Validação de vídeo indisponível no servidor (ffprobe)") from error
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise InvalidImage("Vídeo inválido: use MP4/3GP, até 120 segundos, 60 MiB e 4K") from error
    return mime
