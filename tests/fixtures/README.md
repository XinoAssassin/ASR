# Test fixtures

Real recordings used by the backend tests. **Audio files in this directory are git-ignored**
(`.gitignore`: `tests/fixtures/*.m4a|wav|mp3`); only this README is committed. Private meeting audio
must never be committed, not even as a short excerpt.

| File | Source | How to create |
|---|---|---|
| `4people_20s.m4a` | 20 s excerpt (from 60 s) of `ASR_4people-test.m4a`, a 31-min, 4-speaker Chinese meeting recorded as 48 kHz stereo AAC | `cd backend && uv run python scripts/make_fixtures.py` |

`backend/scripts/make_fixtures.py` decodes the excerpt with PyAV and re-encodes it as AAC-LC with the
source's sample rate and channel layout (no host ffmpeg). Options: `--source`, `--start`,
`--duration`, `--force`.

Tests that need a fixture skip when it is absent, so a clean checkout still passes
(EXECUTION_LOG D10). All other test audio is synthesised with PyAV/NumPy in temporary directories
(`backend/tests/test_audio_fixtures.py`).
