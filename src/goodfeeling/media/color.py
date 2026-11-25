"""Colour conversion chains shared by proxies and renders."""

from __future__ import annotations

import logging

from goodfeeling.media.ffmpeg import available_filters
from goodfeeling.schemas.clip import ClipInfo

log = logging.getLogger(__name__)

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


def escape_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def to_rec709(transfer: str | None, lut: str | None) -> list[str]:
    """Filters that bring a source to 8-bit-ready Rec.709 before scaling.

    A LUT (for D-Log M) wins; HLG/PQ sources are tone-mapped when zscale is available;
    everything else is assumed to already be Rec.709 and only loses bit depth later.
    """
    if lut:
        return [f"lut3d=file='{escape_filter_path(lut)}'"]
    if transfer in HDR_TRANSFERS:
        if "zscale" in available_filters() and "tonemap" in available_filters():
            return [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
            ]
        log.warning("source is %s but this ffmpeg lacks zscale/tonemap; colours will be off", transfer)
    return []


def clip_color_chain(clip: ClipInfo, lut: str | None) -> list[str]:
    return to_rec709(clip.color_transfer, lut)


REC709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
