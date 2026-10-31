"""Spoken numbers -> digits, for the identity parser and the PII masker (T24).

Callers say their details the way people speak, and speech recognition often writes them as words:
"fourteenth March nineteen ninety two", "one four zero three", "see one zero zero one",
"chaudah March unnis sau baanve", "दो हज़ार एक". spoken_numbers() rewrites number words as digits
and leaves every other word as it was, so the regexes downstream only ever deal with digits:

    "fourteenth March nineteen ninety two"   -> "14th March 1992"
    "one four zero three one nine nine two"  -> "14031992"
    "चौदह मार्च उन्नीस सौ बानवे"               -> "14 मार्च 1992"
    "last four seven three zero six"         -> "last 4 7306"   ("four" after "last" stays a cue)

Pure and side-effect free; no model is involved (identity factors are read by code, D10).
"""
from __future__ import annotations

import re

_UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
             "twentieth": 20, "thirtieth": 30}

# Hindi 0-99, Devanagari and the common romanised spellings speech recognition produces.
# "do" (2) is left out on purpose: it is an English word; "do hazaar" is handled as a phrase.
_HINDI_TABLE = """
0 शून्य shunya shoonya sunya
1 एक ek
2 दो
3 तीन teen
4 चार char chaar
5 पांच पाँच paanch panch
6 छह छः छे chhah chhe che chheh
7 सात saat
8 आठ aath
9 नौ nau
10 दस das
11 ग्यारह gyarah gyaarah
12 बारह barah baarah
13 तेरह terah
14 चौदह chaudah chaudha choudah
15 पंद्रह पन्द्रह pandrah pandhrah
16 सोलह solah
17 सत्रह satrah satarah
18 अठारह atharah athaarah
19 उन्नीस unnis unnees unees
20 बीस bees bis
21 इक्कीस ikkis ikkees
22 बाईस bais baees
23 तेईस teis teyees
24 चौबीस chaubis chaubees
25 पच्चीस pachchis pachees pachis
26 छब्बीस chhabbis chabbees chabbis
27 सत्ताईस sattais sattaees
28 अट्ठाईस अठाईस atthais athais athaees
29 उनतीस untis unatees
30 तीस tees tis
31 इकतीस इकत्तीस iktis ikattis iktees
32 बत्तीस battis battees
33 तैंतीस taintis tentis
34 चौंतीस chauntis chautis
35 पैंतीस paintis
36 छत्तीस chhattis chattis
37 सैंतीस saintis
38 अड़तीस artis adtis
39 उनतालीस untalis
40 चालीस chalis chaalees
41 इकतालीस iktalis
42 बयालीस bayalis
43 तैंतालीस taintalis
44 चवालीस chavalis chauvalis
45 पैंतालीस paintalis
46 छियालीस chhiyalis
47 सैंतालीस saintalis
48 अड़तालीस artalis adtalis
49 उनचास unchas
50 पचास pachas pachaas
51 इक्यावन ikyavan
52 बावन bavan baavan
53 तिरेपन tirepan
54 चौवन chauvan
55 पचपन pachpan
56 छप्पन chhappan chappan
57 सत्तावन sattavan
58 अट्ठावन atthavan
59 उनसठ unsath
60 साठ saath sath
61 इकसठ iksath
62 बासठ basath
63 तिरसठ tirsath
64 चौंसठ chaunsath
65 पैंसठ painsath
66 छियासठ chhiyasath
67 सड़सठ sadsath
68 अड़सठ adsath
69 उनहत्तर unhattar
70 सत्तर sattar
71 इकहत्तर ikhattar
72 बहत्तर bahattar
73 तिहत्तर tihattar
74 चौहत्तर chauhattar
75 पचहत्तर pachhattar
76 छिहत्तर chhihattar
77 सतहत्तर sathattar
78 अठहत्तर athhattar athattar
79 उन्यासी उनासी unyasi unasi
80 अस्सी assi
81 इक्यासी ikyasi
82 बयासी bayasi
83 तिरासी tirasi
84 चौरासी chaurasi
85 पचासी pachasi
86 छियासी chhiyasi
87 सत्तासी sattasi
88 अट्ठासी atthasi athasi
89 नवासी navasi
90 नब्बे nabbe
91 इक्यानवे ikyanve
92 बानवे baanve banve baanwe
93 तिरानवे tiranve
94 चौरानवे chauranve
95 पचानवे pachanve
96 छियानवे chhiyanve
97 सत्तानवे sattanve
98 अट्ठानवे atthanve
99 निन्यानवे ninyanve
"""
_HINDI = {w: int(line.split()[0]) for line in _HINDI_TABLE.strip().splitlines() for w in line.split()[1:]}
_HUNDRED = {"सौ", "sau"}
_THOUSAND = {"thousand", "हज़ार", "हजार", "hazaar", "hazar", "hajar"}
_STRIP = ".!?;:'\"()"


