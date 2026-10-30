"""Dialer Phases 2 + 3: the decisions the handling agent makes, as pure functions.

The call sorter (call_sorter.py) says WHO answered: a call screener or a phone menu.
When the account has switched handling on, call_handling.py takes the prospect's line
away from the rep and works it; this module is every judgement it makes, kept free of
I/O so each rule can be tested on text alone:

* screener answers -- which answer to play (rotation), the words of an agent answer
  (template), and what the screener said afterwards (waiting / put through /
  unavailable / repeating itself / a follow-up question);
* phone menus -- reading a menu's options, choosing keys in the agreed order
  (known extension -> dial by name -> department from job title -> operator),
  spelling a name on the keypad, replaying a learned path, and the give-up rules.

Nothing here touches a call. Only machines are ever handled: any person -- a
receptionist included -- goes straight to the rep.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from api.services.telephony.dialer.call_sorter_rules import normalise

# ── screener answers ─────────────────────────────────────────────────────────────

SCREENER_MODES = ("off", "rep", "admin", "agent", "rotate")
SCREENER_STYLES = ("rep", "admin", "agent")
# The longest a prerecorded screener answer may be (seconds).
SCREENER_CLIP_MAX_SECONDS = 6
# After our answer: this long with nothing decisive from the screener ends the call.
SCREENER_NO_ANSWER_SECONDS = 45.0
DEFAULT_REASON = "a quick business follow-up"
DEFAULT_AGENT_TEMPLATE = (
    "Hi, this is {rep_first} from {company}, calling for {prospect_first} about {reason}."
)

_PLACEHOLDER = re.compile(r"\{(rep_first|company|reason|prospect_first)\}")


def first_word(value: Any) -> str:
    text = str(value or "").strip()
    return text.split()[0] if text else ""


def render_template(template: str | None, facts: dict[str, Any]) -> str:
    """Fill {rep_first}, {company}, {reason}, {prospect_first}.

    A missing fact never leaves a brace or a dangling "for  about" behind: the
    phrase around an empty {prospect_first} collapses ("calling for  about" ->
    "calling about"), and an unknown company reads "our team".
    """
    text = str(template or DEFAULT_AGENT_TEMPLATE)
    values = {
        "rep_first": first_word(facts.get("rep_first")) or "a colleague",
        "company": str(facts.get("company") or "").strip() or "our team",
        "reason": str(facts.get("reason") or "").strip() or DEFAULT_REASON,
        "prospect_first": first_word(facts.get("prospect_first")),
    }
    if not values["prospect_first"]:
        text = re.sub(r"\s*\b(?:for|to speak with|to speak to|for you,)\s+\{prospect_first\}", "", text)
    text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    text = re.sub(r"\s+([,.?!])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
    return text[:600]


@dataclass(frozen=True)
class ScreenerAnswer:
    answer_ref: str            # 'drop:<uuid>' (a rep's recording) | 'answer:<uuid>'
    style: str                 # 'rep' | 'admin' | 'agent'
    name: str = ""
    storage_path: str | None = None
    template: str | None = None
    pinned: bool = False
    plays: int = 0
    put_through: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ScreenerAnswer | None":
        ref = str(row.get("answer_ref") or "")
        style = str(row.get("style") or "")
        if not re.fullmatch(r"(drop|answer):[0-9a-f-]{36}", ref) or style not in SCREENER_STYLES:
            return None
        if style == "agent" and not str(row.get("template") or "").strip():
            return None
        if style in {"rep", "admin"} and not str(row.get("storage_path") or "").strip():
            return None
        return cls(
            answer_ref=ref, style=style, name=str(row.get("name") or ""),
            storage_path=row.get("storage_path") or None, template=row.get("template") or None,
            pinned=bool(row.get("pinned")), plays=max(0, int(row.get("plays") or 0)),
            put_through=max(0, int(row.get("put_through") or 0)),
        )


def answer_weight(answer: ScreenerAnswer) -> float:
    """How strongly rotation favours an answer that has had its minimum plays.

    The put-through rate, smoothed (one success in two imagined plays) so a lucky
    first few calls do not lock an answer in, then squared so a clearly better answer
    gets most -- not all -- of the traffic.
    """
    rate = (answer.put_through + 1) / (answer.plays + 2)
    return rate * rate


def choose_answer(
    candidates: Sequence[ScreenerAnswer], *, min_plays: int, rng: random.Random | None = None,
) -> ScreenerAnswer | None:
    """Pick the answer to play.

    1. An admin's pinned answer always plays (pause = not a candidate at all).
    2. Until every answer has had ``min_plays`` plays, the least-played one goes next
       (ties: the order given), so each style is measured fairly.
    3. Then favour the best performers: weighted by ``answer_weight``.
    """
    pool = [c for c in candidates if c is not None]
    if not pool:
        return None
    pinned = [c for c in pool if c.pinned]
    if pinned:
        return pinned[0]
    floor = max(1, int(min_plays or 1))
    learning = [c for c in pool if c.plays < floor]
    if learning:
        return min(learning, key=lambda c: c.plays)
    rng = rng or random.Random()
    weights = [answer_weight(c) for c in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def answers_for_mode(payload: dict[str, Any] | None) -> tuple[list[ScreenerAnswer], list[ScreenerAnswer]]:
    """(candidates, fallback) from dialer_screener_candidates. A rep without their own
    screener recording falls back to the next style (admin, then agent)."""
    payload = payload or {}
    def parse(rows: Iterable[Any]) -> list[ScreenerAnswer]:
        out = []
        for row in rows or []:
            answer = ScreenerAnswer.from_row(row) if isinstance(row, dict) else None
            if answer:
                out.append(answer)
        return out
    return parse(payload.get("candidates")), parse(payload.get("fallback"))


# What the screener says after our answer.
_WAITING = re.compile(
    r"please (?:stay|hold) on the line|stay on the line|hold while i (?:try to )?(?:connect|see)|"
    r"let me (?:try|see|check) if|i'?ll see if (?:they'?re|he'?s|she'?s) available|"
    r"(?:trying|try) to connect you|one moment while|checking with the person|"
    r"please wait while|wait while i"
)
_UNAVAILABLE = re.compile(
    r"leave an additional message|(?:is|are) (?:not|un)available,? (?:to take|please leave)|"
    r"(?:would like to |want to |can )?leave a message,? please (?:reply|speak|record)|reply after the tone|"
    r"isn'?t available|is not available|can'?t (?:take|answer) (?:your|the) call|"
    r"(?:doesn'?t|does not) want to (?:take|answer)"
)
_SCREENER_ASK = re.compile(
    r"record(?:ed)? (?:your )?name and (?:the )?reason for (?:calling|your call)|"
    r"say your name and what you'?re calling about|(?:say|state|tell me) (?:who you are|your name)|"
    r"say who you are and why you'?re calling|explain the purpose of your call"
)
_FOLLOW_UP = re.compile(
    r"(?:can|could) you (?:please )?(?:say|tell me|give me|share|clarify|repeat)|"
    r"what (?:is|'?s) (?:this|it|the call) (?:regarding|about|in regards? to)|"
    r"which company|what company|who (?:are you|is this) (?:calling )?(?:with|from)|"
    r"i (?:can'?t|cannot|didn'?t) (?:hear|catch|understand)|one last thing|anything else"
)
_PERSON = re.compile(
    r"\bhello\b|who'?s this|who is (?:this|calling|it)|this is [a-z]|\bspeaking\b|"
    r"take me off|remove me|what do you want|how can i help|from who|^(?:yeah|yes|yep|hi|hey)\b"
)


def screener_reply_kind(text: str) -> str | None:
    """Classify what the screener (or the person behind it) said after our answer.

    'unavailable' (a message is offered: Phase 1 voicemail rules), 'waiting'
    ("please stay on the line": silent, keep listening), 'repeat' (it asks for our
    name again: our answer did not land), 'follow_up' (a question the agent style can
    answer from its facts), 'person' (somebody picked up) or None (nothing decisive).
    Order matters: an offer to leave a message wins over the patter around it.
    """
    t = normalise(text)
    if not t:
        return None
    if _UNAVAILABLE.search(t):
        return "unavailable"
    if _SCREENER_ASK.search(t):
        return "repeat"
    if _WAITING.search(t):
        return "waiting"
    if _FOLLOW_UP.search(t):
        return "follow_up"
    if _PERSON.search(t):
        return "person"
    return None


def follow_up_reply(question: str, facts: dict[str, Any]) -> str:
    """A short answer to a screener's follow-up that never goes beyond the facts."""
    t = normalise(question)
    rep = first_word(facts.get("rep_first")) or "a colleague"
    company = str(facts.get("company") or "").strip() or "our team"
    reason = str(facts.get("reason") or "").strip() or DEFAULT_REASON
    if re.search(r"company|who (?:are you|is this)|calling (?:from|with)", t):
        return f"It's {rep} from {company}."
    if re.search(r"regarding|about|purpose|reason", t):
        return f"It's about {reason}."
    return f"This is {rep} from {company}, calling about {reason}."


