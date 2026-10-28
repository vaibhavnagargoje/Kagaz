"""
Understand short typed requests without the AI, so nobody has to remember
exact commands: "list", "lsit", "my documents", "kitne documents hai",
"pin bhool gaya" and "upgrade" all work.

Messages longer than a few words are left for the AI, so a request like
"send my 12th marksheet under 100 kb" is never mistaken for a command.
Erasing everything is the exception to forgiving matching: it needs the exact
words, and still asks for confirmation.
"""

import re
from dataclasses import dataclass

MAX_COMMAND_WORDS = 5
MAX_PATTERN_WORDS = 8

# Words that don't change what a short message asks for.
FILLER = {
    "please", "pls", "plz", "kagaz", "bhai", "bro", "sir", "ji", "yaar", "ok", "okay", "now",
    "show", "see", "view", "check", "get", "me", "my", "mine", "the", "a", "an",
    "mujhe", "mera", "meri", "mere", "dikhao", "dikha", "batao", "bata", "hai", "hain",
}

PHRASES = {
    "help": [
        "hi", "hii", "hiii", "hello", "helo", "hlo", "hey", "hy", "help", "start", "namaste", "namaskar",
        "madad", "how to use", "how does it work", "what can you do", "commands", "options",
    ],
    "menu": ["menu", "main menu"],
    "list": [
        "list", "documents", "document", "docs", "files", "my documents", "document list", "documents list",
        "list documents", "list of documents", "all documents", "all docs", "all files", "show all", "all",
        "saved documents", "sab documents", "saare documents", "sare documents",
    ],
    "count": [
        "count", "how many", "how many documents", "kitne", "kitne documents", "kitne document",
        "total documents", "number of documents", "documents count",
    ],
    "pin": ["pin", "set pin", "change pin", "new pin", "create pin", "pin change", "pin set"],
    "forgot_pin": [
        "forgot pin", "forgot my pin", "forgot", "forget pin", "reset pin", "reset my pin", "pin reset",
        "pin bhool gaya", "pin bhul gaya", "pin bhool gayi", "pin bhul gayi", "pin yaad nahi",
    ],
    "cancel_reset": ["cancel reset", "cancel pin reset", "stop reset", "stop pin reset"],
    "web": ["web", "website", "login", "log in", "open web", "browser", "laptop", "computer"],
    "privacy": ["privacy", "privacy policy"],
    "plus": [
        "plus", "kagaz plus", "upgrade", "pricing", "price", "prices", "plans", "buy", "renew", "subscribe",
        "premium", "cost", "kitne ka hai", "kitna paisa",
    ],
    "plan": ["plan", "my plan", "usage", "my usage", "limit", "limits", "current plan"],
    "refer": [
        "refer", "invite", "referral", "refer a friend", "invite friends", "invite friend", "my code", "code",
    ],
}
WIPE_PHRASES = {"delete my data", "delete all my data", "delete everything", "erase my data"}

CODE_COMMAND = re.compile(r"^(?:code|promo|coupon)\s*[:#-]?\s*([a-z0-9_-]{2,32})$")
REFERRAL_COMMAND = re.compile(r"^ref(?:erral)?(?:\s*code)?\s*[:#-]?\s*[a-z0-9]{6}$")

# Longer questions about the vault itself, e.g. "how many documents do you have".
DOC_WORDS = re.compile(r"\b(docs?|documents?|files?|papers?|kagaz)\b")
COUNT_WORDS = re.compile(r"\b(how many|kitne|kitni|count|total|number of)\b")
LIST_WORDS = re.compile(r"\b(list|all|sab|saare|sare|saari|sari)\b")
# Words that mean they want something done to files, not to see the list.
ACTION_WORDS = {
    "send", "bhejo", "bhej", "pdf", "jpg", "jpeg", "png", "kb", "mb", "merge", "combine", "compress",
    "resize", "convert", "delete", "remove", "hatao", "under", "one",
}