def _word(token: str) -> tuple[str, int | None] | None:
    """(kind, value) for a number word, else None. kind: digit | teen | tens | ord | hindi | hundred | thousand | and."""
    w = token.lower().strip(_STRIP)
    if w in _UNITS:
        return "digit", _UNITS[w]
    if w in _TEENS:
        return "teen", _TEENS[w]
    if w in _TENS:
        return "tens", _TENS[w]
    if w in _ORDINALS:
        return "ord", _ORDINALS[w]
    if w in _HINDI:
        return ("digit" if _HINDI[w] < 10 else "hindi"), _HINDI[w]
    if w in _HUNDRED:
        return "hundred", 100
    if w in _THOUSAND:
        return "thousand", 1000
    if w == "and":
        return "and", None
    if w == "do":   # Hindi 2, only as part of "do hazaar"
        return "do", 2
    return None


def _suffix(n: int) -> str:
    if 10 <= n % 100 <= 13:
        return f"{n}th"
    return str(n) + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _convert(run: list[tuple[str, int | None, str]]) -> list[str]:
    """A run of number words -> digit chunks, e.g. [fourteen, zero, three, nineteen, ninety, two] -> 14 03 1992."""
    out: list[str] = []
    i = 0
    while i < len(run):
        kind, value, raw = run[i]
        nxt = run[i + 1] if i + 1 < len(run) else None
        nxt2 = run[i + 2] if i + 2 < len(run) else None
        # "nineteen ninety two", "nineteen oh five", "twenty ten", "twenty twenty one": a year
        if value in (19, 20) and kind in ("teen", "tens") and nxt and (
                nxt[0] in ("teen", "tens") or (nxt[0] == "digit" and nxt[1] == 0 and nxt2 and nxt2[0] == "digit")) and not (
                kind == "tens" and nxt[0] == "digit"):
            if nxt[0] == "tens" and nxt2 and nxt2[0] == "digit" and nxt2[1]:
                out.append(str(value * 100 + nxt[1] + nxt2[1]))
                i += 3
            elif nxt[0] == "digit":   # "oh five"
                out.append(str(value * 100 + nxt2[1]))
                i += 3
            else:
                out.append(str(value * 100 + nxt[1]))
                i += 2
            continue
        # "two thousand (and) one", "do hazaar ek"
        if kind in ("digit", "do") and nxt and nxt[0] == "thousand":
            j, rest = i + 2, 0
            if j < len(run) and run[j][0] == "and":
                j += 1
            if j < len(run) and run[j][0] == "tens" and j + 1 < len(run) and run[j + 1][0] == "digit" and run[j + 1][1]:
                rest, j = run[j][1] + run[j + 1][1], j + 2
            elif j < len(run) and run[j][0] in ("digit", "teen", "tens", "hindi"):
                rest, j = run[j][1], j + 1
            out.append(str(value * 1000 + rest))
            i = j
            continue
        # Hindi "unnis sau baanve" (19 x 100 + 92)
        if kind in ("hindi", "digit", "teen") and nxt and nxt[0] == "hundred":
            rest, j = 0, i + 2
            if j < len(run) and run[j][0] in ("hindi", "digit", "teen", "tens"):
                rest, j = run[j][1], j + 1
            out.append(str(value * 100 + rest))
            i = j
            continue
        # "twenty one" -> 21, "twenty first" -> 21st
        if kind == "tens" and nxt and nxt[0] == "digit" and nxt[1]:
            out.append(str(value + nxt[1]))
            i += 2
            continue
        if kind == "tens" and nxt and nxt[0] == "ord" and nxt[1] < 10:
            out.append(_suffix(value + nxt[1]))
            i += 2
            continue
        if kind == "ord":
            out.append(_suffix(value))
            i += 1
            continue
        # a run of single digits: "one four zero three" -> 1403
        if kind == "digit":
            j = i
            while j < len(run) and run[j][0] == "digit":
                j += 1
            out.append("".join(str(run[k][1]) for k in range(i, j)))
            i = j
            continue
        if kind in ("teen", "tens", "hindi"):
            out.append(str(value))
        i += 1      # a stray "and" / "hundred" / "thousand" / "do" is dropped from the run
    return out


