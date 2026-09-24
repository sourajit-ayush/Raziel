"""
calc.py - math, unit conversion and currency for Raziel, computed IN CODE (never by the LLM).

An 8B language model gets arithmetic wrong, so every "what's 15 plus 27", "10 km in miles" and
"100 dollars in rupees" is answered here, in microseconds, in English or Hindi (Devanagari).

Layout
------
  1. format_number()                      spoken-friendly numbers (no scientific notation)
  2. evaluate() / evaluate_exact()        SAFE expression evaluator: `ast` only, whitelisted nodes,
                                          whitelisted functions, size/exponent guards. Nothing typed by a
                                          user is ever passed to eval/exec/compile.
  3. text preparation                     lower-case, Hindi folding, Hindi + English number words
                                          ("twenty five", "दो लाख", "1.5 crore", "5k") -> digits
  4. parse_spoken_math()                  "15 plus 27", "10 percent of 250", "18 percent tip on 65.50",
                                          "पंद्रह जमा सत्ताईस", "25 का 10 प्रतिशत" ... -> a Parsed(...) / expression
  5. convert_units()                      table driven (factor to a base unit; temperature by formula)
  6. convert_currency() / fetch_rate()    Frankfurter API (keyless), cached 30 minutes
  7. try_handle(transcript)               strict, anchored matcher used before the LLM
  8. calculate(expression)                the LLM tool function

Settings (read with getattr, defaults in brackets): CALC_ENABLED [True], CURRENCY_ENABLED [True].
"""

from __future__ import annotations

import ast
import datetime
import decimal
import logging
import math
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config
import lang
import netutil

logger = logging.getLogger("voice_assistant")

MAX_EXPR_LEN = 200
MAX_FACTORIAL = 170
MAX_POW_EXPONENT = 10000
MAX_INT_BITS = 1300          # about 390 decimal digits; anything larger is refused
RATE_CACHE_SECONDS = 30 * 60
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"


def _enabled(name: str) -> bool:
    return bool(getattr(config, name, True))


