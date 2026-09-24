"""
routing.py - small types shared between the feature modules and main.py's router.

AskLLM
------
A deterministic matcher normally returns the finished sentence to speak (a str). Some
requests need the language model AFTER the code has done its part - "summarize what I
copied": the code reads the clipboard, the model writes the summary. A matcher (or a tool)
returns AskLLM(prompt) for that: main.route_transcript() sends `prompt` to the brain
instead of the user's own words, and the model's answer is spoken as usual.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AskLLM:
    prompt: str                      # what the model is asked (contains any data it needs)
    note: str = ""                   # short label for assistant.log

    def __bool__(self):              # a matcher's "did it match?" test is `if reply:`
        return bool(self.prompt)
