#!/usr/bin/env bash
#
# Starts the bot in the foreground, from the project folder.
#
# Comments and code are in English; what the operator reads on screen is in
# Portuguese, same split as the rest of the project.
#
# This is the "run it on my own machine" path — a laptop, a test, a first run.
# On a server you want install.sh instead, which makes it a systemd service that
# survives a reboot. This script is only in charge of the parts people forget:
# the venv, the dependencies, and keeping the Mac awake.
#
#   ./start.sh
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV=.venv
PY="$VENV/bin/python"

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "→ $*"; }

command -v python3 >/dev/null || die "Python 3 não encontrado. Instale e rode de novo."

if [[ ! -f .env ]]; then
    [[ -f .env.example ]] || die "Faltam o .env e o .env.example. Está na pasta certa?"
    cp .env.example .env
    cat >&2 <<'EOF'
❌ Criei um .env a partir do exemplo, mas ele ainda está em branco.

Preencha BOT_TOKEN e CHAT_ID:

    nano .env

O passo a passo completo está no SETUP-TELEGRAM.md. Para achar o CHAT_ID,
mande uma mensagem no grupo e rode:  ./start.sh --chat-id
EOF
    exit 1
fi

if [[ ! -x "$PY" ]]; then
    say "Criando o ambiente virtual (só na primeira vez)..."
    python3 -m venv "$VENV"
fi

# Reinstall only when requirements.txt is newer than the stamp we wrote after
# the last successful install. Without this the bot pays a pip round-trip on
# every start; with a plain `[[ -d ]]` check it would instead never notice a
# changed requirements.txt after a git pull.
STAMP="$VENV/.deps-installed"
if [[ ! -f "$STAMP" || requirements.txt -nt "$STAMP" ]]; then
    say "Instalando as dependências..."
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    touch "$STAMP"
fi

if [[ "${1:-}" == "--chat-id" ]]; then
    exec "$PY" chat_id.py
fi

# caffeinate -s is not a nicety on a Mac: if the machine sleeps the bot stops
# and nobody in the group gets anything, which looks exactly like "no ticket
# was ever cheap enough". Linux has no equivalent and needs none.
RUN=("$PY" bot.py)
if [[ "$(uname -s)" == "Darwin" ]] && command -v caffeinate >/dev/null; then
    say "Mac detectado — segurando o sono com caffeinate."
    RUN=(caffeinate -s "${RUN[@]}")
fi

echo
say "No ar. Ctrl-C para parar."
echo
exec "${RUN[@]}"
