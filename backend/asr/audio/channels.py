"""Channel-policy heuristic and resolution (PLAN §4.5 [0b]). Owned by ``backend-core`` (M1)."""

from pathlib import Path

from asr.audio.types import ChannelPolicy, ProbeInfo, ResolvedChannels
from asr.providers.base import ChannelsOption


def detect_channel_policy(
    path: Path, stream_index: int = 0, *, window_s: float = 120.0
) -> tuple[ChannelPolicy, float | None]:
    """Return the detected policy and, for 2-channel audio, the inter-channel correlation."""
    raise NotImplementedError


def resolve_channels(probe: ProbeInfo, option: ChannelsOption) -> ResolvedChannels:
    """Combine probed metadata with the job's channel option."""
    raise NotImplementedError
