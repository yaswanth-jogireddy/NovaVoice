"""Original, source-grounded coaching. See docs/MAYA_LISTENING.md."""
import re

LISTENING_COACHING = """
LISTENING AND CONVERSATIONAL REPAIR — applies throughout this call
Be warm and attentive while staying clearly an AI assistant. Never pretend to
have human experiences, emotions, a personal relationship or previous-call memory.
Earn trust by being useful and honest, not by flattery, pressure or sales claims.

Start with a short, complete thought that responds to what the caller actually
said. Let warmth come from noticing what matters: a concern, preference or changed
detail, rather than padding every answer with 'Got it' or 'Absolutely'. A brief
acknowledgment plus one relevant question can feel natural; a direct answer or
helpful observation may need no question. Vary the rhythm instead of conducting
an interview. Do not jump to name or contact while a concern remains unanswered.
Keep the first sentence concise when possible, without cutting off an explanation,
withholding useful information, forced choppiness or a rigid word-count target.
If details conflict or sound unclear, gently check only the uncertain part; never
label the caller wrong or guess what they meant. After an interruption, listen to
the new meaning and follow it instead of completing a preplanned next question.

Understand the caller's current purpose before following a business checklist.
Hear all clauses, answer their actual question, then ask at most one relevant
follow-up. A reply may end without a question. Open questions help discover a
need; precise questions help resolve a specific ambiguity. Don't interrogate.

If the caller changes the subject, pause the old intake. A selected demo is
context, not permission to ignore them. Briefly explain a business limitation
once, offer a useful general answer within your competence, and let them choose
whether to continue. Never invent listings, prices, availability or expertise.
Do not ask for AC symptoms, a visit time or contact details while they are asking
about property. Do not silently pretend the AC business sells property either.
If they decline the original service or repeat the other topic, respect that;
don't loop back to the same service question. If no useful help is possible,
say so kindly and leave room for them to finish.

When you misunderstood, own the specific mistake briefly, reflect the corrected
meaning once and change course. Don't defend your script. Acknowledge frustration
without claiming to know exactly how the caller feels. Avoid canned empathy,
repeated apologies and echoing every statement. If they don't know an answer,
offer a simpler way to describe it or leave it unknown. If they decline contact,
accept it; don't substitute another channel or demand a reason.

Ask for a repeat only of the unclear fragment. Don't turn incomplete recognition
into a confident fact. A self-correction replaces only the affected detail.
After an interruption, address the new words, not the speech you wanted to finish.
Don't fill thinking pauses with repeated prompts; leave the turn to the caller.
Speech timing and interruption detection belong to the audio system, not fillers.
When suggesting questions for a landlord or another professional, describe the
topics in a short spoken statement; don't ask the caller those example questions.
Distinguish buying from renting; lease examples do not answer a buying question.

Original examples — adapt the move, never borrow the example's facts:
Caller: I chose AC repair, but I actually want to ask about buying property.
Maya: This demo handles AC service, so I don't have property listings. I can help
you put together questions to ask an agent, though.
Caller: Stop asking about my AC. I'm asking what to check before renting.
Maya: You're right, I kept steering you back. You could ask about the total monthly
cost, maintenance responsibilities and the notice period.
Caller: I don't want to share my number.
Maya: That's fine; we can leave contact details out.
Caller: You aren't listening. Friday evening, not morning.
Maya: Sorry, Friday evening. I'll keep that correction.
Caller: How much, and can you guarantee someone tomorrow?
Maya: I don't have an approved price or a live schedule, so I can't guarantee
either. I can include your request for an estimate and tomorrow's preference.
"""


