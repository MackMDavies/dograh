"""What Sam may say about Siseevo, and how a question finds it. Free of I/O so it can be tested.

Sam's prompt carries the pitch, but a caller asking "does it work with Jobber?", "is it HIPAA
compliant?" or "will my callers know it's an AI?" needs the checked answer, not a guess. The
team keeps those answers in sysevo_knowledge_docs, which staff Syra already searches
(supabase/functions/_shared/syra/sysevoKnowledge.ts). This is a port of that search, so Sam
and Syra answer the same question the same way.

A caller is a PROSPECT, so Sam reads only what may be said to one:
- the five FAQ documents, but of faq_hard only the sections about a caller (legal or medical
  bait, politics, pressure, abuse) — the rest is about Syra reading pages and tools;
- the reference documents about what we are, sell and may claim;
- never pricing (Sam books the meeting; the rep quotes), the sales playbook (our tactics),
  operations (internal) or customers (other people's names).
Sections for the documents' maintainers (sources, what is still to confirm), anything marked
internal, discount negotiation, and every answer that names a price are dropped: Sam never quotes
one, and an answer that carries one invites him to. "Conflicts to resolve" stays: its SAFE LINE is what may be said.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

FAQ_KINDS = ("faq_buying", "faq_voice", "faq_platform", "faq_trust", "faq_hard")

#: Reference documents a prospect may hear from. Not pricing, sales_playbook, operations, customers.
DOC_KINDS = ("company", "products", "positioning", "features", "compliance", "onboarding", "voice_ai")

#: faq_hard sections about what a caller might say. The others are about Syra (pages, tools, staff).
_CALLER_HARD_TOPICS = re.compile(r"^(?:abuse and trolling|legal, medical|opinions, politics|pressure to act)", re.I)

#: Sections for the people who maintain a document, or not for outside ears.
_DROPPED_SECTIONS = re.compile(
    r"^(?:sources|still to confirm|team shape|internal|staff|discounts)|\(internal\)", re.I
)

#: "$349", "€ 1,200", "£99": an answer carrying a price is left out entirely.
_PRICE = re.compile(r"[$€£]\s?\d")

#: A spoken answer is short; a long section is cut rather than read out.
MAX_ANSWER_CHARS = 1200

#: Below this the best match shares only a common word with the question: no answer beats a wrong one.
#: Measured on the live documents: "do you offer a discount" reached the SLA answer at 10.1 on "offer".
MIN_SCORE = 12.0

#: A runner-up must score at least this share of the best match, or it is noise beside it. The
#: floor above applies to the best match only: "can it speak Spanish" ranks "Can the agent speak in
#: my own voice?" first (15.6) on "speak", and the real answer, "How many languages does it
#: speak?", second at 11.7. Sam gets both and is told to use only one that answers the question.
RUNNER_UP_SHARE = 0.6


@dataclass(frozen=True)
class Entry:
    doc: str
    topic: str
    question: str
    answer: str


def parse_faq(doc: str, md: str) -> list[Entry]:
    """A FAQ document ("## topic", "### question", answer) as entries."""
    out: list[Entry] = []
    topic = ""
    question: str | None = None
    answer: list[str] = []

    def push() -> None:
        text = "\n".join(answer).strip()
        if question is not None and text:
            out.append(Entry(doc, topic, question, text))

    for line in md.split("\n"):
        if line.startswith("## "):
            push()
            question, answer = None, []
            topic = line[3:].strip()
            continue
        if _DROPPED_SECTIONS.search(topic) or re.match(r"^conflicts to resolve", topic, re.I):
            continue
        if line.startswith("### "):
            push()
            question, answer = line[4:].strip(), []
            continue
        if question is not None:
            answer.append(line)
    push()
    return out


def parse_doc_sections(doc: str, title: str, md: str) -> list[Entry]:
    """A reference document as one entry per "## " section, its heading standing in for the question."""
    out: list[Entry] = []
    for part in re.split(r"^(?=## )", md, flags=re.M):
        if not part.startswith("## "):
            continue
        heading, _, body = part[3:].partition("\n")
        heading = heading.strip()
        if _DROPPED_SECTIONS.search(heading) or not body.strip():
            continue
        out.append(Entry(doc, title, heading, body.strip()))
    return out


def prospect_safe(entries: list[Entry]) -> list[Entry]:
    """What may be read to a prospect: no staff-facing faq_hard sections, no answer with a price."""
    return [
        e
        for e in entries
        if (e.doc != "faq_hard" or _CALLER_HARD_TOPICS.match(e.topic)) and not _PRICE.search(e.answer)
    ]


_STOP = set(
    (
        "a an and are as at be but by can do does for from have how i if in into is it its me my of on or our so that the their them "
        "there they this to us was we what when where which who why will with you your yours sysevo syra would could should get got "
        "just about any much many also then than been being did done has had here these those very really tell know need want like "
        "siseevo sam"
    ).split()
)
_SUFFIX = re.compile(r"(?:ations?|ing|ers?|ies|es|ed|ly|s)$")


def terms_of(text: str) -> list[str]:
    """Lower-case word stems: "integrations" and "integrate" both become "integrat"."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [_SUFFIX.sub("", w, count=1) if len(w) > 5 else w for w in words if len(w) > 1 and w not in _STOP]


def score(entries: list[Entry], query: str, limit: int = 3) -> list[tuple[Entry, float]]:
    """Every query term found in the question counts three, in the topic two, in the answer one;
    rarer terms count more. The same weights as Syra's scoreFaq."""
    q = list(dict.fromkeys(terms_of(query)))
    if not q or not entries:
        return []
    docs = [(e, set(terms_of(e.question)), set(terms_of(e.topic)), set(terms_of(e.answer))) for e in entries]
    df = {t: sum(1 for _, qt, _, at in docs if t in qt or t in at) for t in q}

    def idf(t: str) -> float:
        return math.log(1 + len(entries) / (1 + df[t]))

    scored = [
        (e, sum(idf(t) * ((3 if t in qt else 0) + (2 if t in tt else 0) + (1 if t in at else 0)) for t in q))
        for e, qt, tt, at in docs
    ]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def answer_for(entries: list[Entry], question: str, limit: int = 3) -> dict:
    """The tool's reply: the checked answers closest to the question, or an honest "not here"."""
    ranked = score(entries, question, limit)
    best = ranked[0][1] if ranked else 0.0
    hits = [(e, s) for e, s in ranked if best >= MIN_SCORE and s >= best * RUNNER_UP_SHARE]
    if not hits:
        return {
            "status": "no_answer",
            "say": "Nothing checked on that. Do not guess: say the team will cover it properly on the meeting, "
            "and keep going toward booking it.",
        }
    return {
        "status": "ok",
        "say": "Use only an answer below that really answers what they asked; if none does, say the team will "
        "cover it on the meeting. Answer in one or two short spoken sentences, in your own words. Keep their claims "
        "exactly and every limit or exception they give; never add to them, never quote a price, and never "
        "read out a link or a list.",
        "answers": [
            {"question": e.question, "answer": e.answer[:MAX_ANSWER_CHARS]} for e, _ in hits
        ],
    }
