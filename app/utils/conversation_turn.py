"""Bound spoken turns without changing the general LLM or summary pipeline."""
import re


def expand_spoken_contractions(text):
    """Formatting fallback for unambiguous English forms, not dialogue logic.

    Leave possessives and ambiguous 's / 'd to the model's full-word instruction.
    Protect identifiers: an apostrophe can legitimately occur in an email address.
    """
    negatives = {
        "can't": "cannot", "won't": "will not", "shan't": "shall not",
        "don't": "do not", "doesn't": "does not", "didn't": "did not",
        "isn't": "is not", "aren't": "are not", "wasn't": "was not",
        "weren't": "were not", "haven't": "have not", "hasn't": "has not",
        "hadn't": "had not", "couldn't": "could not", "wouldn't": "would not",
        "shouldn't": "should not", "mustn't": "must not", "needn't": "need not",
    }

    def expand(match):
        word = match.group().replace("’", "'")
        lower = word.lower()
        if lower == "i'm":
            return "I am"
        if lower in negatives:
            result = negatives[lower]
            return result.capitalize() if word[0].isupper() else result
        stem, suffix = word.split("'")
        return ("I" if stem.lower() == "i" else stem) + " " + {
            "ll": "will", "re": "are", "ve": "have",
        }[suffix.lower()]

    pattern = r"\b(?:I['’]m|(?:I|you|we|they|he|she|it)['’]ll|(?:you|we|they)['’]re|(?:I|you|we|they)['’]ve|(?:" + "|".join(negatives).replace("'", "['’]") + r"))\b"
    # Process whitespace-delimited pieces so protected identifiers remain intact.
    return re.sub(r"\S+", lambda m: m.group() if "@" in m.group() or "://" in m.group()
                  else re.sub(pattern, expand, m.group(), flags=re.I), text)


# A full stop after a title ("St.", "Dr.") or an initial ("S.L.") is not a
# sentence end; splitting there cut a reply off at "schools such as St.".
_BOUNDARY = (r"[?？؟]|[!।](?=\s|$)|(?<!\b[A-Z])(?<!\bSt)(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bMrs)"
             r"(?<!\bMt)(?<!\bapprox)\.(?=\s|$)")


class ConversationTurn:
    """Stop at the first question; never stream an imagined next customer turn.

    Keep a small pending fragment so placeholders/role labels split across chunks
    are inspected before speech. A word budget bounds the complete turn without
    treating a short acknowledgment as a reason to drop the last useful sentence.
    """
    def __init__(self, expand_contractions=True, soft_words=60, max_words=75, hard_words=100, first_clause_chars=0):
        # Defaults bound ordinary presets. Information-heavy agents may allow a
        # longer turn so a useful answer is not cut after its first points.
        self.expand_contractions = expand_contractions
        self.soft_words, self.max_words, self.hard_words = soft_words, max_words, hard_words
        # Speak the reply's opening clause as soon as it ends at a comma (0 = off).
        self.first_clause_chars = first_clause_chars
        self.pending = ""
        self.sentences = 0
        self.words = 0
        self.done = False

    def feed(self, fragment, final=False):
        if self.done:
            return []
        self.pending += fragment
        output = []
        while self.pending.strip():
            # Question marks terminate even if the model omits following spaces.
            boundary = re.search(_BOUNDARY, self.pending)
            if self.first_clause_chars and not self.sentences:
                clause = re.compile(r'[,،]\s+').search(self.pending, self.first_clause_chars)
                if clause and (not boundary or clause.end() < boundary.start()):
                    boundary = clause
            # A streamed fragment can end at "2." before "07 crore" arrives.
            # Hold only a numeric trailing dot until its next character is known.
            if (boundary and not final and boundary.end() == len(self.pending)
                    and re.search(r'\d\.$', self.pending)):
                break
            if not boundary and not final:
                break
            end = boundary.end() if boundary else len(self.pending)
            sentence, self.pending = self.pending[:end].strip(), self.pending[end:].lstrip()
            if re.search(r"\[[^\]]*\]|\b(?:user|customer|caller|buyer|assistant|maya)\s*:", sentence, re.I):
                self.done = True
                break
            count = len(sentence.split())
            if self.words + count > self.max_words and output:
                self.done = True
                break
            if self.words + count > self.hard_words:
                self.done = True
                break
            output.append((expand_spoken_contractions(sentence) if self.expand_contractions else sentence) + " ")
            self.words += count
            self.sentences += 1
            if re.search(r"[?？؟]", sentence) or self.words >= self.soft_words:
                self.done = True
                break
        return output
