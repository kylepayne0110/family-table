# "Cook with me": a spoken, hands-busy conversation about one planned dinner.
#
# The app keeps the cook's place (one step at a time) and answers cues itself: next,
# repeat, go back, "I'm on step 4", "read the whole recipe". Questions about amounts,
# conversions, serving changes, and permanent edits go through the same assistant code as
# the typed Cooking Assistant, so the app's own math stays authoritative. Anything else
# goes to the local model with the current step as context.
#
# Spoken confirmation of a permanent recipe edit is deliberately narrow: only an exact,
# short "yes, save it" style answer, only for the proposal the phone actually finished
# reading aloud (sent back as proposal_id), only while it's still pending, and the saved
# recipe must not have changed meanwhile. Anything else leaves the recipe alone.

# re recognizes spoken cues.
import re

# sqlite3 types the connection; dataclass describes a spoken reply.
import sqlite3
from dataclasses import dataclass, field

# Project modules.
from app.assistant import Reply, _clean
from app.database import write_transaction

MAX_PROPOSAL_AGE_MINUTES = 10


@dataclass
class CookReply:
    say: str                        # what the phone speaks (short, words instead of symbols)
    show: str                       # what the panel shows
    step: int                       # current step after this turn (0 = not started)
    kind: str = "text"              # step, answer, proposal, confirm, clarify, offline, busy
    proposal_id: int | None = None  # a pending recipe change the phone should read out
    reply: Reply | None = None      # an assistant reply with an action (servings, proposal)
    data: dict = field(default_factory=dict)


# ---- Steps and spoken text ----

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                 "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}

_SPOKEN_UNITS = [(r"\btbsps?\b\.?", "tablespoons"), (r"\btsps?\b\.?", "teaspoons"), (r"\blbs?\b\.?", "pounds"),
                 (r"\boz\b\.?", "ounces"), (r"°\s*F\b", " degrees"), (r"°\s*C\b", " degrees Celsius"), (r"°", " degrees"),
                 (r"\bmins?\b\.?", "minutes"), (r"\bhrs?\b\.?", "hours"), (r"\bg\b(?=[\s.,]|$)", "grams")]
_SPOKEN_FRACTIONS = {"1/2": "a half", "1/3": "a third", "2/3": "two thirds", "1/4": "a quarter", "3/4": "three quarters",
                     "1/8": "an eighth", "3/8": "three eighths"}


def speakable(text: str) -> str:
    """Text for speech: '1 1/2 tbsp' -> '1 and a half tablespoons', '400°F' -> '400 degrees'."""
    out = text
    for pattern, word in _SPOKEN_UNITS:
        out = re.sub(pattern, word, out)
    for frac, words in _SPOKEN_FRACTIONS.items():
        out = re.sub(rf"(\d+)\s+{re.escape(frac)}\b", rf"\1 and {words}", out)
        out = re.sub(rf"(?<![\d/]){re.escape(frac)}\b", words, out)
    out = re.sub(r"\s*\([^)]*\)", "", out)                # parenthetical asides are for reading, not listening
    return " ".join(out.split())


def step_text(steps: list[dict], n: int) -> str:
    step = steps[n - 1]
    title = f"{step['title']}. " if step.get("title") else ""
    return f"{title}{step['body']}"


def get_step(conn: sqlite3.Connection, meal_id: int) -> int:
    row = conn.execute("SELECT step FROM cook_sessions WHERE meal_id = ?", (meal_id,)).fetchone()
    return row["step"] if row else 0


def set_step(conn: sqlite3.Connection, meal_id: int, step: int) -> None:
    with write_transaction(conn):
        conn.execute("""INSERT INTO cook_sessions (meal_id, step) VALUES (?, ?)
                        ON CONFLICT(meal_id) DO UPDATE SET step = excluded.step, updated_at = datetime('now')""",
                     (meal_id, step))


def greeting(step: int, total: int) -> str:
    """Always opens with 'How can I help?'; when resuming, says where the cook was."""
    if 0 < step <= total:
        return f"How can I help? You were on step {step} of {total}."
    return "How can I help?"


# ---- Recognizing cues (the whole utterance, after light cleanup) ----

def _plain(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"^(?:ok(?:ay)?|alright|all right|um+|uh+|so|hey|please)[, ]+", "", text)
    text = re.sub(r"[, ]+(?:please|thanks|thank you)$", "", text)
    return text.strip(" ,.!?")


_NEXT = re.compile(r"^(?:(?:ok(?:ay)? )?next(?: step| one)?|what(?:'s| is) next|(?:i'm |i am )?done(?: with that)?|"
                   r"go on|continue|keep going|and then(?: what)?|then what|got it|finished|ready|move on)$")
_REPEAT = re.compile(r"^(?:repeat(?: that| it| the step| step)?|say (?:that|it) again|again|what was that|come again|"
                     r"pardon|sorry what|what did you say|one more time|can you repeat (?:that|it))$")
