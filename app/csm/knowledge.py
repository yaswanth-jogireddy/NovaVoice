"""BM25 keyword search over the Nova Trust policy docs (data/knowledge/*.md).

28 short documents: a vector DB would add a service and buy nothing. Results carry doc ids so the
transcript and the handoff can cite which policy an answer came from.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge"
_STOP = set("a an the is are am was were be been to of in on for and or but my me i you your it this that "
            "what how can do does did with at by from as please tell about any there i'm want need".split())


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if w not in _STOP]


@dataclass(frozen=True)
class Doc:
    id: str
    title: str
    body: str


class KnowledgeBase:
    def __init__(self, folder: Path = DEFAULT_DIR, k1: float = 1.4, b: float = 0.75) -> None:
        self.docs: list[Doc] = []
        for path in sorted(Path(folder).glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            meta = dict(re.findall(r"^(\w+):\s*(.+)$", raw.split("---")[1], re.M)) if raw.startswith("---") else {}
            body = raw.split("---", 2)[-1]
            body = re.sub(r"^#.*$|_Nova Trust Bank is fictional.*_", "", body, flags=re.M).strip()
            self.docs.append(Doc(meta.get("id", path.stem), meta.get("title", path.stem), body))
        self._k1, self._b = k1, b
        # Title and tags count twice: they say what the doc is about.
        self._tf = [Counter(tokens(d.title) * 2 + tokens(d.body)) for d in self.docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / max(1, len(self._len))
        df = Counter(t for tf in self._tf for t in tf)
        n = len(self.docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 3, min_score: float = 1.0) -> list[dict]:
        q = tokens(query)
        scored = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self._idf[t] * f * (self._k1 + 1) / (f + self._k1 * (1 - self._b + self._b * self._len[i] / self._avg))
            if score >= min_score:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [{"doc_id": self.docs[i].id, "title": self.docs[i].title, "text": self.docs[i].body[:600],
                 "score": round(s, 2)} for s, i in scored[:k]]
