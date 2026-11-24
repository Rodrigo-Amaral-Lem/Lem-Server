# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Exercise admission with real encoded media, not fabricated ffprobe responses."""
import shutil
import subprocess

import pytest

from app.images import InvalidImage
from app.media import inspect_evidence


@pytest.mark.parametrize(
    "source,options,suffix,mime",
    [
        pytest.param("color=s=64x64:r=1:d=1",
                     ["-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1:log-level=error"],
                     ".mp4", "video/mp4", id="hevc"),
        pytest.param("color=s=128x96:r=1:d=1", ["-c:v", "h263"],
                     ".3gp", "video/3gpp", id="3gp-h263"),
        pytest.param("color=s=64x64:r=1:d=1",
                     ["-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac"],
                     ".mp4", "video/mp4", id="h264-with-audio"),
        pytest.param("color=s=3840x2160:r=1:d=1", ["-c:v", "libx264", "-preset", "ultrafast"],
                     ".mp4", "video/mp4", id="uhd-4k"),
        pytest.param("color=s=64x64:r=1:d=120", ["-c:v", "libx264"],
                     ".mp4", "video/mp4", id="120-seconds"),
        pytest.param("color=s=64x64:r=1:d=121", ["-c:v", "libx264"],
                     ".mp4", None, id="over-120-seconds"),
        pytest.param("color=s=4096x2304:r=1:d=1", ["-c:v", "libx264", "-preset", "ultrafast"],
                     ".mp4", None, id="over-4k"),
        pytest.param("color=s=64x64:r=1:d=1", ["-c:v", "libvpx-vp9"],
                     ".mp4", None, id="unsupported-vp9"),
        pytest.param("color=s=64x64:r=1:d=1", ["-map", "0:v", "-map", "0:v", "-c:v", "libx264"],
                     ".mp4", None, id="two-video-streams"),
        pytest.param("sine=duration=1", ["-c:a", "aac"],
                     ".mp4", None, id="audio-only"),
    ],
)
def test_real_video_formats_and_limits(tmp_path, source, options, suffix, mime):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required (included in the production image)")
    target = tmp_path / ("evidence" + suffix)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", source,
         *options, "-threads", "1", str(target)],
        check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    data = target.read_bytes()
    if mime:
        assert inspect_evidence(data) == mime
    else:
        with pytest.raises(InvalidImage):
            inspect_evidence(data)


def test_faststart_video_must_include_the_referenced_samples(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required (included in the production image)")
    target = tmp_path / "faststart.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=128x96:r=25:d=3",
         "-c:v", "libx264", "-threads", "1", "-movflags", "+faststart", str(target)],
        check=True, timeout=30,
    )
    data = target.read_bytes()
    assert inspect_evidence(data) == "video/mp4"
    media_box = data.index(b"mdat") - 4
    assert data.index(b"moov") < media_box
    partial = data[:(len(data) + media_box) // 2]
    with pytest.raises(InvalidImage):
        inspect_evidence(partial)
    # A plausible box length cannot replace the video samples referenced by moov.
    adjusted = partial[:media_box] + (len(partial) - media_box).to_bytes(4, "big") + partial[media_box + 4:]
    with pytest.raises(InvalidImage):
        inspect_evidence(adjusted)
