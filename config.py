"""Configuration. Everything that varies per machine comes from the .env.

Code is in English; strings the user reads stay in Portuguese, since they are
either Telegram messages for the group or setup instructions for the operator.
"""

from __future__ import annotations

import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

import envfile

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
# The one file the bot reads. On the server it is a copy: install.sh rebuilds
# /opt/bts-vigia/.env from the project folder's .env on every run, and writes
# ENV_SOURCE into the copy so the messages can name the file to edit instead.
ENV_FILE = ROOT / ".env"


def _load_env(path: Path = ENV_FILE) -> None:
    """Reads the .env without pulling in a dependency. See envfile.read."""
    if not path.exists():
        return
    for key, raw in envfile.read(path).items():
        # A real environment variable always wins over the .env file.
        os.environ.setdefault(key, envfile.value(raw))


_load_env()

# Written by install.sh into the copy it installs; blank when the bot runs
# straight from the project folder (start.sh), where the two files are one.
ENV_SOURCE = os.environ.get("ENV_SOURCE", "").strip()


def env_hint() -> str:
    """Where to fix a missing key, said as the thing the operator has to do.

    On the server an edit to /opt/bts-vigia/.env would be undone by the next
    install, and editing the project's file does nothing until install.sh runs,
    so "fill it in the .env" on its own is how the buy keys went missing before.
    """
    if ENV_SOURCE:
        return f"edite {ENV_SOURCE} e rode sudo ./install.sh"
    return f"edite {ENV_FILE} e reinicie o bot"


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


def _warn(msg: str) -> None:
    """Something worth knowing that is not worth refusing to start over."""
    print(f"Aviso: {msg}", file=sys.stderr)


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


def _digits(value: str) -> str:
    """Keeps only the digits — CPF and CEP are sent unformatted.

    The site masks them on screen (000.000.000-00) and strips them before it
    sends them, so a .env written either way works. The example above is the
    one CPF every validator rejects, on purpose: a valid one in a public repo
    is somebody's.
    """
    return re.sub(r"\D", "", _clean(value))


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
# A shape check, not just a colon check. The .env.example placeholder
# (123456789:AA-put-your-token-here) does contain a colon, so a colon test waves
# it through — and then the failure surfaces sixty lines deep in a Telegram
# traceback that says nothing about the .env. The real token is a bot id and a
# ~35-character secret; anything shorter is a placeholder or a truncated paste.
if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    _die(
        "BOT_TOKEN não parece um token do @BotFather.\n"
        "Ele tem o formato 123456789:AA... — o número do bot, dois-pontos, e\n"
        "uns 35 caracteres. Se o que está no .env ainda é o exemplo\n"
        "('AA-put-your-token-here'), crie o bot no @BotFather e cole o token\n"
        "de verdade. Se você colou um de verdade, confira se veio inteiro."
    )
# Deliberately wide range: this only rejects what cannot be a chat at all.
CHAT_ID = _integer("CHAT_ID", _required("CHAT_ID"), -(10**15), 10**15)

# --- what to monitor --------------------------------------------------------
# The site was rewritten in September 2026: /evento/<slug> became /event/<slug>,
# the slug lost its en-dash, and the session key went from an epoch timestamp to
# a UUID. Every URL below was re-read off the live site on 20/09/2026, and the
# route shapes come from the site's own JavaScript, not from guessing.
EVENT = "bts-2026worldtourarirang"
EVENT_NAME = "BTS · 2026 World Tour Arirang"
VENUE = "Estádio MorumBIS, São Paulo"

SITE = "https://buyticketbrasil.com"
# The site's own browser code does `fetch("/api/backend" + path)` for everything
# and `POST /api/auth/login` for signing in — the Next.js server in front of the
# real backend, which is also what holds the session cookie. So these two are the
# whole surface: monitor.py reads from API, checkout.py writes to it.
BACKEND = f"{SITE}/api/backend"
API = f"{BACKEND}/marketplaces"
LOGIN = f"{SITE}/api/auth/login"

# The three nights at the Morumbis, label -> session UUID. The label is what
# shows up in messages and is also the key in state.json.
#
# The event actually lists five sessions: 30 and 31/10 each have a second entry
# at "Estádio do Morumbis - Camarote Enter Experience". Those are hospitality
# packages with no published face value, so there is no fair cap to compare
# against and they could never be alerted anyway.
SESSIONS = {
    "28/10/2026 (quarta)": "e4ac9384-ccd7-4f29-82b8-7ba8677bc2ed",
    "30/10/2026 (sexta)": "ced30381-0cc4-4779-9c6b-ba05f44d086e",
    "31/10/2026 (sábado)": "7dbb3a2b-985d-4bf2-9d36-df893169e098",
}


def session_url(session: str) -> str:
    """The page for one night, with every sector on it."""
    return f"{SITE}/event/{EVENT}/session/{SESSIONS[session]}"


