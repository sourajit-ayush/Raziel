"""
avatar_forms.py - "Raziel, become a dragon": voice commands for the orb avatar's shapes.

The orb avatar (avatar_orb.html, config.AVATAR_STYLE = "orb") is 24,000 glowing particles that
can pull together into other shapes. This is the deterministic matcher main.py runs before the
LLM, so a shape change is instant and costs no model call:

    become a dragon / turn into a sword / transform into a knight / show me a butterfly
    make a heart / become a planet / become a galaxy / back to the orb / back to normal
    roar / breathe fire / fly around / stop flying            (dragon)
    swing your sword                                           (sword)
    attack / block / raise your sword                          (knight)
    dance / stop dancing                                       (music on the PC, music_listener.py)
    what can you become?

Hindi works too ("ड्रैगन बन जाओ", "दहाड़ो", "आग उगलो", "उड़ो", "तलवार बन जाओ", "नाचो",
"वापस गोला बन जाओ") and so does Hinglish ("dragon ban jao", "udo").

Every pattern must match the WHOLE sentence (after a few polite fillers), so "send a message to
Fazal saying become a dragon" or "how do I become a doctor" never land here.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import config
import lang

logger = logging.getLogger(__name__)

FORMS = ("orb", "dragon", "sword", "knight", "butterfly", "planet", "heart", "galaxy")
ACTIONS = ("roar", "fire", "fly", "stay", "swing", "attack", "block", "raise")

REPLIES = {
    "dragon":    {"en": "Becoming a dragon.",               "hi": "ड्रैगन बन रही हूँ।"},
    "sword":     {"en": "Here's your sword.",               "hi": "ये रही तलवार।"},
    "knight":    {"en": "Knight, reporting for duty.",      "hi": "योद्धा हाज़िर है।"},
    "butterfly": {"en": "A butterfly, just for you.",       "hi": "आपके लिए एक तितली।"},
    "planet":    {"en": "Turning into a planet.",           "hi": "ग्रह बन रही हूँ।"},
    "heart":     {"en": "Here's a heart.",                  "hi": "ये रहा दिल।"},
    "galaxy":    {"en": "Becoming a galaxy.",               "hi": "आकाशगंगा बन रही हूँ।"},
    "orb":       {"en": "Back to my orb.",                  "hi": "वापस अपने गोले में।"},
    "roar":      {"en": "Roooar!",                          "hi": "दहाड़!"},
    "fire":      {"en": "Breathing fire!",                  "hi": "आग उगल रही हूँ!"},
    "fly":       {"en": "Taking flight.",                   "hi": "उड़ान भर रही हूँ।"},
    "stay":      {"en": "Hovering right here.",             "hi": "यहीं रुक गई।"},
    "swing":     {"en": "Swish!",                           "hi": "ये लो वार!"},
    "attack":    {"en": "Hyah!",                            "hi": "हमला!"},
    "block":     {"en": "Shields up!",                      "hi": "ढाल तैयार!"},
    "raise":     {"en": "For glory!",                       "hi": "जय हो!"},
    "not_flying": {"en": "I'm not flying right now. Say 'become a dragon' first.",
                   "hi": "मैं अभी उड़ नहीं रही। पहले बोलिए 'ड्रैगन बन जाओ'।"},
    "dance_on":  {"en": "Okay, I'll dance whenever music plays.",
                  "hi": "ठीक है, गाना बजेगा तो मैं नाचूँगी।"},
    "dance_on_deaf": {"en": "I'd love to, but I can't hear what your PC is playing yet. "
                            "Install the PyAudioWPatch package from the readme and restart me.",
                      "hi": "मुझे नाचना अच्छा लगेगा, पर मैं अभी कंप्यूटर का गाना सुन नहीं पा रही। "
                            "README में लिखा PyAudioWPatch पैकेज इंस्टॉल करके मुझे फिर से चालू कीजिए।"},
    "dance_off": {"en": "Okay, I won't dance to music.", "hi": "ठीक है, अब मैं गानों पर नहीं नाचूँगी।"},
    "list":      {"en": "I can become a dragon, a sword, a knight, a butterfly, a planet, a heart or a galaxy. "
                        "As a dragon I can roar, breathe fire and fly; as a knight I can attack and block. "
                        "And when music plays on your PC, I dance to it.",
                  "hi": "मैं ड्रैगन, तलवार, योद्धा, तितली, ग्रह, दिल या आकाशगंगा बन सकती हूँ। ड्रैगन बनकर मैं "
                        "दहाड़ सकती हूँ, आग उगल सकती हूँ और उड़ सकती हूँ; योद्धा बनकर हमला और बचाव कर सकती हूँ। "
                        "और कंप्यूटर पर गाना बजे तो मैं उस पर नाचती हूँ।"},
    "unknown":   {"en": "I can't become {thing} yet. I can become a dragon, a sword, a knight, a butterfly, "
                        "a planet, a heart or a galaxy.",
                  "hi": "मैं अभी {thing} नहीं बन सकती। मैं ड्रैगन, तलवार, योद्धा, तितली, ग्रह, दिल या आकाशगंगा बन सकती हूँ।"},
    "no_orb":    {"en": "I can only change shape as the orb avatar. You can switch to it in the config file: "
                        "AVATAR_STYLE equals orb.",
                  "hi": "मैं सिर्फ़ ऑर्ब अवतार में ही रूप बदल सकती हूँ। config फ़ाइल में AVATAR_STYLE को orb कर दीजिए।"},
}

# ------------------------------------------------------------------ English

# Polite openers that may come before the command, any number of times.
_PRE = (r"(?:(?:hey|hi|ok|okay|so|now|and|then|raziel|please|pls|just|go on|come on|alright|"
        r"can you|could you|would you|will you|won't you|i want you to|i'd like you to|i would like you to|"
        r"i wanna see you|i want to see you|let's see you|let me see you|show me how you|show me you|"
        r"go ahead and|why don't you|quickly|you can|you should)\s+)*")
# ...and a few that may come after it.
_POST = (r"(?:\s+(?:please|pls|now|again|for me|right now|for a bit|for a while|for a second|"
         r"real quick|quickly|once more|one more time|raziel|okay|ok|too|as well))*")

_FORM_WORDS = (
    ("dragon", r"dragon|drake|wyvern"),
    ("sword", r"sword|blade|katana|sabre|saber|longsword"),
    ("knight", r"knight|warrior|soldier|paladin|(?:video |computer )?game (?:character|figure|hero)"),
    ("butterfly", r"butterfly"),
    ("planet", r"planet|saturn"),
    ("heart", r"heart"),
    ("galaxy", r"galaxy|nebula|milky way"),
    ("orb", r"orb|globe|sphere|ball of light"),
)
# The shape word must END the thing, adjectives allowed before it: "a big scary dragon" yes;
# "the heart rate", "dragon ball z", "a heart shaped cake", "planet earth" no.
_FORM_RES = [(form, re.compile(rf"(?:[a-z']+\s+){{0,3}}(?:{words})")) for form, words in _FORM_WORDS]
_ANY_FORM = "|".join(words for _f, words in _FORM_WORDS)

# Verbs that only ever mean "take this shape": an unknown thing gets a friendly "can't yet".
_STRONG = (r"(?:transform(?: yourself)? into|turn yourself into|change yourself into|morph(?: yourself)? into|"
           r"shape ?shift into|shapeshift into|take the (?:form|shape) of)")
# Verbs that also mean other things ("become a teacher for me" is role-play for the model), so they
# only count with a known shape.
_WEAK = (r"(?:become|turn into|turn in ?to|turn to|turning into|turning in ?to|turning to|turned into|change into|"
         r"change to|changing into|convert(?: yourself)? into|convert(?: yourself)? to|converting into|transform to|"
         r"run yourself into|go into|switch into|switch to|shift into|be|make|draw|do)")
# "show me" / "give me" also ask for pictures and facts ("show me the planets", "show me saturn"),
# so only "show me a dragon" / "show me your dragon" count.
_SHOW = r"(?:show me|give me|show us)"
_ARTICLE = r"(?:(?:a|an|the|some|your|my|one|that|this)\s+)?"
_ARTICLE_CAP = r"((?:a|an|the|some|your|my|one|that|this)\s+)?"
_THING = r"((?:[a-z']+\s+){0,3}?[a-z']+)"          # lazy: "a cat please" -> "cat" + a polite filler
_SHAPE_TAIL = r"(?:\s+(?:form|mode|shape|version))?"

_STRONG_RE = re.compile(rf"^{_PRE}{_STRONG}\s+{_ARTICLE_CAP}{_THING}{_SHAPE_TAIL}{_POST}$")
_WEAK_RE = re.compile(rf"^{_PRE}{_WEAK}\s+{_ARTICLE}{_THING}{_SHAPE_TAIL}{_POST}$")
_SHOW_RE = re.compile(rf"^{_PRE}{_SHOW}\s+(?:a|an|your|one)\s+{_THING}{_SHAPE_TAIL}{_POST}$")
# "dragon mode" / "dragon form" on its own
_BARE_RE = re.compile(rf"^{_PRE}{_THING}\s+(?:form|mode){_POST}$")

_BACK_RE = re.compile(
    rf"^{_PRE}(?:"
    r"(?:go |come |turn |change |switch |get )?back (?:to |into )?(?:(?:the|your|an|a|my) )?"
    r"(?:orb|globe|normal|original(?: form| self)?|old self|usual self|self|yourself|orb form|normal form|real form)"
    r"|be (?:yourself|normal|an orb|the orb)(?: again)?"
    r"|return to (?:(?:the|your|an) )?(?:orb|normal|original form|orb form|normal form)"
    r"|turn back(?: in ?to (?:(?:the|your|an|a) )?orb)?"
    rf"|stop being (?:a|an|the) (?:[a-z]+ )?(?:{_ANY_FORM})"
    r"|normal form|orb mode|orb form"
    rf"){_POST}$")

_ACTION_RES = (
    ("roar", r"(?:(?:let out|give(?: me| us)?|do|make) )?(?:a )?(?:(?:big|loud|mighty|huge|scary) )?roar"
             r"(?: (?:loudly|louder|like a dragon|at me))?"),
    ("fire", r"(?:breathe|breath|spit|shoot|blow|spew|throw)(?: out| some)? (?:fire|flames?|fire ?balls?)"
             r"|fire breath|breathe some fire"),
    ("fly", r"fly(?: (?:around|away|in (?:a )?circles?|around the (?:screen|window|room)|high))?"
            r"|start flying|take (?:off|flight)|soar|go fly(?: around)?|spread your wings"),
    ("stay", r"stop flying|land|hover|stay (?:still|put)|stop moving"),
    ("swing", r"swing(?: (?:the|your|my) (?:sword|blade)| it)?|slash|swing it around"),
    ("attack", r"attack(?: (?:now|them|me|with (?:the|your) sword))?|strike|fight|charge"),
    ("block", r"block(?: (?:it|that|with (?:the|your) shield))?|shields? up|raise (?:the|your) shield"
              r"|defend(?: yourself)?|use (?:the|your) shield"),
    ("raise", r"raise (?:the|your) sword|victory pose|salute|strike a pose|hold (?:the|your) sword up"),
)
_ACTION_RES = [(name, re.compile(rf"^{_PRE}(?:{pat}){_POST}$")) for name, pat in _ACTION_RES]

_DANCE_OFF_RE = re.compile(
    rf"^{_PRE}(?:stop dancing|don't dance|do not dance|no (?:more )?dancing|(?:turn off|disable|switch off) "
    r"(?:the )?music mode|music mode off|stop reacting to (?:the )?music|don't react to (?:the )?music"
    rf"|(?:stop|quit) dancing to (?:the |this )?(?:music|song|songs)){_POST}$")
_DANCE_ON_RE = re.compile(
    rf"^{_PRE}(?:dance|start dancing|dance (?:to|with) (?:the |this |my )?(?:music|song|songs|beat)"
    r"|dance for me|(?:turn on |enable |switch on )?(?:the )?music mode(?: on)?|react to (?:the )?music"
    rf"|let's dance|dance again){_POST}$")
_LIST_RE = re.compile(
    rf"^{_PRE}(?:what (?:all )?(?:can you (?:become|turn into|transform into|change into|shapeshift into)"
    r"|(?:forms?|shapes?) can you (?:take|do|make|become)|(?:forms?|shapes?) do you have)"
    r"|what are your (?:forms|shapes)|list your (?:forms|shapes)|(?:show me )?all your (?:forms|shapes)"
    rf"|what (?:else )?can you turn into|what can you shapeshift into)(?: then)?{_POST}$")

# ------------------------------------------------------------------ Hindi (compared after lang.fold_hindi)

_HI_FORMS = (
    ("dragon", "ड्रैगन|ड्रेगन|ड्रगन|ड्रैगान|ड्रैगॉन"),
    ("sword", "तलवार"),
    ("knight", "योद्धा|योधा|नाइट|सिपाही|शूरवीर|वीर"),
    ("butterfly", "तितली"),
    ("planet", "ग्रह|प्लैनेट|प्लेनेट|शनि"),
    ("heart", "दिल|हार्ट|हृदय"),
    ("galaxy", "आकाशगंगा|गैलेक्सी|गेलेक्सी"),
    ("orb", "गोला|गोले|ऑर्ब|ओर्ब|गेंद"),
)
_HI_PRE = r"(?:(?:रज़ियल|रजियल|अब|तुम|आप|प्लीज़|प्लीज|ज़रा|जरा|एक बार|वापस|जल्दी से|चलो|मुझे|मुझको|हमें)\s+)*"
_HI_ADJ = r"(?:(?:एक|कोई)\s+)?(?:(?:बड़ा|बड़ी|छोटा|छोटी|सुंदर|प्यारा|प्यारी|डरावना|खूबसूरत|चमकता|चमकती)\s+)?"
_HI_VERB = (r"(?:बन जाओ|बन जाइए|बन जाइये|बन जा|बनो|बनिए|बनिये|बन के दिखाओ|बनके दिखाओ|बनकर दिखाओ|बन कर दिखाओ"
            r"|में बदल जाओ|में बदलो|का रूप (?:लो|ले लो|धारण करो)|बनाओ|बना के दिखाओ|बनाकर दिखाओ|दिखाओ)")
_HI_POST = r"(?:\s+(?:ना|न|प्लीज़|प्लीज|अब|जल्दी|जी))*"
_HI_ACTIONS = (
    ("roar", r"दहाड़ो|दहाड़ लगाओ|दहाड़ मारो|ज़ोर से दहाड़ो|जोर से दहाड़ो|गर्जना करो|गरजो|दहाड़ के दिखाओ|दहाड़कर दिखाओ"),
    ("fire", r"आग (?:उगलो|फेंको|फेको|निकालो|छोड़ो|बरसाओ|उगल के दिखाओ|उगलकर दिखाओ)|मुँह से आग निकालो"),
    ("fly", r"उड़ो|उड़ के दिखाओ|उड़कर दिखाओ|उड़ कर दिखाओ|उड़ान भरो|उड़ना शुरू करो|उड़ जाओ|उड़ते हुए दिखाओ"),
    ("stay", r"उड़ना बंद करो|हवा में रुको|नीचे आ जाओ"),
    ("swing", r"तलवार (?:चलाओ|घुमाओ|भांजो|भाँजो)"),
    ("attack", r"हमला करो|वार करो|अटैक करो|आक्रमण करो"),
    ("block", r"बचाव करो|ढाल (?:उठाओ|दिखाओ)|ब्लॉक करो|ब्लाक करो"),
    ("raise", r"तलवार (?:ऊपर )?उठाओ|जीत का जश्न मनाओ"),
)
_HI_BACK = (r"(?:वापस\s+)?(?:(?:अपने|अपनी)\s+)?(?:(?:असली|पुराने|पुराना|नॉर्मल|नार्मल)\s+)?(?:रूप|फ़ॉर्म|फॉर्म|शेप|शक्ल)"
            r"(?:\s+में)?\s+(?:आ जाओ|आओ|आ जा|लौट आओ)|(?:नॉर्मल|नार्मल) हो जाओ")
_HI_DANCE_ON = r"नाचो|डांस करो|नाच के दिखाओ|नाचकर दिखाओ|(?:गाने|म्यूज़िक|म्यूजिक|संगीत) (?:पर|पे) नाचो"
_HI_DANCE_OFF = r"नाचना बंद करो|डांस बंद करो|मत नाचो|नाचो मत|डांस मत करो"
_HI_LIST = r"(?:तुम|आप)?\s*(?:क्या क्या|क्या-क्या|क्या) बन सकती (?:हो|हैं)|(?:तुम्हारे|आपके) (?:कौन कौन से )?रूप (?:कौन से|क्या) (?:हैं|है)"

# Hinglish in Latin letters
_HL_FORMS = (
    ("dragon", r"dragon|dragan|draigan"), ("sword", r"sword|talwar|talvar"), ("knight", r"knight|yoddha|yodha|sipahi"),
    ("butterfly", r"butterfly|titli|titali"), ("planet", r"planet|graha|grah"), ("heart", r"heart|dil"),
    ("galaxy", r"galaxy|akashganga"), ("orb", r"orb|gola"),
)
_HL_VERB = r"(?:ban jao|ban jaao|ban ja|bano|banke dikhao|ban ke dikhao|ban kar dikhao|bankar dikhao|banao|dikhao)"
_HL_PRE = r"(?:(?:raziel|ab|tum|aap|please|zara|jara|ek baar|wapas|vapas|jaldi se|chalo|mujhe|humein)\s+)*"
_HL_POST = r"(?:\s+(?:na|please|ab|jaldi|ji))*"
_HL_ACTIONS = (
    ("roar", r"dahado|dahaado|dahaad lagao|dahad lagao|dahad karo|garjo|roar karo"),
    ("fire", r"aag (?:ugalo|ugla|phenko|feko|nikalo|chhodo|barsao)"),
    ("fly", r"udo|uddo|ud ke dikhao|udkar dikhao|udaan bharo|udan bharo|ud jao"),
    ("attack", r"hamla karo|vaar karo|war karo|attack karo"),
    ("block", r"bachav karo|bachao karo|dhaal uthao|block karo"),
    ("swing", r"talwar (?:chalao|ghumao)"),
)
_HL_DANCE_ON = r"nacho|naacho|dance karo|nach ke dikhao"
_HL_DANCE_OFF = r"nachna band karo|naachna band karo|dance band karo|mat nacho|mat naacho"


def _fold_re(pattern: str) -> str:
    return lang.fold_hindi(pattern)


def _hi_compile(body: str) -> "re.Pattern":
    return re.compile("^" + _fold_re(_HI_PRE) + "(?:" + _fold_re(body) + ")" + _fold_re(_HI_POST) + "$")


_HI_FORM_RE = re.compile(
    "^" + _fold_re(_HI_PRE) + _fold_re(_HI_ADJ) + "(" + _fold_re("|".join(w for _f, w in _HI_FORMS)) + r")\s+"
    + _fold_re(_HI_VERB) + _fold_re(_HI_POST) + "$")
_HI_FORM_LOOKUP = [(form, re.compile("^(?:" + _fold_re(words) + ")$")) for form, words in _HI_FORMS]
_HI_ACTION_RES = [(name, _hi_compile(pat)) for name, pat in _HI_ACTIONS]
_HI_BACK_RE = _hi_compile(_HI_BACK)
_HI_DANCE_ON_RE = _hi_compile(_HI_DANCE_ON)
_HI_DANCE_OFF_RE = _hi_compile(_HI_DANCE_OFF)
_HI_LIST_RE = _hi_compile(_HI_LIST)

_HL_FORM_RE = re.compile(rf"^{_HL_PRE}(?:ek\s+)?({'|'.join(w for _f, w in _HL_FORMS)})\s+{_HL_VERB}{_HL_POST}$")
_HL_FORM_LOOKUP = [(form, re.compile(rf"^(?:{words})$")) for form, words in _HL_FORMS]
_HL_ACTION_RES = [(name, re.compile(rf"^{_HL_PRE}(?:{pat}){_HL_POST}$")) for name, pat in _HL_ACTIONS]
_HL_DANCE_ON_RE = re.compile(rf"^{_HL_PRE}(?:{_HL_DANCE_ON}){_HL_POST}$")
_HL_DANCE_OFF_RE = re.compile(rf"^{_HL_PRE}(?:{_HL_DANCE_OFF}){_HL_POST}$")


# ------------------------------------------------------------------ parsing

def _normalize(text: str) -> str:
    """Lower case, Hindi spelling variants folded, punctuation gone, curly apostrophes straightened."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = lang.fold_hindi(text)
    text = re.sub(r"[^\w\s'ऀ-ॿ]+", " ", text)
    text = re.sub(r"(?<![a-z])'|'(?![a-z])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# "show me a picture of a butterfly" / "give me dragon songs" want media, not a shape.
_MEDIA_RE = re.compile(r"\b(?:pictures?|photos?|images?|videos?|pics?|drawings?|paintings?|songs?|movies?|films?|"
                       r"shows?|clips?|gifs?|wallpapers?|facts?|info|information|emojis?|stickers?)\b")


def _form_in(thing: str) -> Optional[str]:
    """The shape a thing names ("big scary dragon" -> dragon), or None."""
    thing = thing.strip()
    if _MEDIA_RE.search(re.sub(r"\b(?:video|computer) game\b", "game", thing)):
        return None
    for form, rx in _FORM_RES:
        if rx.fullmatch(thing):
            return form
    return None


_JOIN_RE = re.compile(r"\s+(?:and then|and|then|और फिर|और|फिर|aur phir|aur fir|aur)\s+")


def parse_all(transcript: str) -> list:
    """Like parse(), but also splits "become a dragon and roar" into [("form", "dragon"), ("action", "roar")]."""
    text = _normalize(transcript)
    for m in _JOIN_RE.finditer(text):                # tried first: "become a dragon and roar" would
        first, second = parse(text[:m.start()]), parse(text[m.end():])     # otherwise read as one shape
        if first and second and first[0] in ("form", "action") and second[0] in ("form", "action"):
            return [first, second]
    one = parse(transcript)
    return [one] if one is not None else []


def parse(transcript: str) -> Optional[Tuple[str, str]]:
    """
    ("form", name) | ("action", name) | ("dance", "on"/"off") | ("list", "") |
    ("unknown", thing) | None when the sentence isn't a shape command.
    """
    text = _normalize(transcript)
    if not text or len(text) > 120:
        return None

    # --- Hindi (Devanagari) and Hinglish
    if lang.has_devanagari(text):
        m = _HI_FORM_RE.match(text)
        if m:
            for form, rx in _HI_FORM_LOOKUP:
                if rx.match(m.group(1)):
                    return ("form", form)
        if _HI_BACK_RE.match(text):
            return ("form", "orb")
        for name, rx in _HI_ACTION_RES:
            if rx.match(text):
                return ("action", name)
        if _HI_DANCE_OFF_RE.match(text):
            return ("dance", "off")
        if _HI_DANCE_ON_RE.match(text):
            return ("dance", "on")
        if _HI_LIST_RE.match(text):
            return ("list", "")
        return None

    m = _HL_FORM_RE.match(text)
    if m:
        for form, rx in _HL_FORM_LOOKUP:
            if rx.match(m.group(1)):
                return ("form", form)
    for name, rx in _HL_ACTION_RES:
        if rx.match(text):
            return ("action", name)
    if _HL_DANCE_OFF_RE.match(text):
        return ("dance", "off")
    if _HL_DANCE_ON_RE.match(text):
        return ("dance", "on")

    # --- English
    if _BACK_RE.match(text):
        return ("form", "orb")
    if _LIST_RE.match(text):
        return ("list", "")
    if _DANCE_OFF_RE.match(text):
        return ("dance", "off")
    if _DANCE_ON_RE.match(text):
        return ("dance", "on")
    for name, rx in _ACTION_RES:
        if rx.match(text):
            return ("action", name)
    for rx in (_WEAK_RE, _SHOW_RE, _BARE_RE):
        m = rx.match(text)
        if m:
            form = _form_in(m.group(1))
            if form:
                return ("form", form)
    m = _STRONG_RE.match(text)
    if m:
        form = _form_in(m.group(2))
        if form:
            return ("form", form)
        # "transform into a cat" gets a friendly "can't yet" (only these verbs: they can't mean anything
        # else); no article = not a thing ("transform into something better"), so the model answers.
        article, thing = (m.group(1) or "").strip(), m.group(2).strip()
        if article in ("a", "an", "the", "some", "one") and len(thing.split()) <= 3 and " and " not in f" {thing} ":
            return ("unknown", f"{article} {thing}")
    return None


# ------------------------------------------------------------------ doing it

def _orb_active() -> bool:
    if not getattr(config, "AVATAR_ENABLED", True):
        return False
    try:
        import avatar_window
        return getattr(avatar_window, "STYLE", "orb") == "orb"
    except Exception:
        return str(getattr(config, "AVATAR_STYLE", "orb")).strip().lower() == "orb"


def _speakable(thing: str) -> str:
    """ "a cat" stays "a cat"; "a apple" becomes "an apple"."""
    words = thing.strip().split()
    if len(words) >= 2 and words[0] in ("a", "an"):
        words[0] = "an" if words[1][:1] in "aeiou" else "a"
    return " ".join(words)


def try_handle(transcript: str) -> Optional[str]:
    """main.py matcher: the reply to speak, or None when this isn't a shape command."""
    steps = parse_all(transcript)
    if not steps:
        return None
    if len(steps) == 2:
        if not _orb_active():
            return lang.tr(REPLIES, "no_orb")
        replies = [_do(kind, value) for kind, value in steps]
        return " ".join(r for r in replies if r)
    return _do(*steps[0])


def _do(kind: str, value: str) -> Optional[str]:
    if kind == "unknown":
        # Only when the orb is on screen - otherwise "become a doctor" is better left to the model.
        if not _orb_active():
            return None
        return lang.tr(REPLIES, "unknown", thing=_speakable(value))
    if not _orb_active():
        return lang.tr(REPLIES, "no_orb")

    import avatar_server

    if kind == "list":
        return lang.tr(REPLIES, "list")

    if kind == "form":
        avatar_server.set_form(value)
        logger.info("Orb avatar: form -> %s", value)
        return lang.tr(REPLIES, value)

    if kind == "action":
        if value == "stay" and avatar_server.current_form() != "dragon":
            return lang.tr(REPLIES, "not_flying")
        avatar_server.send_action(value)
        logger.info("Orb avatar: action -> %s", value)
        return lang.tr(REPLIES, value)

    if kind == "dance":
        on = value == "on"
        try:
            import music_listener
            music_listener.set_enabled(on)
            hears = music_listener.available()
        except Exception:
            logger.exception("music_listener unavailable")
            hears = False
        avatar_server.set_dance(on)
        if on and not hears:
            return lang.tr(REPLIES, "dance_on_deaf")
        return lang.tr(REPLIES, "dance_on" if on else "dance_off")
    return None
