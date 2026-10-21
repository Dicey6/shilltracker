"""Solana address + Robinhood detection. Pure functions, no I/O."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_CLASS = "1-9A-HJ-NP-Za-km-z"
CANDIDATE_RE = re.compile(rf"(?<![{_B58_CLASS}])[{_B58_CLASS}]{{32,44}}(?![{_B58_CLASS}])")

try:  # preferred: the real Solana key parser
    from solders.pubkey import Pubkey as _Pubkey  # type: ignore
except Exception:  # pragma: no cover - fallback keeps the scanner usable
    _Pubkey = None

# Well-known non-token program / sysvar / stablecoin addresses. Deliberately small:
# these are things that are overwhelmingly noise, not alpha. Toggle via /filters.
KNOWN_NON_TOKEN = frozenset({
    "11111111111111111111111111111111",              # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    "Stake11111111111111111111111111111111111111",
    "Vote111111111111111111111111111111111111111",
    "BPFLoaderUpgradeab1e11111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",   # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
})

MAX_REGEX_LEN = 200


def _b58_decoded_length(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    pad = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return pad + len(body)


def is_valid_solana_address(candidate: str) -> bool:
    if not (32 <= len(candidate) <= 44):
        return False
    if _Pubkey is not None:
        try:
            _Pubkey.from_string(candidate)
            return True
        except Exception:
            return False
    try:
        return _b58_decoded_length(candidate) == 32
    except ValueError:
        return False


@dataclass(frozen=True)
class DetectionConfig:
    solana_enabled: bool = True
    robinhood_enabled: bool = True
    filter_known_programs: bool = True
    keywords: tuple[str, ...] = ()
    regexes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Detection:
    solana: tuple[str, ...] = ()
    robinhood: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return bool(self.solana or self.robinhood)

    @property
    def signature(self) -> str:
        """Stable fingerprint of *what* matched, used for dedup (edits that add a
        new address produce a new signature; re-delivery of the same event doesn't)."""
        raw = "S:" + ",".join(sorted(self.solana)) + "|R:" + ",".join(sorted(x.lower() for x in self.robinhood))
        return hashlib.sha1(raw.encode()).hexdigest()

    @property
    def label(self) -> str:
        parts = []
        if self.solana:
            parts.append("Solana CA")
        if self.robinhood:
            parts.append("Robinhood-related")
        return " + ".join(parts)


def extract_solana_addresses(text: str, filter_known: bool = True) -> list[str]:
    seen: dict[str, None] = {}
    for m in CANDIDATE_RE.finditer(text):
        cand = m.group(0)
        if cand in seen or not is_valid_solana_address(cand):
            continue
        if filter_known and cand in KNOWN_NON_TOKEN:
            continue
        seen[cand] = None
    return list(seen)


@lru_cache(maxsize=256)
def _keyword_re(keyword: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=256)
def _user_re(pattern: str) -> Optional[re.Pattern]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def validate_regex(pattern: str) -> Optional[str]:
    """Return an error string, or None if the pattern is acceptable."""
    if not pattern or len(pattern) > MAX_REGEX_LEN:
        return f"Pattern must be 1-{MAX_REGEX_LEN} characters."
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Invalid regex: {exc}"
    if compiled.match(""):
        return "Pattern matches empty text and would fire on every message."
    return None


def extract_robinhood(text: str, cfg: DetectionConfig) -> list[str]:
    hits: dict[str, None] = {}
    for kw in cfg.keywords:
        if kw and _keyword_re(kw).search(text):
            hits[kw] = None
    for pat in cfg.regexes:
        rx = _user_re(pat)
        if rx is None:
            continue
        for m in rx.finditer(text):
            if m.group(0):
                hits[m.group(0)] = None
            if len(hits) >= 25:
                break
    return list(hits)


def detect(text: str, cfg: DetectionConfig) -> Detection:
    sol = tuple(extract_solana_addresses(text, cfg.filter_known_programs)) if cfg.solana_enabled else ()
    rh = tuple(extract_robinhood(text, cfg)) if cfg.robinhood_enabled else ()
    return Detection(solana=sol, robinhood=rh)
