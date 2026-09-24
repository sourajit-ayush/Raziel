"""
Standalone diagnostic - NOT part of the main app.
Measures your actual microphone RMS levels so we can set
SILENCE_RMS_THRESHOLD and WAKE_WORD_THRESHOLD correctly instead of guessing.

Run this, follow the prompts, and send back the printed numbers.
"""

import time

import numpy as np
import pyaudio

SAMPLE_RATE = 16000
FRAME_LENGTH = 1280


def rms(chunk_bytes):
    samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples**2)))


def measure(pa, seconds, label):
    stream = pa.open(
        rate=SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=FRAME_LENGTH,
    )
    values = []
    n_chunks = int(seconds * SAMPLE_RATE / FRAME_LENGTH)
    for _ in range(n_chunks):
        data = stream.read(FRAME_LENGTH, exception_on_overflow=False)
        values.append(rms(data))
    stream.stop_stream()
    stream.close()

    values = np.array(values)
    print(f"\n--- {label} ---")
    print(f"  min:  {values.min():.1f}")
    print(f"  max:  {values.max():.1f}")
    print(f"  avg:  {values.mean():.1f}")
    return values


def main():
    pa = pyaudio.PyAudio()

    print("Stay SILENT for 3 seconds (don't talk, just background noise)...")
    time.sleep(1)
    silence_vals = measure(pa, 3, "SILENCE / BACKGROUND NOISE")

    print("\nNow SPEAK NORMALLY (like giving a voice command) for 3 seconds...")
    time.sleep(1)
    speech_vals = measure(pa, 3, "SPEECH")

    pa.terminate()

    suggested = (silence_vals.max() + speech_vals.min()) / 2
    print("\n=== RESULT ===")
    print(f"Background noise max: {silence_vals.max():.1f}")
    print(f"Speech min:           {speech_vals.min():.1f}")
    print(f"Suggested SILENCE_RMS_THRESHOLD: {suggested:.0f}")
    print("\nCopy that suggested number into config.py")


if __name__ == "__main__":
    main()
