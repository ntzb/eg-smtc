"""Parse the plugin against the Python 2.7 grammar.

The plugin only ever runs inside EventGhost's bundled CPython 2.7, so it uses
Python 2 syntax and cannot be checked with py_compile on a modern
interpreter. parso can parse an explicit grammar version, which is enough to
stop a typo from shipping.

parso must be pinned to 0.7.1: 0.8 dropped the 2.7 grammar and later versions
raise NotImplementedError for it. The self-check below fails loudly if the
loaded grammar is not really 2.7, so a bad pin cannot silently turn this into
a no-op.

Run under Python 3: python tools/check_py2.py [path ...]
"""

import glob
import sys

import parso

DEFAULT_GLOB = "plugin/*.py"


def load():
    return parso.load_grammar(version="2.7")


def errors_in(grammar, source):
    return list(grammar.iter_errors(grammar.parse(source)))


def self_check(grammar):
    """Confirm the grammar really is 2.7, in both directions.

    A grammar that quietly fell back to the host version would accept the
    f-string and reject the print statement, and this script would then be
    checking the wrong language while still passing.
    """
    problems = []
    if errors_in(grammar, u'print "python 2 statement"\n'):
        problems.append("the loaded grammar rejects a Python 2 print statement")
    if not errors_in(grammar, u'x = f"{1}"\n'):
        problems.append("the loaded grammar accepts an f-string")
    return problems


def check(grammar, path):
    with open(path, "rb") as handle:
        source = handle.read().decode("utf-8")
    found = errors_in(grammar, source)
    for error in found:
        line, column = error.start_pos
        print("%s:%d:%d: %s" % (path, line, column, error.message))
    return len(found)


def main(argv):
    grammar = load()

    problems = self_check(grammar)
    if problems:
        for problem in problems:
            print("grammar self-check failed: %s" % problem)
        return 1

    targets = argv[1:] or sorted(glob.glob(DEFAULT_GLOB))
    if not targets:
        print("no files matched %s" % DEFAULT_GLOB)
        return 1

    total = sum(check(grammar, path) for path in targets)
    if total:
        print("%d syntax error(s)" % total)
        return 1
    print("%s: parses as Python 2.7" % ", ".join(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
