"""Configuration. Everything that varies per machine comes from the .env.

Code is in English; strings the user reads stay in Portuguese, since they are
either Telegram messages for the group or setup instructions for the operator.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"


def _load_env(path: Path = ROOT / ".env") -> None:
    """Reads a simple .env (KEY=value) without pulling in a dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # A real environment variable always wins over the .env file.
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env()


# sysexits.h EX_CONFIG. The systemd unit pairs this with
# RestartPreventExitStatus=78, which is what actually makes a bad .env stop the
# service. Any other exit code means "crashed, try again" and gets restarted.
EX_CONFIG = 78


def _die(msg: str) -> NoReturn:
    """Invalid config: die at import with an instruction instead of a traceback.

    Exits with EX_CONFIG instead of raising SystemExit(str), which would exit 1.
    Under `Restart=always`, exit 1 reads as a crash worth retrying, so a typo in
    the .env would restart the bot every 30 seconds forever and bury the journal
    in the same message. A bare ValueError would be worse still: nothing in the
    traceback tells the operator which line to fix.
    """
    print(f"Configuração inválida: {msg}", file=sys.stderr)
    raise SystemExit(EX_CONFIG)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        _die(f"falta {name} no .env (copie o .env.example e preencha).")
    return value


def _clean(value: str) -> str:
    """Strips a trailing same-line comment and whitespace.

    chat_id.py prints `CHAT_ID=-100123    (supergroup · BTS Vigia)` to be
    readable, and pasting that whole line into the .env is the most natural
    mistake there is.
    """
    return value.split("#", 1)[0].split("(", 1)[0].strip()


def _integer(name: str, value: str, low: int, high: int) -> int:
    try:
        n = int(_clean(value))
    except ValueError:
        _die(f"{name}={value!r} no .env não é um número inteiro.")
    if not low <= n <= high:
        _die(f"{name}={n} está fora da faixa aceita ({low}–{high}).")
    return n


def _decimal(name: str, value: str, low: str, high: str) -> Decimal:
    try:
        n = Decimal(_clean(value))
    except InvalidOperation:
        _die(f"{name}={value!r} no .env não é um número.")
    if not n.is_finite():
        _die(f"{name}={value!r} não é um número finito.")
    if not Decimal(low) <= n <= Decimal(high):
        _die(f"{name}={n} está fora da faixa aceita ({low}–{high}).")
    return n


# --- credentials ------------------------------------------------------------
BOT_TOKEN = _required("BOT_TOKEN")
if ":" not in BOT_TOKEN:
    _die(
        "BOT_TOKEN parece incompleto: o token do @BotFather tem o formato "
        "123456789:AA... (com dois-pontos). Copie a linha inteira."
    )
# Deliberately wide range: this only rejects what cannot be a chat at all.
CHAT_ID = _integer("CHAT_ID", _required("CHAT_ID"), -(10**15), 10**15)

# --- what to monitor --------------------------------------------------------
# The three nights at the Morumbis. Two extra "Camarote Enter Experience"
# sessions (30 and 31/10) are left out: they are hospitality packages with no
# published face value, so there is no fair cap to compare against.
EVENT = "bts%E2%80%932026worldtourarirang"
EVENT_NAME = "BTS · 2026 World Tour Arirang"
VENUE = "Estádio MorumBIS, São Paulo"
EVENT_VENUE_ID = "1775752182066x607042691407020000"  # Estádio do Morumbis

# The label is what shows up in messages and is also the key in state.json.
SESSIONS = {
    "28/10/2026 (quarta)": "1793242799000",
    "30/10/2026 (sexta)": "1793415599000",
    "31/10/2026 (sábado)": "1793501999000",
}


def session_url(session: str) -> str:
    """The session page, with every sector on it."""
    return (
        f"https://buyticketbrasil.com/evento/{EVENT}"
        f"?data={SESSIONS[session]}"
        f"&evento_local={EVENT_VENUE_ID}"
        f"&cidade=S%C3%A3o%2BPaulo"
    )


def buy_link(id_ref: str, session: str) -> str:
    """Direct link to ONE listing — the same one the site's "Comprar" button uses.

    The `id_ref` from matriz_preco is exactly the `c_anuncio` of that link
    (verified against the site). If you are already logged in it opens the
    ticket directly; if not, it goes through the login screen first.
    """
    if not id_ref:
        return session_url(session)  # no id: fall back to the session page
    return f"https://buyticketbrasil.com/r?event={EVENT}&c_anuncio={id_ref}"


