import asyncio
import os
import tempfile

import edge_tts
import lameenc
import miniaudio
import numpy as np

VOICE = os.environ.get("TTS_VOICE", "en-KE-AsiliaNeural")

THRESH = 0.005
MIN_PAUSE = 0.40
TARGET_GAP = 0.35
KEEP_LEAD = 0.12
KEEP_TAIL = 0.20
BITRATE = 128


def trim_sentence_gaps(pcm: np.ndarray, sr: int) -> np.ndarray:
    """Collapse the ~0.94s pure-zero silences edge-tts inserts at every
    full stop down to a natural ~0.35s gap. Words (and their natural
    onsets) are left untouched."""
    silent = np.abs(pcm) < THRESH
    n = len(pcm)

    pauses = []
    i = 0
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            dur = (j - i) / sr
            if dur >= MIN_PAUSE:
                pauses.append((i, j, dur))
            i = j
        else:
            i += 1

    keep = np.ones(n, dtype=bool)
    for idx, (s, e, dur) in enumerate(pauses):
        is_last = idx == len(pauses) - 1
        if is_last and e >= n - 1:
            new_e = s + int(0.10 * sr)
            keep[new_e:e] = False
            continue
        lead = min(int(KEEP_LEAD * sr), int((e - s) * 0.4))
        tail = min(int(KEEP_TAIL * sr), int((e - s) * 0.5))
        keep_total = int(TARGET_GAP * sr)
        new_e = s + keep_total - tail
        if new_e <= s + lead:
            new_e = s + lead
        if new_e > e - tail:
            new_e = e - tail
        if new_e < e:
            keep[new_e:e] = False

    return pcm[keep]


async def synth(text: str, voice: str = VOICE) -> bytes:
    """Synthesize text with edge-tts, trim sentence-gap silences, return MP3 bytes."""
    com = edge_tts.Communicate(text, voice)
    chunks = []
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    raw = b"".join(chunks)

    if not raw:
        raise RuntimeError("edge-tts returned no audio")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        d = miniaudio.decode_file(
            tmp_path, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1
        )
    finally:
        os.unlink(tmp_path)

    sr = d.sample_rate
    x = np.asarray(d.samples, dtype=np.float32)
    y = trim_sentence_gaps(x, sr)

    pcm16 = (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)
    enc = lameenc.Encoder()
    enc.set_bit_rate(BITRATE)
    enc.set_in_sample_rate(sr)
    enc.set_channels(1)
    enc.set_quality(4)
    out = bytearray()
    out += enc.encode(pcm16.tobytes())
    out += enc.flush()
    return bytes(out)


def synth_sync(text: str) -> bytes:
    return asyncio.run(synth(text))
