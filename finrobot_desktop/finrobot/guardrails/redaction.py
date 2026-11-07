"""Scrub personal identifiers from user-authored text before an LLM sees it.

Applied to what the USER wrote (chat messages, selected text) — never to tool
output, which is public market data where a "phone number" is a company's
investor-relations line and a name is a CEO's.

Precision over recall: a finance chat is full of numbers, and mangling
"revenue grew 12.5% to 394,328" would be worse than useless. So every pattern
is anchored and, where a checksum exists, validated — card numbers must pass
Luhn, IBANs must pass ISO 13616 mod-97, phone numbers need a phone shape and
10-15 digits, bare account numbers only count when labelled ("account no.").

The user's own SEC identity (name + email) is ALWAYS redacted, whether or not
general PII redaction is on: identity is a locked "never share" category.

Pure stdlib — imported on the sidecar cold-start path.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

# 13-19 digits, optionally grouped by single spaces/dashes; not glued to other
# digits or to a decimal/thousands separator followed by a digit.
_CARD = re.compile(r"(?<!\d)(?<!\d[.,])\d(?:[ -]?\d){12,18}(?!\d|[.,]\d)")

_SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")

# "account number: 12345678", "acct # 1234-5678-90", "routing no 021000021".
_ACCOUNT = re.compile(
    r"(?i)\b(?P<label>(?:bank |brokerage |checking |savings )?(?:account|acct|a/c)"
    r"|routing|sort code)"
    r"(?P<sep>(?:\s*(?:number|num|no\.?|#))?\s*[:#]?\s*)"
    r"(?P<num>\d(?:[\d -]{4,}\d))"
)

_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?!\d)")
_PHONE_NANP = re.compile(r"(?<![\w(])(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d|[.,]\d)")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def _identity_terms(sec_user_agent: str | None) -> list[str]:
    """Split an SEC identity ("Jane Doe jane@acme.com") into terms to scrub.

    The placeholder identity shipped as the default (``@example.com``) is not
    anyone's identity, so it yields nothing.
    """
    if not sec_user_agent:
        return []
    raw = sec_user_agent.strip()
    emails = _EMAIL.findall(raw)
    if any(e.lower().endswith("@example.com") for e in emails):
        return []
    name = _EMAIL.sub(" ", raw)
    name = " ".join(name.split())
    terms = [*emails]
    if len(name) >= 3:
        terms.append(name)
    return terms


@dataclass
class RedactionResult:
    text: str
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def changed(self) -> bool:
        return bool(self.counts)


class PiiRedactor:
    """Replace personal identifiers with ``[redacted <kind>]`` placeholders."""

    def __init__(self, *, enabled: bool, sec_user_agent: str | None = None) -> None:
        self.enabled = enabled
        terms = sorted(_identity_terms(sec_user_agent), key=len, reverse=True)
        self._identity = (
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)",
                re.IGNORECASE,
            )
            if terms
            else None
        )

    def redact(self, text: str) -> RedactionResult:
        result = RedactionResult(text=text)
        if not text:
            return result
        if self._identity is not None:
            self._sub(result, self._identity, "identity")
        if not self.enabled:
            return result
        self._sub(result, _EMAIL, "email")
        self._sub(result, _IBAN, "IBAN", check=_iban_ok)
        self._sub(result, _CARD, "card number", check=lambda m: _luhn_ok(_digits(m)))
        self._sub(result, _SSN, "SSN")
        self._sub_account(result)
        self._sub(result, _PHONE_INTL, "phone", check=lambda m: 10 <= len(_digits(m)) <= 15)
        self._sub(result, _PHONE_NANP, "phone")
        return result

    def redact_all(self, texts: Iterable[str]) -> list[RedactionResult]:
        return [self.redact(t) for t in texts]

    @staticmethod
    def _sub(
        result: RedactionResult,
        pattern: re.Pattern[str],
        kind: str,
        *,
        check: Callable[[str], bool] | None = None,
    ) -> None:
        def repl(match: re.Match[str]) -> str:
            if check is not None and not check(match.group(0)):
                return match.group(0)
            result.counts[kind] += 1
            return f"[redacted {kind}]"

        result.text = pattern.sub(repl, result.text)

    @staticmethod
    def _sub_account(result: RedactionResult) -> None:
        def repl(match: re.Match[str]) -> str:
            if len(_digits(match.group("num"))) < 6:
                return match.group(0)
            result.counts["account number"] += 1
            return f"{match.group('label')}{match.group('sep')}[redacted account number]"

        result.text = _ACCOUNT.sub(repl, result.text)
