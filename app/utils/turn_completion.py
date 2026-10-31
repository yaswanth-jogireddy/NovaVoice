"""Conservative pause hints, not language detection or another model call.

Only recognizable unfinished thoughts get a short grace period. Unknown wording
stays on the fast path; speech activity and final transcripts own cancellation.
"""
import re


def incomplete_reason(text, previous=""):
    text = text.strip().rstrip(".,!।… ").casefold()
    if not text or text.endswith(("?", "？")):
        return None
    if re.search(r"(?:^|\s)(?:but|and|because|or|around|about|between|actually|"
                 r"lekin|aur|kyunki|matlab|लेकिन|और|क्योंकि|मतलब|परंतु|आणि|पण)$", text):
        return "unfinished_clause"
    if re.search(r"(?:\bI (?:want|need|would like)|\blooking for|\b(?:which|that) has|"
                 r"जिसमें|जिसके|जिसका|जिथे)$", text, re.I):
        return "unfinished_requirement"
    if re.search(r"(?:\b(?:my|our) (?:budget|number|phone number|requirement) (?:is|would be)"
                 r"|\b(?:mera|hamara) (?:budget|number)(?: hai)?"
                 r"|(?:मेरा|हमारा|माझं|माझे) (?:budget|बजट|बजेट|नंबर)(?: है|आहे)?)$", text):
        return "missing_value"
    if re.search(r"(?:let me (?:think|see)|one moment|ek minute|सोचने दीजिए|एक मिनट)$", text):
        return "thinking"
    # An amount without units is ambiguous only in a money context. A BHK,
    # phone number or visit-time answer must not acquire a budget interpretation.
    money_context = bool(re.search(r"budget|बजट|बजेट|कितने तक", previous, re.I))
    bare_amount = r"(?:[0-9०-९]{1,2}(?:[.][0-9०-९]{1,2})?|two|three|dhai|दो|तीन|ढाई)"
    if money_context and re.fullmatch(bare_amount, text):
        return "amount_without_unit"
    if re.search(r"(?:budget|बजट|बजेट)\s+(?:is\s+|है\s+)?" + bare_amount + r"$", text):
        return "amount_without_unit"
    return None
