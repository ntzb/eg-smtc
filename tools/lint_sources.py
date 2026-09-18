"""Catch a string literal broken across lines in the C++ sources.

This exists because a newline landed inside a printf format string three
separate times while editing these files with search-and-replace scripts.
MSVC does catch it (error C2001), but only after a push and a CI round trip,
and the same slip in a rarely compiled branch would be worse.

Deliberately simple: it counts unescaped double quotes per line, ignoring
comments and character literals, and reports any line that leaves a string
open. That is enough for this codebase, which has no raw or multi-line
string literals.

Run under Python 3: python tools/lint_sources.py [path ...]
"""

import glob
import sys

DEFAULT_GLOBS = ["src/*.cpp", "src/*.h"]


def open_quote_count(line):
    """Number of unescaped double quotes outside a // comment."""
    count = 0
    index = 0
    escaped = False
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'" and line[index:index + 3] in ("'\"'", "'\\''"):
            # A character literal such as L'"' must not count as a quote.
            index += 2
        elif char == "/" and line[index:index + 2] == "//" and count % 2 == 0:
            break
        elif char == '"':
            count += 1
        index += 1
    return count


def check(path):
    with open(path, "rb") as handle:
        lines = handle.read().decode("utf-8").split("\n")
    problems = []
    for number, line in enumerate(lines, 1):
        if open_quote_count(line) % 2:
            problems.append((number, line.strip()))
    for number, text in problems:
        print("%s:%d: string literal left open: %s" % (path, number, text[:80]))
    return len(problems)


def main(argv):
    targets = argv[1:]
    if not targets:
        for pattern in DEFAULT_GLOBS:
            targets.extend(sorted(glob.glob(pattern)))
    if not targets:
        print("no files matched %s" % ", ".join(DEFAULT_GLOBS))
        return 1

    total = sum(check(path) for path in targets)
    if total:
        print("%d unterminated string literal(s)" % total)
        return 1
    print("%s: no unterminated string literals" % ", ".join(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
