#!/usr/bin/env bash
#
# Stops bts-vigia and deletes everything the running install holds.
#
# Comments and code are in English; what the operator reads on screen is in
# Portuguese, same split as the rest of the project.
#
# Stopped for good, not paused: the service is disabled and its unit removed, so
# a reboot does not bring it back, and /opt/bts-vigia is deleted — the service's
# copy of the .env, the logged-in cookies, state.json and the venv. None of that
# is the source of anything. The project folder's .env is, and whatever existed
# only in the copy is moved into it before the copy goes. Coming back is
# `sudo ./install.sh`, which runs this first and then builds from zero.
#
#   sudo ./stop.sh      on the server
#   ./stop.sh           on a Mac running start.sh: stops the process, nothing else
#
set -euo pipefail

APP=bts-vigia
DEST=/opt/$APP
UNIT=/etc/systemd/system/$APP.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# install.sh passes this: it has already moved the .env keys, and it prints its
# own ending.
FOR_INSTALL=0
[[ "${1:-}" == "--for-install" ]] && FOR_INSTALL=1

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "→ $*"; }

if [[ ( -f "$UNIT" || -d "$DEST" ) && $EUID -ne 0 ]]; then
    die "Rode com sudo: sudo ./stop.sh"
fi
DID=0

# --- 1. keep what only the service's copy of the .env has ---------------------
# Before anything else, so that a failure here leaves the bot running. An install
# made by the old install.sh may have been configured only in /opt (it told
# people to `sudo nano /opt/bts-vigia/.env`); then the project gets a new .env
# holding all of it.
if [[ $FOR_INSTALL -eq 0 && -f "$DEST/.env" ]]; then
    OWNER=${SUDO_USER:-root}
    if [[ ! -f "$SRC/.env" ]]; then
        install -m 0600 -o "$OWNER" -g "$(id -gn "$OWNER")" /dev/null "$SRC/.env"
    fi
    python3 "$SRC/envfile.py" adopt "$DEST/.env" "$SRC/.env"
fi

# --- 2. the service -----------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [[ -f "$UNIT" ]]; then
    say "Parando o serviço $APP..."
    systemctl stop "$APP" 2>/dev/null || true
    systemctl disable --quiet "$APP" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    systemctl reset-failed "$APP" 2>/dev/null || true
    DID=1
fi

# --- 3. any bot of this project still holding the token -----------------------
# A start.sh left running in a terminal, or a process systemd no longer knows
# about. Telegram gives updates to one poller per token, so a leftover one makes
# the new bot fail with Conflict. Matched by working directory, not only by name,
# so some other program's bot.py on the same machine is never touched. On macOS
# the match also catches the caffeinate wrapper start.sh puts around the bot.
ours() {
    local pid cwd
    for pid in $(pgrep -f '[Pp]ython.*bot\.py' 2>/dev/null); do
        if [[ -e "/proc/$pid/cwd" ]]; then
            cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
        else
            cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
        fi
        if [[ "$cwd" == "$SRC" || "$cwd" == "$DEST" ]]; then
            echo "$pid"
        fi
    done
    return 0
}
PIDS=$(ours | tr '\n' ' ')
if [[ -n "${PIDS// /}" ]]; then
    say "Derrubando o bot que ainda estava rodando (PID ${PIDS% })..."
    # SIGTERM first: the bot closes the Telegram connection cleanly, which frees
    # the token right away. SIGKILL leaves it hanging for a few seconds.
    kill $PIDS 2>/dev/null || true
    for _ in $(seq 1 20); do
        [[ -z "$(ours)" ]] && break
        sleep 0.5
    done
    LEFT=$(ours | tr '\n' ' ')
    if [[ -n "${LEFT// /}" ]]; then
        say "Não saiu com SIGTERM depois de 10 s — forçando."
        kill -9 $LEFT 2>/dev/null || true
    fi
    DID=1
fi

# --- 4. the install itself ----------------------------------------------------
if [[ -d "$DEST" ]]; then
    say "Apagando $DEST (cópia do .env, login salvo, state.json e venv)..."
    rm -rf "$DEST"
    DID=1
fi

[[ $FOR_INSTALL -eq 1 ]] && exit 0
echo
if [[ $DID -eq 0 ]]; then
    echo "Não tinha nada rodando nem instalado."
elif command -v systemctl >/dev/null 2>&1; then
    echo "✅ Parado e limpo. O seu $SRC/.env continua aí."
    echo "   Para voltar: sudo ./install.sh"
else
    echo "✅ Parado. Para voltar: ./start.sh"
fi
