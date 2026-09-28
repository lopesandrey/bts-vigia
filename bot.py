"""Telegram bot that watches BTS tickets and speaks up when the price is fair.

Runs as a single process: python-telegram-bot's JobQueue does the scheduling, so
there is no cron, no worker and no queue.

    .venv/bin/python bot.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import sys
import time
from decimal import Decimal

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict, InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import checkout
import config
import monitor
from monitor import Offer, brl

log = logging.getLogger("bts")

# How many failed checks in a row before we tell the group the bot is blind.
# Expressed as a duration, not a count: at one round every 30s, "3 in a row"
# would cry wolf over a 90-second wifi blip. Ten minutes of continuous failure
# is a real outage worth waking people for.
BLIND_AFTER = 600
FAILURES_BEFORE_WARNING = max(3, BLIND_AFTER // config.INTERVAL_SECONDS)

# Whole round timeout. Each request already has a 10s socket timeout, but that
# is per socket operation: a server that drips one byte at a time never trips
# it. Without a ceiling here a single hung request freezes the monitor forever,
# because run_repeating will not start a new round while the old one is alive.
# A healthy round takes ~0.5s, so 20s is generous and still under the interval.
ROUND_TIMEOUT = 20.0

# Politeness brake, defined in monitor.py next to the code that obeys it.
PUSHBACK_CODES = monitor.PUSHBACK_CODES
COOLDOWN = 600.0
_cooldown_until = 0.0

# One line a day proving the bot is alive. A watcher that is correctly silent
# for six months is indistinguishable from a dead one.
HEARTBEAT_EVERY = 24 * 3600

# A half-price category this event actually sells, named once so the messages
# can point at a concrete example instead of saying "meia". It is the one the
# group holds cards for. Between 20/09 and 28/09/2026 the site did not sell it
# and this read "Meia Idoso"; check config.KNOWN_CATEGORIES before changing it.
HALF = "Meia Estudante"

# The periodic job and /checar can land at the same instant. Without
# serializing, both read and write STATE interleaved and one erases the mark the
# other just wrote — which comes out as a repeated alert.
_LOCK = asyncio.Lock()

# Separate from _LOCK on purpose. A purchase takes tens of seconds and holds a
# browser; blocking the watcher on it would stop the other two nights from being
# checked at exactly the busiest moment. This one only stops a second purchase.
_BUY_LOCK = asyncio.Lock()


# --- state (survives a restart) ----------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    """Write to a temp file and rename: a crash mid-write must not leave a
    truncated state.json, which load_state would throw away — taking every
    record low and every "already alerted" mark with it."""
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.STATE_FILE)


STATE = load_state()

# Buying used to be a mode you switched into with /armar. It is not any more, and
# the flag that turned it on is dropped on sight: the person this bot exists for
# is at work with the phone in a pocket, so a bot that waits to be told "go" is a
# bot that watches the one fairly-priced listing of the month go by. It buys by
# itself, and the only two things that stop it are a human typing /desarmar and a
# failure that a human has to clear first.
if "armed" in STATE:
    log.info("dropping the old 'armed' flag — buying is the default now")
    STATE.pop("armed", None)
    STATE.pop("rearms", None)
    save_state(STATE)

# A /agio typed once during a test used to shadow MAX_MARKUP forever: editing
# the .env afterwards looked broken. We remember which .env value the override
# was made against, and if the .env changed since, the explicit edit wins.
if STATE.get("markup") is not None and STATE.get("markup_base") != str(config.MAX_MARKUP):
    log.info("MAX_MARKUP changed in the .env — dropping the /agio override")
    STATE.pop("markup", None)
    STATE.pop("markup_base", None)
    STATE["alerted"] = {}
    save_state(STATE)


def current_markup() -> Decimal:
    return Decimal(str(STATE.get("markup", config.MAX_MARKUP)))


def markup_overridden() -> bool:
    """Whether a /agio typed in the group is shadowing MAX_MARKUP from the .env."""
    return STATE.get("markup") is not None and current_markup() != config.MAX_MARKUP


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --- what has already been tried ---------------------------------------------
#
# Buying by itself every thirty seconds only works if it remembers. Without this
# the cheapest listing under the cap is picked again on the very next round, so a
# listing that can never be bought — sold, or with no seller proof — would be
# attempted twice a minute, all night, with a message each time. That is the one
# way an automatic buyer becomes worse than no buyer at all: the person it exists
# for is not reading, and by morning the real alert is buried under two hundred
# copies of the same failure.
#
# So each listing gets its own small budget, and the budget depends on what went
# wrong. Nothing here is global: a listing being hopeless says nothing about the
# next one, and the bot has to stay free to go after that one instantly.
STRIKES_OUT = 3       # tries per listing before it is left alone
STRIKE_MEMORY = 200   # listings remembered; the board turns over far slower


def strike(listing_id: str, final: bool) -> None:
    """Records a failed attempt on one listing.

    `final` is for the failures that are a permanent fact about that listing —
    somebody else bought it, or it has no seller proof. Those get no second try:
    the answer cannot change, so asking again is pure noise. Everything else
    (the site wobbled, the price disagreed) gets STRIKES_OUT tries, because those
    can come good on the next round.
    """
    strikes: dict[str, int] = STATE.setdefault("strikes", {})
    strikes[listing_id] = (
        STRIKES_OUT if final else int(strikes.get(listing_id, 0)) + 1
    )
    # Oldest first, because dicts keep insertion order. Bounded so that months of
    # running cannot grow state.json without limit.
    while len(strikes) > STRIKE_MEMORY:
        strikes.pop(next(iter(strikes)))


def struck_out(listing_id: str) -> bool:
    """Whether this listing used up its tries and should be left alone."""
    return int((STATE.get("strikes") or {}).get(listing_id, 0)) >= STRIKES_OUT


def buying_on() -> bool:
    """Whether the bot may spend money right now.

    On by default, and that is the whole point: it buys without being asked. Two
    things turn it off, both of them needing a person — /desarmar, and a failure
    no retry can fix (a broken .env, or a reservation already sitting on the
    account). A successful charge turns it off too: the site allows one open order
    per account, so the next attempt could only fail until that Pix is paid.
    """
    return not STATE.get("buying_off")


def stop_buying(why: str) -> None:
    """Turns off buying until a person turns it back on, and says why in the log."""
    STATE["buying_off"] = why
    save_state(STATE)
    log.info("buying is off: %s", why)


# Why it is not buying, in the words the group needs. Stored as a bare key rather
# than a sentence so that editing the wording later cannot strand an old
# state.json with a message nobody writes any more.
OFF_REASONS = {
    "pix": "gerei um Pix e ele está esperando pagamento",
    checkout.PENDING: "tem reserva ou pedido aberto nessa conta",
    checkout.SETUP: "falta alguma coisa no .env do servidor",
    "human": "você me mandou parar",
}


def off_reason() -> str:
    """Plain Portuguese for whatever turned buying off."""
    return OFF_REASONS.get(str(STATE.get("buying_off")), "parei por precaução")


# --- rules -------------------------------------------------------------------

def eligible(offers: list[Offer]) -> list[Offer]:
    """Only what you can actually prove at the gate (see CATEGORIES in .env)."""
    return [o for o in offers if o.category in config.CATEGORIES]


def markup_of(offer: Offer) -> Decimal | None:
    face = config.face_value(offer.sector)
    if face is None:
        return None
    return ((offer.price / face - 1) * 100).quantize(Decimal("1"))


def times_of(offer: Offer) -> str:
    """'3,1×' — how many times face value the listing is asking.

    This is what messages people read show, instead of a percentage. "+208%"
    costs the reader a mental step (208 over 100, so about three times over);
    "3,1×" is the same fact with the step already done. Comma, not dot: these
    are read in Portuguese.
    """
    face = config.face_value(offer.sector)
    if face is None:
        return "sem referência"
    return f"{offer.price / face:.1f}×".replace(".", ",")


def is_fair(offer: Offer, markup: Decimal) -> bool:
    """Is the listing at or under its sector's cap? A Camarote never is.

    No published face means no cap, and "no cap" must read as "cannot say it is
    fair", never as "anything goes".
    """
    cap = config.price_cap(offer.sector, markup)
    return cap is not None and offer.price <= cap


def _fee_note(o: Offer) -> str:
    """Where the price in the alert comes from.

    Replaces the old "N disponíveis": the rewritten API does not say how many of
    a listing exist, and it never mattered — you buy one seat, and a count of two
    changed no decision.

    What does need saying is this. The site shows the seller's asking price and
    adds 10% at the till, so every number in this bot is the asking price plus
    that fee — the money that actually leaves your bank. Without this line the
    alert says R$ 1.430 while the page says R$ 1.300, and a gap like that in a
    message about money reads as a bug and costs you the seconds you were saving.
    """
    return f"anúncio de {brl(o.listed)} + 10% de taxa"


def _half_warning(category: str) -> str:
    """Half-price with no proof at the gate is a lost ticket, not a discount.

    The one emoji left inside a message body. The others (📅 📍 🎫) were field
    labels — decoration standing in for a word — and they competed with the glyph
    at the top that says what kind of message this is. This one is not a label,
    it is a warning, and it is the difference between a seat and a refused entry.
    """
    if category == "Inteira":
        return ""
    return f"\n\n🎓 {html.escape(category)}: leve o comprovante, senão a entrada é negada."


def buy_intent(o: Offer) -> str:
    """The line that tells the reader whether they have to do anything.

    Deliberately about the round and not about this exact listing: the bot goes
    after the cheapest thing under the cap, and two sectors can clear the cap in
    the same round. "Vou atrás do mais barato" is true either way, where "vou
    atrás desse" would be a lie on the dearer of the two — and the ⚡ message
    that lands seconds later names the listing and its id.
    """
    if not buying_on():
        return (
            f"🔒 <b>Eu não estou comprando agora</b> — {off_reason()}. Se você "
            f"quiser esse, tem que ser na mão, pelo link."
        )
    if o.price > config.HARD_CEILING:
        return (
            f"🔒 Esse passa do meu limite absoluto de "
            f"{brl_round(config.HARD_CEILING)}, então eu não compro. Se valer a "
            f"pena pra você, é na mão."
        )
    if _BUY_LOCK.locked():
        return (
            "⏳ Estou no meio de outra compra, então esse eu não pego agora. Se "
            "valer a pena pra você, é na mão, pelo link."
        )
    return (
        "⚡ <b>Já estou indo atrás</b> — do mais barato que apareceu. Reservo e "
        "mando o Pix aqui em segundos. <b>Você não precisa fazer nada</b> além "
        "de pagar quando o código chegar."
    )


def alert_message(o: Offer) -> str:
    """The alert that matters. Says the price, and says who is acting on it.

    One line of context, and no more. The first version carried none, reasoning
    that a message which arrived had already cleared the filter. That collapses
    the moment MAX_MARKUP is set loose: "PREÇO JUSTO · R$ 4,178.90" with no
    reference reads as the bot asserting that R$ 4,178 is fair for a R$ 1,250
    ticket, and the message holds nothing to check it against. The 📉 alert
    carried this line from the start; the alert people actually act on needs it
    more, not less.

    The closing line used to be "fique logado no site para cair direto no
    checkout" — advice from when a human was the one racing. The bot buys by
    itself now, so this message's job changed: it is a heads-up, and what the
    reader needs from it is whether their phone is about to matter. buy_intent
    says that, and it is the only line here that can say "faça algo".
    """
    face = config.face_value(o.sector)
    cap = config.price_cap(o.sector, current_markup())
    context = (
        f"\n{times_of(o)} o valor original de {brl(face)} — eu compro "
        f"até {brl(cap)}."
        if face is not None
        else ""
    )
    # The venue is gone from here. It never changes, the startup message already
    # says it, and a line that is identical in every alert is a line the eye
    # learns to skip — taking the lines next to it with it.
    return (
        f"🎯 <b>ESSE É O PREÇO — {html.escape(o.sector)}</b>\n\n"
        f"<b>{brl(o.price)}</b> · {html.escape(o.category)}\n"
        f"{html.escape(o.session)} · {_fee_note(o)}{context}"
        f"{_half_warning(o.category)}\n\n"
        f"{config.buy_link(o.listing_id, o.session)}\n\n"
        f"{buy_intent(o)}"
    )


def record_message(o: Offer, previous: Decimal) -> str:
    """A new all-time low that is still nowhere near the target.

    The heading carries the whole fix. "MENOR PREÇO ATÉ AGORA" is true and still
    misleading: it is the shape of good news, so a listing at 4,7× face read as
    an opportunity when it was a robbery that got marginally cheaper. Someone
    seeing it asked, reasonably, whether the bot was supposed to send that at
    all. It was — a floor dropping is worth knowing — but not phrased as a win.

    So the heading says both halves at once: it fell, and it is not the target
    price. "Not the price" alone left open the price of what — and the bot already
    has a word for the number it waits for ("alvo"), used in the target block, in
    /agio and in /status. This was the one message about the gap that did not name
    the thing on the far side of it.
    The body then has to name the gap in reais, because "4,7× the original" only
    means something next to the number the bot is actually waiting for. And the
    link goes last, labelled as looking rather than buying: this message exists
    to inform, and the one that exists to be acted on is 🎯.
    """
    face = config.face_value(o.sector)
    cap = config.price_cap(o.sector, current_markup())
    if face is None:
        gap = ""
    else:
        gap = (
            f"\n\nAinda está {times_of(o)} o valor original de {brl(face)}, e eu "
            f"só considero justo até {brl(cap)}.\n"
            f"<b>Nada bateu o alvo ainda</b>, então eu não vou comprar esse. "
            f"Aviso e compro na hora que bater."
        )
    url = html.escape(config.buy_link(o.listing_id, o.session), quote=True)
    return (
        f"📉 <b>Caiu de preço — mas ainda não é o preço alvo</b>\n\n"
        f"{html.escape(o.sector)} · <b>{brl(o.price)}</b> · "
        f"{html.escape(o.category)}\n"
        f"{html.escape(o.session)} · {_fee_note(o)}\n"
        f"Era {brl(previous)}. É a mínima que já vi para essa noite."
        f"{gap}"
        f"{_half_warning(o.category)}\n\n"
        f'<a href="{url}">ver o anúncio</a> — sem pressa, ainda não é o preço alvo.'
    )


# --- buying ------------------------------------------------------------------

def buyable(candidates: list[Offer], markup: Decimal) -> Offer | None:
    """The cheapest listing we would actually spend money on, or None.

    One gate stricter than is_fair: the flat HARD_CEILING. is_fair decides
    whether to speak, which costs a message; this decides whether to pay, which
    costs money that does not come back — so it also has to clear a number that
    cannot be reasoned wrong by a renamed sector or a misparsed price.

    Listings that used up their tries are out (see `strike`). That is what keeps
    an automatic buyer from picking the same hopeless listing every thirty
    seconds: the cheapest one on the board is not the cheapest one it can buy.
    """
    ok = [
        o for o in candidates
        if is_fair(o, markup)
        and o.price <= config.HARD_CEILING
        and not struck_out(o.listing_id)
    ]
    return min(ok, key=lambda o: o.price) if ok else None


def id_line(o: Offer) -> str:
    """The listing's own id, tap-to-copy.

    In <code> so Telegram copies it on a tap. It is the only string that survives
    every other one: the link can 404 once the listing is gone, the price changes,
    the sector name is the site's to rename — the id is what support, the order
    page and the site's own logs agree on. Goes on every message where money moved
    or was about to, so that whatever needs sorting out afterwards can be.
    """
    return f"\n🆔 <code>{html.escape(o.listing_id)}</code>"


def ticket_block(o: Offer) -> str:
    """What is being bought, in full. The same lines everywhere money is at stake.

    "mostrar tudo" is a real requirement and not decoration: the group is several
    people and the bot buys on its own, so a message saying money is moving cannot
    make anybody scroll up to find out which night it is about. So every purchase
    message carries sector, category, night, the price with the fee spelled out,
    the two reference numbers that say whether it is a good deal, and the id.
    """
    face = config.face_value(o.sector)
    cap = config.price_cap(o.sector, current_markup())
    reference = (
        f"\n{times_of(o)} o valor original de {brl(face)} — "
        f"meu teto pra esse setor é {brl(cap)}"
        if face is not None
        else ""
    )
    return (
        f"<b>{brl(o.price)}</b> · {html.escape(o.sector)} · "
        f"{html.escape(o.category)}\n"
        f"{html.escape(o.session)} · {_fee_note(o)}{reference}"
        f"{id_line(o)}"
    )


# How often the step list is redrawn while a purchase runs. One second: the whole
# flow takes three to five, so this shows every step without spending the group's
# rate limit on a message nobody is reading yet.
PAINT_EVERY = 1.0

# The four marks. ⏳ is the only one that moves, which is what makes the list
# readable at a glance — the eye finds the hourglass, not a line of text.
DONE, NOW, WAITING, BROKE = "✅", "⏳", "▫️", "❌"


def progress_message(o: Offer, progress: checkout.Progress) -> str:
    """The purchase mid-flight: what it is buying, and which step it is on.

    All four steps listed rather than just the current one, because "reservo o
    ingresso" on its own does not say whether a Pix is still coming. And it is one
    message edited in place, not four messages: the group needs to watch this
    happen, not be notified four times in five seconds.
    """
    lines = []
    for at, step in enumerate(checkout.STEPS):
        if at < progress.done:
            lines.append(f"{DONE} {step}")
        elif at == progress.done:
            mark = BROKE if progress.failed else NOW
            note = f" — {html.escape(progress.note)}" if progress.note else ""
            lines.append(f"{mark} <b>{step}</b>{note}")
        else:
            lines.append(f"{WAITING} {step}")
    if progress.failed:
        tail = "Parei aqui. Explico na próxima mensagem."
    elif progress.done >= len(checkout.STEPS):
        tail = "Pronto — o código vem agora."
    else:
        tail = "Não faça nada ainda. O código do Pix vem aqui."
    steps = "\n".join(lines)
    return (
        f"⚡ <b>ACHEI — estou indo atrás desse</b>\n\n"
        f"{ticket_block(o)}{_half_warning(o.category)}\n\n"
        f"{steps}\n\n"
        f"{tail}"
    )


def pix_message(o: Offer, charge: checkout.PixCharge) -> str:
    """The message that matters most in the whole bot: the ticket is held.

    Says "reservado" and not "comprado" on purpose. The site's own words are
    "Pague R$ X via PIX para garantir seu ingresso" next to a ten-minute
    countdown — the hold is real, and it is also temporary. Someone reading
    "comprado" would put the phone down.
    """
    if charge.total != o.price:
        money = (
            f"<b>{brl(charge.total)}</b> a pagar — o anúncio dizia "
            f"{brl(o.price)}. <b>Confira antes de pagar.</b>"
        )
    else:
        money = f"<b>{brl(charge.total)}</b> a pagar"
    prazo = (
        f"\nExpira em <b>{html.escape(charge.expires_in)}</b> — passando disso, "
        f"a reserva cai e o ingresso volta pra venda."
        if charge.expires_in
        else "\nA cobrança tem prazo curto (uns 10 min). Pague agora."
    )
    if charge.intact:
        integrity = "O código passou na verificação (checksum do próprio Pix)."
    else:
        integrity = (
            "⚠️ O código <b>não</b> passou na verificação — pode ter vindo "
            "cortado. Se o banco recusar, pague direto no site."
        )
    # The order page goes in because it is the way out of every problem this
    # message can have: código recusado, valor estranho, QR em vez de copia-e-cola.
    fallback = f"\n\n{charge.url}" if charge.url else ""
    closing = (
        "Abaixo: o QR e o código copia-e-cola. Toque no código para copiar."
        if charge.qr
        else "O código vem na próxima mensagem, sozinho. "
        "Toque nele para copiar e pague no app do banco."
    )
    return (
        f"💠 <b>PIX GERADO — o ingresso está reservado</b>\n\n"
        f"{html.escape(o.sector)} · {html.escape(o.category)} · "
        f"{html.escape(o.session)}"
        f"{id_line(o)}\n"
        f"{money}{prazo}\n\n"
        f"Levei {charge.seconds:.0f}s da oferta até aqui. {integrity}"
        f"{_half_warning(o.category)}{fallback}\n\n"
        f"{closing}"
    )


# What each kind of failure means for the reader: the heading carrying its
# severity, what happened to the money, and whether the bot should keep hunting.
#
# This table is the fix for one message doing five jobs. "Me desarmei — /armar
# pra tentar de novo" is correct advice for a lost race, useless advice for a
# missing .env key, and actively wrong when the account is already holding a
# reservation: retrying there hits the one-hold-per-account rule, and the real
# task is to go and finish the order that exists.
FAILURES = {
    checkout.GONE: (
        "🏃 <b>Perdi essa — alguém foi mais rápido</b>",
        "Nada foi pago e nada ficou reservado.",
        True,
    ),
    checkout.TRANSIENT: (
        "📡 <b>O site não respondeu</b>",
        "Tentei várias vezes antes de desistir dessa. Nada foi pago e nada "
        "ficou reservado.",
        True,
    ),
    checkout.PRICE: (
        "🛡️ <b>Travei por preço — e isso é o certo</b>",
        "Nada foi pago e nada ficou reservado. O preço mudou entre o alerta e a "
        "hora de reservar, e eu não pago mais do que o combinado.",
        True,
    ),
    checkout.SETUP: (
        "🔧 <b>Preciso de você — isso não se resolve tentando de novo</b>",
        "Nada foi pago e nada ficou reservado. Conserte o que está acima. Se o "
        "ingresso ainda estiver no ar, vale comprar na mão agora — link abaixo.",
        False,
    ),
    checkout.PENDING: (
        "🚨 <b>Tem algo seu no site AGORA</b>",
        "<b>Eu não paguei nada</b> — mas essa conta está com um ingresso "
        "reservado ou um pedido aberto, e isso tem prazo. Resolva pelo link "
        "acima antes de qualquer outra coisa.",
        False,
    ),
    checkout.UNVERIFIED: (
        "🕵️ <b>Anúncio sem comprovante do vendedor</b>",
        "Nada foi pago e nada ficou reservado, e continuo caçando os outros. O "
        "site não mostra comprovante de compra nesse anúncio, e sem ele ninguém "
        "consegue conferir de quem é o ingresso — nem eu, nem você. Dá pra abrir "
        "no link abaixo e olhar, mas eu não gasto dinheiro num ingresso que não "
        "dá pra checar.",
        True,
    ),
}

# How many tries one listing gets before the bot leaves it alone lives in
# STRIKES_OUT, next to `strike`. There is deliberately no global counter any more:
# a listing being hopeless says nothing about the next listing, and stopping the
# whole buyer after three unrelated lost races is how an automatic buyer ends up
# asleep exactly when it is needed.


def buy_failed_message(o: Offer, why: BaseException, still_buying: bool) -> str:
    """Why it stopped, what it means for the money, and whose turn it is.

    In that order, every time, because that is the order the reader needs: the
    heading says how bad it is, the body says what happened, the last line says
    what to do. A failure with no next step is the one thing this must never be.

    `still_buying` is the decision already taken by fire(), not a guess made here —
    "sigo comprando sozinho" has to be true when it is written.
    """
    kind = getattr(why, "kind", checkout.TRANSIENT)
    heading, body, _ = FAILURES.get(kind, FAILURES[checkout.TRANSIENT])
    if still_buying:
        tail = (
            "<b>Sigo comprando sozinho</b>, de olho nas três noites — se aparecer "
            "outro no preço, vou atrás na hora, sem você precisar fazer nada. "
            "Esse anúncio eu risquei da lista pra não ficar repetindo."
        )
    else:
        tail = (
            "<b>Parei de comprar</b> até isso ser resolvido — insistir a cada "
            "30 s só viraria spam e não consertaria nada. /armar quando estiver "
            "resolvido."
        )
    # The pending link is the entire point of the message it belongs to, so it
    # goes above the advice instead of at the bottom with the shopping link.
    pending = getattr(why, "url", "")
    pending_line = f"\n\n👉 {pending}" if pending else ""
    # Where the ticket still might be. Pointless once it is gone, and a trap on a
    # PENDING failure, where the link you want is the order and not the listing.
    # UNVERIFIED gets it too, and on purpose: the refusal is a judgement about
    # that listing, so the reader should be able to open it and judge for
    # themselves instead of taking the bot's word for it.
    chase = (
        f"\n\n{config.buy_link(o.listing_id, o.session)}"
        if kind in (
            checkout.GONE,
            checkout.TRANSIENT,
            checkout.SETUP,
            checkout.UNVERIFIED,
        )
        else ""
    )
    return (
        f"{heading}\n\n"
        f"{ticket_block(o)}\n\n"
        f"<b>O que aconteceu:</b> {html.escape(str(why))}"
        f"{pending_line}\n\n"
        f"{body} {tail}{chase}"
    )


def rearm_after(why: BaseException) -> bool:
    """Whether the bot should carry on buying by itself after this failure.

    Yes for everything that said nothing about our ability to buy — the listing
    was taken, the site blinked, the price moved, the seller had no proof. In all
    of those the next round has a clean shot at a different listing, and the
    listing that just failed is already struck out, so there is no loop to fear.

    No only where a person is genuinely in the way: a broken .env, or a
    reservation already sitting on the account. Carrying on there would fail
    identically every thirty seconds and bury the message that explains it.
    """
    kind = getattr(why, "kind", checkout.TRANSIENT)
    return FAILURES.get(kind, FAILURES[checkout.TRANSIENT])[2]


async def paint(board, o: Offer, progress: checkout.Progress) -> None:
    """Redraws the step list while the purchase runs, until cancelled.

    Polls instead of being called back from the worker thread, which keeps the
    thread free of anything to do with Telegram. Only redraws when something
    actually changed: a purchase that finishes in three seconds costs three edits,
    and one that sits in a retry costs one edit saying so.
    """
    last = None
    while True:
        await asyncio.sleep(PAINT_EVERY)
        now = (progress.done, progress.note, progress.failed)
        if now == last:
            continue
        last = now
        try:
            await board.edit_text(
                progress_message(o, progress),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001 — a display must never break a purchase
            log.warning("could not redraw the progress message", exc_info=True)


async def draw_board(context: ContextTypes.DEFAULT_TYPE, o: Offer, progress):
    """Posts the live step list, or None if Telegram would not take it.

    Awaited rather than fired and forgotten, because this message is the one that
    gets edited: there is nothing to draw on until it exists. Never raises — a
    purchase must not be lost because the display failed.
    """
    try:
        return await context.bot.send_message(
            config.CHAT_ID,
            progress_message(o, progress),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 — never let the display stop the purchase
        log.warning("could not post the progress message", exc_info=True)
        return None


async def close_board(board, o: Offer, progress: checkout.Progress) -> None:
    """One last redraw, leaving the list showing where the flow stopped.

    Needed because the painter loop is very likely asleep when the flow ends, so
    without this the last step drawn is one second stale — which on a failure
    means the list never shows the ❌ at all.
    """
    if board is None:
        return
    try:
        await board.edit_text(
            progress_message(o, progress),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        log.warning("could not finish the progress message", exc_info=True)


async def fire(context: ContextTypes.DEFAULT_TYPE, o: Offer) -> None:
    """Drives the checkout to a Pix charge and drops the code in the group.

    Nobody asked for this to happen, which is the entire design: it runs off the
    watcher, by itself, seconds after a fair listing appears. So the two things
    that matter are that it can never run twice at once (_BUY_LOCK) and that it
    can never come back to the same listing forever (`strike`).

    What it does NOT do is stop buying because one attempt failed. Losing a race
    is the normal outcome of a race, and a bot that steps back after one loss is
    asleep for the listing that shows up forty seconds later.
    """
    if _BUY_LOCK.locked():
        log.info("a purchase is already running — ignoring this one")
        return
    async with _BUY_LOCK:
        STATE["last_buy_at"] = time.time()
        save_state(STATE)

        progress = checkout.Progress()
        board = await draw_board(context, o, progress)
        painter = (
            asyncio.create_task(paint(board, o, progress))
            if board is not None
            else None
        )
        charge, failure = None, None
        try:
            charge = await checkout.buy(o, progress)
        except Exception as exc:  # noqa: BLE001 — every failure has to reach the group
            log.exception("purchase failed")
            failure = exc
            progress.failed = True
        if painter is not None:
            painter.cancel()
        await close_board(board, o, progress)

        if failure is not None:
            # Decided before the message is written, so that what the message says
            # is what is actually on disk.
            kind = getattr(failure, "kind", checkout.TRANSIENT)
            # GONE and UNVERIFIED are facts about the listing that will not change:
            # it is sold, or it has no seller proof. One try is all they get.
            strike(o.listing_id, final=kind in (checkout.GONE, checkout.UNVERIFIED))
            keep = rearm_after(failure)
            if not keep:
                stop_buying(kind)
            save_state(STATE)
            await notify(context, buy_failed_message(o, failure, keep))
            return

        # A charge exists, so there is money owed on this account. The site allows
        # one open order at a time, which means every further attempt could only
        # fail until this Pix is paid or expires — and a second charge is the one
        # mistake an automatic buyer must never make. So it steps back here and
        # says so, and /armar is how a person says "that one is settled, keep
        # going". The listing gets a strike too: it is ours now, not a target.
        strike(o.listing_id, final=True)
        stop_buying("pix")
        await notify(context, pix_message(o, charge))
        # The QR before the code, so the copy-and-paste block is the last thing on
        # screen — it is the one that actually gets paid, and on a phone the last
        # message is the one under your thumb.
        if charge.qr:
            try:
                await context.bot.send_photo(
                    config.CHAT_ID,
                    charge.qr,
                    caption="Ou aponte a câmera do app do banco aqui.",
                )
            except Exception:  # noqa: BLE001 — the code below is what matters
                log.warning("could not send the QR", exc_info=True)
        # Alone in its own message so that one tap copies the whole code and
        # nothing else.
        await context.bot.send_message(
            config.CHAT_ID,
            f"<code>{html.escape(charge.code)}</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# --- collecting --------------------------------------------------------------

async def collect() -> tuple[list[Offer], list[str], set[str], bool]:
    """Fetches the three sessions in parallel.

    Returns (offers, errors, read, pushback). `read` is the set of sessions that
    answered — including the ones that answered empty, which is why it cannot be
    derived from `offers`: a sold-out session still counts as read. `pushback` is
    True when the site answered with a slow-down code.
    """
    gather = asyncio.gather(
        *(
            asyncio.to_thread(monitor.fetch, config.SESSIONS[s], s)
            for s in config.SESSIONS
        ),
        return_exceptions=True,
    )
    try:
        results = await asyncio.wait_for(gather, timeout=ROUND_TIMEOUT)
    except TimeoutError:
        return [], [f"a rodada passou de {ROUND_TIMEOUT:.0f}s e foi abortada"], set(), False

    offers: list[Offer] = []
    errors: list[str] = []
    read: set[str] = set()
    pushback = False
    for session, r in zip(config.SESSIONS, results):
        if isinstance(r, BaseException):
            errors.append(f"{session}: {type(r).__name__}: {r}")
            if getattr(r, "code", None) in PUSHBACK_CODES:
                pushback = True
        else:
            read.add(session)
            offers.extend(r)
    return sorted(offers, key=lambda o: o.price), errors, read, pushback


# --- checking ----------------------------------------------------------------

async def check(context: ContextTypes.DEFAULT_TYPE, forced: bool = False) -> None:
    async with _LOCK:
        await _check(context, forced)


async def _check(context: ContextTypes.DEFAULT_TYPE, forced: bool) -> None:
    global _cooldown_until
    if STATE.get("paused") and not forced:
        return
    # A manual /checar is one request, not hammering, so it may pass.
    if not forced and time.monotonic() < _cooldown_until:
        log.info("cooling down for %.0fs more", _cooldown_until - time.monotonic())
        return

    offers, errors, read, pushback = await collect()
    if pushback:
        _cooldown_until = time.monotonic() + COOLDOWN
        log.warning("site pushed back — pausing rounds for %.0fs", COOLDOWN)
        if not STATE.get("warned_pushback"):
            await notify(
                context,
                "🐢 <b>Indo mais devagar por um tempo</b>\n\n"
                "O site pediu pra eu diminuir o ritmo, então vou espaçar as "
                f"checagens pelos próximos {COOLDOWN / 60:.0f} min.\n\n"
                "Continuo vigiando as três noites. Insistir agora é o jeito "
                "mais rápido de tomar bloqueio de IP, e aí eu ficaria cego de "
                "vez — melhor chegar atrasado do que não chegar.",
            )
            STATE["warned_pushback"] = True
            save_state(STATE)
    if errors:
        # Any failed session counts as a failed round. If only one of the three
        # were broken forever, treating the round as a success would reset the
        # counter and the bot would go blind on that night without saying so.
        log.warning("sessions with errors: %s", " | ".join(errors))
        await record_failure(context, errors[0], len(errors))
        if len(errors) == len(config.SESSIONS):
            return
    else:
        await record_success(context)

    candidates = eligible(offers)
    if not candidates:
        log.info("no eligible offer (%d offers in total)", len(offers))

    # The heartbeat runs even with nothing on sale — a night that sold out is
    # exactly when you most want proof the bot is still watching.
    changed = await heartbeat(context, candidates, read)
    if candidates:
        changed |= await cap_rule(context, candidates, read)
        changed |= await record_rule(context, candidates)
        if buying_on():
            target = buyable(candidates, current_markup())
            if target is not None:
                # Its own task: the purchase takes tens of seconds, and the
                # watcher must keep watching while it runs.
                context.application.create_task(fire(context, target))
                changed = True
    if changed:
        save_state(STATE)


async def worth_alerting(o: Offer) -> bool:
    """Whether this listing survives a look at the listing itself.

    The public list carries only a price and an id, and a price alone cannot tell
    a bargain from bait — see checkout.inspect for the R$ 650 Pista that was
    locked in the same second it was alerted. Records a strike on whatever it
    turns down so the same listing does not cost a read every thirty seconds:
    final for a missing seller proof, which will never appear, soft for a lock,
    which can come undone.

    True when there is nothing to check with. An unchecked alert beats no alert.
    """
    if not checkout.can_check():
        return True
    wrong = await checkout.inspect(o.listing_id)
    if not wrong:
        return True
    log.info("not alerting %s: %s", o.listing_id, wrong)
    strike(o.listing_id, final="comprovante" in wrong)
    return False


async def cap_rule(
    context: ContextTypes.DEFAULT_TYPE, candidates: list[Offer], read: set[str]
) -> bool:
    """Alerts when a sector's price drops to that sector's fair cap.

    Every alert is checked against the listing itself first, by worth_alerting.
    An alert nobody can act on is worse than no alert: it spends the one thing
    this bot has, which is the reader believing the next one.
    """
    alerted: dict[str, str] = STATE.setdefault("alerted", {})
    markup = current_markup()
    changed = False

    under_cap = set()
    for o in candidates:
        if not is_fair(o, markup):
            continue
        under_cap.add(o.key)
        already = _dec(alerted.get(o.key))
        if already is not None and o.price >= already:
            continue  # we already warned at this price or a cheaper one
        if struck_out(o.listing_id):
            continue  # looked at before, and found wanting
        if not await worth_alerting(o):
            changed = True  # a strike was written down
            continue
        # Send first, mark after: if Telegram times out nothing gets marked and
        # the next check tries again. The other way round, a network blip would
        # swallow the one alert we waited months to receive.
        await notify(context, alert_message(o))
        alerted[o.key] = str(o.price)
        changed = True

    # Left the cap (went up or sold out): forget it, so we can alert again.
    # Only for the sessions we managed to read — a session that timed out did
    # not "leave the cap", and clearing it would repeat the same alert on return.
    for key in [
        k for k in alerted
        if k not in under_cap and k.split("||", 1)[0] in read
    ]:
        del alerted[key]
        changed = True
    return changed


async def record_rule(
    context: ContextTypes.DEFAULT_TYPE, candidates: list[Offer]
) -> bool:
    """Alerts on every new all-time low, per session.

    Without this the bot could stay quiet for months and you would not know
    whether it works or died.

    It stays quiet when the new low is itself within the cap, and that is not an
    optimisation — it is a correctness fix. record_message says, in every line,
    that the price fell but is "ainda não é o preço alvo". On 20/09/2026 a Pista
    at R$ 715 came in under the cap and the group got both messages one second
    apart: "🎯 ESSE É O PREÇO" and, right below it, "Nada bateu o alvo ainda".
    Two messages about the same listing, one of them false. cap_rule has already
    said the stronger and truer thing, so the low is recorded without a word.
    """
    records: dict[str, str] = STATE.setdefault("record_lows", {})
    markup = current_markup()
    changed = False

    for session in config.SESSIONS:
        of_session = [o for o in candidates if o.session == session]
        if not of_session:
            continue
        cheapest = min(of_session, key=lambda o: o.price)
        low = _dec(records.get(session))
        if low is not None and cheapest.price >= low:
            continue
        if low is not None and not is_fair(cheapest, markup):
            await notify(context, record_message(cheapest, low))
        records[session] = str(cheapest.price)
        changed = True
    return changed


async def heartbeat(
    context: ContextTypes.DEFAULT_TYPE, candidates: list[Offer], read: set[str]
) -> bool:
    """Once a day, one line per night, so silence stays distinguishable from death.

    It also carries whether buying is switched on, and that is not filler. The bot
    buys on its own, so "nada bateu o alvo" and "eu parei de comprar" produce the
    exact same silence for days on end — and this message is the only one anybody
    is guaranteed to read in between.
    """
    now = time.time()
    last = STATE.get("last_heartbeat")
    if last is None:
        STATE["last_heartbeat"] = now  # first round: the startup message covers it
        return True
    if now - last < HEARTBEAT_EVERY or len(read) < len(config.SESSIONS):
        return False

    # best_deal(), not min(price): everywhere else in the bot the best listing is
    # the one closest to its own face value, and a heartbeat that ranked by reais
    # would keep naming Arquibancada — it has the lowest face, so it wins on
    # price while being the worst buy of the lot.
    lines = ["💓 <b>Estou aqui, vigiando</b>", "", "Nada bateu o alvo nas últimas 24 h."]
    rows = []
    for session in config.SESSIONS:
        best = best_deal([o for o in candidates if o.session == session])
        rows.append(
            f"{short_session(session)}  "
            + (
                f"{brl_round(best.price):>9}  {best.sector} ({times_of(best)})"
                if best is not None
                else "     nada à venda"
            )
        )
    lines.append(f"<pre>{html.escape(chr(10).join(rows))}</pre>")
    lines.append("")
    if buying_on():
        lines.append(
            "🎯 Comprando sozinho: se bater o alvo, eu reservo e mando o Pix aqui."
        )
    else:
        lines.append(
            f"🔒 <b>Não estou comprando</b> — {off_reason()}. /armar pra eu voltar."
        )
    lines.append("/status para ver setor por setor, com link.")
    await notify(context, "\n".join(lines))
    STATE["last_heartbeat"] = now
    return True


async def record_failure(
    context: ContextTypes.DEFAULT_TYPE, reason: str, how_many: int
) -> None:
    STATE["failures"] = STATE.get("failures", 0) + 1
    if STATE["failures"] >= FAILURES_BEFORE_WARNING and not STATE.get("warned_failure"):
        minutes = STATE["failures"] * config.INTERVAL_SECONDS / 60
        # A 404 is the one failure that needs no waiting to diagnose: the URL is
        # wrong, and no amount of retrying fixes a wrong URL. Saying so beats the
        # generic "if it lasts an hour, maybe the site changed" — the site HAS
        # changed, and the fix is a config edit, not patience.
        moved = (
            "\n\n<b>Isso é 404: o endereço mudou.</b> Não é falha de rede e não "
            "vai voltar sozinho — alguém precisa atualizar a URL do evento no "
            "config.py."
            if "404" in reason
            else (
                "\n\nSigo tentando sozinho a cada rodada, e aviso quando voltar. "
                "Se passar de uma hora, provavelmente o site mudou e o parser "
                "precisa de ajuste."
            )
        )
        await notify(
            context,
            f"⚠️ <b>Estou cego</b>\n\n"
            f"Não consigo ler {how_many} de {len(config.SESSIONS)} noites há "
            f"~{minutes:.0f} min. <b>Não confie no meu silêncio agora</b>: o que "
            "eu não vejo eu também não compro, então pode aparecer ingresso no "
            "preço e eu não reservar.\n\n"
            f"Último erro: <code>{html.escape(reason)}</code>"
            f"{moved}",
        )
        STATE["warned_failure"] = True
    save_state(STATE)


async def record_success(context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.get("warned_failure"):
        back = (
            "Voltei a comprar sozinho também."
            if buying_on()
            else f"Mas não estou comprando — {off_reason()}."
        )
        await notify(
            context,
            "✅ <b>Voltei a ver o site</b>\n\n"
            f"Estava cego, agora estou lendo as três noites de novo. {back}\n\n"
            "Não sei o que passou enquanto eu estava cego — /status pra ver como "
            "está agora.",
        )
    if STATE.get("failures") or STATE.get("warned_failure"):
        STATE["failures"] = 0
        STATE["warned_failure"] = False
        save_state(STATE)
    # Armed again for the next time the site pushes back. Without this reset the
    # 🐢 message would be sent once in the bot's lifetime and never again.
    if STATE.get("warned_pushback"):
        STATE["warned_pushback"] = False
        save_state(STATE)


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Every message the group gets goes through here, always as HTML.

    HTML for one reason: a heading has to look like a heading. Before this the
    alerts leaned on ALL CAPS, which reads as shouting rather than as structure,
    and left the price — the thing you actually decide on — in the same weight as
    the rest of the body.
    """
    await context.bot.send_message(
        config.CHAT_ID, text, disable_web_page_preview=True,
        parse_mode=ParseMode.HTML,
    )