_BACK = re.compile(r"^(?:go back(?: a step| one)?|back(?: one| a step)?|previous(?: step)?|the step before|"
                   r"(?:go to )?the previous step|last step please)$")
_START = re.compile(r"^(?:start(?: cooking| from the (?:beginning|top)| at the beginning| at step one| with step one)?|"
                    r"(?:let's |lets )?(?:begin|start|get started)|from the (?:beginning|top)|step one|"
                    r"walk me through(?: it| the recipe)?|guide me(?: through it)?|take me through(?: it)?|"
                    r"start over|restart)$")
_GOTO = re.compile(r"\b(?:i'm|i am|we're|we are|go to|skip to|jump to|on|at|start (?:at|from)|take me to)\s+"
                   r"(?:the\s+)?step\s+(?P<n>\d{1,2}|[a-z]+)\b|^step\s+(?P<m>\d{1,2}|[a-z]+)$|"
                   r"\b(?P<o>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\s+step\b")
_HALFWAY = re.compile(r"\b(?:halfway|half way|part way|partway|already started|in the middle|i've already|i already)\b")
_WHERE = re.compile(r"^(?:what step (?:am i|are we) on|where (?:am i|are we|was i|were we)|which step(?: am i on)?)$")
_WHOLE = re.compile(r"\b(?:read|tell|give) (?:me )?(?:the )?(?:whole|entire|full|complete) recipe\b|"
                    r"\bread (?:me )?(?:all|every|the whole|everything)(?: of)?(?: the)? (?:steps|recipe|thing)?\b|"
                    r"\bread (?:me )?everything\b|\bthe whole thing\b")
_INGREDIENTS = re.compile(r"^(?:read|list|tell me|what are)(?: me)?(?: the)? ingredients(?: again)?|"
                          r"^what (?:do i|ingredients do i) need$")
_STOP = re.compile(r"^(?:stop|wait|hold on|hang on|pause|shh+|quiet|be quiet|one (?:sec|second|moment)|just a (?:sec|second))$")
_CONFIRM = re.compile(r"^(?:yes|yeah|yep|yes please|yes,? save (?:it|that)|save (?:it|that)|yes,? confirm|confirm|"
                      r"do it|yes,? do it|go ahead|yes,? go ahead|sounds good,? save it|please save it|make the change|"
                      r"yes,? make the change)$")
_CANCEL = re.compile(r"^(?:no|nope|cancel|cancel that|don't|do not|don't save(?: it| that)?|never ?mind|"
                     r"no,? (?:thanks|thank you|leave it|don't save it|cancel)|leave it(?: alone)?|forget it)$")


def _step_number(word: str, total: int) -> int | None:
    if word.isdigit():
        n = int(word)
    else:
        n = _NUMBER_WORDS.get(word)
        if n == -1:
            return total
    return n if n and 1 <= n <= total else None


def _best_step_for(text: str, steps: list[dict]) -> int | None:
    """'I just put it in the oven' -> the step that talks about the oven (needs a clear winner)."""
    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())} - {"just", "already", "have", "done", "with", "that",
                                                                  "this", "then", "into", "from", "finished", "step"}
    if not words:
        return None
    scores = []
    for n, step in enumerate(steps, start=1):
        body = f"{step.get('title', '')} {step['body']}".lower()
        scores.append((sum(1 for w in words if w in body), n))
    scores.sort(reverse=True)
    if scores and scores[0][0] > 0 and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return scores[0][1]
    return None


# ---- One spoken turn ----

