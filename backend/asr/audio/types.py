"""Audio metadata types shared by core and API (EXECUTION_LOG §3.2). Frozen contract."""

from typing import Literal

from pydantic import BaseModel, Field

ChannelPolicy = Literal["mono", "downmix", "per_channel"]
"""How a recording's channels are fed to recognition.

- ``mono``: single-channel source, used as is.
- ``downmix``: channels carry the same scene (correlated stereo); average them.
- ``per_channel``: channels carry different parties (telephony); recognise each channel separately.
"""


class ProbeInfo(BaseModel):
    """Container / stream metadata of one audio stream, as reported by the bundled FFmpeg."""

    container: str = Field(description="Container format name, e.g. 'mov,mp4,m4a,3gp,3g2,mj2'.")
    codec: str = Field(description="Audio codec name, e.g. 'aac'.")
    duration: float | None = Field(default=None, ge=0, description="Seconds; None if unknown.")
    sample_rate: int = Field(ge=0, description="Hz.")
    channels: int = Field(ge=0)
    bit_rate: int | None = Field(
        default=None, ge=0, description="Bits per second; None if unknown."
    )
    stream_index: int = Field(ge=0, description="Index among the container's audio streams.")
    audio_streams: int = Field(ge=0, description="Number of audio streams in the container.")
    size: int = Field(ge=0, description="File size in bytes.")
    channel_policy: ChannelPolicy = Field(
        description="Policy auto-detected by the channel-correlation heuristic."
    )
    channel_correlation: float | None = Field(
        default=None, description="Inter-channel correlation; only set for 2-channel streams."
    )


class ResolvedChannels(BaseModel):
    """Concrete channel handling after combining ``ProbeInfo`` with the job's ``ChannelsOption``."""

    mode: Literal["mono", "downmix", "per_channel", "select"]
    channel_ids: list[int] = Field(
        description="mono/downmix -> [0]; per_channel -> [0, 1]; select -> [k]."
    )
