"""Which mocks a release of mock-acme was tested against, kept in the README.

    python3 tools/tested_against.py            # what is installed here, as a row
    python3 tools/tested_against.py --write    # put that row in the README
    python3 tools/tested_against.py --check    # the README's row is what is installed

The table under "Tested against" in `README.md` has one row for each release.
A row is not typed in: `--write` takes the versions from the mocks installed
where the tests have just been run, and `--check` refuses a row that says
anything else. `publish.yml` runs `--check` after its own run of the tests, so
a release cannot go out with a row its own tests do not bear out (#43).

It says "tested against" and no more. Nothing runs the tests against the
oldest mock the `test` extra in `pyproject.toml` allows, so the table makes no
claim about the lowest version that works.

Standard library only, and Python 3.8, like the package.
"""
import os
import re
import sys

try:
    from importlib import metadata
except ImportError:                                 # pragma: no cover
    metadata = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")

MOCKS = ("mock-sap", "mock-edi", "mock-bank", "mock-einvoice")
HEADER = "| mock-acme | %s |" % " | ".join(MOCKS)
RULE = "|" + " --- |" * (len(MOCKS) + 1)
# What a release that did not use a mock has in that mock's column.
NONE = "not used"


def number(version):
    """`0.21.0` as (0, 21, 0), so that 0.21.0 is later than 0.9.0."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def own_version(pyproject=PYPROJECT):
    with open(pyproject, encoding="utf-8") as handle:
        return re.search(r'(?m)^version\s*=\s*"([^"]+)"', handle.read()).group(1)


def floors(pyproject=PYPROJECT):
    """The lowest version of each mock the `test` extra allows; "" for none."""
    with open(pyproject, encoding="utf-8") as handle:
        extra = re.search(r"(?m)^test\s*=\s*\[(.*?)\]", handle.read(), re.S).group(1)
    found = {}
    for requirement in re.findall(r'"([^"]+)"', extra):
        name, _, floor = requirement.partition(">=")
        found[name.strip()] = floor.strip()
    return found


def installed():
    """The version of each mock installed here; "" for one that is not."""
    found = {}
    for mock in MOCKS:
        try:
            found[mock] = metadata.version(mock)
        except metadata.PackageNotFoundError:
            found[mock] = ""
    return found


def row(version, versions):
    return "| %s | %s |" % (version, " | ".join(versions[m] or NONE for m in MOCKS))


def table(text):
    """The table's rows in the README: [(mock-acme version, {mock: version})]."""
    lines = text.splitlines()
    if HEADER not in lines:
        raise ValueError("README.md has no table headed %r" % HEADER)
    rows = []
    for line in lines[lines.index(HEADER) + 2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(MOCKS) + 1:
            raise ValueError("a row of the table does not have %d cells: %r"
                             % (len(MOCKS) + 1, line))
        rows.append((cells[0], {m: "" if c == NONE else c
                                for m, c in zip(MOCKS, cells[1:])}))
    return rows


def written(text, version, versions):
    """The README with this release's row in the table: replaced, or put first."""
    lines = text.splitlines(True)
    plain = [line.rstrip("\r\n") for line in lines]
    if HEADER not in plain:
        raise ValueError("README.md has no table headed %r" % HEADER)
    start = plain.index(HEADER) + 2
    new = row(version, versions) + "\n"
    for at in range(start, len(lines)):
        if not lines[at].startswith("|"):
            break
        if lines[at].strip("|").split("|")[0].strip() == version:
            lines[at] = new
            return "".join(lines)
    lines.insert(start, new)
    return "".join(lines)


def disagreements(text, version, versions):
    """Why the README's row for this release is not what is installed; [] if it is."""
    rows = dict(table(text))
    if version not in rows:
        return ["README.md has no row for mock-acme %s under \"Tested against\"; "
                "run the tests, then python3 tools/tested_against.py --write" % version]
    return ["README.md says mock-acme %s was tested against %s %s, and what is "
            "installed here is %s" % (version, mock, rows[version][mock] or NONE,
                                      versions[mock] or "not installed")
            for mock in MOCKS if rows[version][mock] != versions[mock]]


def main(arguments):
    version, versions = own_version(), installed()
    with open(README, encoding="utf-8") as handle:
        text = handle.read()
    if arguments == ["--write"]:
        with open(README, "w", encoding="utf-8", newline="") as handle:
            handle.write(written(text, version, versions))
    elif arguments == ["--check"]:
        wrong = disagreements(text, version, versions)
        for line in wrong:
            print(line, file=sys.stderr)
        return 1 if wrong else 0
    elif arguments:
        print(__doc__, file=sys.stderr)
        return 2
    print(row(version, versions))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
