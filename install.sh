#!/usr/bin/env bash
#
# Installs bts-vigia as a systemd service on Ubuntu 24.04 LTS, from zero.
#
# Comments and code are in English; what the operator reads on screen is in
# Portuguese, same split as the rest of the project.
#
# Every run is a clean install. stop.sh takes the old one down and deletes it,
# and this builds a new one from the project folder: new code, new venv, empty
# state.json, no saved login, and a fresh copy of the .env. There is no update
# path to get wrong because there is no update. The one there used to be kept
# /opt/bts-vigia/.env forever, so the buy keys added to the project's .env never
# reached the service and every purchase said "falta preencher".
#
# The project folder's .env is the only config. Edit it and run this again.
#
#   git pull && sudo ./install.sh
#
set -euo pipefail

APP=bts-vigia
DEST=/opt/$APP
SERVICE_USER=bts
UNIT=/etc/systemd/system/$APP.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNER=${SUDO_USER:-root}

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

# --- 1. the .env, settled before anything running is touched ------------------
# Whatever only the running install's copy holds moves into the project's first,
# so nothing configured there is lost and the check below sees the config the
# new install will actually get.
if [[ -f "$DEST/.env" ]]; then
    if [[ ! -f "$SRC/.env" ]]; then
        install -m 0600 -o "$OWNER" -g "$(id -gn "$OWNER")" /dev/null "$SRC/.env"
    fi
    python3 "$SRC/envfile.py" adopt "$DEST/.env" "$SRC/.env"
fi
if [[ ! -f "$SRC/.env" ]]; then
    install -m 0600 -o "$OWNER" -g "$(id -gn "$OWNER")" "$SRC/.env.example" "$SRC/.env"
    cat <<EOF

❌ Não tinha .env, então criei um a partir do exemplo. Preencha:

    nano $SRC/.env

BOT_TOKEN e CHAT_ID para ele avisar (o passo a passo está no SETUP-TELEGRAM.md;
para achar o CHAT_ID: ./start.sh --chat-id), e o bloco de compra — CPF, PASS,
POSTAL_CODE, NUMBER e RECEIVER_EMAIL — para ele comprar. Depois:

    sudo ./install.sh
EOF
    exit 1
fi

# The new code reads the new config before the old bot is stopped. A broken .env
# stops the install here, with the old bot still running, instead of after it is
# gone. No bytecode: this runs as root inside the operator's folder.
say "Conferindo o $SRC/.env..."
if ! (cd "$SRC" && PYTHONDONTWRITEBYTECODE=1 python3 -c 'import config'); then
    die "Corrija o $SRC/.env (o problema está logo acima) e rode de novo. Nada foi parado nem apagado."
fi

# --- 2. down to zero ----------------------------------------------------------
bash "$SRC/stop.sh" --for-install

# --- 3. a new install ---------------------------------------------------------
# --system: no password, no aging, no home of its own. --shell nologin: if the
# token ever leaks, it does not come with a usable login.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "Criando o usuário de serviço '$SERVICE_USER'..."
    useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Copiando o código para $DEST..."
install -d -m 0755 "$DEST"
# Every module bot.py imports has to be here. Leaving one out does not degrade
# the install, it breaks it: the service starts, dies on ImportError, and systemd
# restarts it forever.
for f in bot.py config.py envfile.py monitor.py checkout.py chat_id.py \
         requirements.txt; do
    install -m 0644 "$SRC/$f" "$DEST/$f"
done

# .auth holds the logged-in session cookies, so it is the service's own and it is
# 0700 — not 0755 like the code. It starts empty: the bot logs in within 20
# seconds of starting and saves the session there.
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DEST/.auth"

say "Criando o venv e instalando as dependências..."
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/.venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

# The service's .env is a copy, and says so on its first line to anybody who
# opens it. ENV_SOURCE tells the bot which file its messages should send people
# to. TEST_ keys stay behind: nothing reads them at run time. umask 077 because
# the file holds the token and the site password from the moment it exists, not
# from the chmod after.
say "Copiando o .env..."
(
    umask 077
    {
        echo "# Cópia de $SRC/.env feita pelo install.sh. NÃO edite aqui — a"
        echo "# próxima instalação apaga. Edite $SRC/.env e rode sudo ./install.sh."
        grep -vE '^[[:space:]]*(export[[:space:]]+)?(TEST_[A-Za-z0-9_]*|ENV_SOURCE)[[:space:]]*=' \
            "$SRC/.env" || true
        echo
        echo "ENV_SOURCE=$SRC/.env"
    } > "$DEST/.env"
)
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
STARTED=$(date +%s)
systemctl start "$APP"

# --- 4. proof it is the new one, running, and able to buy --------------------
# The boot line only exists in the current code, so seeing it is also the proof
# that the old copy is gone.
say "Esperando o bot subir..."
BOOT=""
for _ in $(seq 1 30); do
    BOOT=$(journalctl -u "$APP" --since "@$STARTED" --no-pager -o cat 2>/dev/null \
        | grep -m1 "in force this boot" || true)
    [[ -n "$BOOT" ]] && break
    systemctl is-active --quiet "$APP" || break
    sleep 1
done
systemctl is-active --quiet "$APP" \
    || die "O serviço não subiu. Veja: journalctl -u $APP -n 30 --no-pager"

# The same check the bot runs, as the service user, against the installed copy:
# if this says ready, the bot reads the same answer.
BLOCKED=$(cd "$DEST" && sudo -u "$SERVICE_USER" env PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -c 'import checkout; print("\n".join(checkout.blockers()))' \
    2>/dev/null) || BLOCKED="não consegui conferir — veja journalctl -u $APP -n 30"

echo
echo "✅ Instalado do zero e rodando."
[[ -n "$BOOT" ]] && echo "   ${BOOT#*in force this boot: }"
if [[ -z "$BLOCKED" ]]; then
    echo "   Compra: pronta — ele compra sozinho."
else
    echo "   ⚠️  Compra: NÃO está pronta, ele só vai avisar:"
    echo "$BLOCKED" | sed 's/^/      · /'
fi
cat <<EOF

   Acompanhar:      journalctl -u $APP -f
   Mudar a config:  nano $SRC/.env  e depois  sudo ./install.sh
   Parar e limpar:  sudo ./stop.sh
EOF