# ── phone menus (IVR) ────────────────────────────────────────────────────────────

IVR_DEFAULT_MAX_LEVELS = 3
IVR_DEFAULT_MAX_SECONDS = 60
# A menu has finished offering its options when it has been quiet this long.
MENU_SETTLED_SILENCE_SECONDS = 1.2

_WORD_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "star": "*", "pound": "#", "hash": "#",
}
_KEY = r"(?:\d|zero|one|two|three|four|five|six|seven|eight|nine|star|pound|hash)"
_OPTION_PATTERNS = (
    # "for sales, press 1" / "to reach accounts press two" / "if you are a customer, press 3"
    re.compile(rf"(?:for|to reach|to speak (?:to|with)|to|if you(?: are|'re)?(?: a| an)?)\s+([a-z][a-z' &/-]{{1,60}}?),?\s+(?:please\s+)?(?:press|dial|enter)\s+({_KEY})\b"),
    # "press 1 for sales" / "press two to reach the operator"
    re.compile(rf"(?:press|dial)\s+({_KEY})\s+(?:for|to reach|to speak (?:to|with)|to|if)\s+([a-z][a-z' &/-]{{1,60}}?)(?=[.,;]|\s+(?:press|dial|or|for)\b|$)"),
)
_EXTENSION_PROMPT = re.compile(
    r"(?:know|have) (?:your |the )?(?:party'?s |person'?s )?extension|enter (?:the |your )?(?:party'?s )?extension|"
    r"dial (?:the |your )?(?:party'?s )?extension|extension at any time|extension now"
)
_DIRECTORY_PROMPT = re.compile(
    r"(?:spell|enter|key in|type) (?:the )?(?:first (?:few |three |\w+ )?letters of )?(?:the |their |your party'?s |the person'?s )?(?:last|first)? ?name|"
    r"using your (?:telephone )?keypad,? (?:spell|enter)|dial by name"
)
_DIRECTORY_OPTION = re.compile(r"(?:company |employee |staff |dial[- ]by[- ]name |name )?directory|dial by name")
_NOT_FOUND = re.compile(
    r"(?:not|no) (?:a )?valid|invalid|not recogni[sz]ed|(?:was|were|is) not found|no (?:match|matches)|"
    r"(?:could|can) ?not (?:find|be found|locate)|i'?m sorry,? (?:that|i didn'?t)|please try again|not available at this extension"
)
_OPERATOR_WORDS = re.compile(
    r"operator|reception(?:ist)?|front desk|main (?:office|desk)|attendant|all other|other (?:inquiries|enquiries|calls|questions)|"
    r"anyone else|someone|a (?:team )?member of (?:our|the) (?:team|staff)|general"
)
_CONFIRM = re.compile(rf"(?:if this is correct|to (?:confirm|accept|connect)|if (?:that|this) is (?:right|who)[^,.]*),?\s*(?:please\s+)?press\s+({_KEY})")

