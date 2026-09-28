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

# playwright stopped being a dependency when the site got a real API in September
# 2026 and checkout.py stopped driving Chrome. `pip install -r` never removes
# what left the file, so an upgraded install keeps carrying it and the 150 MB
# browser behind it. Removing it here instead of in a note somebody has to read.
if "$PY" -m pip show playwright >/dev/null 2>&1; then
    say "Removendo o playwright, que não é mais usado..."
    "$PY" -m pip uninstall --quiet --yes playwright || true
fi

if [[ "${1:-}" == "--chat-id" ]]; then
    exec "$PY" chat_id.py
fi

# --- make sure nothing else is holding the token -----------------------------
#
# Telegram gives updates to ONE poller per token. A second one gets
# "Conflict: terminated by other getUpdates request" every few seconds and never
# recovers on its own, so starting without clearing the field just produces a
# broken run that looks like a bug in the bot.

# The systemd service first, and before the processes: killing the process while
# the unit is active only makes systemd restart it 30 seconds later.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl list-unit-files bts-vigia.service >/dev/null 2>&1 \
   && systemctl is-active --quiet bts-vigia 2>/dev/null; then
    say "O serviço bts-vigia está no ar. Parando ele para rodar aqui..."
    if [[ $EUID -eq 0 ]]; then
        systemctl stop bts-vigia
    else
        sudo systemctl stop bts-vigia
    fi
    say "Parado. Para religar depois: sudo systemctl start bts-vigia"
fi

# Then any bot.py already running. Matching on ".venv/bin/python" looks tighter
# and does not work: macOS resolves the venv symlink, so the cmdline reads
# ".../Python.framework/.../Python bot.py" and the venv path is nowhere in it.
# So the match is python + bot.py, which is wide enough to also catch a run
# started by hand and the caffeinate wrapper around it — both of which hold the
# token and both of which have to go. This script does not match itself (its own
# cmdline is "bash ./start.sh"), and the PIDs are printed before anything dies.
PATTERN="[Pp]ython.*bot\.py"
if PIDS=$(pgrep -f "$PATTERN" 2>/dev/null) && [[ -n "$PIDS" ]]; then
    say "Já tinha bot rodando aqui (PID $(echo "$PIDS" | tr '\n' ' ' | sed 's/ $//')). Derrubando..."
    # SIGTERM first: the bot closes the Telegram connection cleanly, which
    # releases the token right away. SIGKILL leaves it hanging for a few seconds
    # and the new process starts into a Conflict anyway.
    kill $PIDS 2>/dev/null || true
    for _ in $(seq 1 20); do
        pgrep -f "$PATTERN" >/dev/null 2>&1 || break
        sleep 0.5
    done
    if REMAINING=$(pgrep -f "$PATTERN" 2>/dev/null) && [[ -n "$REMAINING" ]]; then
        say "Não saiu com SIGTERM depois de 10 s — forçando."
        kill -9 $REMAINING 2>/dev/null || true
        sleep 1
    fi
    say "Campo limpo."
fi

# caffeinate -s is not a nicety on a Mac: if the machine sleeps the bot stops
# and nobody in the group gets anything, which looks exactly like "no ticket
# was ever cheap enough". Linux has no equivalent and needs none.
RUN=("$PY" bot.py)
if [[ "$(uname -s)" == "Darwin" ]] && command -v caffeinate >/dev/null; then
    say "Mac detectado — segurando o sono com caffeinate."
    RUN=(caffeinate -s "${RUN[@]}")
fi

# This bot does two different jobs and the .env decides whether it can do both:
# watching needs BOT_TOKEN and CHAT_ID, buying needs the login and the billing
# address. Saying so here, out loud, is the difference between knowing now and
# finding out when /armar refuses — which is whenever you next go looking, quite
# possibly after the one ticket that mattered went by.
if BLOCKED=$("$PY" -c 'import checkout; print("|".join(checkout.blockers()))' 2>/dev/null); then
    if [[ -z "$BLOCKED" ]]; then
        say "Compra: pronta — ele compra sozinho."
    else
        say "Compra: indisponível, então ele só vai avisar. Falta:"
        echo "$BLOCKED" | tr '|' '\n' | sed 's/^/     · /'
    fi
else
    say "Não consegui checar a compra (o config recusou). Veja a mensagem acima."
fi

echo
say "No ar. Ctrl-C para parar."
echo
exec "${RUN[@]}"
