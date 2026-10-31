"""Speech-only text for Hindi and Hinglish TTS. Display text keeps digits/Latin.

Sarvam's own agents expose "Output numbers in Indic" (500 -> paanch sau) and
advise writing proper nouns in Indic script for Bulbul. Replies sent "2.07 crore"
and Indian names in Latin script as written, and spoke dashes/semicolons mid-sentence.
Only amounts with a crore/lakh unit change: BHK, phone numbers, sq ft, dates
and years stay as digits so their meaning cannot shift.
"""
import re

_ONES = ('शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस '
         'बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस '
         'पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस '
         'अड़तालीस उनचास पचास इक्यावन बावन तिरेपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरेसठ '
         'चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर '
         'अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे '
         'बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे').split()
assert len(_ONES) == 100

# Indian-origin names Bulbul may anglicise in Latin script -> Devanagari spelling.
# Empty by default: a business adds its own names with register_names().
_NAMES = {}
_NAME = None


def register_names(names):
    """Add {'Latin name': 'देवनागरी'} pairs spoken in Devanagari on Hindi/Hinglish calls."""
    global _NAME
    _NAMES.update(names)
    _NAME = re.compile(r"(" + '|'.join(sorted(map(re.escape, _NAMES), key=len, reverse=True)) + r")(?:'s)?")


_NUM = r'(\d{1,2}(?:\.\d{1,2})?)'
_CRORE = r'(?:crore|cr\b|करोड़|करोड)'
_LAKH = r'(?:lakhs?|लाख)'
_TO = r'\s*(?:से|to|-|–)\s*'


def _words(n):
    return _ONES[n] if 0 <= n < 100 else str(n)


def crore_words(value):
    """2.07 -> दो करोड़ सात लाख; 0.6 -> साठ लाख; 2.5 -> ढाई करोड़; 11.9 -> ग्यारह करोड़ नब्बे लाख."""
    crore = int(value)
    lakh = round((value - crore) * 100)
    if lakh == 50 and crore:  # How buyers say it: डेढ़, ढाई, साढ़े तीन करोड़.
        return {1: 'डेढ़ करोड़', 2: 'ढाई करोड़'}.get(crore, f'साढ़े {_words(crore)} करोड़')
    if lakh == 100:
        crore, lakh = crore + 1, 0
    parts = ([f'{_words(crore)} करोड़'] if crore else []) + ([f'{_words(lakh)} लाख'] if lakh else [])
    return ' '.join(parts) or 'शून्य'


def speakable(text, language):
    """Text for the speech engine only; never stored, displayed or checked."""
    # Em dash, spaced en dash and semicolon read as pauses; a digit range (2.4–2.8) stays.
    text = re.sub(r'\s*(?:—|;|\s–\s)\s*', ', ', text)
    text = re.sub(r'([?!।.,]),\s*', lambda m: m[1] + ' ', text)
    # "10-digit" was spelled out as "d-i-g-i-t": a number hyphenated to a word reads
    # as a code. Only digit-hyphen-letter changes; phone numbers, dates and ranges stay.
    text = re.sub(r'(\d)-(?=[A-Za-zऀ-ॿ])', lambda m: m[1] + ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    if language not in ('hi', 'hinglish'):
        return text
    if _NAME is not None:
        text = _NAME.sub(lambda m: _NAMES[m[1]] + ('्स' if m[0].endswith("'s") else ''), text)
    text = re.sub(_NUM + _TO + _NUM + r'\s*' + _CRORE,
                  lambda m: f'{crore_words(float(m[1]))} से {crore_words(float(m[2]))}', text, flags=re.I)
    text = re.sub(_NUM + r'\s*' + _CRORE, lambda m: crore_words(float(m[1])), text, flags=re.I)
    text = re.sub(r'(\d{1,2})' + _TO + r'(\d{1,2})\s*' + _LAKH,
                  lambda m: f'{_words(int(m[1]))} से {_words(int(m[2]))} लाख', text, flags=re.I)
    return re.sub(r'(\d{1,2})\s*' + _LAKH, lambda m: f'{_words(int(m[1]))} लाख', text, flags=re.I)