DEPARTMENTS: dict[str, tuple[re.Pattern, re.Pattern]] = {
    # department: (job-title keywords, menu-label keywords)
    "sales": (
        re.compile(r"sales|revenue|business development|commercial|account executive|bdr|sdr|growth|marketing"),
        re.compile(r"sales|new (?:business|customers?|accounts?)|purchase|buy|business development|marketing"),
    ),
    "accounts": (
        re.compile(r"account(?:s|ing|ant)? (?:payable|receivable|manager)?|accounting|finance|financial|cfo|controller|bookkeep|billing|payroll|treasur"),
        re.compile(r"accounts?(?: payable| receivable)?|accounting|billing|finance|invoice|payment"),
    ),
    "operations": (
        re.compile(r"operations?|\bcoo\b|logistic|warehouse|plant|production|facilit|supply|procurement|purchas|dispatch|service manager"),
        re.compile(r"operations?|logistic|warehouse|dispatch|service|scheduling|procurement|purchasing|shipping"),
    ),
}


@dataclass(frozen=True)
class MenuOption:
    key: str
    label: str


@dataclass(frozen=True)
class KeyChoice:
    digits: str
    reason: str     # 'learned' | 'extension' | 'dial_by_name' | 'directory' | 'department' | 'operator' | 'confirm'
    label: str = ""


