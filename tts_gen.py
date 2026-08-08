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
KEEP_START_SILENCE = 0.02
BITRATE = 128

DEFAULT_RATE = "+0%"
DEFAULT_PITCH = "+0Hz"
DEFAULT_VOLUME = "+0%"


def trim_sentence_gaps(pcm: np.ndarray, sr: int) -> np.ndarray:
    """Collapse the ~0.94s pure-zero silences edge-tts inserts at every
    full stop down to a natural ~0.35s gap. Words (and their natural
    onsets) are left untouched. The final trailing silence is reduced to
    ~0.10s so an explicit <break> can be spliced in precisely afterwards."""
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
        # The very first pause starts at index 0: that is the utterance's
        # natural leading silence plus the (quiet) onset of its first word,
        # NOT an inter-sentence gap. Collapsing it here slices the first
        # consonant (e.g. "Hello" -> "ello"). Leave it untouched; the caller
        # decides whether to trim the lead via trim_leading_silence.
        if s == 0:
            continue
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


def trim_leading_silence(
    pcm: np.ndarray, sr: int, keep_s: float = KEEP_START_SILENCE
) -> np.ndarray:
    """Remove the near-silent run edge-tts prepends to every utterance
    (often ~0.3s). If left in place, each segment's lead silence sits on top
    of the previous segment's requested break, so the real gap reads ~0.3s
    longer than asked and the next sentence's first word sounds late."""
    if len(pcm) == 0:
        return pcm
    silent = np.abs(pcm) < THRESH
    n = len(pcm)
    i = 0
    while i < n and silent[i]:
        i += 1
    start = max(0, i - int(keep_s * sr))
    if start == 0:
        return pcm
    return pcm[start:]


def trailing_silence(pcm: np.ndarray, sr: int) -> float:
    """Duration (seconds) of the near-silent run at the very end of pcm."""
    if len(pcm) == 0:
        return 0.0
    silent = np.abs(pcm) < THRESH
    n = len(pcm)
    i = n
    while i > 0 and silent[i - 1]:
        i -= 1
    return (n - i) / sr


def pad_to_gap(pcm: np.ndarray, sr: int, break_ms: int) -> np.ndarray:
    """Pad pcm so the total inter-segment gap equals exactly break_ms."""
    target = break_ms / 1000.0
    trail = trailing_silence(pcm, sr)
    need = target - trail
    if need > 0:
        pad = np.zeros(int(round(need * sr)), dtype=np.float32)
        return np.concatenate([pcm, pad])
    return pcm


async def synth_segment(
    text: str,
    voice: str = VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    volume: str = DEFAULT_VOLUME,
    trim_lead: bool = True,
) -> tuple:
    """Synthesize one plain-text segment, trim natural sentence gaps,
    return (pcm float32 mono, sample_rate)."""
    com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    chunks = []
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    raw = b"".join(chunks)

    if not raw:
        raise RuntimeError("edge-tts returned no audio for segment")

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
    x = trim_sentence_gaps(x, sr)
    if trim_lead:
        x = trim_leading_silence(x, sr)
    return x, sr


async def synth_segments(segments: list, voice: str = VOICE) -> bytes:
    """Synthesize an ordered list of segments and splice them into one MP3.

    Each segment dict: {"text", "rate", "pitch", "volume", "break_after_ms"}.
    A break_after_ms > 0 inserts that many milliseconds of silence (on top of
    the trimmed natural sentence pause) before the next segment, matching the
    <break time="..."/> pacing the Edge service itself refuses to support.
    """
    master = None
    sr = None

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # First segment keeps its full natural leading silence: edge-tts
        # prepends ~0.2-0.3s of true silence, and the amplitude threshold in
        # trim_leading_silence can slice the attack of the very first word
        # (e.g. "Hey" -> "ey"). A short natural lead at the start of the audio
        # is fine — there is no previous break to overrun. Later segments keep
        # the aggressive trim so explicit inter-segment breaks stay exact.
        x, s = await synth_segment(
            text,
            voice,
            rate=seg.get("rate", DEFAULT_RATE),
            pitch=seg.get("pitch", DEFAULT_PITCH),
            volume=seg.get("volume", DEFAULT_VOLUME),
            trim_lead=master is not None,
        )
        if sr is None:
            sr = s

        break_ms = int(seg.get("break_after_ms", 0) or 0)
        if break_ms > 0:
            x = pad_to_gap(x, sr, break_ms)

        if master is None:
            master = x
        else:
            master = np.concatenate([master, x])

    if master is None:
        raise RuntimeError("no segments produced audio")

    pcm16 = (np.clip(master, -1.0, 1.0) * 32767).astype(np.int16)
    enc = lameenc.Encoder()
    enc.set_bit_rate(BITRATE)
    enc.set_in_sample_rate(sr)
    enc.set_channels(1)
    enc.set_quality(4)
    out = bytearray()
    out += enc.encode(pcm16.tobytes())
    out += enc.flush()
    return bytes(out)


async def synth(text: str, voice: str = VOICE) -> bytes:
    """Backward-compatible single-text synthesis."""
    return await synth_segments([{"text": text}], voice)


def synth_sync(text: str) -> bytes:
    return asyncio.run(synth(text))