@dataclass(frozen=True)
class Command:
    action: str
    arg: str = ""


def clean(text: str) -> str:
    """Lowercase, without punctuation or emoji, single spaces."""
    return " ".join(re.sub(r"[^\w\s-]", " ", text.lower()).split())


def normalize(text: str) -> str:
    return " ".join(word for word in clean(text).split() if word not in FILLER)


def edit_distance(a: str, b: str) -> int:
    """Letters to add, drop, change or swap to turn a into b."""
    previous2, previous = None, list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            cost = 0 if char_a == char_b else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if previous2 and i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                value = min(value, previous2[j - 2] + 1)
            current.append(value)
        previous2, previous = previous, current
    return previous[-1]


def similar(a: str, b: str) -> bool:
    """The same word, or a small typo of it. Short words must match exactly."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return edit_distance(a, b) <= (2 if max(len(a), len(b)) >= 8 else 1)


_EXACT = {normalize(phrase): action for action, phrases in PHRASES.items() for phrase in phrases}
_BY_WORDS = [(tuple(phrase.split()), action) for phrase, action in _EXACT.items() if phrase]


def match(text: str) -> Command | None:
    """The command a short message asks for, or None to let the AI decide."""
    plain = clean(text)
    if not plain:
        return Command("help") if text.strip() == "?" else None
    if plain in WIPE_PHRASES:
        return Command("wipe")
    if found := CODE_COMMAND.match(plain):
        return Command("code", found.group(1))
    if REFERRAL_COMMAND.match(plain):
        return Command("friend_code", plain)

    words = plain.split()
    if len(words) <= MAX_PATTERN_WORDS and not ACTION_WORDS & set(words) and DOC_WORDS.search(plain):
        if COUNT_WORDS.search(plain):
            return Command("count")
        if LIST_WORDS.search(plain):
            return Command("list")

    short = normalize(plain)
    if not short or len(short.split()) > MAX_COMMAND_WORDS:
        return None
    if short in _EXACT:
        return Command(_EXACT[short])
    words = short.split()
    hits = {
        action for phrase, action in _BY_WORDS
        if len(phrase) == len(words) and all(similar(a, b) for a, b in zip(words, phrase))
    }
    return Command(hits.pop()) if len(hits) == 1 else None


# Words that say a message carries a discount code. Plain "code" is too common
# ("IFSC code", "pin code") to mean that on its own.
COUPON_WORDS = {"coupon", "coupons", "coupen", "cupon", "coupan", "copun", "copon", "promo", "promocode", "voucher"}
APPLY_WORDS = {"apply", "use", "redeem", "lagao", "lagana", "laga", "dalo", "daalo"}


def code_candidates(text: str) -> tuple[list[str], bool]:
    """Words that could be a promo or invite code, and whether the message is surely about a code.

    A word mixing letters and digits ("SAVE20") is tried in any message. Other
    words are tried when the message is that one word, or mentions a code
    ("apply code diwali", "coupen DIWALI").
    """
    words = re.findall(r"[A-Za-z0-9_-]+", text)
    lower = [word.lower() for word in words]
    coupon = any(word in COUPON_WORDS or similar(word, "coupon") for word in lower)
    sure = coupon or ("code" in lower and bool(APPLY_WORDS & set(lower)))
    any_word = sure or "code" in lower or len(words) == 1
    skip = COUPON_WORDS | APPLY_WORDS | FILLER | {"code", "codes", "promo-code"}
    found = []
    for word, low in zip(words, lower):
        if not 4 <= len(word) <= 32 or low in skip or similar(low, "coupon"):
            continue
        mixed = bool(re.search(r"[a-z]", low) and re.search(r"\d", low))
        if mixed or any_word:
            found.append(word)
    return found[:10], sure


def likely_code(candidates: list[str]) -> str:
    """The word most likely meant as a code (it has digits or is in capitals), else ""."""
    return next((w for w in candidates if re.search(r"\d", w) or (w.isupper() and len(w) >= 4)), "")
