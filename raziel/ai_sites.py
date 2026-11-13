"""How the AI chat sites' names are heard, in one place (tools.py, multi_command.py, ai_answers.py).

The log: "Open JetGPT and type Hey Hi" -> "I don't know how to type into jetgpt yet", and then every
later "send it" got that same line again from the model. Whisper writes ChatGPT a dozen ways.
"""
import re
from typing import Optional

# Compared with everything but letters removed.
AI_SITE_ALIASES = {
    "chatgpt": ("chatgpt", "jetgpt", "chatgbt", "chadgpt", "chatgtp", "chatgp", "chatjpt", "chargegpt", "chartgpt",
                "jetgbt", "chatcpt", "chatgpd", "chatgptee", "chatchpt", "chaatgpt", "chetgpt", "chatgpg",
                "chatgptai", "openaichatgpt"),
    "gemini": ("gemini", "jemini", "gemni", "gemnai", "jiminy", "gemany", "geminai", "googlegemini", "geminiai",
               "jeminai", "gemini_"),
    "claude": ("claude", "claudeai"),
    "perplexity": ("perplexity", "perplexityai", "perplexcity"),
    "copilot": ("copilot", "microsoftcopilot"),
}

# The same names as a regex fragment (no groups), for sentences like "open JetGPT and type ...".
SITE_WORDS = (r"chat\s?g\.?\s?p\.?\s?t|jet\s?g\s?[pb]\s?t|chat\s?g\s?[bt]\s?[tp]|chad\s?g\s?p\s?t|chat\s?gp|"
              r"chat\s?jpt|charge\s?gpt|chart\s?gpt|chat\s?cpt|chet\s?gpt|chat\s?gpd|"
              r"google gemini|gemini|jemini|gemni|gemnai|jiminy|"
              r"claude|perplexity|microsoft copilot|co-?\s?pilot")

DISPLAY = {"chatgpt": "ChatGPT", "gemini": "Gemini", "claude": "Claude", "perplexity": "Perplexity",
           "copilot": "Copilot"}


def canonical(name: str) -> Optional[str]:
    """ "JetGPT" / "chat gbt" / "Jemini" -> "chatgpt" / "gemini"; None when it isn't one of them."""
    key = re.sub(r"[^a-z]", "", (name or "").lower())
    if not key:
        return None
    for site, aliases in AI_SITE_ALIASES.items():
        if key in aliases:
            return site
    return None