@dataclass
class LeadFacts:
    extension: str = ""
    last_name: str = ""
    first_name: str = ""
    job_title: str = ""

    @property
    def department(self) -> str | None:
        title = normalise(self.job_title)
        if not title:
            return None
        for name, (title_rx, _menu_rx) in DEPARTMENTS.items():
            if title_rx.search(title):
                return name
        return None


def _key(token: str) -> str:
    token = token.strip().lower()
    return _WORD_DIGITS.get(token, token if re.fullmatch(r"[0-9*#]", token) else "")


def parse_menu(text: str) -> list[MenuOption]:
    """Options a menu offered, in the order heard. First mention of a key wins."""
    t = normalise(text)
    options: list[tuple[int, MenuOption]] = []
    seen: set[str] = set()
    for pattern_index, pattern in enumerate(_OPTION_PATTERNS):
        for match in pattern.finditer(t):
            label, raw = (match.group(1), match.group(2)) if pattern_index == 0 else (match.group(2), match.group(1))
            key = _key(raw)
            label = re.sub(r"^(?:the|our|a|an)\s+", "", label.strip(" ,.'"))
            if key and label and key not in seen:
                seen.add(key)
                options.append((match.start(), MenuOption(key, label)))
    return [option for _, option in sorted(options, key=lambda pair: pair[0])]


def is_not_found(text: str) -> bool:
    return bool(_NOT_FOUND.search(normalise(text)))


def menu_signature(text: str) -> str:
    """A menu, reduced so the same menu heard twice compares equal."""
    t = normalise(text)
    for word, digit in _WORD_DIGITS.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    t = re.sub(r"[^a-z0-9*# ]", " ", t)
    return " ".join(t.split())[:400]