# Seconds between rounds. This is a real-time watcher: good listings have been
# seen appearing and vanishing inside 15 minutes, so a 10-minute poll can miss
# the only fair price of the week entirely.
#
# Measured against the site on 17/09/2026: one round is 3 requests, ~0.5s and
# 18.7 KB each; 60 requests over 7 minutes at a 20s cadence drew no rate
# limiting and no slowdown. 30s is therefore half the rate already proven to be
# tolerated — roughly 8,600 requests a day.
#
# The 10s floor is not arbitrary politeness: getting the IP blocked is permanent
# blindness, which is far worse than being 30 seconds late. bot.py also adds
# jitter and backs off on its own if the site pushes back.
for _old in ("INTERVAL", "INTERVALO"):
    if os.environ.get(_old):
        _die(
            f"{_old} virou INTERVAL_SECONDS, e agora o valor é em SEGUNDOS, não\n"
            f"em minutos. Troque a linha do .env por INTERVAL_SECONDS=30 (deixar\n"
            f"{_old}={os.environ[_old]} ali viraria um intervalo errado em silêncio)."
        )
INTERVAL_SECONDS = _integer(
    "INTERVAL_SECONDS", os.environ.get("INTERVAL_SECONDS", "30"), 10, 3600
)

# Random 0–JITTER seconds added to each round, so the bot does not hit the site
# on a perfect metronome — which is both rude and the easiest pattern to block.
JITTER = max(2, INTERVAL_SECONDS // 5)

# Categories that can trigger an alert. The site sells Inteira, Meia Estudante,
# Meia PCD, Meia Jovem Baixa Renda and Acompanhante PCD. Only list the ones you
# can actually prove at the gate — alerting on a discount you cannot use is the
# same as alerting on a price that does not exist for you.
CATEGORIES = tuple(
    c.strip()
    for c in os.environ.get("CATEGORIES", "Inteira,Meia Estudante").split(",")
    if c.strip()
)
# What the site actually sells. A typo here would raise no error at all: the bot
# would filter everything out and stay quiet forever, looking exactly like
# "nothing hit the cap". It is the hardest failure of this bot to notice.
KNOWN_CATEGORIES = (
    "Inteira",
    "Meia Estudante",
    "Meia PCD",
    "Meia Jovem Baixa Renda",
    "Acompanhante PCD",
)
if not CATEGORIES:
    _die("CATEGORIES está vazio — o bot nunca alertaria nada.")
_unknown = [c for c in CATEGORIES if c not in KNOWN_CATEGORIES]
if _unknown:
    _die(
        f"CATEGORIES tem valor que o site não vende: {_unknown}.\n"
        f"Válidos: {', '.join(KNOWN_CATEGORIES)}."
    )

# --- how much markup is acceptable ------------------------------------------
# Face value announced by the promoter (G1, 27/03/2026), full-price ticket.
# Camarotes are hospitality packages with no published face: left out.
FACE_VALUE = {
    "Arquibancada": Decimal("680"),
    "Cadeira Superior": Decimal("980"),
    "Cadeira Inferior": Decimal("1080"),
    "Pista": Decimal("1250"),
}

# Maximum markup, in %, over face value. The 36% default is what the person who
# bought on Ticketmaster actually spent (face + ~15% booking fee) plus a modest
# 20% premium. On full-price Pista that is R$ 1,700; on half-price, R$ 850.
MAX_MARKUP = _decimal("MAX_MARKUP", os.environ.get("MAX_MARKUP", "36"), "0", "1000")


def face_value(sector: str, category: str) -> Decimal | None:
    """Face value of that sector IN THAT category, or None if unknown.

    Half-price is exactly half of face — the promoter's table confirms the ratio
    in every sector (Pista R$ 625 / R$ 1,250, Arquibancada R$ 340 / R$ 680,
    Cadeira Superior R$ 490 / R$ 980, Cadeira Inferior R$ 540 / R$ 1,080).

    Only "Inteira" pays full; every other category the site sells is half-price
    by law (student, disability, companion, low-income youth). If the site
    invents a new category it falls into half, making the cap stricter: it errs
    toward not alerting, which is the right side to err on.
    """
    face = FACE_VALUE.get(sector)
    if face is None:
        return None
    return face if category == "Inteira" else face / 2


def price_cap(
    sector: str, category: str, markup: Decimal | None = None
) -> Decimal | None:
    """Highest fair price for that sector+category, or None if face is unknown."""
    face = face_value(sector, category)
    if face is None:
        return None
    pct = MAX_MARKUP if markup is None else markup
    return (face * (1 + pct / 100)).quantize(Decimal("0.01"))