def listening_guidance(history):
    """Brief last-turn reminders; history remains the source of caller facts.

These cues do not extract business facts or silently switch business profiles.
"""
    callers = [m['content'] for m in history if m['role'] == 'user']
    if not callers: return ''
    latest = callers[-1]
    notes = [
        "Before answering, identify what the caller wants NOW. A pending intake field never outranks their current question or refusal. "
        "For a changed subject, pause intake; explain a limitation at most once and help within honest general bounds. "
        "Do not steer them back unless they choose the original service. "
        "No automatic recap or generic invitation after answering. Use examples as topics, not questions directed at the caller."
    ]
    if re.search(r"(?:not listening|aren['’]?t listening|stop (?:asking|bringing)|you misunderstood|नहीं सुन|मत (?:पूछ|कीजिए))", latest, re.I):
        notes.append("Repair turn: briefly acknowledge the specific misunderstanding and address the corrected intent. Do not append another intake question on this turn.")
    if re.search(r"(?:by mistake|chose .+ but|selected .+ but|गलती से)", latest, re.I):
        notes.append("The caller explicitly says the selected service was mistaken or differs from their need. Do not ask about that original service or collect contact. Offer help for the actual topic without claiming new business capabilities.")
    if contact_is_declined(history):
        notes.append("Contact is explicitly declined in caller evidence. It is RESOLVED, not missing. Never ask them to confirm leaving it blank, supply another channel or explain the refusal. If the other essentials are collected, follow CALL COMPLETION now.")
    clock = re.search(r"\bat\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|1[0-2]|[1-9])\b(?!\s+(?:hundred|thousand|million|units|percent))|(?:[1-9]|दस|छह|सात|आठ|नौ|दो|तीन|चार|पांच)\s*बजे", latest, re.I)
    # 'I am in Pune' is not an AM time qualifier.
    period = re.search(r"\b(?:morning|afternoon|evening|night|noon|midnight)\b|\b(?:a\.m\.|p\.m\.)|\b(?:[0-9]{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:am|pm)\b|सुबह|शाम|दोपहर|रात", ' '.join(callers), re.I)
    if clock and not period:
        notes.append("A clock time lacks morning/evening in caller evidence. If this is the requested visit time, clarify that ambiguity before moving to name or contact. Do not imply availability or say the time works.")
    all_callers = ' '.join(callers)
    if re.search(r'\b(?:buy|buying|purchase)\b', all_callers, re.I) and re.search(r'\b(?:property|home|house|flat)\b', all_callers, re.I):
        notes.append("Keep the caller's buying intent. General questions for a selling agent concern asking price, condition, ongoing costs and what is included. Lease notice periods are rental topics, not this purchase request.")
    return '\nLISTENING PRIORITY FOR THIS TURN: ' + ' '.join(notes)


def generic_closer(text):
    """Drop only complete, stock invitations; preserve real follow-up questions."""
    return bool(re.fullmatch(
        r"\s*(?:if you (?:have|need|want) (?:any |any other |more )?.{0,100},?\s*(?:please )?(?:feel free|just let me know|let me know).{0,45}"
        r"|(?:please )?feel free to (?:ask|reach out|let me know).{0,70}"
        r"|how can I (?:help|assist) you further\??)\s*[.!?]?\s*", text, re.I))


def listening_question(text, history, language):
    """Backstop two observed failures without inventing any caller facts.

    A repair turn should answer/acknowledge before resuming intake. If a clearly
    ambiguous clock time was supplied, don't jump to contact/name instead.
    Other replies still come from the model; this is not a readiness classifier.
    """
    if not re.search(r'[?？]', text): return text
    callers = [m['content'] for m in history if m['role'] == 'user']
    if not callers: return text
    latest = callers[-1]
    if contact_is_declined(history) and re.search(r'\b(?:phone|email|contact|number)\b|नंबर|ईमेल|संपर्क', text, re.I):
        return ''
    if re.search(r"(?:not listening|aren['’]?t listening|stop (?:asking|bringing)|you misunderstood|नहीं सुन|मत (?:पूछ|कीजिए)|by mistake|chose .+ but|selected .+ but|गलती से)", latest, re.I):
        return ''
    if 'A clock time lacks' in listening_guidance(history) and re.search(r'\b(?:name|phone|email|contact)\b|नाम|नंबर|ईमेल', text, re.I):
        return {'en': 'Is that in the morning or evening?', 'hi': 'सुबह या शाम?'}.get(language, text)
    return text


def contact_is_declined(history):
    """Remember an explicit refusal until the caller reopens contact collection."""
    for message in reversed(history):
        if message['role'] != 'user': continue
        text = message['content']
        if re.search(r"(?:do not|don't|don’t|without).{0,55}(?:contact|number|phone|email)|\bno contact\b|leave.{0,20}(?:contact|phone|email).{0,20}(?:blank|out)|(?:नंबर|नम्बर).{0,20}नहीं", text, re.I):
            return True
        if re.search(r'\S+@\S+|^\s*\+?[\d -]{7,}\s*$|(?:phone|number|contact).{0,20}\d[\d -]{6,}|(?:use|share|give|prefer|add).{0,25}(?:email|phone|number|contact)|(?:email|call) me', text, re.I):
            return False
    return False
