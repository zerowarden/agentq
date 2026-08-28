from __future__ import annotations

import re

# Single source of truth for secret redaction. All command output, file reads,
# diagnostics, tails, and logs must flow through this module so redaction
# semantics live in exactly one place.

PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
PRIVATE_KEY_END_RE = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.S,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|service[_-]?role[_-]?key|private[_-]?key)\b(\s*[:=]\s*)([^\s,;\]}]{6,})"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED_JWT]",
    ),
    (
        re.compile(r"(?i)(https?://[^:/\s]+:)([^@/\s]+)(@)"),
        r"\1[REDACTED]\3",
    ),
]


def redact_text(text: str) -> str:
    """Redact all known secret shapes in one pass (one-shot input)."""
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class StreamingRedactor:
    """Stateful redactor for streamed command output.

    Handles private-key blocks that span chunks, appear on a single line, or
    contain multiple blocks, while preserving unrelated text before/after each
    block. Inline secret patterns are handled by :func:`redact_text` on each
    emitted segment.

    In ``strict`` mode (used for ordinary source reads) a line is treated as a
    key-block boundary only when the entire (stripped) line is exactly a begin
    or end marker, so string literals or comments containing a marker are left
    intact. Non-strict mode (used for command output and sensitive files)
    redacts any line that contains a marker.
    """

    def __init__(self, strict: bool = False) -> None:
        self.strict: bool = strict
        self.in_block: bool = False
        self.buffer: str = ""
        self.private_key_blocks: int = 0
        self.redacted_lines: int = 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.buffer += chunk
        segments = self.buffer.split("\n")
        self.buffer = segments.pop()  # trailing incomplete line stays buffered
        return "".join(self._process_line(line, "\n") for line in segments)

    def finish(self) -> str:
        tail = self.buffer
        self.buffer = ""
        if not tail:
            return ""
        if self.in_block:
            self.in_block = False
            self.private_key_blocks += 1
            self.redacted_lines += 1
            return "[REDACTED_PRIVATE_KEY_BLOCK]"
        return self._redact_simple(tail)

    def stats(self) -> dict[str, int]:
        return {
            "private_key_blocks": self.private_key_blocks,
            "redacted_lines": self.redacted_lines,
            "unterminated_private_key_blocks": int(self.in_block),
        }

    def _matches_begin(self, line: str) -> bool:
        if self.strict:
            return bool(PRIVATE_KEY_BEGIN_RE.fullmatch(line.strip()))
        return bool(PRIVATE_KEY_BEGIN_RE.search(line))

    def _matches_end(self, line: str) -> bool:
        if self.strict:
            return bool(PRIVATE_KEY_END_RE.fullmatch(line.strip()))
        return bool(PRIVATE_KEY_END_RE.search(line))

    def _redact_simple(self, text: str) -> str:
        return redact_text(text)

    def _process_line(self, line: str, eol: str) -> str:
        if self.in_block:
            self.redacted_lines += 1
            end = PRIVATE_KEY_END_RE.search(line)
            if end:
                self.in_block = False
                tail = line[end.end():]
                return self._redact_simple(tail) if tail else ""
            return ""
        if not self._matches_begin(line):
            return self._redact_simple(line) + eol
        begin = PRIVATE_KEY_BEGIN_RE.search(line)
        self.private_key_blocks += 1
        self.redacted_lines += 1
        head = line[: begin.start()]
        after = line[begin.end():]
        end = PRIVATE_KEY_END_RE.search(after)
        if end:
            # BEGIN and END on the same line: redact only the block.
            tail = after[end.end():]
            return (
                self._redact_simple(head)
                + "[REDACTED_PRIVATE_KEY_BLOCK]"
                + self._redact_simple(tail)
            )
        # Block opens on this line and continues past it.
        self.in_block = True
        prefix = self._redact_simple(head)
        return (prefix + "[REDACTED_PRIVATE_KEY_BLOCK]\n") if prefix else "[REDACTED_PRIVATE_KEY_BLOCK]\n"