_TOKEN = re.compile(r"[^\s,]+")
_MONTH_WORDS = {"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june", "jul",
                "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november", "dec",
                "december", "of"}


def _expand_hyphens(text: str) -> str:
    """'twenty-one' -> 'twenty one' when every part is a number word."""
    def fix(m):
        parts = m.group(0).split("-")
        return " ".join(parts) if all(_word(p) for p in parts) else m.group(0)
    return re.sub(r"[A-Za-z]+(?:-[A-Za-z]+)+", fix, text)


def _expand_repeats(text: str) -> str:
    """'double eight one five' -> 'eight eight one five' (how Indian English reads digit strings)."""
    def fix(m):
        digit = m.group(2)
        info = _word(digit)
        if not (len(digit) == 1 and digit.isdigit() or info and info[0] == "digit"):
            return m.group(0)
        return " ".join([digit] * (2 if m.group(1).lower() == "double" else 3))
    return re.sub(r"\b(double|triple)\s+(\w+)", fix, text, flags=re.I)


def spoken_numbers(text: str) -> str:
    """Rewrite number words in text as digits; everything else is kept as it was."""
    if not text:
        return text
    text = _expand_hyphens(_expand_repeats(text))
    tokens = list(_TOKEN.finditer(text))
    pieces, last_end, i = [], 0, 0
    while i < len(tokens):
        info = _word(tokens[i].group(0))
        if info is None or info[0] in ("and", "hundred", "thousand"):
            i += 1
            continue
        run, j = [], i
        while j < len(tokens):
            info = _word(tokens[j].group(0))
            if info is None:
                break
            if info[0] == "and" and not (run and run[-1][0] == "thousand"):
                break
            # A number word glued to punctuation ends the run there ("seven three, zero six" stays one run,
            # "fourteen. Nineteen" does not)
            run.append((*info, tokens[j].group(0)))
            j += 1
            if tokens[j - 1].group(0)[-1:] in ".!?;:":
                break
        # trim words that only count inside a number: a leading "oh"/"o", a trailing "and"
        while run and (run[0][2].lower().strip(_STRIP) in ("oh", "o") or run[0][0] in ("do", "and")):
            if run[0][0] == "do" and len(run) > 1 and run[1][0] == "thousand":
                break
            run.pop(0)
            i += 1
        while run and run[-1][0] in ("and",) or (run and run[-1][2].lower().strip(_STRIP) in ("oh", "o")):
            run.pop()
            j -= 1
        if not run:
            i = max(i + 1, j)
            continue
        # "second" is a number only before a month ("second November"); "one second" is not a date
        if run[-1][2].lower().strip(_STRIP) == "second":
            follow = tokens[j].group(0).lower().strip(_STRIP) if j < len(tokens) else ""
            if follow not in _MONTH_WORDS:
                run.pop()
                j -= 1
                if not run:
                    i = j + 1
                    continue
        # "last four ..." / "aakhri chaar ...": the first "four" is the cue, not a digit
        prev = tokens[i - 1].group(0).lower().strip(_STRIP) if i > 0 else ""
        lead = []
        if prev in ("last", "aakhri", "akhri", "आखिरी", "आख़िरी") and run[0][1] == 4 and run[0][0] == "digit":
            lead, run = ["4"], run[1:]
        chunks = lead + (_convert(run) if run else [])
        start, end = tokens[i].start(), tokens[j - 1].end()
        trailing = tokens[j - 1].group(0)[-1:] if tokens[j - 1].group(0)[-1:] in ".!?;:" else ""
        pieces.append(text[last_end:start])
        pieces.append(" ".join(chunks) + trailing)
        last_end, i = end, j
    pieces.append(text[last_end:])
    return "".join(pieces)