class CalcError(Exception):
    """kind: 'zero' | 'big' | 'domain' | 'invalid' | 'unit' | 'currency'."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def _hf(pattern: str) -> str:
    """Fold only the Devanagari runs of a regex, the way lang.fold_hindi folds the input."""
    return re.sub("[ऀ-ॿ]+", lambda m: lang.fold_hindi(m.group()), pattern)


# ------------------------------------------------------------------ spoken strings

_T = {
    "cant_zero": {"en": "I can't divide by zero.", "hi": "मैं शून्य से भाग नहीं दे सकती।"},
    "too_big": {"en": "That number is too big for me to work out.",
                "hi": "यह संख्या मेरे हिसाब के लिए बहुत बड़ी है।"},
    "domain": {"en": "That doesn't have a real answer.", "hi": "इसका कोई सही जवाब नहीं बनता।"},
    "neg_root": {"en": "I can't take the square root of a negative number.",
                 "hi": "मैं ऋणात्मक संख्या का वर्गमूल नहीं निकाल सकती।"},
    "cant_work": {"en": "I couldn't work that out.", "hi": "मैं यह हल नहीं कर पाई।"},
    "answer": {"en": "The answer is {r}.", "hi": "जवाब {r} है।"},
    "binary": {"en": "{a} {op} {b} is {r}.", "hi": "{a} {op} {b} बराबर {r}।"},
    "percent_of": {"en": "{p} percent of {n} is {r}.", "hi": "{n} का {p} प्रतिशत {r} होता है।"},
    "pct_is": {"en": "{a} is {r} percent of {b}.", "hi": "{a}, {b} का {r} प्रतिशत है।"},
    "inc": {"en": "{n} increased by {p} percent is {r}.", "hi": "{n} में {p} प्रतिशत बढ़ाने पर {r} होता है।"},
    "dec": {"en": "{n} decreased by {p} percent is {r}.", "hi": "{n} में {p} प्रतिशत घटाने पर {r} होता है।"},
    "discount": {"en": "{n} with a {p} percent discount is {r}. You save {s}.",
                 "hi": "{n} पर {p} प्रतिशत की छूट के बाद {r} होता है। आपकी {s} की बचत होगी।"},
    "tip": {"en": "A tip of {p} percent on {n} is {t}, so the total is {r}.",
            "hi": "{n} पर {p} प्रतिशत टिप {t} होती है, यानी कुल {r} होगा।"},
    "split": {"en": "{n} split between {k} people is {r} each.",
              "hi": "{n} को {k} लोगों में बाँटने पर हर एक के हिस्से में {r} आता है।"},
    "avg": {"en": "The average of {l} is {r}.", "hi": "{l} का औसत {r} है।"},
    "sqrt": {"en": "The square root of {n} is {r}.", "hi": "{n} का वर्गमूल {r} है।"},
    "cbrt": {"en": "The cube root of {n} is {r}.", "hi": "{n} का घनमूल {r} है।"},
    "square": {"en": "{n} squared is {r}.", "hi": "{n} का वर्ग {r} है।"},
    "cube": {"en": "{n} cubed is {r}.", "hi": "{n} का घन {r} है।"},
    "fact": {"en": "{n} factorial is {r}.", "hi": "{n} का फैक्टोरियल {r} है।"},
    "power": {"en": "{a} to the power of {b} is {r}.", "hi": "{a} की घात {b} बराबर {r}।"},
    "half": {"en": "Half of {n} is {r}.", "hi": "{n} का आधा {r} है।"},
    "double": {"en": "Double {n} is {r}.", "hi": "{n} का दोगुना {r} है।"},
    "triple": {"en": "Triple {n} is {r}.", "hi": "{n} का तिगुना {r} है।"},
    "frac": {"en": "{f} of {n} is {r}.", "hi": "{n} का {f} {r} है।"},
    "sum": {"en": "The sum of {l} is {r}.", "hi": "{l} का जोड़ {r} है।"},
    "product": {"en": "The product of {l} is {r}.", "hi": "{l} का गुणनफल {r} है।"},
    "diff": {"en": "The difference between {a} and {b} is {r}.", "hi": "{a} और {b} का अंतर {r} है।"},
    "trig": {"en": "The {f} of {n} {u} is {r}.", "hi": "{n} {u} का {f} {r} है।"},
    "logn": {"en": "The {f} of {n} is {r}.", "hi": "{n} का {f} {r} है।"},
    "conv": {"en": "{q} is {about}{r}.", "hi": "{q} {about}{r} होते हैं।"},
    "conv1": {"en": "{q} is {about}{r}.", "hi": "{q} {about}{r} होता है।"},
    "conv_x": {"en": "{q} is {r}.", "hi": "{q} {r} के बराबर होते हैं।"},
    "conv_x1": {"en": "{q} is {r}.", "hi": "{q} {r} के बराबर होता है।"},
    "about_en": {"en": "about ", "hi": "लगभग "},
    "no_net": {"en": "Sorry, I can't reach the exchange-rate service right now.",
               "hi": "मैं अभी एक्सचेंज रेट की सेवा तक नहीं पहुँच पा रही हूँ।"},
    "unsupported": {"en": "Sorry, I can't look up {c} rates. I only cover about 30 major currencies.",
                    "hi": "माफ़ कीजिए, मैं {c} की दर नहीं देख सकती। मेरे पास लगभग 30 मुख्य मुद्राओं की दर है।"},
    "cur": {"en": "{q} is about {r} as of {d}.", "hi": "{q}, {d} की दर से लगभग {r} होते हैं।"},
    "cur_same": {"en": "{q} is {r}.", "hi": "{q} बराबर {r} होते हैं।"},
    "rate": {"en": "One {a} is about {r} as of {d}.", "hi": "1 {a}, {d} की दर से लगभग {r} होता है।"},
    "below_zero": {"en": "That temperature is below absolute zero.",
                   "hi": "यह तापमान परम शून्य से भी नीचे है।"},
    "deg": {"en": "degrees", "hi": "डिग्री"},
    "deg1": {"en": "degree", "hi": "डिग्री"},
    "rad1": {"en": "radian", "hi": "रेडियन"},
    "rad": {"en": "radians", "hi": "रेडियन"},
    "minus": {"en": "minus ", "hi": "माइनस "},
    "times10": {"en": "{m} times 10 to the power of {e}", "hi": "{m} गुणा 10 की घात {e}"},
    "and": {"en": " and ", "hi": " और "},
    "calc_off": {"en": "The calculator is turned off.", "hi": "कैलकुलेटर बंद है।"},
    "cur_off": {"en": "Currency conversion is turned off.", "hi": "मुद्रा बदलने की सुविधा बंद है।"},
}


def _t(key: str, lg: Optional[str] = None, **kw) -> str:
    return lang.tr(_T, key, lang=lg, **kw)


# ------------------------------------------------------------------ number formatting

def _big(x: float, lg: Optional[str] = None) -> str:
    sign = "-" if x < 0 else ""
    a = abs(x)
    exp = int(math.floor(math.log10(a)))
    mant = a / (10 ** exp)
    m = format_number(round(mant, 3))
    return sign + _t("times10", lg, m=m, e=exp)


def format_number(x) -> str:
    """
    Speech-friendly number: whole numbers without a decimal point, otherwise up to 4 decimals with
    trailing zeros trimmed (tiny values keep 4 significant digits), never scientific notation, never
    thousands separators. Astronomically large values are said as "1.5 times 10 to the power of 30".
    """
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, int):
        digits = len(str(abs(x)))
        if digits <= 21:
            return str(x)
        return _big(x)
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        raise CalcError("big")
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    a = abs(x)
    if a >= 1e21:
        return _big(x)
    if a >= 1e15:
        return f"{x:.0f}"
    decimals = 4 if a >= 1 else min(12, 3 - int(math.floor(math.log10(a))))
    s = f"{x:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def _say(x, lg: Optional[str] = None) -> str:
    """format_number() with a spoken minus sign ('minus 5' / 'माइनस 5')."""
    s = format_number(x)
    if s.startswith("-"):
        return _t("minus", lg) + s[1:]
    return s


def _round_result(x: float) -> float:
    """Conversion answers: 2 decimals from 1 upwards, 3 significant digits below 1."""
    if x == 0 or not math.isfinite(x):
        return x
    if abs(x) >= 1:
        return round(x, 2)
    return float(f"{x:.3g}")


# ------------------------------------------------------------------ safe evaluator

def _need_finite(v):
    if isinstance(v, float) and not math.isfinite(v):
        raise CalcError("big")
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > MAX_INT_BITS:
        raise CalcError("big")
    if isinstance(v, complex):
        raise CalcError("domain")
    return v


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CalcError("invalid")
    return v


def _pow(a, b):
    a, b = _num(a), _num(b)
    if a == 0 and b < 0:
        raise CalcError("zero")
    if abs(b) > MAX_POW_EXPONENT and abs(a) not in (0, 1):
        raise CalcError("big")
    if isinstance(a, int) and isinstance(b, int) and b >= 0 and abs(a) > 1:
        if b * math.log2(abs(a)) > MAX_INT_BITS:
            raise CalcError("big")
    try:
        r = a ** b
    except OverflowError:
        raise CalcError("big") from None
    except ZeroDivisionError:
        raise CalcError("zero") from None
    return _need_finite(r)


def _factorial(x):
    x = _num(x)
    if isinstance(x, float):
        if x != int(x):
            raise CalcError("domain")
        x = int(x)
    if x < 0:
        raise CalcError("domain")
    if x > MAX_FACTORIAL:
        raise CalcError("big")
    return math.factorial(x)


def _sqrt(x):
    x = _num(x)
    if x < 0:
        raise CalcError("domain", "neg_root")
    try:
        r = math.sqrt(x)
    except OverflowError:
        raise CalcError("big") from None
    return int(r) if r == int(r) and abs(r) < 1e15 else r


def _cbrt(x):
    x = _num(x)
    try:
        r = math.copysign(abs(x) ** (1.0 / 3.0), x)
    except OverflowError:
        raise CalcError("big") from None
    rr = round(r)
    return rr if abs(rr ** 3 - x) < 1e-9 * max(1.0, abs(x)) else r


def _ln(x):
    x = _num(x)
    if x <= 0:
        raise CalcError("domain")
    return math.log(x)


def _log10(x):
    x = _num(x)
    if x <= 0:
        raise CalcError("domain")
    r = math.log10(x)
    return round(r) if abs(r - round(r)) < 1e-12 else r


def _log2(x):
    x = _num(x)
    if x <= 0:
        raise CalcError("domain")
    r = math.log2(x)
    return round(r) if abs(r - round(r)) < 1e-12 else r


def _exp(x):
    x = _num(x)
    if x > 700:
        raise CalcError("big")
    return math.exp(x)


def _round(x, nd=0):
    x, nd = _num(x), _num(nd)
    if nd != int(nd) or abs(nd) > 15:
        raise CalcError("invalid")
    if abs(x) >= 1e15:
        return x
    q = decimal.Decimal(1).scaleb(-int(nd))                # round half away from zero, the way people count
    r = decimal.Decimal(repr(x)).quantize(q, rounding=decimal.ROUND_HALF_UP)
    return int(r) if int(nd) <= 0 or r == r.to_integral_value() else float(r)


def _floor(x):
    return math.floor(_num(x))


def _ceil(x):
    return math.ceil(_num(x))


def _abs(x):
    return abs(_num(x))


_FUNCS = {
    "sqrt": _sqrt, "cbrt": _cbrt, "abs": _abs, "round": _round, "floor": _floor, "ceil": _ceil,
    "factorial": _factorial, "ln": _ln, "log": _log10, "log10": _log10, "log2": _log2, "exp": _exp,
}
_TRIG = ("sin", "cos", "tan")
_CONSTS = {"pi": math.pi, "e": math.e}
_FUNC_ARITY = {"round": (1, 2)}


def _trig(name: str, x, radians: bool):
    x = _num(x)
    if not radians:
        x = math.radians(x)
    if not math.isfinite(x) or abs(x) > 1e12:
        raise CalcError("big")
    v = getattr(math, name)(x)
    if abs(v) < 1e-12:
        return 0
    if name == "tan" and abs(v) > 1e12:
        raise CalcError("domain")
    return v


def _ev(node, radians: bool, depth: int = 0):
    if depth > 120:
        raise CalcError("invalid")
    d = depth + 1
    if isinstance(node, ast.Expression):
        return _ev(node.body, radians, d)
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise CalcError("invalid")
        return v
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise CalcError("invalid")
    if isinstance(node, ast.UnaryOp):
        v = _num(_ev(node.operand, radians, d))
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        raise CalcError("invalid")
    if isinstance(node, ast.BinOp):
        a = _num(_ev(node.left, radians, d))
        b = _num(_ev(node.right, radians, d))
        op = node.op
        try:
            if isinstance(op, ast.Add):
                r = a + b
            elif isinstance(op, ast.Sub):
                r = a - b
            elif isinstance(op, ast.Mult):
                r = a * b
            elif isinstance(op, ast.Div):
                if b == 0:
                    raise CalcError("zero")
                r = a / b
            elif isinstance(op, ast.FloorDiv):
                if b == 0:
                    raise CalcError("zero")
                r = a // b
            elif isinstance(op, ast.Mod):
                if b == 0:
                    raise CalcError("zero")
                r = a % b
            elif isinstance(op, ast.Pow):
                r = _pow(a, b)
            else:
                raise CalcError("invalid")
        except OverflowError:
            raise CalcError("big") from None
        except ZeroDivisionError:
            raise CalcError("zero") from None
        return _need_finite(r)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalcError("invalid")
        name = node.func.id
        args = [_ev(a, radians, d) for a in node.args]
        if name in _TRIG:
            if len(args) != 1:
                raise CalcError("invalid")
            return _trig(name, args[0], radians)
        fn = _FUNCS.get(name)
        if fn is None:
            raise CalcError("invalid")
        lo, hi = _FUNC_ARITY.get(name, (1, 1))
        if not lo <= len(args) <= hi:
            raise CalcError("invalid")
        try:
            return _need_finite(fn(*args))
        except OverflowError:
            raise CalcError("big") from None
        except (ValueError, ZeroDivisionError):
            raise CalcError("domain") from None
    raise CalcError("invalid")          # Attribute, Lambda, comprehensions, Subscript, Compare, strings ...


def evaluate_exact(expr: str, radians: bool = False):
    """Evaluate an arithmetic expression safely; returns an int or float. Raises CalcError."""
    expr = (expr or "").strip()
    if not expr or len(expr) > MAX_EXPR_LEN:
        raise CalcError("invalid", "empty or too long")
    expr = expr.replace("^", "**")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise CalcError("invalid", "syntax") from None
    try:
        v = _ev(tree, radians)
    except RecursionError:
        raise CalcError("invalid") from None
    v = _need_finite(_num(v))
    if isinstance(v, float):
        v = float(f"{v:.15g}")                      # 0.1 + 0.2 -> 0.3, sin(30) -> 0.5
        if abs(v - round(v)) < 1e-9 * max(1.0, abs(v)) and abs(v) < 1e15:
            return int(round(v))
    return v


def evaluate(expr: str, radians: bool = False) -> float:
    """Safe arithmetic (see module docstring). Raises CalcError('zero'|'big'|'domain'|'invalid')."""
    v = evaluate_exact(expr, radians)
    try:
        return float(v)
    except OverflowError:
        raise CalcError("big") from None


def _error_sentence(err: CalcError, lg: Optional[str] = None) -> str:
    if err.kind == "zero":
        return _t("cant_zero", lg)
    if err.kind == "big":
        return _t("too_big", lg)
    if err.kind == "domain":
        return _t("neg_root", lg) if str(err) == "neg_root" else _t("domain", lg)
    return _t("cant_work", lg)


# ------------------------------------------------------------------ text preparation

_EN_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_EN_TENS = {w: (i + 2) * 10 for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_EN_SCALES = {"thousand": 10 ** 3, "lakh": 10 ** 5, "lakhs": 10 ** 5, "lac": 10 ** 5, "lacs": 10 ** 5,
              "crore": 10 ** 7, "crores": 10 ** 7, "million": 10 ** 6, "millions": 10 ** 6,
              "billion": 10 ** 9, "billions": 10 ** 9, "trillion": 10 ** 12}
_EN_SCALE_OR_HUNDRED = set(_EN_SCALES) | {"hundred", "hundreds"}
_TOKEN_RE = re.compile(r"\s+|\d+(?:\.\d+)?|[a-z']+|[^\sa-z0-9']")


def _next_word(toks: List[str], j: int) -> Optional[str]:
    j += 1
    while j < len(toks) and toks[j].isspace():
        j += 1
    return toks[j] if j < len(toks) else None


def _digit_word(tok: Optional[str]) -> Optional[str]:
    if tok is not None and tok in _EN_UNITS and _EN_UNITS[tok] < 10:
        return str(_EN_UNITS[tok])
    if tok is not None and re.fullmatch(r"\d", tok):
        return tok
    return None


def _en_run(toks: List[str], i: int):
    """Parse one English number-word run starting at toks[i]. -> (value, index_after) or None."""
    n = len(toks)
    j, total, cur, last, consumed, end = i, 0, 0, None, 0, i
    value: Optional[float] = None
    while j < n:
        t = toks[j]
        if t.isspace():
            j += 1
            continue
        if t in _EN_UNITS:
            v = _EN_UNITS[t]
            if last in ("unit", "teen") or (last == "tens" and v >= 10) or (last == "tens" and v == 0):
                break
            if v == 0 and consumed:
                break
            cur += v
            last = "teen" if v >= 10 else "unit"
        elif t in _EN_TENS:
            if last in ("unit", "teen", "tens"):
                break
            cur += _EN_TENS[t]
            last = "tens"
        elif t in ("hundred", "hundreds"):
            if last == "hundred":
                break
            cur = (cur or 1) * 100
            last = "hundred"
        elif t in _EN_SCALES:
            total += (cur or 1) * _EN_SCALES[t]
            cur, last = 0, "scale"
        elif t in ("a", "an"):
            nxt = _next_word(toks, j)
            if consumed or nxt not in _EN_SCALE_OR_HUNDRED:
                break
            cur, last = 1, "article"
        elif t == "and":
            nxt = _next_word(toks, j)
            if consumed and last in ("hundred", "scale") and nxt is not None and (nxt in _EN_UNITS or nxt in _EN_TENS):
                j += 1
                continue
            break
        elif re.fullmatch(r"\d+(?:\.\d+)?", t):
            nxt = _next_word(toks, j)
            if nxt in _EN_SCALE_OR_HUNDRED and (consumed == 0 or last == "scale"):
                cur = float(t) if "." in t else int(t)
                last = "num"
            else:
                break
        elif t == "point" and consumed and last in ("unit", "teen", "tens", "hundred", "scale", "num"):
            k, digits = j + 1, ""
            while k < n:
                if toks[k].isspace():
                    k += 1
                    continue
                dw = _digit_word(toks[k])
                if dw is None:
                    break
                digits += dw
                end = k
                k += 1
            if not digits:
                break
            value = total + cur + float("0." + digits)
            j = k
            consumed += 1
            break
        else:
            break
        consumed += 1
        end = j
        j += 1
    if not consumed:
        return None
    if value is None:
        value = total + cur
    return value, end + 1


def _words_to_numbers_en(s: str) -> str:
    s = re.sub(r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-(one|two|three|four|five|six|seven|eight|nine)\b",
               r"\1 \2", s)
    toks = _TOKEN_RE.findall(s)
    out: List[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        starts = (t in _EN_UNITS or t in _EN_TENS or t in _EN_SCALE_OR_HUNDRED or t in _EN_SCALES
                  or t in ("a", "an") or re.fullmatch(r"\d+(?:\.\d+)?", t))
        if starts and not t.isspace():
            r = _en_run(toks, i)
            if r is not None:
                val, nxt = r
                if isinstance(val, float) and val == int(val):
                    val = int(val)
                out.append(str(val) if not isinstance(val, float) else format_number(val))
                i = nxt
                continue
        out.append(t)
        i += 1
    return "".join(out)


_HI_FRACTIONS = {"डेढ": 1.5, "ढाई": 2.5}
_HI_POWERS = (100, 1000, 100000, 10000000)


def _hindi_fractions(s: str) -> str:
    """डेढ़ / ढाई / सवा / साढ़े / पौने followed by a number (after hindi_to_ascii_numbers)."""
    def mult(m):
        base = _HI_FRACTIONS[m.group(1)]
        n = int(m.group(2))
        return format_number(base * n if n in _HI_POWERS else base) + ("" if n in _HI_POWERS else " " + m.group(2))

    s = re.sub(_hf(r"(?<!\S)(डेढ़|ढाई)\s+(\d+)"), mult, s)
    s = re.sub(_hf(r"(?<!\S)(डेढ़|ढाई)(?!\S)"), lambda m: str(_HI_FRACTIONS[m.group(1)]), s)

    def sava(m):
        n = float(m.group(2))
        w = m.group(1)
        if w == _hf("सवा"):
            return format_number(n * 1.25 if n in _HI_POWERS else n + 0.25)
        if w == _hf("साढ़े"):
            return format_number(n + 0.5)
        return format_number(n - 0.25)

    return re.sub(_hf(r"(?<!\S)(सवा|साढ़े|पौने)\s+(\d+(?:\.\d+)?)"), sava, s)


_SYMBOL_WORDS = {"$": "dollars", "₹": "rupees", "€": "euros", "£": "pounds", "¥": "yen"}
_LEAD_FILLERS = [
    "raziel", "hey", "hi", "hello", "ok", "okay", "so", "please", "kindly", "just", "quickly", "yo", "um", "umm",
    "uh", "uhh", "actually", "and", "now", "well", "alright", "can you", "could you", "would you", "will you",
    "i want you to", "i need you to", "i'd like you to", "tell me", "let me know", "do you know", "i want to know",
    "i wanna know", "can you please", "could you please", "help me",
    "batao", "bataiye", "mujhe batao", "mujhe bataiye", "aur",
]
_LEAD_FILLERS_HI = [
    "रजील", "रेजील", "राजील", "रज़ील", "कृपया", "जरा", "प्लीज", "अच्छा", "अरे", "सुनो", "क्या", "मुझे बताओ",
    "मुझे", "बताओ", "बताइए", "ओके", "हे", "अभी", "फिर", "और", "बता", "निकालो", "निकाल", "हिसाब लगाओ",
    "आप", "तुम", "बता सकते हैं", "बता सकती हैं", "बता सकते हो", "बता सकते", "बता सकती",
]
_TRAIL_FILLERS = ["please", "for me", "now", "right now", "quickly", "raziel", "thanks", "thank you", "today",
                  "equals", "equal", "is", "?", "=",
                  "kitna hota hai", "kitna hoga", "kitna hai", "kitne hote hain", "kitne hote hai", "kya hota hai",
                  "kya hai", "kya hoga", "batao", "bataiye", "bata do", "hota hai", "hoga", "hai"]
_TRAIL_FILLERS_HI = ["कृपया", "प्लीज", "जरा", "रजील", "बताइए", "बताओ", "बताइये", "बताए", "निकालो", "निकालिए",
                     "करो", "कीजिए", "कीजिये", "जी", "बराबर", "क्या", "कितना", "कितने", "कितनी", "होगा", "होगी",
                     "होंगे", "होता", "होती", "होते", "है", "हैं", "हुआ", "हूँ", "कर", "दीजिए", "दीजिये",
                     "बता सकते", "बता सकती", "सकते", "सकती"]
_LEAD_RE = re.compile(r"^(?:%s)[,.]?\s+" % "|".join(
    re.escape(w) for w in sorted(_LEAD_FILLERS + [lang.fold_hindi(w) for w in _LEAD_FILLERS_HI], key=len, reverse=True)))
_TRAIL_RE = re.compile(r"\s+(?:%s)$" % "|".join(
    re.escape(w) for w in sorted(_TRAIL_FILLERS + [lang.fold_hindi(w) for w in _TRAIL_FILLERS_HI], key=len, reverse=True)))


_HI_SCALE_WORDS = {lang.fold_hindi(w): v for w, v in
                   (("सौ", 100), ("हज़ार", 1000), ("हजार", 1000), ("लाख", 10 ** 5), ("करोड़", 10 ** 7), ("करोड", 10 ** 7))}


def _digit_scales_hi(s: str) -> str:
    """'1.5 करोड़' / '2.5 लाख' (a digit number before a Hindi scale word) -> the plain number."""
    def sub(m):
        mult = _HI_SCALE_WORDS.get(lang.fold_hindi(m.group(2)))
        if mult is None:
            return m.group(0)
        return format_number(float(m.group(1)) * mult) if "." in m.group(1) else str(int(m.group(1)) * mult)
    return re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s+([ऀ-ॿ]+)(?![ऀ-ॿ])", sub, s)


_SAVA = {lang.fold_hindi(w): d for w, d in (("सवा", 0.25), ("साढ़े", 0.5), ("साढे", 0.5), ("पौने", -0.25))}


def _sava_scale_hi(s: str) -> str:
    """'साढ़े तीन सौ' (350), 'सवा दो हज़ार' (2250), 'पौने दो लाख' (175000): quarter/half words before number + scale."""
    toks = re.findall(r"\s+|\S+", s)
    out: List[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        d = None if t.isspace() else _SAVA.get(lang.fold_hindi(t))
        if d is not None and i + 4 < len(toks) and toks[i + 1].isspace() and toks[i + 3].isspace():
            digits = lang.hindi_to_ascii_numbers(toks[i + 2])
            mult = _HI_SCALE_WORDS.get(lang.fold_hindi(toks[i + 4]))
            if mult and re.fullmatch(r"\d+", digits):
                out.append(format_number((int(digits) + d) * mult))
                i += 5
                continue
        out.append(t)
        i += 1
    return "".join(out)


def _prepare(text: str) -> str:
    """
    Transcript -> canonical lower-case text: Hindi folded, all numbers as ASCII digits, symbols
    spoken, punctuation reduced to the few math characters, filler words stripped.
    """
    s = (text or "").strip()
    if not s:
        return ""
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = s.replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
    s = re.sub(r"(\d)²", r"\1^2", s)
    s = re.sub(r"(\d)³", r"\1^3", s)
    s = s.replace("²", "2").replace("³", "3")
    s = re.sub(r"([$₹€£¥])\s*(\d[\d,]*(?:\.\d+)?)", lambda m: f"{m.group(2)} {_SYMBOL_WORDS[m.group(1)]}", s)
    s = re.sub(r"\b[Rr]s\.?\s*(\d[\d,]*(?:\.\d+)?)", r"\1 rupees", s)
    if lang.has_devanagari(s):
        s = re.sub(r"(?<!\S)(बता|बताओ|कर|निकाल|बदल|बाँट|बांट|जोड़|जोड)\s+दो(?!\S)", r"\1", s)
        s = _digit_scales_hi(_sava_scale_hi(lang.devanagari_digits_to_ascii(s)))          # "1.5 करोड़" -> 15000000
        s = lang.hindi_to_ascii_numbers(s)
    s = lang.fold_hindi(s)
    s = s.replace("।", " ")
    s = _hindi_fractions(s)
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    s = re.sub(r"[?!;:\"“”]", " ", s)
    s = re.sub(r"\bper\s+cent\b", "percent", s)
    s = s.replace("%", " percent ")
    s = _words_to_numbers_en(s)
    s = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)k\b", lambda m: format_number(float(m.group(1)) * 1000), s)
    s = re.sub(r"(\d+)\s+point\s+(\d+)\b", r"\1.\2", s)
    s = re.sub(_hf(r"(\d+)\s+(?:पॉइंट|पाइंट|प्वाइंट|दशमलव)\s+(\d+)"), r"\1.\2", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(6):
        before = s
        s = re.sub(r"^[.,\s]+|[.,\s]+$", "", s)
        s = _LEAD_RE.sub("", s, count=1)
        s = _TRAIL_RE.sub("", s, count=1)
        s = s.strip()
        if s == before:
            break
    return s


# ------------------------------------------------------------------ spoken math -> Parsed

@dataclass
class Parsed:
    kind: str
    expr: str
    vals: Dict[str, str] = field(default_factory=dict)
    radians: bool = False
    nums: List[str] = field(default_factory=list)


_N = r"(-?\d+(?:\.\d+)?)"
_PCT = r"(?:percent|percentage)"
_PCT_HI = _hf(r"(?:प्रतिशत|फीसदी|फीसद|परसेंट|percent)")
_LIST = r"(\d+(?:\.\d+)?(?:[\s,]+(?:and\s+|और\s+|aur\s+)?\d+(?:\.\d+)?)+)"
_LEAD_MATH_RE = re.compile(
    r"^(?:(?:what(?:'s| is| are| would be| will be)|whats|how much (?:is|are)|calculate|compute|solve|"
    r"work out|find|evaluate|figure out|give me|do)\s+)+(?:the (?:answer|result|value) (?:to|of|for)\s+)?")
_PEOPLE = r"(?:\s+(?:people|persons|person|ways|of us|friends|guys|folks|members))?"
_FRACS = {"half": (1, 2), "halves": (1, 2), "third": (1, 3), "thirds": (1, 3), "quarter": (1, 4),
          "quarters": (1, 4), "fourth": (1, 4), "fourths": (1, 4), "fifth": (1, 5), "fifths": (1, 5),
          "sixth": (1, 6), "sixths": (1, 6), "eighth": (1, 8), "eighths": (1, 8), "tenth": (1, 10),
          "tenths": (1, 10)}
_FRAC_WORDS = "|".join(sorted(_FRACS, key=len, reverse=True))
_FRAC_HI = {_hf("तिहाई"): (1, 3), _hf("चौथाई"): (1, 4), _hf("पांचवां"): (1, 5), _hf("पाचवा"): (1, 5)}
_OPS_EN = {"plus": "+", "minus": "-", "times": "*", "multiplied by": "*", "x": "*", "divided by": "/",
           "over": "/", "mod": "%", "modulo": "%", "modulus": "%"}
_OPS_HI = {_hf("जमा"): "+", _hf("प्लस"): "+", _hf("घटा"): "-", _hf("माइनस"): "-", _hf("गुणा"): "*",
           _hf("गुना"): "*", _hf("टाइम्स"): "*", _hf("भाग"): "/", _hf("बटा"): "/", _hf("मोड"): "%"}
_OP_WORDS = "|".join(sorted([re.escape(k) for k in list(_OPS_EN) + list(_OPS_HI)], key=len, reverse=True))

# (kind, regex, group names).  Regexes are matched with fullmatch on the prepared text.
_PATTERNS: List[Tuple[str, "re.Pattern", Tuple[str, ...]]] = []


def _add(kind: str, pattern: str, names: Tuple[str, ...], hindi: bool = False) -> None:
    _PATTERNS.append((kind, re.compile(_hf(pattern) if hindi else pattern), names))


# --- English
_add("percent_of", rf"{_N} {_PCT} of {_N}", ("p", "n"))
_add("pct_is", rf"what {_PCT} is {_N} of {_N}", ("a", "b"))
_add("pct_is", rf"what {_PCT} of {_N} is {_N}", ("b", "a"))
_add("pct_is", rf"{_N} is what {_PCT} of {_N}", ("a", "b"))
_add("inc", rf"(?:increase|raise|grow|bump up|add to) {_N} by {_N} {_PCT}", ("n", "p"))
_add("inc", rf"add {_N} {_PCT} to {_N}", ("p", "n"))
_add("inc", rf"{_N} (?:increased|raised|plus|\+) (?:by )?{_N} {_PCT}", ("n", "p"))
_add("dec", rf"(?:decrease|reduce|cut|lower|drop|subtract) {_N} by {_N} {_PCT}", ("n", "p"))
_add("dec", rf"{_N} (?:decreased|reduced|minus|less) (?:by )?{_N} {_PCT}", ("n", "p"))
_add("dec", rf"(?:take|knock|subtract) {_N} {_PCT} (?:off|from) {_N}", ("p", "n"))
_add("discount", rf"{_N} {_PCT} (?:discount|off)(?: (?:on|of|from|for))? {_N}", ("p", "n"))
_add("discount", rf"discount of {_N} {_PCT} (?:on|of|from|for) {_N}", ("p", "n"))
_add("discount", rf"{_N} (?:with|at) (?:a )?{_N} {_PCT} (?:discount|off)", ("n", "p"))
_add("tip", rf"(?:(?:a|an) )?{_N} {_PCT} tip (?:on|for|of) (?:a )?(?:the )?(?:bill of )?{_N}", ("p", "n"))
_add("tip", rf"(?:the )?tip (?:of|at|for) {_N} {_PCT} (?:on|for) (?:a )?(?:the )?(?:bill of )?{_N}", ("p", "n"))
_add("tip", rf"(?:the )?tip (?:on|for) (?:a )?(?:the )?(?:bill of )?{_N} at {_N} {_PCT}", ("n", "p"))
_add("split", rf"(?:split|share|divide) (?:the )?(?:bill (?:of |for )?)?{_N} (?:between|among|amongst|in|with) {_N}{_PEOPLE}", ("n", "k"))
_add("split", rf"{_N} (?:split|divided|shared) (?:between|among|amongst) {_N}{_PEOPLE}", ("n", "k"))
_add("split", rf"{_N} split {_N} ways", ("n", "k"))
_add("split", rf"(?:split|share) (?:the )?(?:bill (?:of |for )?)?{_N} {_N} ways", ("n", "k"))
_add("avg", rf"(?:the )?(?:average|mean) (?:of )?{_LIST}", ("list",))
_add("sum", rf"(?:the )?sum of {_LIST}", ("list",))
_add("sum", rf"add {_LIST}", ("list",))
_add("sum", rf"add {_N} to {_N}", ("n", "n"))
_add("product", rf"(?:the )?product of {_LIST}", ("list",))
_add("product", rf"multiply {_N} (?:and|by|with|times)? ?{_N}", ("n", "n"))
_add("sub_from", rf"subtract {_N} from {_N}", ("a", "b"))
_add("divide", rf"divide {_N} by {_N}", ("a", "b"))
_add("diff", rf"(?:the )?difference (?:between|of) {_N} and {_N}", ("a", "b"))
_add("half", rf"(?:a )?half (?:of )?{_N}", ("n",))
_add("frac", rf"(?:(\d+) |a |an )?({_FRAC_WORDS}) of {_N}", ("k", "f", "n"))
_add("double", rf"(?:double|twice) {_N}", ("n",))
_add("triple", rf"(?:triple|thrice) {_N}", ("n",))
_add("sqrt", rf"(?:the )?(?:square root of|root of|root|sqrt of|sqrt) {_N}", ("n",))
_add("cbrt", rf"(?:the )?cube root of {_N}", ("n",))
_add("square", rf"(?:the )?square of {_N}", ("n",))
_add("square", rf"{_N} squared", ("n",))
_add("cube", rf"(?:the )?cube of {_N}", ("n",))
_add("cube", rf"{_N} cubed", ("n",))
_add("fact", rf"(?:the )?factorial of {_N}", ("n",))
_add("fact", rf"{_N} factorial", ("n",))
_add("power", rf"{_N} (?:to the power of|to the power|raised to the power of|raised to|power) {_N}", ("a", "b"))
_add("trig", rf"(sin|sine|cos|cosine|tan|tangent) (?:of )?{_N}(?: (degrees|degree|radians|radian))?", ("f", "n", "u"))
_add("logn", rf"(?:the )?(natural log|natural logarithm|ln|log|logarithm|log2|log base 2|log base 10) (?:of )?{_N}", ("f", "n"))

# --- Hindi (Devanagari; folded by _hf like the input)
_add("percent_of", rf"{_N} का {_N} {_PCT_HI}", ("n", "p"), True)
_add("percent_of", rf"{_N} {_PCT_HI} {_N} का", ("p", "n"), True)
_add("percent_of", rf"{_N} ka {_N} {_PCT}", ("n", "p"))                       # Hinglish
_add("sqrt", rf"{_N} ka (?:square root|sqrt|varg ?mool)", ("n",))
_add("square", rf"{_N} ka (?:square|varg)", ("n",))
_add("pct_is", rf"{_N}[\s,]+{_N} का (?:कितना|कितने) {_PCT_HI}", ("a", "b"), True)
_add("pct_is", rf"{_N} (?:है )?{_N} का (?:कितना|कितने) {_PCT_HI}", ("a", "b"), True)
_add("pct_is", rf"{_N} का (?:कितना|कितने) {_PCT_HI} {_N}", ("b", "a"), True)
_add("inc", rf"{_N} (?:मे|पर) {_N} {_PCT_HI} (?:जोड|बढ)\S*(?: करो)?", ("n", "p"), True)
_add("dec", rf"{_N} (?:मे से|मे|पर|से) {_N} {_PCT_HI} (?:घटा|कम|कट)\S*(?: करो)?", ("n", "p"), True)
_add("discount", rf"{_N} पर {_N} {_PCT_HI} (?:की )?(?:छूट|डिस्काउंट)", ("n", "p"), True)
_add("tip", rf"{_N} पर {_N} {_PCT_HI} (?:की )?टिप", ("n", "p"), True)
_add("tip", rf"{_N} {_PCT_HI} टिप {_N} पर", ("p", "n"), True)
_add("split", rf"{_N} को {_N} (?:लोगो|लोग|दोस्तो|व्यक्तियो|आदमियो|हिस्सो|हिस्से|जनो)? ?मे बाट\S*", ("n", "k"), True)
_add("avg", rf"{_LIST} का (?:औसत|एवरेज)", ("list",), True)
_add("sum", rf"{_LIST} का (?:जोड|योग)", ("list",), True)
_add("sum", rf"{_N} और {_N} का (?:जोड|योग)", ("n", "n"), True)
_add("product", rf"{_LIST} का गुणनफल", ("list",), True)
_add("product", rf"{_N} और {_N} का गुणनफल", ("n", "n"), True)
_add("diff", rf"{_N} और {_N} का (?:अंतर|फर्क|फरक)", ("a", "b"), True)
_add("half", rf"(?:आधा|आधे) {_N}", ("n",), True)
_add("half", rf"{_N} का (?:आधा|आधे)", ("n",), True)
_add("double", rf"{_N} का (?:दोगुना|दुगना|दुगुना|दोगुनी)", ("n",), True)
_add("double", rf"(?:दोगुना|दुगना|दुगुना) {_N}", ("n",), True)
_add("triple", rf"{_N} का (?:तिगुना|तीनगुना)", ("n",), True)
_add("triple", rf"(?:तिगुना|तीनगुना) {_N}", ("n",), True)
_add("frac_hi", rf"{_N} का (?:(\d+) )?({'|'.join(_FRAC_HI)})", ("n", "k", "f"), True)
_add("frac_hi", rf"(?:(\d+) )?({'|'.join(_FRAC_HI)}) {_N}", ("k", "f", "n"), True)
_add("sqrt", rf"{_N} का (?:वर्गमूल|वर्ग मूल|स्क्वायर रूट)", ("n",), True)
_add("cbrt", rf"{_N} का (?:घनमूल|घन मूल|क्यूब रूट)", ("n",), True)
_add("square", rf"{_N} का (?:वर्ग|स्क्वायर)", ("n",), True)
_add("cube", rf"{_N} का (?:घन|क्यूब)", ("n",), True)
_add("fact", rf"{_N} का (?:फैक्टोरियल|फैक्टोरिअल|फैक्टोरीयल|क्रमगुणित)", ("n",), True)
_add("power", rf"{_N} (?:की|का) (?:घात|पावर) {_N}", ("a", "b"), True)
_add("power", rf"{_N} पावर {_N}", ("a", "b"), True)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _paren(x: str) -> str:
    return f"({x})"


def _build(kind: str, v: Dict[str, str], nums: List[str], extra: Dict[str, str]) -> Optional[Parsed]:
    g = v.get
    if kind == "percent_of":
        return Parsed(kind, f"({g('n')})*({g('p')})/100", v)
    if kind == "pct_is":
        return Parsed(kind, f"({g('a')})/({g('b')})*100", v)
    if kind == "inc":
        return Parsed(kind, f"({g('n')})*(1+({g('p')})/100)", v)
    if kind in ("dec", "discount"):
        return Parsed(kind, f"({g('n')})*(1-({g('p')})/100)", v)
    if kind == "tip":
        return Parsed(kind, f"({g('n')})*(1+({g('p')})/100)", v)
    if kind == "split":
        return Parsed(kind, f"({g('n')})/({g('k')})", v)
    if kind == "avg":
        return Parsed(kind, f"({'+'.join(_paren(x) for x in nums)})/{len(nums)}", v, nums=nums)
    if kind == "sum":
        return Parsed(kind, "+".join(_paren(x) for x in nums), v, nums=nums)
    if kind == "product":
        return Parsed(kind, "*".join(_paren(x) for x in nums), v, nums=nums)
    if kind == "sub_from":
        return Parsed("binary", f"({g('b')})-({g('a')})", {"a": g("b"), "b": g("a"), "op": "-"})
    if kind == "divide":
        return Parsed("binary", f"({g('a')})/({g('b')})", {"a": g("a"), "b": g("b"), "op": "/"})
    if kind == "diff":
        return Parsed(kind, f"abs(({g('a')})-({g('b')}))", v)
    if kind == "half":
        return Parsed(kind, f"({g('n')})/2", v)
    if kind in ("frac", "frac_hi"):
        k = int(g("k") or 1)
        table = _FRACS if kind == "frac" else _FRAC_HI
        key = g("f")
        num, den = table[key]
        if kind == "frac" and key in ("half", "halves"):
            num, den = 1, 2
        return Parsed("frac", f"({g('n')})*{num * k}/{den}", {"n": g("n"), "k": str(k), "f": key, "den": str(den)})
    if kind == "double":
        return Parsed(kind, f"({g('n')})*2", v)
    if kind == "triple":
        return Parsed(kind, f"({g('n')})*3", v)
    if kind == "sqrt":
        return Parsed(kind, f"sqrt({g('n')})", v)
    if kind == "cbrt":
        return Parsed(kind, f"cbrt({g('n')})", v)
    if kind == "square":
        return Parsed(kind, f"({g('n')})**2", v)
    if kind == "cube":
        return Parsed(kind, f"({g('n')})**3", v)
    if kind == "fact":
        return Parsed(kind, f"factorial({g('n')})", v)
    if kind == "power":
        return Parsed(kind, f"({g('a')})**({g('b')})", v)
    if kind == "trig":
        f = g("f")
        fn = {"sin": "sin", "sine": "sin", "cos": "cos", "cosine": "cos", "tan": "tan", "tangent": "tan"}[f]
        rad = (g("u") or "").startswith("radian")
        return Parsed(kind, f"{fn}({g('n')})", {"n": g("n"), "f": fn, "u": "radians" if rad else "degrees"}, radians=rad)
    if kind == "logn":
        f = g("f")
        fn = "ln" if f in ("ln", "natural log", "natural logarithm") else ("log2" if f in ("log2", "log base 2") else "log")
        return Parsed(kind, f"{fn}({g('n')})", {"n": g("n"), "f": fn})
    return None


_CHAIN_SUBS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s+squared"), r"(\1)**2"),
    (re.compile(r"(\d+(?:\.\d+)?)\s+cubed"), r"(\1)**3"),
    (re.compile(r"(\d+(?:\.\d+)?)\s+factorial"), r"factorial(\1)"),
    (re.compile(r"(?:the )?square root of (\d+(?:\.\d+)?)"), r"sqrt(\1)"),
    (re.compile(r"(?:the )?cube root of (\d+(?:\.\d+)?)"), r"cbrt(\1)"),
    (re.compile(r"\b(?:to the power of|to the power|raised to the power of|raised to)\b"), " ** "),
    (re.compile(r"\b(?:multiplied by|times)\b"), " * "),
    (re.compile(r"\b(?:divided by|over)\b"), " / "),
    (re.compile(r"\bplus\b"), " + "),
    (re.compile(r"\b(?:minus|less)\b"), " - "),
    (re.compile(r"\bmod(?:ulo|ulus)?\b"), " % "),
    (re.compile(r"(?<=\d)\s*x\s*(?=\d)"), " * "),
    (re.compile(r"(?<!\S)(?:" + "|".join(re.escape(_hf(w)) for w in ("जमा", "प्लस")) + r")(?!\S)"), " + "),
    (re.compile(r"(?<!\S)(?:" + "|".join(re.escape(_hf(w)) for w in ("घटा", "माइनस")) + r")(?!\S)"), " - "),
    (re.compile(r"(?<!\S)(?:" + "|".join(re.escape(_hf(w)) for w in ("गुणा", "गुना", "टाइम्स")) + r")(?!\S)"), " * "),
    (re.compile(r"(?<!\S)(?:" + "|".join(re.escape(_hf(w)) for w in ("भाग", "बटा")) + r")(?!\S)"), " / "),
]
_SYM_OK = re.compile(r"[\d\s.+\-*/%()]+")
_BINARY_RE = re.compile(rf"{_N} ({_OP_WORDS}) {_N}")


def _chain(s: str) -> Optional[Parsed]:
    """'15 plus 27', '3 plus 5 squared', 'पंद्रह जमा सत्ताईस' ... -> Parsed('binary'|'chain')."""
    m = _BINARY_RE.fullmatch(s)
    if m:
        a, opw, b = m.group(1), m.group(2), m.group(3)
        sym = _OPS_EN.get(opw) or _OPS_HI[opw]
        return Parsed("binary", f"({a}){sym}({b})", {"a": a, "b": b, "op": opw})
    t = s
    for rx, rep in _CHAIN_SUBS:
        t = rx.sub(rep, t)
    t = re.sub(r"\s+", " ", t).strip()
    if t == s:
        return None                                    # no spoken operator word: not the chain form
    probe = re.sub(r"\b(?:sqrt|cbrt|factorial|pi)\b", "0", t)
    if not _SYM_OK.fullmatch(probe):
        return None
    if not re.search(r"[+\-*/%]|sqrt|cbrt|factorial", t):
        return None
    return Parsed("chain", t)


def _symbolic(s: str) -> Optional[Parsed]:
    """A plain symbolic expression ('15 * (3 + 2)', '2^10'); phone numbers, dates and bare numbers are refused."""
    t = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", s)
    if not re.fullmatch(r"[\d\s.+\-*/^()]+", t) or len(t) > MAX_EXPR_LEN:
        return None
    if not re.search(r"(?<=[\d)])\s*[+*/^]\s*(?=[\d(])|(?<=[\d)])\s+-\s+(?=[\d(])|\)\s*-\s*[\d(]|[\d)]\s*-\s*\(", t):
        return None            # bare numbers, '+919876543210', '555-1234', '12-05-2026' have no binary operator
    if re.fullmatch(r"\s*\d{1,4}([/.\-])\d{1,2}\1\d{1,4}\s*", t):
        return None                                    # dates
    if re.search(r"\d\s+\d", t):
        return None                                    # '98765 43210'
    return Parsed("symbolic", t)


def _parse_math(s: str) -> Optional[Parsed]:
    """`s` is _prepare()d text. Returns Parsed or None (not clearly arithmetic)."""
    s = _LEAD_MATH_RE.sub("", s, count=1).strip()
    s = re.sub(r"^(?:the )?(?:answer|result|value) (?:to|of|for) ", "", s)
    if not s or not re.search(r"\d", s):
        return None
    for kind, rx, names in _PATTERNS:
        m = rx.fullmatch(s)
        if not m:
            continue
        if names == ("list",):
            nums = _NUM_RE.findall(m.group(1))
            if len(nums) < 2:
                continue
            return _build(kind, {}, nums, {})
        if names == ("n", "n"):
            return _build(kind, {}, [m.group(1), m.group(2)], {})
        vals = {n: m.group(i + 1) for i, n in enumerate(names)}
        if kind in ("frac", "frac_hi") and vals.get("f") is None:
            continue
        return _build(kind, vals, [], {})
    return _chain(s) or _symbolic(s)


def parse_spoken_math(text: str) -> Optional[str]:
    """Spoken/typed math -> an expression string that evaluate() understands, or None."""
    try:
        p = _parse_math(_prepare(text))
    except Exception:
        logger.exception("calc: parse_spoken_math failed")
        return None
    return p.expr if p else None


# ------------------------------------------------------------------ spoken answers for parsed math

_OPW = {"+": {"en": "plus", "hi": "जमा"}, "-": {"en": "minus", "hi": "घटा"},
        "*": {"en": "times", "hi": "गुणा"}, "/": {"en": "divided by", "hi": "भाग"},
        "%": {"en": "mod", "hi": "मॉड"}}
_TRIG_NAMES = {"en": {"sin": "sine", "cos": "cosine", "tan": "tangent"},
               "hi": {"sin": "साइन", "cos": "कॉस", "tan": "टैन"}}
_LOG_NAMES = {"en": {"ln": "natural log", "log": "log", "log2": "log base 2"},
              "hi": {"ln": "प्राकृतिक लॉग", "log": "लॉग", "log2": "लॉग बेस 2"}}
_FRAC_EN = {2: ("half", "halves"), 3: ("third", "thirds"), 4: ("quarter", "quarters"), 5: ("fifth", "fifths"),
            6: ("sixth", "sixths"), 8: ("eighth", "eighths"), 10: ("tenth", "tenths")}
_FRAC_HINDI = {2: "आधा", 3: "तिहाई", 4: "चौथाई", 5: "पाँचवाँ हिस्सा", 6: "छठा हिस्सा", 8: "आठवाँ हिस्सा",
               10: "दसवाँ हिस्सा"}


def _lg(text: str = "") -> str:
    """Devanagari in the request means a Hindi answer, otherwise the language of the latest utterance."""
    return "hi" if lang.has_devanagari(text or "") else lang.current()


def _ns(x, lg: Optional[str] = None) -> str:
    """A number typed in the request ('65.50', '-3', '200000') -> spoken form ('65.5', 'minus 3', '200000')."""
    try:
        v = int(x) if re.fullmatch(r"-?\d+", x) else float(x)
    except (ValueError, TypeError):
        return str(x)
    return _say(v, lg)


def _money(v, lg: Optional[str] = None) -> str:
    return _say(round(v, 2) if isinstance(v, float) else v, lg)


def _answer_math(p: Parsed, lg: str) -> Optional[str]:
    """Evaluate a Parsed expression and phrase the answer. None if the expression turned out invalid."""
    try:
        r = evaluate_exact(p.expr, p.radians)
    except CalcError as e:
        if e.kind in ("zero", "big", "domain"):
            return _error_sentence(e, lg)
        return None
    v, k = p.vals, p.kind
    n = lambda key: _ns(v[key], lg)                     # noqa: E731
    R = _say(r, lg)
    if k == "binary":
        op = v["op"] if v["op"] in _OPW else (_OPS_EN.get(v["op"]) or _OPS_HI.get(v["op"], "+"))
        return _t("binary", lg, a=n("a"), op=_OPW[op][lg if lg in ("en", "hi") else "en"], b=n("b"), r=R)
    if k == "percent_of":
        return _t("percent_of", lg, p=n("p"), n=n("n"), r=R)
    if k == "pct_is":
        return _t("pct_is", lg, a=n("a"), b=n("b"), r=R)
    if k == "inc":
        return _t("inc", lg, n=n("n"), p=n("p"), r=_money(r, lg))
    if k == "dec":
        return _t("dec", lg, n=n("n"), p=n("p"), r=_money(r, lg))
    if k == "discount":
        saved = evaluate_exact(f"({v['n']})-({p.expr})")
        return _t("discount", lg, n=n("n"), p=n("p"), r=_money(r, lg), s=_money(saved, lg))
    if k == "tip":
        tip = evaluate_exact(f"({p.expr})-({v['n']})")
        return _t("tip", lg, n=n("n"), p=n("p"), t=_money(tip, lg), r=_money(r, lg))
    if k == "split":
        return _t("split", lg, n=n("n"), k=n("k"), r=_money(r, lg))
    if k in ("avg", "sum", "product"):
        lst = lang.join_list([_ns(x, lg) for x in p.nums], lg)
        return _t(k, lg, l=lst, r=R)
    if k == "diff":
        return _t("diff", lg, a=n("a"), b=n("b"), r=R)
    if k in ("half", "double", "triple"):
        return _t(k, lg, n=n("n"), r=R)
    if k == "frac":
        kk, den = int(v["k"]), int(v["den"])
        if lg == "hi":
            name = _FRAC_HINDI.get(den, f"1/{den}")
            f = f"{'एक' if kk == 1 else kk} {name}"
        else:
            one, many = _FRAC_EN.get(den, (f"1/{den}", f"1/{den}s"))
            f = f"one {one}" if kk == 1 else f"{kk} {many}"
            f = f[0].upper() + f[1:]
        return _t("frac", lg, f=f, n=n("n"), r=R)
    if k in ("sqrt", "cbrt", "square", "cube", "fact"):
        return _t(k, lg, n=n("n"), r=R)
    if k == "power":
        return _t("power", lg, a=n("a"), b=n("b"), r=R)
    if k == "trig":
        return _t("trig", lg, f=_TRIG_NAMES[lg if lg in _TRIG_NAMES else "en"][v["f"]], n=n("n"),
                  u=_t(("rad" if v["u"] == "radians" else "deg") + ("1" if float(v["n"]) == 1 else ""), lg), r=R)
    if k == "logn":
        return _t("logn", lg, f=_LOG_NAMES[lg if lg in _LOG_NAMES else "en"][v["f"]], n=n("n"), r=R)
    return _t("answer", lg, r=R)                      # chain / symbolic


# ------------------------------------------------------------------ unit tables

@dataclass(frozen=True)
class Unit:
    key: str
    cat: str
    factor: float                                   # to the category's base unit (temperature: unused)
    en: Tuple[str, str]                             # singular, plural
    hi: Tuple[str, str]


_UNITS: Dict[str, Unit] = {}
_UNIT_ALIAS: Dict[str, str] = {}


def _fold(a: str) -> str:
    return lang.fold_hindi(a).strip()


def _unit(key: str, cat: str, factor: float, en: str, hi: str, aliases: str) -> None:
    en1, _, en2 = en.partition("/")
    hi1, _, hi2 = hi.partition("/")
    _UNITS[key] = Unit(key, cat, factor, (en1, en2 or en1), (hi1, hi2 or hi1))
    for a in aliases.split(","):
        a = _fold(a)
        if a:
            _UNIT_ALIAS.setdefault(a, key)


# length (metre)
_unit("millimeter", "length", 0.001, "millimeter/millimeters", "मिलीमीटर", "mm,millimeter,millimetre,मिलीमीटर,मिमी")
_unit("centimeter", "length", 0.01, "centimeter/centimeters", "सेंटीमीटर",
      "cm,centimeter,centimetre,सेंटीमीटर,सेन्टीमीटर,सेमी,से.मी.")
_unit("meter", "length", 1.0, "meter/meters", "मीटर", "m,meter,metre,मीटर,मीटर्स")
_unit("kilometer", "length", 1000.0, "kilometer/kilometers", "किलोमीटर",
      "km,kms,kilometer,kilometre,kilo meter,किलोमीटर,किमी,कि.मी.,किलो मीटर")
_unit("inch", "length", 0.0254, "inch/inches", "इंच", "inch,inche,इंच,इंचों")
_unit("foot", "length", 0.3048, "foot/feet", "फुट", "foot,feet,ft,फुट,फीट,फूट,फ़ीट")
_unit("yard", "length", 0.9144, "yard/yards", "गज", "yard,yd,yds,गज")
_unit("mile", "length", 1609.344, "mile/miles", "मील", "mile,mi,मील,मिल")
# mass (gram)
_unit("milligram", "mass", 0.001, "milligram/milligrams", "मिलीग्राम", "mg,milligram,मिलीग्राम")
_unit("gram", "mass", 1.0, "gram/grams", "ग्राम", "g,gm,gms,gram,gramme,ग्राम,ग्राम्स")
_unit("kilogram", "mass", 1000.0, "kilogram/kilograms", "किलोग्राम",
      "kg,kgs,kilo,kilogram,kilogramme,किलो,किलोग्राम,केजी,कि.ग्रा.")
_unit("tonne", "mass", 1e6, "tonne/tonnes", "टन", "tonne,ton,metric ton,metric tonne,टन,टॉन")
_unit("quintal", "mass", 1e5, "quintal/quintals", "क्विंटल", "quintal,क्विंटल,कुंतल,क्विण्टल")
_unit("pound", "mass", 453.59237, "pound/pounds", "पाउंड", "pound,lb,lbs,पाउंड,पाउण्ड")
_unit("ounce", "mass", 28.349523125, "ounce/ounces", "औंस", "ounce,oz,औंस")
_unit("tola", "mass", 11.6638038, "tola/tolas", "तोला", "tola,तोला,तोले")
_unit("stone", "mass", 6350.29318, "stone/stones", "स्टोन", "stone,स्टोन")
# volume (litre)
_unit("milliliter", "volume", 0.001, "milliliter/milliliters", "मिलीलीटर", "ml,milliliter,millilitre,मिलीलीटर,मिली")
_unit("liter", "volume", 1.0, "liter/liters", "लीटर", "l,liter,litre,ltr,लीटर")
_unit("gallon", "volume", 3.785411784, "gallon/gallons", "गैलन", "gallon,gal,गैलन")
_unit("quart", "volume", 0.946352946, "quart/quarts", "क्वार्ट", "quart,qt,क्वार्ट")
_unit("pint", "volume", 0.473176473, "pint/pints", "पाइंट", "pint,pt,पाइंट")
_unit("cup", "volume", 0.2365882365, "cup/cups", "कप", "cup,कप")
_unit("floz", "volume", 0.0295735295625, "fluid ounce/fluid ounces", "फ्लूइड औंस", "fluid ounce,fl oz,floz,फ्लूइड औंस")
_unit("tbsp", "volume", 0.01478676478125, "tablespoon/tablespoons", "टेबलस्पून", "tablespoon,tbsp,tbs,टेबलस्पून")
_unit("tsp", "volume", 0.00492892159375, "teaspoon/teaspoons", "टीस्पून", "teaspoon,tsp,टीस्पून")
_unit("cubic_meter", "volume", 1000.0, "cubic meter/cubic meters", "घन मीटर",
      "cubic meter,cubic metre,m3,घन मीटर")
_unit("cubic_cm", "volume", 0.001, "cubic centimeter/cubic centimeters", "घन सेंटीमीटर",
      "cubic centimeter,cubic centimetre,cc,cm3,घन सेंटीमीटर")
# temperature (formulas)
_unit("celsius", "temp", 0.0, "degree Celsius/degrees Celsius", "डिग्री सेल्सियस",
      "celsius,centigrade,c,सेल्सियस,सेल्सीयस,सेंटीग्रेड,सेन्टीग्रेड")
_unit("fahrenheit", "temp", 0.0, "degree Fahrenheit/degrees Fahrenheit", "डिग्री फ़ारेनहाइट",
      "fahrenheit,f,फारेनहाइट,फैरेनहाइट,फारनहाइट,फेरनहाइट,फ़ैरेनहाइट,फेहरनहाइट,फारेनहीट")
_unit("kelvin", "temp", 0.0, "kelvin/kelvins", "केल्विन", "kelvin,केल्विन")
# speed (metre per second)
_unit("kmh", "speed", 1000.0 / 3600.0, "kilometer per hour/kilometers per hour", "किलोमीटर प्रति घंटा",
      "kmh,kmph,kph,km/h,km/hr,km per hour,kms per hour,km per hr,kilometer per hour,kilometre per hour,kilometers per hour,kilometres per hour,"
      "किलोमीटर प्रति घंटा,किमी प्रति घंटा,किलोमीटर प्रति घंटे")
_unit("mph", "speed", 0.44704, "mile per hour/miles per hour", "मील प्रति घंटा",
      "mph,mile per hour,miles per hour,mile an hour,miles an hour,मील प्रति घंटा,मील प्रति घंटे")
_unit("mps", "speed", 1.0, "meter per second/meters per second", "मीटर प्रति सेकंड",
      "m/s,mps,meter per second,metre per second,meters per second,metres per second,मीटर प्रति सेकंड")
_unit("knot", "speed", 1852.0 / 3600.0, "knot/knots", "नॉट", "knot,kt,kn,नॉट")
_unit("fps", "speed", 0.3048, "foot per second/feet per second", "फुट प्रति सेकंड",
      "fps,ft/s,foot per second,feet per second,फुट प्रति सेकंड")
# area (square metre)
_unit("sqm", "area", 1.0, "square meter/square meters", "वर्ग मीटर",
      "sqm,sq m,m2,sq meter,square meter,square metre,वर्ग मीटर")
_unit("sqkm", "area", 1e6, "square kilometer/square kilometers", "वर्ग किलोमीटर",
      "sqkm,sq km,km2,square kilometer,square kilometre,वर्ग किलोमीटर")
_unit("sqft", "area", 0.09290304, "square foot/square feet", "वर्ग फुट",
      "sqft,sq ft,ft2,square foot,square feet,वर्ग फुट,वर्ग फीट,वर्ग फूट")
_unit("sqyd", "area", 0.83612736, "square yard/square yards", "वर्ग गज", "sqyd,sq yd,square yard,वर्ग गज")
_unit("acre", "area", 4046.8564224, "acre/acres", "एकड़", "acre,एकड़,एकड")
_unit("hectare", "area", 10000.0, "hectare/hectares", "हेक्टेयर", "hectare,ha,हेक्टेयर,हेक्टर")
_unit("sqmi", "area", 2589988.110336, "square mile/square miles", "वर्ग मील", "sq mi,sqmi,square mile,वर्ग मील")
_unit("sqcm", "area", 0.0001, "square centimeter/square centimeters", "वर्ग सेंटीमीटर",
      "sq cm,sqcm,cm2,square centimeter,square centimetre,वर्ग सेंटीमीटर")
_unit("sqin", "area", 0.00064516, "square inch/square inches", "वर्ग इंच", "sq in,sqin,square inch,वर्ग इंच")
# data (byte, 1024 based)
_unit("bit", "data", 0.125, "bit/bits", "बिट", "bit,बिट")
_unit("byte", "data", 1.0, "byte/bytes", "बाइट", "byte,बाइट")
_unit("kb", "data", 1024.0, "kilobyte/kilobytes", "किलोबाइट", "kb,kilobyte,केबी,किलोबाइट")
_unit("mb", "data", 1024.0 ** 2, "megabyte/megabytes", "मेगाबाइट", "mb,megabyte,एमबी,मेगाबाइट")
_unit("gb", "data", 1024.0 ** 3, "gigabyte/gigabytes", "गीगाबाइट", "gb,gigabyte,जीबी,गीगाबाइट")
_unit("tb", "data", 1024.0 ** 4, "terabyte/terabytes", "टेराबाइट", "tb,terabyte,टीबी,टेराबाइट")
_unit("pb", "data", 1024.0 ** 5, "petabyte/petabytes", "पेटाबाइट", "pb,petabyte,पेटाबाइट")
# time (second)
_unit("millisecond", "time", 0.001, "millisecond/milliseconds", "मिलीसेकंड", "ms,msec,millisecond,मिलीसेकंड")
_unit("second", "time", 1.0, "second/seconds", "सेकंड", "second,sec,सेकंड,सेकेंड,सेकण्ड,सैकंड,सेकेण्ड,सेकन्ड")
_unit("minute", "time", 60.0, "minute/minutes", "मिनट", "minute,min,मिनट,मिनिट")
_unit("hour", "time", 3600.0, "hour/hours", "घंटा/घंटे", "hour,hr,घंटा,घंटे,घंटो,घंटों,घण्टा,घण्टे,घण्टों")
_unit("day", "time", 86400.0, "day/days", "दिन", "day,दिन,दिनों")
_unit("week", "time", 604800.0, "week/weeks", "हफ्ता/हफ्ते", "week,हफ्ता,हफ्ते,हफ्तों,सप्ताह,हफता,हफते")
_unit("month", "time", 2629800.0, "month/months", "महीना/महीने", "month,महीना,महीने,महीनों,माह")
_unit("year", "time", 31557600.0, "year/years", "साल", "year,yr,साल,सालों,वर्ष,बरस")


def _norm_phrase(text: str) -> str:
    t = _fold(text)
    t = re.sub(r"^(?:degrees?|deg|°|डिग्री)\s*", "", t)
    return re.sub(r"\s+", " ", t).strip(" .,")


def _alias_lookup(table: Dict[str, str], phrase: str) -> Optional[str]:
    t = _norm_phrase(phrase)
    if not t:
        return None
    if t in table:
        return table[t]
    if t.endswith("s") and t[:-1] in table:
        return table[t[:-1]]
    if t.endswith("es") and t[:-2] in table:
        return table[t[:-2]]
    return None


def find_unit(phrase: str) -> Optional[Unit]:
    """'kilometers' / 'km' / 'किलोमीटर' -> Unit, or None."""
    k = _alias_lookup(_UNIT_ALIAS, phrase or "")
    return _UNITS.get(k) if k else None


def _to_celsius(v: float, key: str) -> float:
    if key == "celsius":
        return v
    if key == "fahrenheit":
        return (v - 32.0) * 5.0 / 9.0
    return v - 273.15


def _from_celsius(c: float, key: str) -> float:
    if key == "celsius":
        return c
    if key == "fahrenheit":
        return c * 9.0 / 5.0 + 32.0
    return c + 273.15


def convert_value(value: float, src, dst) -> float:
    """Numeric conversion. `src`/`dst` are Unit objects or any name/abbreviation ('km', 'miles', 'फुट').
    Raises CalcError('unit') for unknown or incompatible units, CalcError('domain') below absolute zero."""
    a = src if isinstance(src, Unit) else find_unit(str(src))
    b = dst if isinstance(dst, Unit) else find_unit(str(dst))
    if a is None or b is None or a.cat != b.cat:
        raise CalcError("unit")
    value = float(value)
    if a.cat == "temp":
        c = _to_celsius(value, a.key)
        if c < -273.15 - 1e-9:
            raise CalcError("domain", "below_zero")
        return _from_celsius(c, b.key)
    return value * a.factor / b.factor


# ------------------------------------------------------------------ currency tables

@dataclass(frozen=True)
class Currency:
    code: str
    en: Tuple[str, str]
    hi: Tuple[str, str]
    supported: bool = True


_CURRENCIES: Dict[str, Currency] = {}
_CUR_ALIAS: Dict[str, str] = {}


def _cur(code: str, en: str, hi: str, aliases: str, supported: bool = True) -> None:
    en1, _, en2 = en.partition("/")
    hi1, _, hi2 = hi.partition("/")
    _CURRENCIES[code] = Currency(code, (en1, en2 or en1), (hi1, hi2 or hi1), supported)
    for a in (code.lower() + "," + aliases).split(","):
        a = _fold(a)
        if a:
            _CUR_ALIAS.setdefault(a, code)


_cur("USD", "US dollar/US dollars", "डॉलर",
     "dollar,us dollar,american dollar,buck,डॉलर,डालर,डॉलर्स,अमेरिकी डॉलर,यूएस डॉलर")
_cur("INR", "rupee/rupees", "रुपया/रुपये",
     "rupee,rs,indian rupee,rupaye,rupaya,rupiye,रुपया,रुपये,रुपए,रुपयों,रूपये,रुपिया,भारतीय रुपये,रुपयें,रूपया")
_cur("EUR", "euro/euros", "यूरो", "euro,यूरो")
_cur("GBP", "British pound/British pounds", "पाउंड",
     "pound,british pound,pound sterling,sterling,quid,पाउंड,ब्रिटिश पाउंड,पौंड")
_cur("JPY", "Japanese yen/Japanese yen", "येन", "yen,japanese yen,येन")
_cur("AUD", "Australian dollar/Australian dollars", "ऑस्ट्रेलियाई डॉलर",
     "australian dollar,aussie dollar,ऑस्ट्रेलियाई डॉलर,ऑस्ट्रेलियन डॉलर,आस्ट्रेलियाई डॉलर")
_cur("CAD", "Canadian dollar/Canadian dollars", "कनाडाई डॉलर", "canadian dollar,कनाडाई डॉलर,कनाडियन डॉलर")
_cur("CHF", "Swiss franc/Swiss francs", "स्विस फ्रैंक", "franc,swiss franc,स्विस फ्रैंक,फ्रैंक")
_cur("CNY", "Chinese yuan/Chinese yuan", "युआन", "yuan,renminbi,rmb,chinese yuan,युआन")
_cur("SGD", "Singapore dollar/Singapore dollars", "सिंगापुर डॉलर", "singapore dollar,सिंगापुर डॉलर")
_cur("NZD", "New Zealand dollar/New Zealand dollars", "न्यूज़ीलैंड डॉलर",
     "new zealand dollar,kiwi dollar,न्यूजीलैंड डॉलर,न्यूज़ीलैंड डॉलर")
_cur("HKD", "Hong Kong dollar/Hong Kong dollars", "हांगकांग डॉलर", "hong kong dollar,हांगकांग डॉलर")
_cur("KRW", "South Korean won/South Korean won", "वॉन", "won,korean won,south korean won,वॉन")
_cur("SEK", "Swedish krona/Swedish kronor", "स्वीडिश क्रोना", "swedish krona,swedish kronor,krona,स्वीडिश क्रोना")
_cur("NOK", "Norwegian krone/Norwegian kroner", "नॉर्वेजियन क्रोन", "norwegian krone,norwegian kroner,नॉर्वेजियन क्रोन")
_cur("DKK", "Danish krone/Danish kroner", "डेनिश क्रोन", "danish krone,danish kroner,डेनिश क्रोन")
_cur("PLN", "Polish zloty/Polish zloty", "ज़्लॉटी", "zloty,polish zloty,ज़्लॉटी,ज्लॉटी")
_cur("CZK", "Czech koruna/Czech koruna", "चेक कोरुना", "koruna,czech koruna,चेक कोरुना")
_cur("HUF", "Hungarian forint/Hungarian forint", "फ़ोरिंट", "forint,hungarian forint,फोरिंट")
_cur("RON", "Romanian leu/Romanian lei", "रोमानियाई लेउ", "leu,romanian leu,romanian lei,रोमानियाई लेउ")
_cur("BGN", "Bulgarian lev/Bulgarian leva", "बुल्गारियाई लेव", "lev,leva,bulgarian lev,बुल्गारियाई लेव")
_cur("TRY", "Turkish lira/Turkish lira", "तुर्की लीरा", "lira,turkish lira,तुर्की लीरा,लीरा")
_cur("ZAR", "South African rand/South African rand", "दक्षिण अफ़्रीकी रैंड",
     "rand,south african rand,दक्षिण अफ्रीकी रैंड,रैंड")
_cur("BRL", "Brazilian real/Brazilian reais", "ब्राज़ीलियाई रियाल", "brazilian real,brazilian reais,ब्राजीलियाई रियाल")
_cur("MXN", "Mexican peso/Mexican pesos", "मैक्सिकन पेसो", "mexican peso,मैक्सिकन पेसो")
_cur("IDR", "Indonesian rupiah/Indonesian rupiah", "इंडोनेशियाई रुपिया", "rupiah,indonesian rupiah,इंडोनेशियाई रुपिया")
_cur("MYR", "Malaysian ringgit/Malaysian ringgit", "मलेशियाई रिंगिट", "ringgit,malaysian ringgit,मलेशियाई रिंगिट")
_cur("PHP", "Philippine peso/Philippine pesos", "फ़िलीपीनी पेसो", "philippine peso,filipino peso,फिलीपीनी पेसो")
_cur("THB", "Thai baht/Thai baht", "थाई बाट", "baht,thai baht,थाई बाट,बाट")
_cur("ILS", "Israeli shekel/Israeli shekels", "इज़राइली शेकेल", "shekel,israeli shekel,इजराइली शेकेल")
_cur("ISK", "Icelandic krona/Icelandic kronur", "आइसलैंडिक क्रोना", "icelandic krona,आइसलैंडिक क्रोना")
# named so that we can say "not supported" instead of guessing
_cur("AED", "UAE dirham/UAE dirhams", "दिरहम", "dirham,uae dirham,emirati dirham,दिरहम", False)
_cur("SAR", "Saudi riyal/Saudi riyals", "सऊदी रियाल", "riyal,saudi riyal,सऊदी रियाल,रियाल", False)
_cur("PKR", "Pakistani rupee/Pakistani rupees", "पाकिस्तानी रुपये", "pakistani rupee,पाकिस्तानी रुपये,पाकिस्तानी रुपया", False)
_cur("BDT", "Bangladeshi taka/Bangladeshi taka", "टका", "taka,bangladeshi taka,टका", False)
_cur("NPR", "Nepali rupee/Nepali rupees", "नेपाली रुपये", "nepali rupee,nepalese rupee,नेपाली रुपये", False)
_cur("LKR", "Sri Lankan rupee/Sri Lankan rupees", "श्रीलंकाई रुपये", "sri lankan rupee,श्रीलंकाई रुपये", False)
_cur("RUB", "Russian ruble/Russian rubles", "रूबल", "ruble,rouble,russian ruble,रूबल", False)
_cur("QAR", "Qatari riyal/Qatari riyals", "क़तरी रियाल", "qatari riyal", False)
_cur("KWD", "Kuwaiti dinar/Kuwaiti dinars", "कुवैती दीनार", "dinar,kuwaiti dinar,कुवैती दीनार,दीनार", False)
_cur("EGP", "Egyptian pound/Egyptian pounds", "मिस्री पाउंड", "egyptian pound", False)
_cur("NGN", "Nigerian naira/Nigerian naira", "नाइजीरियाई नायरा", "naira,nigerian naira", False)


def find_currency(phrase: str) -> Optional[Currency]:
    """'dollars' / 'usd' / 'रुपये' -> Currency, or None."""
    k = _alias_lookup(_CUR_ALIAS, phrase or "")
    return _CURRENCIES.get(k) if k else None


# ------------------------------------------------------------------ exchange rates (Frankfurter, keyless)

_RATE_CACHE: Dict[Tuple[str, str], Tuple[float, float, str]] = {}
_clock = time.time                                   # tests replace this


def fetch_rate(from_code: str, to_code: str) -> Tuple[float, str]:
    """One live ECB reference rate: (rate, 'YYYY-MM-DD'). Raises netutil.NetError. Tests monkeypatch this."""
    data = netutil.get_json(FRANKFURTER_URL, {"from": from_code, "to": to_code})
    try:
        rate = float(data["rates"][to_code])
        date = str(data.get("date", ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise netutil.NetError("unexpected answer from the exchange-rate service") from None
    if not rate > 0 or not math.isfinite(rate):
        raise netutil.NetError("bad exchange rate")
    return rate, date


def get_rate(from_code: str, to_code: str) -> Tuple[float, str]:
    """fetch_rate() behind a 30 minute cache. Same currency -> (1.0, '')."""
    f, t = from_code.upper(), to_code.upper()
    if f == t:
        return 1.0, ""
    hit = _RATE_CACHE.get((f, t))
    now = _clock()
    if hit and now - hit[0] < RATE_CACHE_SECONDS:
        return hit[1], hit[2]
    rate, date = fetch_rate(f, t)
    _RATE_CACHE[(f, t)] = (now, rate, date)
    return rate, date


def convert_amount(amount: float, from_code: str, to_code: str) -> Tuple[float, str]:
    """(converted amount, rate date). Raises netutil.NetError; CalcError('currency') for unsupported codes."""
    for c in (from_code.upper(), to_code.upper()):
        cur = _CURRENCIES.get(c)
        if cur is None or not cur.supported:
            raise CalcError("currency", c)
    rate, date = get_rate(from_code, to_code)
    return amount * rate, date


def _rate_date(d: str, lg: str) -> str:
    try:
        return lang.fmt_date(datetime.date.fromisoformat(d), lg, with_weekday=False)
    except (ValueError, TypeError):
        return d or ("आज" if lg == "hi" else "today")


def _cur_name(c: Currency, amount: float, lg: str) -> str:
    names = c.hi if lg == "hi" else c.en
    return names[0] if abs(amount) == 1 else names[1]


# ------------------------------------------------------------------ conversion phrases

_NUM = r"(?:minus |negative |-)?\d+(?:\.\d+)?"
_CONNECT = _hf("in|into|to|as|and|equal|equals|is|are|for|worth|from|how|many|much|"
               "को|मे|से|के|का|की|है|हैं|और|कितने|कितना|कितनी|ko|me|mein|kitne|kitna|kitni|hai|hain")
_WORD = rf"(?!(?:{_CONNECT})(?![^\s]))[^\s\d]+"          # one word that is not a linking word
_W = rf"{_WORD}(?: {_WORD}){{0,2}}?"                    # a unit or currency name: one to three words
_SRC = (rf"(?:(?P<q1>{_NUM}) ?|(?P<a1>a|an) )(?P<u1>{_W})"
        rf"(?: (?:and |और )?(?P<q2>\d+(?:\.\d+)?) ?(?P<u2>{_W}))?")
_DST = rf"(?P<u3>{_W})"
_U1 = rf"(?P<u1>{_W})"
_HI_TAIL = _hf(r"(?: (?:होते|होता|होती|है|हैं|बनते|बनता|बनती|बने|बनेगा|बनेंगे|मिलेगा|मिलेंगे|होगा|होगी|होंगे))*")
_HOW_VERB = (r"(?:(?:is|are|in|for|equals?|are in|is in|are there in|is there in|do i get for|will i get for|"
             r"would i get for|can i get for|do you get for|are there for) )?")

_CONV_PATTERNS: List[Tuple[str, "re.Pattern"]] = []


def _addc(kind: str, pattern: str, hindi: bool = False) -> None:
    _CONV_PATTERNS.append((kind, re.compile(_hf(pattern) if hindi else pattern)))


_addc("convert", rf"(?:from )?{_SRC}(?: worth)? (?:in|into|to|as|equals?|equal to|=|in how many|is how many) {_DST}")
_addc("convert", rf"how (?:many|much) {_DST} {_HOW_VERB}{_SRC}")
_addc("rate", rf"(?:the )?(?:exchange )?rate (?:of |for |from |between )?(?:(?:a|an|1|one) )?{_U1} "
              rf"(?:to|in|into|against|and|vs|versus) {_DST}")
_addc("rate", rf"(?:the )?{_U1} (?:to|in|into|against) {_DST} (?:exchange )?rate")
_addc("rate1", rf"(?:the )?(?:current |latest |today's |todays )?{_U1} (?:exchange )?(?:rate|price)")
# Hindi (Devanagari)
_addc("convert", rf"{_SRC}(?: को)? {_DST} मे (?:बदल|कन्वर्ट|तब्दील|कर)\S*(?: (?:दो|करो|दीजिए))*", True)
_addc("convert", rf"{_SRC}(?: को)? {_DST} मे", True)
_addc("convert", rf"{_SRC}(?: मे)? (?:कितने|कितना|कितनी) {_DST}(?: के बराबर| का| की)?{_HI_TAIL}", True)
_addc("convert", rf"(?:कितने|कितना|कितनी) {_DST}{_HI_TAIL} {_SRC}(?: मे)?", True)
_addc("rate1", rf"(?:आज )?(?:का )?{_U1} (?:का |की )?(?:रेट|भाव|दर|कीमत)", True)
_addc("rate", rf"(?:आज )?{_U1} (?:से|को) {_DST} (?:का |की )?(?:रेट|भाव|दर)", True)
# Hinglish
_addc("convert", rf"{_SRC} (?:kitne|kitna|kitni) {_DST}(?: (?:hote|hota|hoti|hai|hain|honge|hoga|banenge|banta|bante))*")
_addc("convert", rf"{_SRC}(?: ko)? {_DST} (?:me|mein) (?:convert|badlo|badal)\w*(?: (?:karo|do|kijiye|kar do))*")

_CONV_LEAD_RE = re.compile(
    r"^(?:(?:what(?:'s| is| are| would be| will be)|whats|how much (?:is|are)|convert|change|turn|translate|"
    r"show me|find|calculate|compute|work out|the value of|exchange)\s+)+")


@dataclass
class Conv:
    kind: str                       # 'units' | 'currency' | 'rate'
    q1: float
    src: object
    dst: object
    q2: Optional[float] = None
    src2: Optional[object] = None


def _qval(text: str) -> float:
    t = text.strip()
    if t.startswith(("minus", "negative")):
        return -float(t.split()[-1])
    return float(t)


def _resolve_conv(kind: str, d: Dict[str, Optional[str]]) -> Optional[Conv]:
    u1, u3 = d.get("u1"), d.get("u3")
    q1 = 1.0 if d.get("a1") or kind in ("rate", "rate1") else (_qval(d["q1"]) if d.get("q1") else None)
    if q1 is None or not u1:
        return None
    c1 = find_currency(u1)
    if kind == "rate1":
        if c1 is None:
            return None
        return Conv("rate", 1.0, c1, _CURRENCIES["USD" if c1.code == "INR" else "INR"])
    if not u3:
        return None
    c3 = find_currency(u3)
    if c1 is not None and c3 is not None and not d.get("q2"):
        return Conv("rate" if kind == "rate" else "currency", q1, c1, c3)
    if kind == "rate":
        return None
    n1, n3 = find_unit(u1), find_unit(u3)
    if n1 is None or n3 is None or n1.cat != n3.cat:
        return None
    if d.get("q2"):
        n2 = find_unit(d.get("u2") or "")
        if n2 is None or n2.cat != n1.cat or n1.cat == "temp":
            return None
        return Conv("units", q1, n1, n3, float(d["q2"]), n2)
    return Conv("units", q1, n1, n3)


def _match_conversion(s: str) -> Optional[Conv]:
    """`s` is _prepare()d text. A resolved conversion request, or None."""
    s = _CONV_LEAD_RE.sub("", s, count=1).strip()
    if not s or len(s) > 120:
        return None
    for kind, rx in _CONV_PATTERNS:
        m = rx.fullmatch(s)
        if not m:
            continue
        conv = _resolve_conv(kind, m.groupdict())
        if conv is not None:
            return conv
    return None


def _units_sentence(c: Conv, lg: str) -> str:
    src, dst = c.src, c.dst
    try:
        if c.q2 is not None:
            val = (c.q1 * src.factor + c.q2 * c.src2.factor) / dst.factor
        else:
            val = convert_value(c.q1, src, dst)
    except CalcError as e:
        if str(e) == "below_zero":
            return _t("below_zero", lg)
        raise
    names = (lambda u, x: (u.hi if lg == "hi" else u.en)[0 if abs(x) == 1 else 1])
    q = f"{_say(c.q1, lg)} {names(src, c.q1)}"
    if c.q2 is not None:
        q += f" {_say(c.q2, lg)} {names(c.src2, c.q2)}"
    rounded = _round_result(val)
    exact = abs(val - rounded) <= 1e-9 * abs(val)
    r = f"{_say(rounded, lg)} {names(dst, rounded)}"
    about = "" if exact else _t("about_en", lg)
    key = "conv_x" if exact else "conv"
    return _t(key + ("1" if (lg == "hi" and abs(rounded) == 1) else ""), lg, q=q, about=about, r=r)


def _currency_sentence(c: Conv, lg: str) -> str:
    src, dst = c.src, c.dst
    for cur in (src, dst):
        if not cur.supported:
            return _t("unsupported", lg, c=(cur.hi if lg == "hi" else cur.en)[0])
    try:
        amount, date = convert_amount(c.q1, src.code, dst.code)
    except CalcError as e:
        if e.kind == "currency":
            cur = _CURRENCIES.get(str(e))
            return _t("unsupported", lg, c=((cur.hi if lg == "hi" else cur.en)[0] if cur else str(e)))
        raise
    except Exception as e:                            # NetError, or anything a broken fetch raises
        logger.info("calc: exchange rate lookup failed: %s", e)
        return _t("no_net", lg)
    if src.code == dst.code:
        q = f"{_say(c.q1, lg)} {_cur_name(src, c.q1, lg)}"
        return _t("cur_same", lg, q=q, r=q)
    when = _rate_date(date, lg)
    rounded = _round_result(amount)
    r = f"{_say(rounded, lg)} {_cur_name(dst, rounded, lg)}"
    if c.kind == "rate":
        return _t("rate", lg, a=_cur_name(src, 1, lg), r=r, d=when)
    q = f"{_say(c.q1, lg)} {_cur_name(src, c.q1, lg)}"
    return _t("cur", lg, q=q, r=r, d=when)


def convert_units(text: str) -> Optional[str]:
    """'convert 10 kilometers to miles' / '10 किलोमीटर कितने मील' -> the sentence to speak, or None."""
    try:
        lg = _lg(text)
        c = _match_conversion(_prepare(text))
        if c is None or c.kind != "units":
            return None
        return _units_sentence(c, lg)
    except Exception:
        logger.exception("calc: convert_units failed")
        return None


def convert_currency(text: str) -> Optional[str]:
    """'100 dollars in rupees' / '100 डॉलर कितने रुपये' -> the sentence to speak, or None (not a currency phrase)."""
    try:
        lg = _lg(text)
        c = _match_conversion(_prepare(text))
        if c is None or c.kind == "units":
            return None
        return _currency_sentence(c, lg)
    except Exception:
        logger.exception("calc: convert_currency failed")
        return _t("no_net", _lg(text))


# ------------------------------------------------------------------ matcher + tool

_MAX_TRANSCRIPT = 300


def _handle(text: str, lg: str, from_tool: bool) -> Optional[str]:
    """math -> units -> currency. None when the text is not clearly one of them (or cannot be computed)."""
    try:
        s = _prepare(text)
        if not s:
            return None
        p = _parse_math(s)
        conv = None if p is not None else _match_conversion(s)
    except Exception:
        logger.exception("calc: parsing failed")
        return None
    if p is not None:
        out = _answer_math(p, lg)
        logger.info("calc: math %r -> %r", p.expr, out)
        return out
    if conv is None:
        return None
    if conv.kind == "units":
        out = _units_sentence(conv, lg)
        logger.info("calc: units %s -> %s: %r", conv.src.key, conv.dst.key, out)
        return out
    if not _enabled("CURRENCY_ENABLED"):
        return _t("cur_off", lg) if from_tool else None
    out = _currency_sentence(conv, lg)
    logger.info("calc: currency %s -> %s: %r", conv.src.code, conv.dst.code, out)
    return out


def try_handle(transcript: str) -> Optional[str]:
    """
    The strict matcher used before the LLM. Returns the finished sentence for a clear arithmetic, unit
    conversion or currency request, otherwise None (and does nothing). Never raises, does no I/O before a match
    (only currency lookups touch the network, through netutil).
    """
    try:
        if not _enabled("CALC_ENABLED") or not isinstance(transcript, str):
            return None
        text = transcript.strip()
        if not text or len(text) > _MAX_TRANSCRIPT:
            return None
        return _handle(text, _lg(text), from_tool=False)
    except Exception:
        logger.exception("calc: try_handle failed")
        return _t("cant_work", _lg(transcript if isinstance(transcript, str) else ""))


def calculate(expression: str = "", **_ignored) -> str:
    """LLM tool: a math expression ('15*(3+2)', 'sqrt(144)'), a spoken math phrase, a unit conversion or a
    currency conversion. Always returns a sentence to speak."""
    try:
        text = "" if expression is None else str(expression).strip()
        lg = _lg(text)
        if not text:
            return _t("cant_work", lg)
        if not _enabled("CALC_ENABLED"):
            return _t("calc_off", lg)
        if len(text) <= _MAX_TRANSCRIPT:
            out = _handle(text, lg, from_tool=True)
            if out:
                return out
        raw = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text.replace("×", "*").replace("÷", "/"))
        radians = bool(re.search(r"\bradians?\b", raw, re.I))
        raw = re.sub(r"\bradians?\b", "", raw, flags=re.I).strip()
        try:
            r = evaluate_exact(raw, radians)
        except CalcError as e:
            if e.kind in ("zero", "big", "domain"):
                return _error_sentence(e, lg)
            return _t("cant_work", lg)
        logger.info("calc: tool expression %r -> %r", raw, r)
        return _t("answer", lg, r=_say(r, lg))
    except Exception:
        logger.exception("calc: calculate failed")
        return _t("cant_work", lang.current())
