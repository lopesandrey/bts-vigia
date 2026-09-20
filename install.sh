#!/usr/bin/env bash
#
# Installs bts-vigia as a systemd service on Ubuntu 24.04 LTS.
#
# Comments and code are in English; what the operator reads on screen is in
# Portuguese, same split as the rest of the project.
#
# Safe to re-run: it updates an existing install instead of duplicating it, and
# it never overwrites an existing .env.
#
#   sudo ./install.sh
#
set -euo pipefail

APP=bts-vigia
DEST=/opt/$APP
SERVICE_USER=bts
UNIT=/etc/systemd/system/$APP.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "→ $*"; }

[[ $EUID -eq 0 ]] || die "Rode com sudo: sudo ./install.sh"
[[ -f "$SRC/bot.py" ]] || die "Rode o script de dentro da pasta do projeto."

# Ubuntu 24.04 is PEP 668 ("externally managed"): pip refuses to touch the
# system Python at all, so the venv is not optional, and python3-venv is a
# separate package from python3.
say "Instalando dependências do sistema..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv ca-certificates

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "Python do sistema: $PYVER (testado em 3.12)"

# --system: no password, no aging, no home of its own. --shell nologin: if the
# token ever leaks, it does not come with a usable login.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "Criando o usuário de serviço '$SERVICE_USER'..."
    useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Copiando o código para $DEST..."
mkdir -p "$DEST"
# state.json and .env are not in this list. state.json belongs to the running
# install, and the .env is handled separately below — it needs mode 0600, not
# the 0644 these get.
#
# Every module bot.py imports has to be here. It imports checkout, so leaving
# that one out does not degrade the install, it breaks it: the service starts,
# dies on ImportError, and systemd restarts it forever.
for f in bot.py config.py monitor.py checkout.py chat_id.py requirements.txt \
         .env.example; do
    install -m 0644 "$SRC/$f" "$DEST/$f"
done

# .auth holds the logged-in session cookies, so it is the service's own and it is
# 0700 — not 0755 like the code. checkout.py creates it on the first login; made
# here so the directory belongs to the service user from the start.
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DEST/.auth"

say "Criando o venv e instalando as dependências..."
if [[ ! -x "$DEST/.venv/bin/python" ]]; then
    python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/.venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

# Same reason as in start.sh: playwright left requirements.txt when checkout.py
# stopped driving a browser, and `pip install -r` never removes what left.
if "$DEST/.venv/bin/pip" show playwright >/dev/null 2>&1; then
    say "Removendo o playwright, que não é mais usado..."
    "$DEST/.venv/bin/pip" uninstall --quiet --yes playwright || true
fi

# On a first install, take the .env the operator already filled in next to the
# code. Skipping it and seeding from .env.example instead is worse than useless:
# the install "succeeds", the service starts, and it dies on the placeholder
# token — which looks like the operator forgot a step they in fact did.
# An existing $DEST/.env always wins: it belongs to the running service, and
# silently replacing it on an update would swap a working token for a stale one.
if [[ ! -f "$DEST/.env" ]]; then
    if [[ -f "$SRC/.env" ]]; then
        say "Usando o .env que já está na pasta do projeto."
        install -m 0600 "$SRC/.env" "$DEST/.env"
    else
        install -m 0600 "$SRC/.env.example" "$DEST/.env"
        NEEDS_ENV=1
    fi
elif [[ -f "$SRC/.env" ]] && ! cmp -s "$SRC/.env" "$DEST/.env"; then
    say "Mantendo o $DEST/.env atual (o da pasta do projeto é diferente e foi ignorado)."
fi
# 0600 always, even on an existing .env: the token must not be world-readable
# on a machine with other users.
chmod 0600 "$DEST/.env"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DEST"

say "Instalando o serviço systemd..."
cat > "$UNIT" <<EOF
[Unit]
Description=bts-vigia — watches BTS ticket listings and alerts a Telegram group
Documentation=https://github.com/lopesandrey/bts-vigia
# Wants= is what actually waits for the network. After= alone only orders the
# units if network-online.target happens to be pulled in by something else.
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$DEST
ExecStart=$DEST/.venv/bin/python bot.py
Restart=always
RestartSec=30
# config.py exits 78 (EX_CONFIG) when the .env is wrong. Without this line
# Restart=always would read that as a crash and retry every 30 seconds forever,
# burying the journal in the same message instead of stopping with it on top.
RestartPreventExitStatus=78
# Log to the journal; journalctl -u $APP -f to follow it.
StandardOutput=journal
StandardError=journal
# Modest hardening: the box holds a live bot token. ReadWritePaths is required
# because ProtectSystem=strict would otherwise make both state.json and the
# .auth/ cookie jar unwritable — and an unwritable .auth means logging in again
# on every single purchase.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DEST

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet "$APP"

echo
if [[ ${NEEDS_ENV:-0} -eq 1 ]]; then
    cat <<EOF
✅ Instalado, mas ainda NÃO ligado — falta o .env.

Edite o arquivo e preencha BOT_TOKEN e CHAT_ID:

    sudo nano $DEST/.env

(o passo a passo do Telegram está no SETUP-TELEGRAM.md; para descobrir o
CHAT_ID: sudo -u $SERVICE_USER $DEST/.venv/bin/python $DEST/chat_id.py)

Depois:

    sudo systemctl start $APP
    journalctl -u $APP -f
EOF
else
    systemctl restart "$APP"
    cat <<EOF
✅ Instalado e rodando.

    journalctl -u $APP -f       # acompanhar
    sudo systemctl stop $APP    # parar
    sudo systemctl restart $APP # depois de editar o .env

EOF
    sleep 2
    systemctl is-active --quiet "$APP" \
        && say "Serviço ativo." \
        || die "O serviço não subiu. Veja: journalctl -u $APP -n 30 --no-pager"
fi
