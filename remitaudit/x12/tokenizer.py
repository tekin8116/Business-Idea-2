"""Delimiter-aware tokenizer for X12 interchanges.

X12 does not have fixed delimiters. They are declared positionally inside the
ISA header itself, which is the one fixed-width segment in the standard:

    ISA[3]   element separator     (position 3)
    ISA[104] component separator   (ISA16)
    ISA[105] segment terminator    (the character immediately after ISA16)

Clearinghouses vary. Availity commonly emits ``*``/``:``/``~`` with no line
breaks; Optum Pay wraps segments at the terminator; some payers use ``|``.
Reading the delimiters off the header rather than assuming them is the
difference between a parser that works on one practice's files and one that
works on everybody's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Sequence

# Fallback delimiters, used only when a file has no parseable ISA header.
DEFAULT_ELEMENT = "*"
DEFAULT_COMPONENT = ":"
DEFAULT_SEGMENT = "~"

# An ISA segment is exactly 106 characters: 105 of content plus the terminator.
_ISA_LENGTH = 106
_ISA_ELEMENT_POS = 3
_ISA_COMPONENT_POS = 104
_ISA_TERMINATOR_POS = 105


def _is_delimiter(char: str) -> bool:
    """True if a character is plausible as an X12 separator."""
    return len(char) == 1 and not char.isalnum() and not char.isspace()


class X12Error(ValueError):
    """Raised when a file cannot be read as an X12 interchange."""


@dataclass(frozen=True)
class Delimiters:
    """The three separators that define how an interchange is punctuated."""

    element: str = DEFAULT_ELEMENT
    component: str = DEFAULT_COMPONENT
    segment: str = DEFAULT_SEGMENT

    @classmethod
    def sniff(cls, raw: str) -> "Delimiters":
        """Read delimiters out of the ISA header.

        Falls back to the common defaults when the header is missing or
        truncated, so that a fragment of a file is still parseable for
        debugging rather than raising.
        """
        start = raw.find("ISA")
        if start == -1 or len(raw) < start + _ISA_LENGTH:
            return cls()

        header = raw[start : start + _ISA_LENGTH]
        element = header[_ISA_ELEMENT_POS]
        component = header[_ISA_COMPONENT_POS]
        terminator = header[_ISA_TERMINATOR_POS]

        # A terminator of newline is legal but makes downstream splitting
        # ambiguous when files are also line-wrapped; normalise to the default.
        if terminator in "\r\n":
            terminator = DEFAULT_SEGMENT

        # A malformed or truncated ISA puts these offsets inside the *next*
        # segment, yielding letters and digits as "delimiters" and a parse
        # that silently produces nothing. Separators are always punctuation,
        # so anything alphanumeric means the header lied: fall back per-field
        # rather than trusting it.
        if not _is_delimiter(element):
            element = DEFAULT_ELEMENT
        if not _is_delimiter(component):
            component = DEFAULT_COMPONENT
        if not _is_delimiter(terminator):
            terminator = DEFAULT_SEGMENT

        # The three separators must be distinct; a collision means the header
        # was misread even if each character looked plausible on its own.
        if len({element, component, terminator}) != 3:
            return cls()
        return cls(element=element, component=component, segment=terminator)


@dataclass
class Segment:
    """One X12 segment: an identifier plus its ordered elements.

    Element access is 1-indexed to match how the standard and every payer
    companion guide numbers them, so ``seg[3]`` is CLP03, not the fourth
    element. Out-of-range access returns ``""`` because trailing empty
    elements are routinely omitted rather than sent empty.
    """

    tag: str
    elements: List[str] = field(default_factory=list)
    component: str = DEFAULT_COMPONENT

    def __getitem__(self, index: int) -> str:
        if index < 1:
            raise IndexError("X12 elements are 1-indexed")
        pos = index - 1
        if pos >= len(self.elements):
            return ""
        return self.elements[pos]

    def get(self, index: int, default: str = "") -> str:
        value = self[index]
        return value if value else default

    def components(self, index: int) -> List[str]:
        """Split a composite element into its parts.

        SVC01 is the common case: ``HC:92928:59`` is a procedure qualifier,
        a CPT code, and a modifier.
        """
        value = self[index]
        if not value:
            return []
        return value.split(self.component)

    def component_at(self, index: int, position: int, default: str = "") -> str:
        """Return one part of a composite element, 1-indexed."""
        parts = self.components(index)
        if position < 1 or position > len(parts):
            return default
        return parts[position - 1] or default

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {'|'.join(self.elements)}>"


def tokenize(raw: str, delimiters: Delimiters | None = None) -> List[Segment]:
    """Split an interchange into segments.

    Whitespace between segments is stripped, which handles both the wrapped
    and unwrapped conventions without needing to know which one produced the
    file.
    """
    if not raw or not raw.strip():
        raise X12Error("empty file")

    delims = delimiters or Delimiters.sniff(raw)
    segments: List[Segment] = []

    for chunk in raw.split(delims.segment):
        text = chunk.strip("\r\n \t")
        if not text:
            continue
        parts = text.split(delims.element)
        tag = parts[0].strip().upper()
        if not tag:
            continue
        segments.append(
            Segment(tag=tag, elements=parts[1:], component=delims.component)
        )

    if not segments:
        raise X12Error("no segments found; check delimiters")
    return segments


def iter_transactions(segments: Sequence[Segment]) -> Iterator[List[Segment]]:
    """Yield each ST/SE transaction set as its own list of segments.

    A single 835 file routinely contains several transaction sets — one per
    payer deposit — and they must not be blended, because payment dates and
    payer identity differ between them.
    """
    current: List[Segment] | None = None
    for seg in segments:
        if seg.tag == "ST":
            current = [seg]
        elif seg.tag == "SE":
            if current is not None:
                current.append(seg)
                yield current
                current = None
        elif current is not None:
            current.append(seg)
    if current:  # unterminated set; yield what we have rather than dropping it
        yield current