def same_menu(a: str, b: str, *, threshold: float = 0.8) -> bool:
    """Do two menu transcripts say the same thing (word overlap, order-free)?"""
    wa, wb = set(menu_signature(a).split()), set(menu_signature(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / max(len(wa), len(wb)) >= threshold


_KEYPAD = {
    **dict.fromkeys("abc", "2"), **dict.fromkeys("def", "3"), **dict.fromkeys("ghi", "4"),
    **dict.fromkeys("jkl", "5"), **dict.fromkeys("mno", "6"), **dict.fromkeys("pqrs", "7"),
    **dict.fromkeys("tuv", "8"), **dict.fromkeys("wxyz", "9"),
}


def spell_on_keypad(name: str, *, letters: int = 8) -> str:
    """'Ortiz' -> '67849'. Letters only; directories match on a prefix."""
    return "".join(_KEYPAD[c] for c in normalise(name) if c in _KEYPAD)[:letters]


def _field(key: Any) -> str:
    """'Job Title' / 'job-title' / 'jobTitle' -> 'job_title'."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", str(key or "").strip())
    return re.sub(r"[\s-]+", "_", text).lower()


def extension_from(lead: dict[str, Any]) -> str:
    """A known extension: 'x123' / 'ext. 123' on the phone, or an extension field."""
    phone = str(lead.get("phone") or "")
    match = re.search(r"(?:ext\.?|extension|x|#)\s*(\d{1,6})\s*$", phone, re.I)
    if match:
        return match.group(1)
    for source in (lead.get("enrichment"), lead.get("source_row")):
        if isinstance(source, dict):
            for key, value in source.items():
                if re.fullmatch(r"(?:phone_?)?ext(?:ension)?(?:_?number)?", _field(key)):
                    digits = re.sub(r"\D", "", str(value or ""))
                    if 1 <= len(digits) <= 6:
                        return digits
    return ""


def job_title_from(lead: dict[str, Any]) -> str:
    for source in (lead.get("enrichment"), lead.get("source_row")):
        if isinstance(source, dict):
            for key, value in source.items():
                if re.fullmatch(r"(?:job_?)?title|position|role|job|designation", _field(key)) and value:
                    return str(value)[:120]
    return ""


def lead_facts(context: dict[str, Any] | None) -> LeadFacts:
    context = context or {}
    name = str(context.get("prospect_name") or "").strip()
    parts = name.split()
    return LeadFacts(
        extension=extension_from(context),
        first_name=parts[0] if parts else "",
        last_name=parts[-1] if len(parts) > 1 else "",
        job_title=job_title_from(context),
    )


def choose_keys(
    menu_text: str, lead: LeadFacts, *, tried: Sequence[str] = (),
) -> KeyChoice | None:
    """The keys to press for this menu, in the agreed order:

    1. a known extension on the lead, when the menu takes one;
    2. dial by name: at a "spell the last name" prompt spell it; at a menu offering a
       directory, open the directory first; a read-back with the right name confirms;
    3. the department matching the lead's job title (sales / accounts / operations);
    4. the operator / reception (the option naming it, else 0).

    ``tried`` holds reasons already used at this level, so a "not found" moves on.
    """
    t = normalise(menu_text)
    options = parse_menu(menu_text)

    confirm = _CONFIRM.search(t)
    if confirm and lead.last_name and normalise(lead.last_name) in t and "confirm" not in tried:
        return KeyChoice(_key(confirm.group(1)), "confirm", lead.last_name)

    if lead.extension and "extension" not in tried and (
        _EXTENSION_PROMPT.search(t) or re.search(r"\bextension\b", t)
    ):
        return KeyChoice(lead.extension + "#" if re.search(r"followed by (?:the )?(?:pound|hash)", t) else lead.extension,
                         "extension")

    if lead.last_name and "dial_by_name" not in tried and _DIRECTORY_PROMPT.search(t):
        digits = spell_on_keypad(lead.last_name)
        if digits:
            return KeyChoice(digits, "dial_by_name", lead.last_name)

    if lead.last_name and "directory" not in tried and "dial_by_name" not in tried:
        for option in options:
            if _DIRECTORY_OPTION.search(option.label):
                return KeyChoice(option.key, "directory", option.label)

    department = lead.department
    if department and "department" not in tried:
        menu_rx = DEPARTMENTS[department][1]
        for option in options:
            if menu_rx.search(option.label):
                return KeyChoice(option.key, "department", option.label)

    if "operator" not in tried:
        for option in options:
            if _OPERATOR_WORDS.search(option.label):
                return KeyChoice(option.key, "operator", option.label)
        if options or re.search(r"\boperator\b|press (?:0|zero)", t):
            return KeyChoice("0", "operator", "operator")
    return None


@dataclass(frozen=True)
class LearnedStep:
    heard: str
    pressed: str


def learned_steps(path: dict[str, Any] | None) -> list[LearnedStep]:
    """The stored path that last reached a person; [] when there is none to replay."""
    if not isinstance(path, dict) or path.get("outcome") != "person" or int(path.get("successes") or 0) < 1:
        return []
    steps = []
    for step in path.get("steps") or []:
        if isinstance(step, dict) and re.fullmatch(r"[0-9*#w]{1,24}", str(step.get("pressed") or "")):
            steps.append(LearnedStep(str(step.get("heard") or "")[:400], str(step["pressed"])))
    return steps


def replay_choice(level: int, menu_text: str, steps: Sequence[LearnedStep]) -> KeyChoice | None:
    """Press the learned keys for this level when the menu is the one we learned;
    None means the menu changed here, so the agent listens and chooses again."""
    if level >= len(steps):
        return None
    step = steps[level]
    if not step.heard or same_menu(step.heard, menu_text, threshold=0.6):
        return KeyChoice(step.pressed, "learned")
    return None


@dataclass
class IvrProgress:
    max_levels: int = IVR_DEFAULT_MAX_LEVELS
    max_seconds: float = IVR_DEFAULT_MAX_SECONDS
    started_at: float = 0.0
    levels: int = 0
    not_found: int = 0
    menus: list[str] = field(default_factory=list)

    def give_up_reason(self, now: float, menu_text: str = "") -> str | None:
        """Why the agent should stop working this menu, or None to carry on.

        After ``max_levels`` key presses, ``max_seconds`` overall, the same menu heard
        twice, or "not found" twice.
        """
        if now - self.started_at >= self.max_seconds:
            return "time"
        if self.not_found >= 2:
            return "not_found"
        if menu_text and any(same_menu(menu_text, earlier) for earlier in self.menus):
            return "same_menu"
        if self.levels >= self.max_levels:
            return "levels"
        return None


def number_key(number: str | None) -> str:
    """The last 10 digits of a company number: one learned path per number."""
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-10:] if len(digits) >= 6 else ""


__all__ = [
    "DEFAULT_AGENT_TEMPLATE", "IvrProgress", "KeyChoice", "LeadFacts", "LearnedStep", "MenuOption",
    "SCREENER_MODES", "SCREENER_NO_ANSWER_SECONDS", "ScreenerAnswer", "answer_weight",
    "answers_for_mode", "choose_answer", "choose_keys", "extension_from", "follow_up_reply",
    "is_not_found", "job_title_from", "lead_facts", "learned_steps", "menu_signature",
    "number_key", "parse_menu", "render_template", "replay_choice", "same_menu",
    "screener_reply_kind", "spell_on_keypad",
]