def buy_link(listing_id: str, session: str) -> str:
    """Direct link to ONE listing — the same URL the site's "Comprar" button uses.

    Taken from their own bundle (`/checkout?listing=${v.listingId}`), so it is
    the real route and not a reconstruction. Opening it while logged out
    redirects to /login?redirect=<this>, which lands you straight back here after
    signing in. This is the link a human taps in an alert; checkout.py does not
    use it at all, since it talks to the API directly.
    """
    if not listing_id:
        return session_url(session)  # no id: fall back to the session page
    return f"{SITE}/checkout?listing={listing_id}"


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

# Categories that can trigger an alert. Only list the ones you can actually prove
# at the gate — alerting on a discount you cannot use is the same as alerting on a
# price that does not exist for you.
CATEGORIES = tuple(
    c.strip()
    for c in os.environ.get("CATEGORIES", "Inteira").split(",")
    if c.strip()
)
# What the site actually sells for THIS event, read off the API on 28/09/2026 (the
# category list comes back in full every time, with zeros for whatever nobody is
# offering, so this is the complete set and not just today's stock).
#
# The site edits this list. On 20/09 it had no "Meia Estudante" at all; by 28/09
# that was back, along with the last three below, and "Desconhecida" was gone.
#
# A typo here would raise no error at all: the bot would filter everything out
# and stay quiet forever, looking exactly like "nothing hit the cap". It is the
# hardest failure of this bot to notice, which is why it dies at import instead.
KNOWN_CATEGORIES = (
    "Inteira",
    "Meia Estudante",
    "Meia Idoso",
    "Meia PCD",
    "Meia Jovem Baixa Renda",
    "Meia Professor",
    "Acompanhante PCD",
    "Meia aposentado",
    "Meia Até 21",
    "Meia Pessoas Tea",
    "Meia Profissional da Saúde",
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
# The service fee the checkout adds on top of the listing price, as a fraction.
#
# This one flipped when the site was rewritten, and getting it backwards costs
# real money, so here is the evidence. The old `matriz_preco` was fee-INCLUSIVE:
# every one of fourteen live prices divided by exactly 1.10 into a round figure a
# seller would have typed (3300→3000, 4840→4400, 8800→8000). The new API's
# `amount.value` does not — 1780 and 4823 divide into nothing round — and the
# checkout's own strings settle it:
#
#     summary.serviceFee = "Taxa de serviço (10%)"      <- its own line
#     total.label        = "Total · taxa incluída"
#     payment.methods.pixSubtitle = "Aprovação automática · sem taxa"
#
# So the API now quotes the seller's asking price and the fee is added at the
# till. Pix adds nothing further (a card would add 3.6% more, which is one more
# reason this bot only pays by Pix).
#
# monitor.py therefore stores both numbers: `Offer.listed` is what the site
# shows, `Offer.price` is listed + this fee. Every cap and gate compares against
# `Offer.price`, so a ceiling in this file means money out of the bank — which is
# exactly what it meant before, and the only reading that cannot quietly overpay.
SERVICE_FEE = Decimal("0.10")


def with_fee(listed: Decimal) -> Decimal:
    """Listing price -> what the checkout will actually charge over Pix."""
    return (listed * (1 + SERVICE_FEE)).quantize(Decimal("0.01"))


# Face value announced by the promoter (G1, 27/03/2026), full-price ticket.
# Camarotes are hospitality packages with no published face: left out. This
# doubles as the list of sectors monitor.py polls, since a sector with no face
# value can never pass price_cap() and asking about it every round buys nothing.
FACE_VALUE = {
    "Arquibancada": Decimal("680"),
    "Cadeira Superior": Decimal("980"),
    "Cadeira Inferior": Decimal("1080"),
    "Pista": Decimal("1250"),
}

# Maximum markup, in %, over face value. The 36% default is what the person who
# bought on Ticketmaster actually spent (face + ~15% booking fee) plus a modest
# 20% premium. On Pista that is R$ 1,700 — for a full-price or a half-price
# listing alike, since the cap prices the seat, not the seller's cost. See
# face_value() for why.
MAX_MARKUP = _decimal("MAX_MARKUP", os.environ.get("MAX_MARKUP", "36"), "0", "1000")


def face_value(sector: str) -> Decimal | None:
    """Face value of that sector, or None if the promoter published none.

    Deliberately blind to the category, and this is the whole design decision of
    this bot, so it is worth spelling out.

    Half-price face IS half — R$ 340 for Arquibancada against R$ 680. An earlier
    version compared a half-price listing against that R$ 340, which measures how
    greedy the seller is relative to what THEY paid. That is the wrong question.
    The buyer is not buying the seller's cost, they are buying a seat, and a
    half-price ticket seats you in exactly the same place as a full-price one.

    In practice the old model made half-price tickets unalertable: at +36% the
    Arquibancada half cap came to R$ 462.40, and nobody resells at R$ 462. So
    listing "Meia Estudante" in CATEGORIES bought you nothing but 📉 noise.

    Judging both against the full face asks the question that decides a purchase:
    for this seat, is this price acceptable? On 31/10/2026 a Cadeira Superior
    half sat at R$ 2,970 and the full at R$ 5,059.99 — same seat, R$ 2,000 apart.
    The half is plainly the one to buy, and only this model can say so.

    The catch is real and is handled in the message, not in the math: a
    half-price ticket with no student card at the gate is a refused entry, not a
    discount. Every alert for a non-Inteira category carries that warning, and
    CATEGORIES lets you drop them entirely if nobody in the group has a card.
    """
    return FACE_VALUE.get(sector)


def price_cap(sector: str, markup: Decimal | None = None) -> Decimal | None:
    """Highest fair price for that sector, or None if face is unknown."""
    face = face_value(sector)
    if face is None:
        return None
    pct = MAX_MARKUP if markup is None else markup
    return (face * (1 + pct / 100)).quantize(Decimal("0.01"))


# --- buying -----------------------------------------------------------------
# A flat ceiling in reais, on top of price_cap(). It looks redundant and is not:
# price_cap depends on reading the sector name correctly and on FACE_VALUE being
# right. If the site renames a sector or a price arrives in the wrong unit, that
# arithmetic can approve a number it should not. A bare number cannot be reasoned
# wrong, and this is the last gate before real money leaves.
HARD_CEILING = _decimal("HARD_CEILING", os.environ.get("HARD_CEILING", "2000"), "1", "99999")

# What the order needs, straight from the .env. Deliberately NOT _required: a
# machine that only watches prices must still boot without these. checkout.py
# refuses to run when one is blank, which is the right place for the error since
# it is the only thing that needs them.
#
# CPF is the login, not just a billing field: the site signs in with document +
# password, never with the e-mail (their own analytics calls the method
# "document_password"). So this key does double duty.
#
# Street, neighbourhood, city and state are not needed here: the site fills them
# from the CEP through viacep.com.br, and checkout.py asks viacep the same
# question — same source, same answer, nothing guessed. Fill ADDRESS below and
# they win instead.
BILLING = {
    "document": _digits(os.environ.get("CPF", "")),
    "zip_code": _digits(os.environ.get("POSTAL_CODE", "")),
    "number": _clean(os.environ.get("NUMBER", "")),
    "complement": _clean(os.environ.get("COMPLEMENT", "")),
    # Blank is fine and is the better default: checkout.py falls back to the name
    # on the account, which is the name the payment has to carry anyway.
    "name": _clean(os.environ.get("NAME", "")),
}

# Optional, and worth filling: viacep is the only call in the buying flow that
# leaves buyticketbrasil, so on the one round that matters it is the only step
# that can fail for a reason having nothing to do with the ticket. Filled in,
# these skip that call entirely and the purchase depends on one host instead of
# two. The order rejects a blank street, neighbourhood, city or state, so it is
# all four or none — a partial address here would be worse than no address.
ADDRESS = {
    "street": _clean(os.environ.get("STREET", "")),
    "neighborhood": _clean(os.environ.get("NEIGHBORHOOD", "")),
    "city": _clean(os.environ.get("CITY", "")),
    "state": _clean(os.environ.get("STATE", "")).upper(),
}
if any(ADDRESS.values()) and not all(ADDRESS.values()):
    _warn(
        "o endereço do .env está pela metade: "
        f"falta {', '.join(k.upper() for k, v in ADDRESS.items() if not v)}. "
        "Vou consultar o CEP no viacep, como se nenhum estivesse preenchido — "
        "meio endereço o pedido recusa."
    )
    ADDRESS = dict.fromkeys(ADDRESS, "")


def receiver_email() -> str:
    """Who the ticket gets transferred to at the official box office.

    The event is `transferType: "Email"`, so the order carries an address and the
    seller transfers the ticket to it. It is not necessarily the login address —
    you might be buying for a friend — so it has its own key, falling back to
    EMAIL, which is the right guess when you are buying for yourself.
    """
    return _clean(os.environ.get("RECEIVER_EMAIL", "")) or _clean(
        os.environ.get("EMAIL", "")
    )


def missing_billing() -> list[str]:
    """Names of the .env keys needed to buy that are still blank."""
    keys = {"document": "CPF", "zip_code": "POSTAL_CODE", "number": "NUMBER"}
    blank = [env for field, env in keys.items() if not BILLING[field]]
    if not os.environ.get("PASS", "").strip():
        blank.append("PASS")
    if not receiver_email():
        blank.append("RECEIVER_EMAIL")
    return blank
