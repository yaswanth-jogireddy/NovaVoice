"""Short caller sounds and fixed recovery lines, decided in code, not by the model.

A model call cannot un-stop audio that is already playing, and an empty reply would
ask the caller to repeat. Only whole messages count: "hmm, which one is cheaper?"
is an ordinary turn.
"""
import re

# Thinking sounds in every script auto-detecting STT may return them in ("um" came back
# as Tamil உம் on an English call): Latin, Devanagari, Tamil, Telugu, Kannada,
# Malayalam, Bengali, Gujarati and Gurmukhi.
_THINKING = (r'(?:h+m+|m{2,}|u+m+|u+h+|e+r+m*|a+h+|हम्म?|हम|हं|हँ|ह्म्म|अं|अँ|उम्म?|अम्म?'
             # Repeats are whole consonant+virama clusters: ம்ம் is (ம்)+, not ம followed by viramas.
             r'|உ?(?:ம்)+|ஹ(?:்ம்)+|அம்|ఉ?(?:మ్)+|హ(?:్మ్)+|ఊ+|ಉ?(?:ಮ್)+|ಹ(?:್ಮ್)+|ഉം+|ഹ(?:്മ്)+'
             r'|(?:ম্)+ম?|হু(?:ম|ঁ)+|উম+|હમ્મ?|ઉમ+|ਹਮ+|ਉਮ+)')
_ACK = (r'(?:o+ka+y+|ok|okey|right|al+ ?right|a+c+h+a+|ach+ha|अच्छा|ठीक\s?है|th?ee?k\s?hai'
        r'|ओके|बरं|m+-?h+m+|u+h+-?h+u+h+)')
_SEP, _END = r'[\s,.!…]+', r'[\s.!…]*$'  # no hyphen: "mm-hmm" is assent, not "mm" + "hmm"


def backchannel(text, hinglish=False):
    """'thinking' for a pure thinking sound, 'ack' for a pure acknowledgment, else None.

    "Hmm?" is a question ("what?"), so a question mark always means None.
    """
    if re.fullmatch(rf'\s*{_THINKING}(?:{_SEP}{_THINKING})*{_END}', text, re.I):
        return 'thinking'
    ack = _ACK + (r'|(?:haan|han|हाँ|हां)(?:\s+(?:ji|जी))?|ji|जी|theek hai' if hinglish else '')
    either = rf'(?:{ack}|{_THINKING})'
    if re.fullmatch(rf'\s*{either}(?:{_SEP}{either})*{_END}', text, re.I):
        return 'ack'
    return None


def hinglish_stop(text):
    """An explicit request to end the call, in the mixed scripts codemix STT returns."""
    return bool(re.search(r'(?:call|कॉल)\s*(?:(?:बंद|बन्द|band)\s*(?:कीजिए|कर|karo|kijiye)|(?:मत|mat)\s*(?:कर|kar))'
                          r'|\b(?:stop (?:the )?call|do not call|don.t call|not interested)\b'
                          r'|interest नहीं|दिलचस्पी नहीं', text, re.I))


# Spoken when the caller made a sound but no words were recognised.
REPEAT = {
    'hinglish': 'Sorry, ठीक से सुन नहीं पाई। एक बार फिर बताएँगे?',
    'en': 'Sorry, I missed that. Could you say it once more?',
    'hi': 'माफ़ कीजिए, मैं ठीक से सुन नहीं पाई। क्या आप एक बार फिर बता सकते हैं?',
    'mr': 'माफ करा, मला नीट ऐकू आले नाही. पुन्हा एकदा सांगाल का?',
}

# Appended to the system prompt only for the silence nudge turn.
NUDGE_INSTRUCTION = (
    '\n\nSILENCE NUDGE: the caller made a thinking sound and has said nothing for a few '
    'seconds. Reply with one short, warm sentence, at most 15 words, that gently invites '
    'them to continue, for example by offering a little help with your last question or '
    'their decision. Do not repeat your last reply, add new facts or ask a second question.'
)

# Gentle flow rules for the static prompt (AGENT_FLOW_PRINCIPLES); never at the cost of an answer.
FLOW_PRINCIPLES = """

# Conversation flow (gentle; never at the cost of an answer)
1. Use what the caller already told you in this conversation. Do not ask for it again;
   if one detail is genuinely unclear, ask about that detail only.
2. Do not repeat what you said in your last replies unless the caller asks. If the caller
   asks again, answer again fully and kindly; a repeated question is never wrong.
3. Answer what was asked first, then at most one question that moves the call forward.
   When the caller agrees to something you offered or already gave the details you were
   about to ask for (an order, days, times), act on it and read it back instead of
   asking again.
"""

# Language guidance for the Hinglish call language.
HINGLISH_STYLE = """

# Hinglish style
Use conversational Hinglish: Hindi words always in Devanagari, familiar English words in Latin script,
even when the caller writes or speaks in English or romanised Hindi (the voice mispronounces romanised Hindi).
Keep business, product and place names unchanged. Use polite आप and feminine first person.
Match the caller's vocabulary, without a mixing quota. Short English phrases are fine; avoid
sustained English-only or formal Hindi. Use short speakable sentences; usually 2-3 suffice.
"""
