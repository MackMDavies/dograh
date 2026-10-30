"""Who (or what) picked up a dialer call, decided from the words alone.

The listening stream (call_sorter.py) transcribes the PROSPECT side of the call and
hands the running transcript to `classify_opening` every time Deepgram sends a
result. This module is the decision, and nothing else: a pure, deterministic
function, so it can be measured against real calls
(api/tests/fixtures/dialer_call_sorter_openings.json, 676 hand-labelled openings
from production) and changed without a call in hand.

Kinds:
    person               somebody live (name + gatekeeper flag when it can tell)
    voicemail            a greeting -- personal or carrier
    mailbox_full         greeting ends in "the mailbox is full" / "not set up":
                         a message cannot be left
    screener             iPhone Call Screening, Google Call Screen / Call Assist and
                         their clones: "record your name and reason for calling"
    screener_unavailable the screener gave up and OFFERS a message ("this person is
                         not available ... leave an additional message"); counts as
                         a voicemail
    ivr                  menus, hold queues, recorded-call notices, carrier intercepts
    unknown              not enough yet

THE ONE RULE THAT MATTERS MOST: a screener asking for a name is never a voicemail.
"Please state your name after the tone" and "say your name and what you're calling
about after the beep" both contain the tone/beep wording a greeting uses, so the
screener checks run BEFORE the voicemail ones, and the only way out of "screener"
into a machine verdict is the screener's own "leave an additional message" offer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KINDS = (
    "person",
    "voicemail",
    "mailbox_full",
    "screener",
    "screener_unavailable",
    "ivr",
    "unknown",
)

# Kind -> stage name in the shared CONTRACT (dialer_call_stage_events.stage).
STAGE_FOR_KIND = {
    "person": "person",
    "voicemail": "machine",
    "screener_unavailable": "machine",
    "mailbox_full": "mailbox_full",
    "screener": "screener",
    "ivr": "ivr",
}

# A short opening followed by quiet is somebody waiting for us to talk. A bare
# "Hi." needs longer: greetings pause after it too ("Hi. ... You've reached Sam").
PERSON_PAUSE_MAX_WORDS = 10
PERSON_PAUSE_SECONDS = 1.5
PERSON_PAUSE_SECONDS_ONE_WORD = 2.5


@dataclass(frozen=True)
class SorterVerdict:
    kind: str = "unknown"
    confidence: float = 0.0
    rule: str = ""
    name: str | None = None
    gatekeeper: bool = False
    # Voicemail: the greeting has said its closing line ("at the tone", "record your
    # message") -- the beep is next. Says nothing about whether the beep has happened.
    end_of_greeting_cue: bool = False
    # Extra facts for the stage detail (vendor of a screener, kind of IVR, ...).
    detail: dict = field(default_factory=dict)

    @property
    def stage(self) -> str | None:
        return STAGE_FOR_KIND.get(self.kind)

    @property
    def decided(self) -> bool:
        return self.kind != "unknown"


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# ── mailbox full / not set up ────────────────────────────────────────────────────
_MAILBOX_FULL = _rx(
    r"mailbox(?: belonging to [\w() .-]{0,40})? (?:is |was )?(?:currently )?full",
    r"mailbox is full",
    r"not enough (?:space|room) to leave",
    r"cannot accept (?:any |new |more )?(?:new )?messages",
    r"can'?t accept (?:any |new |more )?messages",
    r"not accepting (?:any |new )?messages",
    r"(?:has not|hasn'?t) been set ?up",
    r"not (?:been )?set ?up yet",
    r"(?:can'?t|cannot|unable to) leave a message because",
    r"\byou can'?t leave\b",
)

# ── screeners ────────────────────────────────────────────────────────────────────
_SCREENER_IPHONE = _rx(
    r"record(?:ed)? (?:your )?name and (?:the )?reason for (?:calling|your call)",
    r"see if (?:this|the) person is (?:available|the)",
    r"if you record(?:ed)? (?:your )?name",
)
_SCREENER_GOOGLE = _rx(
    r"call ?assist(?:ant)? by google",
    r"\bby google,? recording",
    r"google (?:virtual )?(?:calling )?assistant",
    r"google voice will try to connect",
    r"\w+ voice will try to connect you",
)
_SCREENER_GENERIC = _rx(
    r"(?:a |an |\w+'s )?(?:call|calling|ai|virtual) assist(?:ant)?,? (?:recording|for)",
    r"recording (?:this|a|the) call for the person you'?re (?:trying to reach|calling)",
    r"(?:say|state|tell me) (?:who you are|your name)(?: and| after| ,)",
    r"say who you are and why you'?re calling",
    r"(?:explain|clarify) the purpose of your call",
    r"say your name and what you'?re calling about",
    r"before i (?:try to )?connect(?: you)?,? (?:can|could) i ask",
    r"i'?m only able to check the reason for the call",
    r"call for the person you'?re trying to reach",
)
# What a screener says while it works -- not a person, not a message prompt.
_SCREENER_SYSTEM = _rx(
    r"please stay on the line",
    r"^(?:okay|ok|alright|got it)\.?$",
    r"let me (?:try|see|check)",
    r"(?:please )?hold while i (?:try to )?connect you",
    r"let me try to (?:get|reach) the person",
    r"one (?:sec|second|moment)",
    r"checking with the person",
    r"one last thing before i transfer you",
    r"^thanks?(?: you)?\b",
    r"thank you\.?$",
    r"before i (?:try to )?connect",
    r"(?:can|could) you (?:please )?(?:say|tell me|give me|share|clarify)",
    r"i (?:can'?t|cannot|didn'?t) (?:hear|catch|understand)",
    r"sorry\.?$",
    r"what did you say",
    r"i apologi[sz]e",
    r"have a (?:nice|good|great) (?:day|one)",
)
_SCREENER_DECLINED = _rx(
    r"(?:cannot|can'?t|is unable to|isn'?t able to) (?:take|answer) (?:your|the) call",
    r"can'?t (?:answer|take) (?:it )?(?:right )?now",
    r"no longer relevant",
    r"(?:doesn'?t|does not) want to (?:take|answer)",
)
_SCREENER_MESSAGE_OFFER = _rx(
    r"leave an additional message",
    r"(?:would like to |want to |can )?leave a message,? please (?:reply|speak|record)",
    r"reply after the tone",
)
_PERSON_AFTER_SCREENER = _rx(
    r"\bhello\b",
    r"who'?s this",
    r"who is (?:this|calling|it)",
    r"this is [a-z]",
    r"\bspeaking\b",
    r"take me off",
    r"remove me",
    r"what do you want",
    r"(?:^|\. )(?:yeah|yes|yep)\b",
    r"how can i help",
    r"from who",
)

# ── voicemail greetings ──────────────────────────────────────────────────────────
# HARD: a mailbox is listening. Only greetings and mailbox prompts say these.
_VM_HARD = _rx(
    r"\bleave (?:me |us )?(?:a |your |an? )?(?:\w+ ){0,2}(?:message|voice ?mail|name,? (?:and|number|phone))",
    r"\bleave (?:me |us )?(?:your |a )?(?:name|number)",
    r"if you (?:could |will |would |can )?(?:please |just )?leave",
    r"(?:at|after|following) the (?:tone|beep|sound of the tone)",
    r"when you hear the (?:tone|beep)",
    r"record your message",
    r"(?:start|begin) (?:your |recording your )?message",
    r"when you(?:'ve| have)? finished recording",
    r"forwarded to (?:the |an? )?(?:automat\w+ )?voice ?(?:mail|messag\w+|message)",
    r"voice messag(?:ing|e) system",
    r"please leave your message for",
    r"(?:reached|gotten to|this is) (?:the |my |our )?(?:\w+ )?(?:voice ?mail|mailbox)",
    r"\bvoice ?mail(?:box)? (?:of|for)\b",
    r"(?:'s|s') voice ?mail",
    r"general (?:voice ?)?mailbox",
    r"has been forwarded\.?$",
    r"has been forwarded to (?:an? |the )?(?:automat\w+ )?voice\b",
    r"extension [\w-]+ (?:is not available|is on the phone|is busy)",
    r"the person at extension",
    r"(?:text|email|e-mail) me",
    r"send me (?:a text|an email|a message)",
)
# SOFT: unavailability. Greetings say it; so does a receptionist ("he's not
# available, can I take a message?"), so a live cue beside it wins.
_VM_SOFT = _rx(
    r"(?:is|are|am|i'?m) (?:not|currently not|currently un|un)available",
    r"(?:is|are) not (?:available|able)",
    r"\bunavailable\b",
    r"isn'?t available",
    r"(?:can'?t|cannot|couldn'?t|unable to|not able to) (?:take|answer|get to|come to|pick up)",
    r"not here to take",
    r"missed (?:your|the) call",
    r"sorry (?:i|we) (?:missed|didn'?t get|couldn'?t)",
    r"(?:missed|you missed) (?:me|us)",
    r"away from (?:my|the) (?:desk|phone)",
    r"(?:out of|away from) the (?:office|country)",
    r"(?:either|currently) (?:on the phone|on another call|away|out|on the other line)",
    r"(?:get|getting|be|call) (?:right )?back (?:to|with) you",
    r"return your call",
    r"call you back",
    r"give you a call (?:back|as soon)",
    r"(?:the person|the party|the subscriber|the number|the customer) you(?: are|'re| have|'ve)? (?:trying to reach|called|dialed|calling|reached),? (?:is|isn'?t|can'?t|cannot|has|does|doesn'?t|will)\b",
    r"no longer monitored",
    r"\bno one is available to answer",
    r"telephone number [\d() .-]+ can'?t",
    r"^\(?[\d() .-]{3,}\)?\.? (?:is )?not available",
    r"^telephone number [\d() .-]+\.?$",
    r"(?:'s|s') (?:cell|mobile) phone",
    r"(?:reached|this is) the (?:cell|mobile|desk|office|personal|direct)\b",
)
_MAILBOX_PROMPT = _rx(
    r"forwarded to (?:the |an? )?(?:automat\w+ )?voice",
    r"record your message",
    r"(?:at|after) the (?:tone|beep)",
    r"voice ?mail(?:box)? (?:of|for)\b",
    r"welcome to the (?:\w+ )?(?:voice ?)?mailbox",
    r"(?:start|begin) your message",
    r"extension [\w-]+ (?:is not available|is on the phone|is busy)",
)
_VM_END_CUE = _rx(
    r"(?:at|after) the (?:tone|beep|sound of the tone)",
    r"when you hear the (?:tone|beep)",
    r"record your message",
    r"(?:start|begin) (?:your )?message now",
    r"when you(?:'ve| have)? finished recording",
)

# ── IVR / automated front doors ──────────────────────────────────────────────────
_NUM = r"(?:\d|one|two|three|four|five|six|seven|eight|nine|zero|pound|star|the pound|the star|hash)"
_IVR = _rx(
    rf"\bpress(?:ing)? {_NUM}",
    rf"\b(?:dial|enter) {_NUM}\b",
    rf"\bor {_NUM}(?: for|,)",
    r"\bextension\b",
    r"\bdirectory\b",
    r"\bmenu\b",
    r"\b(?:listen|select) (?:carefully|closely|from)",
    r"following options",
    r"say (?:sales|support|service|other|yes or no|the name|their name|that person'?s name)",
    r"say yes or",
    r"(?:please |kindly )?hold (?:for|while|and)",
    r"please (?:hold|wait)\b",
    r"next available",
    r"will be with you (?:shortly|in (?:just )?a moment)",
    r"be right with you",
    r"connect(?:ing)? you(?:r call)? now",
    r"connect you to (?:the|our)",
    r"while we (?:connect|transfer)",
    r"(?:please )?(?:wait|remain on the line) (?:while|and)",
    r"transferring (?:you|your call)",
    r"your call is (?:important|now being)",
    r"(?:business|office|operating|normal) hours",
    r"hours of operation",
    r"office is (?:currently )?closed",
    r"outside (?:of )?(?:our )?(?:normal )?business",
    r"auto ?attendant",
    r"(?:please )?hang up and (?:dial|call)",
    r"number you have dialed",
    r"(?:no longer|not) in service",
    r"could not be completed",
    r"call (?:can ?not|cannot) be completed",
    r"i think you said",
    r"enter it (?:at any time|now)",
    r"your selection",
    r"if you are (?:a |an )?(?:current|existing|new) (?:client|customer|partner)",
    r"we'?re sorry\.? (?:the number|your call)",
)
# Recorded-call notices: automated, but a person or a mailbox may follow straight
# away, so they only decide the call when nothing else has been said.
_IVR_NOTICE = _rx(
    r"(?:this |all |your )?calls? (?:may|will|is|are) (?:be )?(?:being )?(?:recorded|monitored|transcribed)",
    r"for (?:quality|training) (?:assurance|purposes|and)",
    r"\bbeing recorded\b",
)
# A long scripted welcome with no menu yet: no receptionist talks like this.
_IVR_PREAMBLE_START = _rx(r"^(?:hello,? and )?(?:thank you|thanks) for (?:calling|contacting|choosing)", r"^(?:hello,? and )?welcome to", r"^for calling")
_IVR_PREAMBLE_WORDS = 18
_IVR_INTERCEPT = _rx(
    r"number you have dialed",
    r"(?:no longer|not) in service",
    r"could not be completed",
    r"call (?:can ?not|cannot) be completed",
    r"we'?re sorry\.? the number",
)
_MENU_OPTION = _rx(rf"press(?:ing)? {_NUM}", rf"\bdial {_NUM}\b", r"by pressing")
# A sentence that IS a menu option: "To leave a message, press 3." / "Press 1 to ..."
_MENU_SENTENCE = _rx(
    rf"^(?:to|for|if|or|otherwise)\b.*\b(?:press|dial)(?:ing)? {_NUM}",
    rf"^(?:please |you may |you can )?(?:press|dial)(?:ing)? {_NUM},? (?:to|for)\b",
    r"by pressing",
)
_IVR_HOLD = _rx(
    r"hold",
    r"next available",
    r"be (?:right )?with you",
    r"connect",
    r"transfer",
    r"remain on the line",
)

# ── a live person ────────────────────────────────────────────────────────────────
_PERSON = _rx(
    r"how (?:can|may|could) (?:i|we) (?:help|assist|direct)",
    r"how can i help",
    r"what can i do for you",
    r"(?:can|may) i help you",
    r"who(?:'s| is) (?:this|calling|speaking|it)",
    r"(?:may|can) i ask who",
    r"who are you (?:looking for|with|calling)",
    r"what(?:'s| is) this (?:regarding|about|in regards)",
    r"in regards? to\?",
    r"what company",
    r"where are you (?:calling )?from",
    r"from where\?",
    r"\bexcuse me\?",
    r"i'?m sorry\?",
    r"\bsorry\?",
    r"hello\?",
    r"(?:^|\. )(?:yes|yeah|yep)\?",
    r"are you (?:still )?there",
    r"can you hear me",
    r"remove me",
    r"take (?:me|my number) off",
    r"stop calling",
    r"do not call",
    r"not interested",
    r"(?:he|she|they)(?:'s| is| are|'re) (?:not (?:in|here|available)|unavailable|on the other line|out)",
    r"(?:can|may) i take a message",
    r"(?:the )?owner (?:is|isn'?t)",
    r"(?:^|\. )(?:\w+ )?speaking\.?(?: |$)",
    r"you'?re (?:actually )?calling my",
    r"(?:what|who) (?:do|did) you (?:want|say)",
    r"you are speaking with",
    r"(?:he|she)(?:'s| is) not\.",
    r"i can (?:get|take) (?:a |your )?(?:good )?name and number",
    r"(?:^|\. )no\.(?: |$)",
    r"your (?:ai|virtual) (?:coordinator|receptionist)",
)
_GREETING_ONLY = _rx(
    r"^(?:hello|hi|hey|yeah|yes|good (?:morning|afternoon|evening)|afternoon|morning)\b",
    r"\bthis is \w+",
    r"\b\w+ speaking\b",
    r"^[\w'&., -]{1,40}\.$",
)
_GATEKEEPER = _rx(
    r"thank(?:s| you) for (?:calling|contacting|choosing)",
    r"direct your call",
    r"receptionist",
    r"front desk",
    r"(?:he|she|they)(?:'s| is| are|'re) (?:not (?:in|here|available)|unavailable|on the other line|out)",
    r"(?:can|may) i take a message",
    r"who are you looking for",
    r"owner (?:is|isn'?t)",
    r"(?:ai|virtual) (?:receptionist|coordinator|assistant)",
)
# "Acme Systems. This is Sam." -- answering with a business name.
_COMPANY_ANSWER = re.compile(
    r"^(?!(?:hello|hi|hey|yes|yeah|good (?:morning|afternoon|evening)|afternoon|morning)\b)"
    r"[\w'&-]+(?: [\w'&-]+){0,4}\. (?:this is|\w+ speaking)"
)
_AI_RECEPTIONIST = _rx(r"(?:ai|virtual) (?:receptionist|coordinator|assistant)", r"an ai\b")
_NAME = re.compile(
    r"\b(?i:this is|it'?s|my name is|i'?m|you'?ve reached|you have reached)\s+([A-Z][a-z'-]{1,20})\b"
    r"|\b([A-Z][a-z'-]{1,20}) speaking\b"
)
_NOT_A_NAME = {
    "Sorry", "The", "Not", "Calling", "Unable", "Currently", "Here", "Just", "Still",
    "Away", "Out", "On", "In", "With", "From", "Your", "Our", "My", "A", "An", "Call",
    "Google", "Voice", "Speaking", "Yes", "No", "Hello", "Hi", "Hey", "Good", "Thank",
}


def normalise(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _first(pattern: re.Pattern, text: str) -> int | None:
    match = pattern.search(text)
    return match.start() if match else None


def _extract_name(original: str) -> str | None:
    for match in _NAME.finditer(original or ""):
        candidate = match.group(1) or match.group(2)
        if candidate and candidate not in _NOT_A_NAME:
            return candidate
    return None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]


def _sentence_at(text: str, position: int) -> str:
    start = max(text.rfind(". ", 0, position), text.rfind("? ", 0, position), text.rfind("! ", 0, position))
    start = 0 if start < 0 else start + 2
    ends = [i for i in (text.find(". ", position), text.find("? ", position), text.find("! ", position)) if i >= 0]
    return text[start:(min(ends) + 1) if ends else len(text)]


def _is_mailbox_prompt(sentence: str) -> bool:
    """A mailbox speaking ("record your message and press pound"), as opposed to a
    menu option that leads to one ("press 1 to leave a detailed message")."""
    prompt = _MAILBOX_PROMPT.search(sentence)
    if not prompt or _MENU_SENTENCE.search(sentence):
        return False
    option = _MENU_OPTION.search(sentence)
    return option is None or prompt.start() < option.start()


def _screener_vendor(text: str) -> str:
    if _SCREENER_IPHONE.search(text):
        return "iphone"
    if _SCREENER_GOOGLE.search(text):
        return "google"
    return "assistant"


def _classify_screener(text: str, original: str) -> SorterVerdict | None:
    matches = [m for p in (_SCREENER_IPHONE, _SCREENER_GOOGLE, _SCREENER_GENERIC)
               for m in [p.search(text)] if m]
    if not matches:
        return None
    vendor = _screener_vendor(text)
    last_cue_end = max(m.end() for m in matches)
    tail = text[last_cue_end:]
    offer = _SCREENER_MESSAGE_OFFER.search(tail)
    if offer:
        return SorterVerdict(
            "screener_unavailable", 0.95, "screener_message_offer",
            end_of_greeting_cue=bool(_VM_END_CUE.search(tail[offer.start():])),
            detail={"vendor": vendor},
        )
    # Somebody picked up through the screener: a sentence after the screener's
    # own patter that is not more patter and sounds like a person.
    for sentence in _sentences(tail):
        if _SCREENER_SYSTEM.search(sentence):
            continue
        if _PERSON_AFTER_SCREENER.search(sentence):
            return SorterVerdict(
                "person", 0.85, "person_after_screener",
                name=_extract_name(original[-200:]), gatekeeper=False,
                detail={"via": "screener", "vendor": vendor},
            )
    declined = bool(_SCREENER_DECLINED.search(tail))
    detail = {"vendor": vendor}
    if declined:
        detail["declined"] = True
    return SorterVerdict("screener", 0.97, f"screener_{vendor}", detail=detail)


def classify_opening(text: str, *, silence_seconds: float = 0.0) -> SorterVerdict:
    """Classify the prospect side of a call from its running transcript.

    `text` is everything heard so far, in order (finals plus the current interim).
    `silence_seconds` is how long the far end has been quiet since it last spoke
    (the caller measures it from the audio). It is what lets a bare "Hello." count
    as a person: a greeting does not stop after a few words to wait for an answer.
    """
    t = normalise(text)
    if not t:
        return SorterVerdict()
    words = len(t.split())
    needed = PERSON_PAUSE_SECONDS_ONE_WORD if words <= 2 else PERSON_PAUSE_SECONDS
    speech_paused = silence_seconds >= needed

    if _MAILBOX_FULL.search(t):
        not_set_up = bool(re.search(r"set ?up", t))
        return SorterVerdict(
            "mailbox_full", 0.97, "mailbox_not_set_up" if not_set_up else "mailbox_full",
            detail={"reason": "not_set_up" if not_set_up else "full"},
        )

    screener = _classify_screener(t, text)
    if screener is not None:
        return screener

    if _IVR_INTERCEPT.search(t):
        return SorterVerdict("ivr", 0.95, "ivr_intercept", detail={"kind": "intercept"})

    hard = _first(_VM_HARD, t)
    soft = _first(_VM_SOFT, t)
    ivr = _first(_IVR, t)
    person = _first(_PERSON, t)
    end_cue = bool(_VM_END_CUE.search(t))

    def voicemail(rule: str, confidence: float) -> SorterVerdict:
        return SorterVerdict(
            "voicemail", confidence, rule, name=_extract_name(text),
            end_of_greeting_cue=end_cue,
        )

    def ivr_verdict() -> SorterVerdict:
        if _IVR_INTERCEPT.search(t):
            kind = "intercept"
        elif _IVR_HOLD.search(t) and not re.search(rf"press {_NUM}|extension|directory", t):
            kind = "hold"
        else:
            kind = "menu"
        return SorterVerdict("ivr", 0.9, f"ivr_{kind}", detail={"kind": kind})

    def person_verdict(rule: str, confidence: float) -> SorterVerdict:
        detail = {"ai_receptionist": True} if _AI_RECEPTIONIST.search(t) else {}
        return SorterVerdict(
            "person", confidence, rule, name=_extract_name(text),
            gatekeeper=bool(_GATEKEEPER.search(t) or _COMPANY_ANSWER.match(t)),
            detail=detail,
        )

    # A mailbox prompt after a menu ("...your call has been forwarded to the voice
    # mail for...") is where the menu went, so it beats the earlier IVR cue --
    # unless the prompt sits inside a menu option ("to leave a message, press 3"),
    # which is the menu OFFERING a mailbox, not one listening.
    if hard is not None:
        offered_by_menu = ivr is not None and _MENU_SENTENCE.search(_sentence_at(t, hard))
        if not offered_by_menu and (ivr is None or ivr >= hard):
            if person is not None and person < hard and not _VM_END_CUE.search(t[:person]):
                return person_verdict("person_before_greeting", 0.8)
            return voicemail("voicemail_hard", 0.95)
        if any(_is_mailbox_prompt(sentence) for sentence in _sentences(t[ivr:])):
            return voicemail("voicemail_after_menu", 0.9)
        return ivr_verdict()

    if person is not None and (ivr is None or person < ivr):
        return person_verdict("person_cue", 0.9)

    if ivr is not None and (soft is None or ivr < soft):
        return ivr_verdict()

    if soft is not None:
        return voicemail("voicemail_soft", 0.75)

    # Strip a recorded-call notice and judge what is left: "This call is being
    # recorded. Hi, it's Jordan." is a person; the notice on its own is a machine.
    if _IVR_NOTICE.search(t):
        rest = " ".join(s for s in _sentences(t) if not _IVR_NOTICE.search(s))
        if not (speech_paused and rest and _GREETING_ONLY.search(rest)
                and not _IVR_PREAMBLE_START.search(rest)
                and len(rest.split()) <= PERSON_PAUSE_MAX_WORDS):
            return SorterVerdict("ivr", 0.7, "ivr_notice", detail={"kind": "notice"})
        return person_verdict("person_after_notice", 0.7)

    if _IVR_PREAMBLE_START.search(t) and len(t.split()) >= _IVR_PREAMBLE_WORDS:
        return SorterVerdict("ivr", 0.7, "ivr_preamble", detail={"kind": "menu"})

    # Only a finished sentence counts: an interim that stops mid-word is the
    # transcriber catching up, not the far end waiting for us.
    if speech_paused and re.search(r"[.?!]$", t):
        if words <= PERSON_PAUSE_MAX_WORDS and _GREETING_ONLY.search(t):
            return person_verdict("person_short_then_quiet", 0.7)

    return SorterVerdict()


__all__ = [
    "KINDS",
    "STAGE_FOR_KIND",
    "SorterVerdict",
    "classify_opening",
    "normalise",
]