def guide(steps: list[dict], text: str, step: int, history: list[dict]) -> CookReply | None:
    """Cues about where we are in the recipe. None means "not a step cue"."""
    total = len(steps)
    plain = _plain(text)
    if not plain:
        return None
    if _STOP.match(plain):
        return CookReply("Okay.", "Okay. Say “next” or ask me anything when you're ready.", step, "pause")
    if total == 0 and (_NEXT.match(plain) or _START.match(plain) or _REPEAT.match(plain)):
        msg = "This recipe doesn't have any steps written down. You can ask me about the ingredients."
        return CookReply(msg, msg, 0, "answer")

    target = None
    if _START.match(plain):
        target = 1
    elif _NEXT.match(plain):
        if step >= total:
            msg = "That was the last step. Enjoy dinner!" if step else "Let's start. " + _say_step(steps, 1)
            return CookReply(msg, msg, step if step else 1, "step")
        target = step + 1
    elif _REPEAT.match(plain):
        if step == 0:
            last = next((m for m in reversed(history) if m["role"] == "assistant"), None)
            if last:
                return CookReply(speakable(last["body"]), last["body"], step, "answer")
            target = 1
        else:
            target = step
    elif _BACK.match(plain):
        target = max(1, step - 1) if step else 1
    elif _WHERE.match(plain):
        msg = f"You're on step {step} of {total}." if step else f"We haven't started. There are {total} steps."
        return CookReply(msg, msg, step, "answer")
    elif _WHOLE.search(plain):
        return None   # handled by the caller, which has the ingredient list too
    else:
        match = _GOTO.search(plain)
        if match:
            target = _step_number(match["n"] or match["m"] or match["o"], total)
            if target is None:
                msg = f"This recipe has {total} steps. Which one are you on?"
                return CookReply(msg, msg, step, "clarify", data={"pending": "which_step"})
        elif _HALFWAY.search(plain):
            found = _best_step_for(plain, steps)
            if found:
                target = min(found + 1, total)
                msg = f"Sounds like you've done step {found}. " + (_say_step(steps, target) if found < total else
                                                                   "That was the last step. Enjoy dinner!")
                return CookReply(msg, msg, target, "step")
            msg = f"No problem. Which step are you on? There are {total}. You can say a number or what you just did."
            return CookReply(msg, msg, step, "clarify", data={"pending": "which_step"})
        elif _waiting_for_step(history):
            number = re.search(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", plain)
            if number and _step_number(number.group(1), total):
                target = _step_number(number.group(1), total)
            else:
                found = _best_step_for(plain, steps)
                if found and re.search(r"\b(just|already|finished|done|did|have|i've|put)\b", plain):
                    # "I just dipped them in egg": that step is done, so on to the next one.
                    target = min(found + 1, total)
                    msg = f"Sounds like you've done step {found}. " + (_say_step(steps, target) if found < total else
                                                                       "That was the last step. Enjoy dinner!")
                    return CookReply(msg, msg, target, "step", data={"step": target})
                target = found
    if target is None:
        return None
    return CookReply(_say_step(steps, target, total), f"Step {target} of {total}: {step_text(steps, target)}", target,
                     "step", data={"step": target})


def _say_step(steps: list[dict], n: int, total: int | None = None) -> str:
    total = total or len(steps)
    end = " That's the last step." if n == total else ""
    return speakable(f"Step {n}. {step_text(steps, n)}{end}")


def _waiting_for_step(history: list[dict]) -> bool:
    last = next((m for m in reversed(history) if m["role"] == "assistant"), None)
    return bool(last and last.get("kind") == "clarify" and "Which step" in last["body"])


def whole_recipe(lines: list[dict], steps: list[dict], servings: int | None) -> CookReply:
    """Only when the cook explicitly asks: every ingredient, then every step."""
    ings = "; ".join(f"{line['amount']} {line['name']}".strip() for line in lines) or "none listed"
    for_whom = f" for {servings} servings" if servings else ""
    parts = [f"Ingredients{for_whom}: {ings}."]
    parts += [f"Step {n}. {step_text(steps, n)}" for n in range(1, len(steps) + 1)]
    text = " ".join(parts)
    return CookReply(speakable(text), text, 0, "answer")


def ingredient_list(lines: list[dict], servings: int | None) -> CookReply:
    ings = "; ".join(f"{line['amount']} {line['name']}".strip() for line in lines) or "No ingredients are listed."
    text = f"For {servings} servings: {ings}." if servings else f"{ings}."
    return CookReply(speakable(text), text, 0, "answer")


def is_confirm(text: str) -> bool:
    return bool(_CONFIRM.match(_plain(text)))


def is_cancel(text: str) -> bool:
    return bool(_CANCEL.match(_plain(text)))


def pending_proposal(conn: sqlite3.Connection, meal_id: int) -> dict | None:
    """The single pending recipe change for this dinner, if it's the latest thing the assistant said."""
    row = conn.execute(
        """SELECT p.id, p.message_id, p.status, (julianday('now') - julianday(p.created_at)) * 1440 AS age_minutes
           FROM recipe_edit_proposals p WHERE p.meal_id = ? AND p.status = 'pending' ORDER BY p.id DESC LIMIT 1""",
        (meal_id,)).fetchone()
    if row is None:
        return None
    latest = conn.execute("SELECT MAX(id) FROM meal_messages WHERE meal_id = ? AND role = 'assistant'",
                          (meal_id,)).fetchone()[0]
    return {**dict(row), "is_latest": row["message_id"] == latest}


def proposal_speech(changes: list[dict]) -> str:
    """'Breadcrumbs becomes panko, in the ingredients and step 2.' (short, for listening)."""
    parts = []
    for c in changes[:4]:
        if c["kind"] == "amount":
            parts.append(f"{c['name']}: {c['before'] or 'no amount'} becomes {c['after']}")
        elif c["kind"] == "ingredient":
            parts.append(f"{c['before']} becomes {c['after']}")
        elif c["kind"].startswith("step"):
            parts.append(f"step {c['step']} is updated to match")
    extra = f", and {len(changes) - 4} more" if len(changes) > 4 else ""
    return speakable("; ".join(parts) + extra + ". Do you want me to save this to the recipe?")
