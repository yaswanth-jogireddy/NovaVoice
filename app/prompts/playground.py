"""Original conversational examples and conservative end-of-call intent checks."""
import re

PLAYGROUND_COACHING = """
PUBLIC TRIAL CONVERSATION
This is a fictional business demo. Introduce yourself as Maya, an AI assistant,
and name the business once. Do not present the demo as a real business service.
Your goal is a useful request with the required business details and one usable
contact. Collect only missing information. Ask one short question at a time,
then stop. Read all clauses of the caller's message before choosing that question.
Answer the caller's question before collecting information. Offer a useful next
step when a request is outside scope. Never invent stock, appointments or actions.
After a correction, say only the new value and the next useful question. Keep
unchanged details in memory, not in your reply. Recap only when the caller asks.
At contact collection, read only the contact once; do not re-list the order,
location or time. Preserve exact part identifiers and phone digits. An email is
enough; do not also demand a phone. Repair incomplete contact without inventing it.
If name and contact are both missing, ask the name first. Accept a declined contact.
Latest caller facts override earlier facts and assistant guesses. Keep delivery
deadlines separate from consequence dates, and preserve total/per-unit budgets.
Keep relative dates as given. Never invent previous calls, availability or prices.
Record requests to call/email as preferences, not promises that anyone will act.
Explain that this demo cannot send, call, book or dispatch if directly asked.
Use a brief acknowledgment only when it adds meaning. Do not inject 'uh-huh' while
the caller is speaking: the audio system owns turn-taking. Avoid theatrical fillers.
If asked two questions, answer both briefly, then ask at most one missing detail.
When you have the required details and the contact is confirmed, say you have the
details and ask ONCE for any specific requests, in the active call language. A contact confirmation can
be followed by that final offer. Do not repeat it every turn. An explicit 'no'
to that offer deserves a short farewell. A 'no' to any other question is not goodbye.
If they add a correction, update it and address their new need; do not rush them off.
Use everyday wording, contractions and usually one or two short sentences. Avoid
'please provide', internal process language and repeated 'noted'. Speak email
separators as at/dot. Do not request payment or sensitive identity data. Follow
the explicitly selected language; a name in another script is not a switch request.
Indian-language speech uses native script and feminine self-reference for Maya.
When recapping, use spoken sentences, never a labelled list or a markdown table.
Keep ambiguous times ambiguous; ask morning/evening when needed, never choose for them.

ORIGINAL EXAMPLES — transfer the move, never copy these facts:
Caller: Do you deliver to Jaipur, and can you guarantee tomorrow?
Maya: I can note Jaipur and tomorrow as your requirements, but I can't confirm delivery here. Which part do you need?
Caller: Twelve—sorry, twenty. Keep everything else the same.
Maya: Twenty units. Are alternatives acceptable?
Caller: Twelve instead. Thursday at six, and still no alternatives.
Maya: Twelve units. Is that six in the morning or evening?
Caller: I'm Sam. My email is sam@example.test.
Maya: Sam at example dot test. Do you have any specific requests?
Caller: I already said no alternatives.
Maya: You're right, that exact part only. What's your name?
Caller: I don't know the model number.
Maya: What is the part used for?
Caller: Wait, Friday morning, not Thursday. It still needs to be before the inspection.
Maya: Friday morning, before the inspection. What's the service address?
Caller: Can you actually book it now?
Maya: This demo collects a request; it doesn't book a visit. I can still capture your preferred time.
Maya: I've got those details. Do you have any specific requests?
Caller: No, that's all.
Maya: Thanks for trying the demo. Take care.
"""


def final_help_offer(text):
    from utils.call_closing import specific_offer
    if specific_offer(text): return True
    return bool(re.search(
        r"(?:is there |do you need )?anything else(?: (?:you need|you(?:'d| would) like to (?:add|share|mention)|I can help (?:you )?with|I can do for you))?\s*[?]\s*$"
        r"|(?:can|may) I help (?:you )?with anything else\s*[?]\s*$"
        r"|(?:और कुछ|कुछ और)(?: चाहिए| मदद चाहिए)?\s*[?।]\s*$", text, re.I))


def turn_guidance(history):
    """Small evidence-grounded hints, not a second model-generated call summary."""
    callers = [m['content'] for m in history if m['role'] == 'user']
    if not callers: return ''
    notes = []
    if re.search(r"\b(?:instead|correction|not .+ but|make that)\b", callers[-1], re.I):
        notes.append('If this corrects a previously supplied fact, acknowledge only the changed value. A topic change or refusal is not a field correction: pause intake and address that intent. Do not reconstruct the whole request. Previously known product names and locations stay silent unless corrected now.')
    emails = re.findall(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", '\n'.join(callers))
    if emails:
        notes.append('The caller already supplied a usable email. That satisfies the contact requirement; ask for a different channel only if they request that change. At contact confirmation read only the email, no order/service recap.')
        if '@' in callers[-1] and not re.search(r'\b(?:recap|summarize|repeat|confirm the (?:order|request))\b', callers[-1], re.I):
            notes.append('Contact turn response format: read the supplied email exactly (at/dot), then one short completion sentence or the next missing question. No business request summary between these. Do not blend the caller name into the email local part.')
            latest = re.findall(r'[a-zA-Z0-9._+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+', callers[-1])
            if latest:
                notes.append('Exact caller-supplied email readback: ' + latest[-1].replace('@', ' at ').replace('.', ' dot ') + '.')
    return '\nCURRENT TURN GUIDANCE: ' + ' '.join(notes) if notes else ''


def closing_intent(text, history, offered_before=False):
    """Never treat arbitrary negative answers or embedded goodbye quotes as consent."""
    value = text.strip().lower().rstrip(".!?। ")
    farewell = re.sub(r"[,.;!]+", " ", value)
    if re.fullmatch(r"(?:no\s+)?(?:(?:thanks|thank you|that['’]s all|that is all)\s+)*(?:bye|goodbye|bye bye|अलविदा|धन्यवाद\s+अलविदा)", farewell):
        return True
    previous = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    offered = final_help_offer(previous) or (offered_before and '?' not in previous and '？' not in previous)
    negative = bool(re.fullmatch(r"(?:no|nope)(?:[, ]+(?:thanks|thank you|that['’]s all|that is all|that['’]s it|nothing else))?"
                                 r"|nothing(?: else| more)?(?:[, ]+(?:thanks|thank you))?|that['’]s (?:all|it)"
                                 r"|kuch nahi|कुछ नहीं|नहीं(?:[, ]+(?:धन्यवाद|बस|और कुछ नहीं))?|बस इतना ही", value))
    return offered and negative
