"""How a .env line is read, in one place, for the bot and for its scripts.

On the server there used to be two .env files: the project folder's, which is
the one people edit, and /opt/bts-vigia/.env, which is the one the service read.
install.sh kept the second one forever. When buying was added, the buy keys went
into the first and never reached the second, and every purchase answered "falta
preencher" about values that were right there.

Now the project folder's file is the only source. install.sh copies it into /opt
on every run and stop.sh deletes the copy. This module makes that safe: before a
copy is deleted, whatever only it holds is moved into the project's file. And
config.py reads the .env through the same function the scripts use, so the bot
and the scripts cannot disagree about what a line means.

Standard library only, on purpose. install.sh runs it with the system python3,
before any venv exists.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=(.*)$")


def read(path: Path | str) -> dict[str, str]:
    """KEY -> the value text as written, for every assignment in the file.

    Forgiving on purpose. Each of these was a filled-in value that the bot read
    as blank: a leading `export ` (a file written to be sourced by a shell), a
    byte-order mark before the first key (Windows editors), and the same key
    twice with the blank one on top, which is what pasting a block under a file
    that already had the empty line gives you. So `export` is dropped, the BOM
    is eaten, and the last assignment wins, as it would in a shell. Comment lines
    never match: `#` cannot start a key.
    """
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        match = LINE.match(line)
        if match:
            values[match[1]] = match[2].strip()
    return values


def value(raw: str) -> str:
    """The value as the bot uses it: surrounding quotes dropped."""
    return raw.strip().strip("'\"")


def adopt(old: Path | str, into: Path | str) -> tuple[list[str], list[str]]:
    """Moves into `into` every key that `old` has filled and `into` does not.

    Never replaces a value `into` already has: `into` is the file the operator
    edits, and it wins. Keys set in both with different values are only named, so
    the operator knows the running bot will change. TEST_ keys stay behind,
    since nothing reads them at run time, and so does ENV_SOURCE, which only
    means something inside the copy install.sh makes.

    Appends instead of editing in place. A blank line for the same key higher up
    is harmless, because the last assignment is the one that counts.

    Returns (moved, differ): key names only, never values.
    """
    mine = read(into)
    theirs = {
        k: raw for k, raw in read(old).items()
        if value(raw) and not k.startswith("TEST_") and k != "ENV_SOURCE"
    }
    moved = [k for k in theirs if not value(mine.get(k, ""))]
    differ = [k for k in theirs if k not in moved and value(theirs[k]) != value(mine[k])]
    if moved:
        text = Path(into).read_text(encoding="utf-8-sig")
        with open(into, "a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write(f"\n# Trazido de {old} antes de apagar a instalação antiga\n")
            f.writelines(f"{k}={theirs[k]}\n" for k in moved)
    return moved, differ


if __name__ == "__main__":  # python3 envfile.py adopt OLD INTO
    if len(sys.argv) != 4 or sys.argv[1] != "adopt":
        raise SystemExit("uso: python3 envfile.py adopt ANTIGO SEU_ENV")
    old_file, own_file = sys.argv[2], sys.argv[3]
    moved_keys, differing = adopt(old_file, own_file)
    if moved_keys:
        print(
            f"→ Só existiam em {old_file} e passei para o {own_file}: "
            f"{', '.join(moved_keys)}"
        )
    if differing:
        print(
            f"⚠️  Diferente em {old_file}, e vale o do {own_file}: "
            f"{', '.join(differing)}"
        )
