"""
Standalone TTS worker - runs in its own process, one per utterance.

This exists because pyttsx3.init() caches and reuses the same engine
internally (keyed by driver name), even across "new" calls in the same
Python process. That shared state gets corrupted after being interrupted
(engine.stop()), causing 'run loop already started' errors on later calls.
Running each utterance in a brand new OS process sidesteps this entirely -
there's no shared state to corrupt when the process exits after speaking.

Usage: text is passed via stdin (avoids shell-escaping issues with quotes/
punctuation). Rate and volume are passed as command-line args.
"""

import sys

import pyttsx3


def main():
    text = sys.stdin.read()
    rate = int(sys.argv[1]) if len(sys.argv) > 1 else 175
    volume = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    engine = pyttsx3.init()
    engine.setProperty("rate", rate)
    engine.setProperty("volume", volume)
    engine.say(text)
    engine.runAndWait()


if __name__ == "__main__":
    main()
