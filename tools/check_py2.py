"""Parse the plugin against the Python 2.7 grammar.

The plugin only ever runs inside EventGhost's bundled CPython 2.7, so it uses
Python 2 syntax and cannot be checked with py_compile on a modern
interpreter. parso can parse an explicit grammar version, which is enough to
stop a typo from shipping.

Run under Python 3: python tools/check_py2.py [path ...]
"""

import sys

import parso

DEFAULT_TARGETS = ["plugin/__init__.py"]


def check(path):
    with open(path, "rb") as handle:
        source = handle.read().decode("utf-8")
    grammar = parso.load_grammar(version="2.7")
    errors = list(grammar.iter_errors(grammar.parse(source)))
    for error in errors:
        line, column = error.start_pos
        print("%s:%d:%d: %s" % (path, line, column, error.message))
    return len(errors)


def main(argv):
    targets = argv[1:] or DEFAULT_TARGETS
    total = sum(check(path) for path in targets)
    if total:
        print("%d syntax error(s)" % total)
        return 1
    print("%s: parses as Python 2.7" % ", ".join(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
