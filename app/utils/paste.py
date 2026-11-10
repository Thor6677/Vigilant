"""Size limits for pasted text (ISS-166).

The paste parsers run on the event loop of the app's single worker, so the
size of a paste is also how long every other request waits for it. Each
caller picks limits that fit its own clipboard format; this checks them and
words the refusal so the user knows what to change. The messages hold only
numbers, so they can go into HTML as they are.
"""


def paste_limit_error(text: str, *, max_lines: int, max_line_chars: int,
                      sep: str | None = None, max_chars: int | None = None) -> str | None:
    """None when `text` is within the limits, else a message naming the one it breaks.

    Lines are split the way the caller's parser splits them: `splitlines()`
    by default, or on `sep` for a parser that splits on one character.
    `max_chars` caps the whole paste below what the line limits allow.
    """
    if max_chars is not None and len(text) > max_chars:
        return (f"That paste is {len(text):,} characters long — "
                f"the limit is {max_chars:,}.")
    # Any text longer than this breaks one of the limits, so it is refused
    # before being split into lines.
    if len(text) > max_lines * (max_line_chars + 2):
        return (f"That paste is too long — the limit is {max_lines:,} lines "
                f"of up to {max_line_chars:,} characters.")
    lines = text.split(sep) if sep else text.splitlines()
    if len(lines) > max_lines:
        return (f"That paste has {len(lines):,} lines — the limit is {max_lines:,}. "
                "Split it and paste each part.")
    for number, line in enumerate(lines, 1):
        if len(line) > max_line_chars:
            return (f"Line {number:,} is {len(line):,} characters long — "
                    f"the limit is {max_line_chars:,}.")
    return None