# Whether the last keep-alive round found the login broken. Module state and not
# STATE on purpose: it exists only to say each piece of news once, and after a
# restart the news is worth repeating.
_login_broken = False


async def keep_login(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keeps the site session logged in, and says so when it cannot.

    The session staying warm is the cheap half. The half that matters is this
    running while nothing is happening: a password change or a logout-everywhere
    would otherwise surface at the one moment the bot is racing somebody for a
    ticket, in a message that reads like a lost race. Here it surfaces at once,
    with ten minutes' notice at worst.

    Said once per change of state, never once per round: a broken login repeated
    every ten minutes is spam that buries the alert it is warning about.
    """
    global _login_broken
    if not checkout.can_check():
        return  # no credentials at all is a setup choice, not a broken login
    wrong = await checkout.keep_alive()
    if wrong and not _login_broken:
        _login_broken = True
        log.error("the site login is not working: %s", wrong)
        await notify(
            context,
            "🔑 <b>Não estou conseguindo entrar na sua conta do site</b>\n\n"
            f"<b>O que o site diz:</b> {html.escape(wrong)}\n\n"
            "Sigo vigiando e aviso os preços normalmente, mas <b>não vou "
            "conseguir reservar nada</b> até isso voltar. Se a senha mudou, "
            f"troque o <code>PASS</code>: {html.escape(config.env_hint())}.",
        )
    elif not wrong and _login_broken:
        _login_broken = False
        log.info("the site login is working again")
        await notify(
            context,
            "🔑 <b>Voltei a entrar na sua conta</b> — se aparecer um ingresso no "
            "preço, eu reservo.",
        )


async def reply(update: Update, text: str) -> None:
    """A one-shot reply, always HTML.

    Exists so that adding a command cannot quietly opt out of the formatting:
    with reply_text called directly, a missing parse_mode does not fail, it just
    prints the tags at the reader.
    """
    await update.message.reply_text(
        text, disable_web_page_preview=True, parse_mode=ParseMode.HTML
    )


async def reply_long(
    update: Update, text: str, limit: int = 3800, parse_mode: str | None = None
) -> None:
    """Replies in several messages: Telegram cuts off at 4096 chars.

    /status grows with the stock, so this is a question of when, not if.

    Splitting on line boundaries is what makes this safe to use with HTML: every
    tag /status emits opens and closes inside one line, so no chunk can ever end
    mid-tag. Splitting at a raw character count would eventually cut an <a href>
    in half and Telegram would reject that whole chunk as a bad entity.
    """
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            await update.message.reply_text(
                chunk, disable_web_page_preview=True, parse_mode=parse_mode
            )
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        await update.message.reply_text(
            chunk, disable_web_page_preview=True, parse_mode=parse_mode
        )


def target_block(markup: Decimal) -> str:
    """What the bot is hunting: the cap of every sector, aligned.

    One function because three messages have to say this — /status, the startup
    line and /agio — and they used to each phrase it their own way. One of those
    phrasings named Pista alone, which read as "the other three sectors are not
    being watched" even though every one of them has always had its own cap.

    <pre> rather than per-line <code>: this block has no links in it, so it can
    be monospaced without giving anything up, and monospace is what makes the
    prices line up into a column you can scan instead of read.

    One column, not two. Half-price listings answer to this same cap — the cap
    prices the seat, and a half ticket is the same seat. See config.face_value().
    """
    width = max(len(s) for s in config.FACE_VALUE)
    body = "\n".join(
        f"{sector:<{width}}  {brl_round(config.price_cap(sector, markup)):>9}"
        for sector in config.FACE_VALUE
    )
    return f"<pre>{html.escape(body)}</pre>{markup_note()}"


def markup_note() -> str:
    """Where the ágio came from, said out loud, but only when it is not the .env.

    This line exists because of a real morning of confusion. The .env said
    MAX_MARKUP=36, which on Pista is R$ 1.700, and the group was getting alerts up
    to R$ 1.875 — which is 50%. Both numbers were correct: a `/agio 50` typed once
    during a test lives in state.json and outlives every restart, because the
    override is only dropped when the .env value it was made against *changes*,
    and the .env had said 36 the whole time. So editing the .env to the value it
    already had did nothing, forever, in silence.

    Keeping the override is right — /agio exists so the target can change without
    a deploy. What was wrong is that nothing anywhere said which of the two
    numbers was in force. Goes next to the target block, so it appears in every
    message that shows a target: boot, /status and /agio.
    """
    if not markup_overridden():
        return ""
    return (
        f"\n<i>Esses valores são de ágio {current_markup()}%, que alguém definiu "
        f"com /agio. O .env do servidor diz {config.MAX_MARKUP}% — mandar "
        f"<code>/agio env</code> volta pro .env.</i>"
    )


# --- commands ----------------------------------------------------------------

def best_deal(offers: list[Offer]) -> Offer | None:
    """The best offer of a group — cheapest RELATIVE TO FACE, not in reais.

    Sorting by price is actively misleading here. On 31/10 a Cadeira Superior
    meia sat at R$ 2,970 (+203%) while a Pista went for R$ 4,178.90 (+234%) and
    a Cadeira Superior inteira for R$ 5,059.99 (+416%): the cheapest listing is
    not the best buy, and the most expensive one is not the worst. Camarotes are
    skipped — no published face means no markup, so they can never alert and
    only add noise.
    """
    priced = [(markup_of(o), o) for o in offers]
    ranked = [(m, o) for m, o in priced if m is not None]
    if not ranked:
        return None
    return min(ranked, key=lambda pair: pair[0])[1]


def short_session(session: str) -> str:
    """'28/10/2026 (quarta)' -> '28/10 qua'"""
    date, _, weekday = session.partition(" (")
    return f"{date[:5]} {weekday.rstrip(')')[:3]}"


def label_of(o: Offer) -> str:
    """'Pista' or 'Cadeira Superior meia'."""
    return o.sector if o.category == "Inteira" else f"{o.sector} meia"


def brl_round(value: Decimal) -> str:
    """'R$ 3.850' — reais only, no cents.

    /status shows up to twelve prices at once and they are compared against each
    other, not paid. At four thousand reais the cents are two characters of noise
    per line that never change a decision. Alerts keep the cents: there you are
    about to spend the number.
    """
    return "R$ " + f"{value:,.0f}".replace(",", ".")


def offer_line(o: Offer) -> str:
    """One listing as a single tappable line.

    The raw buy link is ~110 characters of percent-encoded query string. /status
    now carries one per sector per night, and twelve raw URLs bury the twelve
    prices they belong to — the version before this one was mostly URL by volume.
    As an anchor the same line fits on one row and reads as a price list.

    Everything interpolated is escaped: sector names come from the site, and a
    stray & in a URL is enough for Telegram to reject the entire message as a
    malformed entity, which would take the whole status down rather than one row.
    """
    url = html.escape(config.buy_link(o.listing_id, o.session), quote=True)
    gate = " 🎓" if o.category != "Inteira" else ""
    return (
        f"<b>{html.escape(o.sector)}</b> · {brl_round(o.price)} · "
        f"{times_of(o)} · anúncio {brl_round(o.listed)}{gate} · <a href=\"{url}\">comprar</a>"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The overview: night by night, the best of every sector, each with a link.

    Night first and sector second, because that is the order the decision is made
    in — you pick a date you can travel on, then the best seat you can afford for
    it. Two earlier versions got this wrong. The first ranked the market as a
    whole and printed Pista three times, once per night, without ever saying what
    an Arquibancada was going for. The second bolted a by-sector block on top of
    that, covering the three nights at once: it answered "where is the market"
    but not "what do I buy for the 30th", and it carried no links at all, so the
    one line worth acting on was the one line you could not act on.

    Now every row is a thing you can buy and every row has its own link.

    The target block above them lists all four sectors. The bot has always
    watched all four, each against its own cap — is_fair() reads
    price_cap(o.sector) — but a status naming one target read as if the other
    three were being ignored, which is a worse bug than the real one would be.
    """
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    offers, errors, *_ = await collect()
    if len(errors) == len(config.SESSIONS):
        await reply(
            update,
            "⚠️ <b>Não consegui ler o site agora</b>\n\n"
            f"Erro: <code>{html.escape(errors[0])}</code>\n\n"
            "Sigo tentando a cada rodada. Tente o /status de novo em um minuto.",
        )
        return

    candidates = eligible(offers)
    markup = current_markup()
    in_range = [o for o in candidates if is_fair(o, markup)]

    if in_range:
        headline = f"🎯 <b>TEM INGRESSO NO PREÇO — {len(in_range)} anúncio(s)!</b>"
    else:
        headline = "🔕 Nada no preço ainda. Aviso na hora que chegar."

    lines = ["📊 <b>Como está agora</b>", "", headline, ""]

    # Whether the bot will buy on its own belongs at the top, not buried: it is
    # the one setting that turns a message into a charge.
    if buying_on():
        lines += [
            f"🎯 <b>Comprando sozinho</b> — assim que algum setor bater o alvo eu "
            f"reservo e mando o Pix aqui, até {brl_round(config.HARD_CEILING)} por "
            f"ingresso. Você não precisa fazer nada.",
            "",
        ]
    else:
        lines += [
            f"🔒 <b>Não estou comprando</b> — {off_reason()}. /armar pra eu voltar.",
            "",
        ]

    # Reference material, so it could go last — but it goes here because it is
    # the frame for everything below: without it the prices further down are just
    # numbers with nothing to be far from.
    lines.append(f"🎯 <b>MEU ALVO</b> — em qualquer um dos {len(config.FACE_VALUE)} setores:")
    lines.append(target_block(markup))

    half_seen = False
    for session in config.SESSIONS:
        tonight = [o for o in candidates if o.session == session]
        lines += ["", f"🗓 <b>{short_session(session)}</b>"]
        ranked = []
        for sector in {o.sector for o in tonight}:
            best = best_deal([o for o in tonight if o.sector == sector])
            if best is not None:
                ranked.append((markup_of(best), best))
        if not ranked:
            lines.append("nada à venda" if not tonight else "nada com preço de referência")
            continue
        # Cheapest relative to face first. The ordering is the ranking — a
        # traffic-light glyph per row would say the same thing again, and right
        # now it would paint every row red and mean nothing.
        ranked.sort(key=lambda pair: pair[0])
        for _, o in ranked:
            half_seen = half_seen or o.category != "Inteira"
            lines.append(offer_line(o))

    lines.append("")
    if half_seen:
        lines.append("🎓 = meia-entrada: leve o comprovante ou barram na catraca.")
    lines.append("<i>3,1× = está custando 3,1 vezes o valor original.</i>")
    if errors:
        lines.append(f"⚠️ {len(errors)} de {len(config.SESSIONS)} noites falharam nesta leitura.")
    if STATE.get("paused"):
        lines.append("⏸ Pausado. /voltar para religar.")
    elif (left := _cooldown_until - time.monotonic()) > 0:
        # Without this, /status would claim "a cada 30s" while the bot is in
        # fact holding still for ten minutes — the one lie that matters here.
        lines.append(f"🐢 Parado por mais {left / 60:.0f} min (o site pediu calma).")
    else:
        lines.append(f"Confiro a cada {config.INTERVAL_SECONDS} s · /agio muda o alvo.")
    await reply_long(update, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_markup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(
            update,
            f"🎯 <b>Meu alvo agora</b>\n\n"
            f"Quando um setor chegar aqui eu aviso e vou comprar:\n"
            f"{target_block(current_markup())}\n\n"
            f"Isso é ágio de até +{current_markup()}% sobre o valor original.\n"
            f"Vale igual para meia ({HALF} etc.): mesmo assento, mesmo teto.\n\n"
            "Para mudar: <code>/agio 36</code> · "
            "para usar o valor do .env: <code>/agio env</code>",
        )
        return
    # "env" drops the override instead of setting a number. The pair of them is
    # the whole fix for a /agio being impossible to undo: before this, the only
    # way back to the .env was to edit the .env to a DIFFERENT value, restart, and
    # edit it back — and nothing said so.
    if context.args[0].strip().lower() in ("env", ".env"):
        had = markup_overridden()
        STATE.pop("markup", None)
        STATE.pop("markup_base", None)
        STATE["alerted"] = {}
        save_state(STATE)
        await reply(
            update,
            ("✅ <b>Voltei pro .env</b> — ágio de até +" if had
             else "Já era o do .env — ágio de até +")
            + f"{config.MAX_MARKUP}%\n\n"
            f"{target_block(config.MAX_MARKUP)}\n\n"
            "Checando agora com esse alvo...",
        )
        await check(context, forced=True)
        return
    try:
        new = Decimal(context.args[0].replace("%", "").replace(",", "."))
        # is_finite() blocks inf and nan, which Decimal happily accepts and which
        # then blow up in price_cap's quantize() — the bot would die on every
        # check, with the poisoned value already written to state.json.
        if not new.is_finite() or not 0 <= new <= 1000:
            raise ValueError
    except (ValueError, ArithmeticError):
        await reply(
            update,
            "Não entendi esse número.\n\n"
            "Use assim: <code>/agio 36</code> — aceito ágio de até 36% sobre o "
            "valor original. Vale de 0 a 1000.\n"
            "<code>/agio env</code> usa o valor que está no .env do servidor.",
        )
        return

    STATE["markup"] = str(new)
    STATE["markup_base"] = str(config.MAX_MARKUP)  # so a .env edit can win later
    STATE["alerted"] = {}  # new cap, old alerts no longer apply
    save_state(STATE)
    await reply(
        update,
        f"✅ <b>Alvo novo</b> — ágio de até +{new}%\n\n"
        f"{target_block(new)}\n\n"
        f"Vale igual para meia ({HALF} etc.): mesmo assento, mesmo teto.\n"
        f"Compro sozinho até esses valores, e nunca acima de "
        f"{brl_round(config.HARD_CEILING)} por ingresso.\n\n"
        "Checando agora com o alvo novo...",
    )
    await check(context, forced=True)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "🔍 Checando as três noites agora...")
    await check(context, forced=True)
    # Says what the silence means. Without this the command ends in nothing and
    # "it did not answer" is indistinguishable from "it found nothing".
    await reply(
        update,
        "Pronto. Se não veio alerta, é porque nada bateu o alvo — e o que não "
        "bate o alvo eu também não compro.\n"
        "/status para ver os preços de agora.",
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATE["paused"] = True
    save_state(STATE)
    await reply(
        update,
        "⏸ <b>Pausado</b>\n\n"
        "Paro de checar, então <b>não aviso e não compro nada</b>, nem se cair "
        "no preço. /voltar quando quiser religar.",
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATE["paused"] = False
    save_state(STATE)
    await reply(
        update,
        f"▶️ <b>Voltei a vigiar</b>\n\n"
        f"Checando as três noites a cada {config.INTERVAL_SECONDS} s de novo.\n"
        + (
            "Volto a comprar sozinho também: se bater o alvo, reservo e mando o "
            "Pix aqui."
            if buying_on()
            else f"Comprar continua parado — {off_reason()}. /armar pra religar."
        ),
    )


async def cmd_arm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turns buying back on after something turned it off.

    Not a gate any more, and that is the whole point: buying is what this bot
    does by default, so this command only ever undoes a stop. Nobody has to be
    at their phone for the bot to buy — which was the requirement all along.
    """
    problems = checkout.blockers()
    if problems:
        await reply(
            update,
            "🔒 <b>Ainda não consigo comprar</b>\n\n"
            + "\n".join(f"· {html.escape(p)}" for p in problems)
            + "\n\nResolva no servidor — credencial não vai por aqui.",
        )
        return

    if buying_on():
        await reply(
            update,
            "🎯 <b>Já estou comprando sozinho</b>\n\n"
            "Não precisa me armar: comprar é o meu normal. Se aparecer ingresso "
            f"dentro do teto (ágio {current_markup()}%, nunca acima de "
            f"{brl_round(config.HARD_CEILING)}), eu reservo e mando o Pix aqui.",
        )
        return

    was = off_reason()
    STATE.pop("buying_off", None)
    # A person saying "go" clears the strikes too: they may well have just fixed
    # the thing that was making every attempt fail, and the listings that got
    # struck for it deserve another look.
    STATE.pop("strikes", None)
    save_state(STATE)
    await reply(
        update,
        f"🎯 <b>Voltei a comprar sozinho</b>\n\n"
        f"Estava parado porque {was}. Agora, na próxima vez que algum setor bater "
        f"o alvo, eu:\n"
        f"1. entro na sua conta\n"
        f"2. confiro o preço na fonte\n"
        f"3. <b>reservo</b> o ingresso — o site segura por 10 min\n"
        f"4. gero o Pix e mando o QR + o código aqui\n\n"
        f"<b>Eu não pago.</b> Você paga do app do banco, dentro do prazo. A "
        f"corrida é ganha na reserva, não no pagamento.\n\n"
        f"Teto: ágio até {current_markup()}% e nunca acima de "
        f"{brl_round(config.HARD_CEILING)} por ingresso. /desarmar pra eu parar.",
    )


async def cmd_disarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the bot buying, until a person says otherwise."""
    stop_buying("human")
    await reply(
        update,
        "🔒 <b>Parei de comprar</b>\n\n"
        "Sigo vigiando e avisando os preços, mas não reservo nada até você "
        "mandar /armar.",
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buys the best listing on the board right now, without waiting for a round.

    The impatient path, and the one that ignores /desarmar: somebody typed this,
    with their own eyes on the board, which is a stronger signal than whatever
    made the bot stop buying on its own.
    """
    if _BUY_LOCK.locked():
        await reply(update, "Já estou comprando um. Deixa esse terminar primeiro.")
        return
    blank = checkout.blockers()
    if blank:
        await reply(
            update,
            "🔒 <b>Não dá</b>\n\n"
            + "\n".join(f"· {html.escape(p)}" for p in blank),
        )
        return

    await reply(update, "🔍 Procurando o melhor anúncio agora...")
    offers, _errors, _read, _pushback = await collect()
    candidates = eligible(offers)
    target = buyable(candidates, current_markup())
    if target is None:
        best = best_deal(candidates)
        if best is None:
            await reply(update, "Não tem nada à venda que eu acompanhe agora.")
            return
        await reply(
            update,
            f"Nada dentro do teto. O melhor agora é "
            f"{html.escape(label_of(best))} a {brl(best.price)} "
            f"({times_of(best)} o original), e meu teto é "
            f"{brl(config.price_cap(best.sector, current_markup()))}.\n\n"
            f"Se quiser comprar esse mesmo assim, sobe o teto com /agio — "
            f"eu não passo dele por conta própria.",
        )
        return
    await fire(context, target)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "🤖 <b>O que eu faço</b>\n\n"
        "Vigio os ingressos do BTS no buyticketbrasil nas três noites "
        f"({', '.join(short_session(s) for s in config.SESSIONS)}) e, quando "
        "algum setor chega num preço honesto, eu aviso e <b>compro sozinho</b> — "
        "você só paga o Pix.\n\n"
        "<b>Comandos</b>\n"
        "/status — noite por noite, o melhor de cada setor, com link\n"
        "/agio 36 — muda o alvo (ágio máximo que eu aceito)\n"
        "/checar — checa agora, sem esperar a próxima rodada\n"
        "/comprar — compra agora o melhor que estiver dentro do teto\n"
        "/desarmar — para de comprar · /armar — volta a comprar\n"
        "/pausar · /voltar — desliga e religa os avisos\n\n"
        "<b>Como eu aviso</b>\n"
        "🎯 bateu o alvo — já estou indo atrás\n"
        "⚡ estou comprando: os quatro passos aparecem ao vivo\n"
        "💠 gerei um Pix: o ingresso está reservado, falta você pagar\n"
        "📉 caiu, mas ainda está longe do alvo\n"
        "💓 uma vez por dia, só para você saber que estou vivo\n"
        "⚠️ não estou conseguindo ler o site\n"
        "🔑 não estou conseguindo entrar na sua conta do site\n"
        "🏃 📡 🛡️ 🕵️ perdi a corrida · o site caiu · travei por preço · "
        "anúncio sem comprovante do vendedor — nada foi pago, e eu sigo "
        "caçando\n"
        "🔧 🚨 <b>esses dois pedem você</b>: algo pra configurar, ou um "
        "pedido em aberto na sua conta\n\n"
        "<b>Como eu compro</b>\n"
        "<b>Sozinho, sem você pedir.</b> Assim que um anúncio bater o teto eu "
        "entro na sua conta, confiro o preço na fonte, olho se o anúncio tem "
        "comprovante do vendedor, <b>reservo</b> o ingresso (o site segura por "
        "10 min) e gero o Pix — mando o QR, o código copia-e-cola e o id do "
        "anúncio. <b>Eu não pago</b> — você paga do app do banco. A corrida por "
        "um anúncio barato é ganha na reserva, não no pagamento, então não "
        "preciso que você esteja olhando o celular.\n\n"
        "Gerei um Pix, eu paro: não faz sentido reservar dois. Se eu perder a "
        "corrida ou o anúncio não prestar, eu risco aquele anúncio e sigo "
        "caçando os outros — cada anúncio tem três tentativas, então não fico "
        "repetindo. Se for algo que só você resolve, eu paro e falo.\n\n"
        "<b>Duas coisas que valem saber</b>\n"
        f"Vigio {', '.join(config.CATEGORIES)}. Meia-entrada responde ao "
        "<b>mesmo</b> teto da inteira: é o mesmo assento, e ninguém revende pela "
        "metade. Mas exige comprovante na catraca — sem ele a entrada é negada, "
        "e aí o ingresso barato virou ingresso perdido.\n\n"
        "Camarote eu ignoro: sem valor original divulgado não há como dizer se o "
        "preço é justo.",
    )


# --- startup -----------------------------------------------------------------

def startup_message() -> str:
    """Hello, and whether buying actually works right now.

    The blockers check earns its place here and not only in /armar: the bot buys
    on its own, so a missing .env key is something nobody would find out about
    until the night an alert came through and did nothing. Boot is the last
    moment where saying it costs nothing.
    """
    nights = ", ".join(short_session(s) for s in config.SESSIONS)
    problems = checkout.blockers()
    if problems:
        buying = (
            "🔧 <b>Mas hoje eu não consigo comprar:</b>\n"
            + "\n".join(f"· {html.escape(p)}" for p in problems)
            + "\nEnquanto isso eu só aviso. Resolva no servidor — credencial não "
            "vai por aqui."
        )
    elif buying_on():
        buying = (
            "🎯 <b>Compro sozinho.</b> Não precisa me mandar nada: quando bater o "
            "preço eu reservo o ingresso e mando o Pix aqui. Você só paga."
        )
    else:
        buying = (
            f"🔒 <b>Não estou comprando</b> — {off_reason()}. /armar pra eu "
            f"voltar a comprar sozinho."
        )
    # Said once in the life of the state file, and never again: a warning repeated
    # at every restart is a warning nobody reads.
    if STATE.get("warned_ban"):
        risk = ""
    else:
        STATE["warned_ban"] = True
        save_state(STATE)
        risk = (
            "\n\n⚠️ Vale saber uma vez: automatizar compra pode render bloqueio "
            "de conta em sites assim, e conta bloqueada leva junto ingresso já "
            "comprado nela. Aqui não tem captcha e eu não faço nada pra me "
            "esconder — mas o risco não é zero."
        )
    return (
        "🤖 <b>No ar</b>\n\n"
        f"Vigiando as três noites do BTS ({nights}) no {config.VENUE}, a cada "
        f"{config.INTERVAL_SECONDS} s.\n\n"
        f"{buying}\n\n"
        "Aviso na hora que qualquer setor chegar aqui:\n"
        f"{target_block(current_markup())}\n\n"
        "<i>Isso é a configuração de agora, quando eu subi. Se você mexer no .env "
        "depois, esta mensagem aqui não muda — /status sempre mostra o que está "
        "valendo.</i>\n\n"
        f"/status para ver como está agora · /ajuda para o resto.{risk}"
    )


async def on_startup(app: Application) -> None:
    """Says hello. Guarded: an exception here dies before polling even starts,
    so a Telegram blip at boot would take the whole watcher down with it."""
    try:
        await app.bot.send_message(
            config.CHAT_ID,
            startup_message(),
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("could not send the startup message — check CHAT_ID")


_warned_conflict = False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _warned_conflict
    if isinstance(context.error, Conflict):
        # Two processes polling the same token — in practice the systemd service
        # on the server while someone also runs bot.py on a laptop. Telegram
        # hands updates to one of them and rejects the other, every few seconds,
        # forever. The traceback is forty lines of telegram internals that never
        # name the cause, so it is replaced by the one sentence that does. Said
        # once per process: repeating it every retry buries everything else.
        if not _warned_conflict:
            _warned_conflict = True
            log.error(
                "Outro bot está rodando com ESTE MESMO token, e o Telegram só "
                "entrega as mensagens para um. Provavelmente o serviço no "
                "servidor: 'sudo systemctl stop bts-vigia' para parar lá, ou "
                "feche este aqui. Vou continuar tentando e assumo sozinho "
                "assim que o outro parar."
            )
        return
    log.error("unhandled error", exc_info=context.error)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"


class RedactingFormatter(logging.Formatter):
    """Formatter that keeps the bot token out of every log line.

    Catching InvalidToken is not enough: python-telegram-bot builds the message
    "The token `<token>` was rejected by the server" and logs the whole
    traceback itself, inside its retry loop, before the exception ever reaches
    us. Under systemd that goes straight into the journal — on disk, readable by
    anyone in the systemd-journal group, rewritten on every restart.

    Redacting here rather than at a Filter is deliberate: the token appears in
    the formatted traceback, not in record.msg, so only the final string can be
    scrubbed. It also covers any future log call that happens to include it.
    """

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(config.BOT_TOKEN, "«TOKEN OCULTO»")


def main() -> None:
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # What is actually in force, in the journal, on one line. The operator reads
    # the logs — and the one question the logs could not answer was "is the number
    # the group is seeing the one in my .env?", because a /agio override lives in
    # state.json and nothing printed it. Saying both numbers costs one line.
    log.info(
        "in force this boot: round every %ss, ágio %s%% (.env says %s%%), "
        "hard ceiling R$ %s, buying %s",
        config.INTERVAL_SECONDS,
        current_markup(),
        config.MAX_MARKUP,
        config.HARD_CEILING,
        "on" if buying_on() else f"off ({STATE.get('buying_off')})",
    )

    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    # UpdateType.MESSAGE excludes edited messages. Without it, editing an old
    # command re-runs the handler with update.message == None: the state changes
    # (a re-edited /pausar pauses the bot) and the reply blows up in AttributeError.
    our_chat_only = filters.Chat(chat_id=config.CHAT_ID) & filters.UpdateType.MESSAGE
    for name, handler in [
        ("status", cmd_status),
        ("agio", cmd_markup),
        ("checar", cmd_check),
        ("armar", cmd_arm),
        ("desarmar", cmd_disarm),
        ("comprar", cmd_buy),
        ("pausar", cmd_pause),
        ("voltar", cmd_resume),
        ("ajuda", cmd_help),
        ("start", cmd_help),
    ]:
        app.add_handler(CommandHandler(name, handler, filters=our_chat_only))
    app.add_error_handler(on_error)

    # jitter: APScheduler adds a random 0–JITTER seconds to each round, so we do
    # not hit the site on a perfect metronome.
    # misfire_grace_time: if a round is late (laptop lid closed, machine busy),
    # run it as soon as possible instead of skipping it. The default is 1 second,
    # which at this cadence would silently drop rounds.
    app.job_queue.run_repeating(
        check,
        interval=config.INTERVAL_SECONDS,
        first=5,
        job_kwargs={
            "jitter": config.JITTER,
            "misfire_grace_time": config.INTERVAL_SECONDS,
            # Never let two rounds overlap: check() would just block on its lock
            # and pile up. Falling behind should drop rounds, not queue them.
            "coalesce": True,
            "max_instances": 1,
        },
    )
    # Staying logged in. Its own job and not part of a round: the round runs every
    # 30 s and must stay credential-free and fast, while this is one authenticated
    # call every ten minutes. first=20 keeps it out of the boot rush, and it is
    # the reason the buy path almost never has to spend a second logging in.
    app.job_queue.run_repeating(
        keep_login,
        interval=checkout.KEEP_ALIVE_SECONDS,
        first=20,
        job_kwargs={
            "misfire_grace_time": checkout.KEEP_ALIVE_SECONDS,
            "coalesce": True,
            "max_instances": 1,
        },
    )
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        # Swallowed on purpose — never re-raised. PTB puts the token itself
        # inside this exception's message, so letting the traceback out writes
        # the live secret into the systemd journal, where it persists on disk
        # for anyone who can read it, once per restart, forever. Exiting with
        # EX_CONFIG also stops that restart loop: a rejected token will be
        # rejected again in 30 seconds, so retrying only reprints the secret.
        print(
            "Configuração inválida: o Telegram recusou o BOT_TOKEN.\n"
            "Confira a linha BOT_TOKEN do .env — o token do @BotFather vem\n"
            "inteiro, com os dois-pontos. Se você revogou o token, gere outro\n"
            "com /token no @BotFather e cole o novo.",
            file=sys.stderr,
        )
        raise SystemExit(config.EX_CONFIG) from None


if __name__ == "__main__":
    main()
