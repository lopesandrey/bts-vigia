"""Telegram bot that watches BTS tickets and speaks up when the price is fair.

Runs as a single process: python-telegram-bot's JobQueue does the scheduling, so
there is no cron, no worker and no queue.

    .venv/bin/python bot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from decimal import Decimal

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, filters

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

# Politeness brake. These codes mean "you are going too fast" or "you are not
# welcome right now". Pushing through them is exactly how a watcher earns a
# blocked IP — permanent blindness, much worse than being a few minutes late.
PUSHBACK_CODES = {403, 429, 503}
COOLDOWN = 600.0
_cooldown_until = 0.0

# One line a day proving the bot is alive. A watcher that is correctly silent
# for six months is indistinguishable from a dead one.
HEARTBEAT_EVERY = 24 * 3600

# Any category other than "Inteira" is half-price; used to show the half cap.
HALF = "Meia Estudante"

# The periodic job and /checar can land at the same instant. Without
# serializing, both read and write STATE interleaved and one erases the mark the
# other just wrote — which comes out as a repeated alert.
_LOCK = asyncio.Lock()


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


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --- rules -------------------------------------------------------------------

def eligible(offers: list[Offer]) -> list[Offer]:
    """Only what you can actually prove at the gate (see CATEGORIES in .env)."""
    return [o for o in offers if o.category in config.CATEGORIES]


def markup_of(offer: Offer) -> Decimal | None:
    face = config.face_value(offer.sector, offer.category)
    if face is None:
        return None
    return ((offer.price / face - 1) * 100).quantize(Decimal("1"))


def _units(qty: int) -> str:
    return f"{qty} disponível" if qty == 1 else f"{qty} disponíveis"


def _half_warning(category: str) -> str:
    """Half-price with no proof at the gate is a lost ticket, not a discount."""
    if category == "Inteira":
        return ""
    return f"\n🎓 {category} — leve o comprovante, senão a entrada é negada."


def alert_message(o: Offer) -> str:
    """The alert that matters. The link comes first in order of importance.

    No face value, markup or cap here on purpose: if the message arrived, the
    price already passed the filter. When you are running for the cart, the link
    is what counts.
    """
    return (
        f"🎯 PREÇO JUSTO — {o.sector}\n\n"
        f"{brl(o.price)} · {o.category}\n"
        f"📅 {o.session}\n"
        f"📍 {config.VENUE}\n"
        f"🎫 {_units(o.qty)} neste preço"
        f"{_half_warning(o.category)}\n\n"
        f"{config.buy_link(o.id_ref, o.session)}\n\n"
        "⚡ O link abre esse anúncio. Fique logado no site para cair direto "
        "no checkout."
    )


def record_message(o: Offer, previous: Decimal) -> str:
    """New low. Here the markup does show up, on a single line.

    "Cheapest so far" with no context reads as an opportunity even when it is
    still a robbery — the number keeps the bot from turning into a salesman.
    """
    markup = markup_of(o)
    face = config.face_value(o.sector, o.category)
    context = (
        f"\nAinda +{markup}% sobre a face de {brl(face)}." if markup is not None else ""
    )
    return (
        f"📉 MENOR PREÇO ATÉ AGORA — {o.sector}\n\n"
        f"{brl(o.price)} · {o.category}  (antes {brl(previous)})\n"
        f"📅 {o.session}\n"
        f"🎫 {_units(o.qty)} neste preço{context}"
        f"{_half_warning(o.category)}\n\n"
        f"{config.buy_link(o.id_ref, o.session)}"
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
            asyncio.to_thread(monitor.fetch, config.session_url(s), s)
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
                "🐢 O site pediu pra eu ir mais devagar, então vou espaçar as "
                f"checagens por {COOLDOWN / 60:.0f} min.\n\n"
                "Continuo vigiando — só com menos frequência por um tempo.",
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
    if changed:
        save_state(STATE)


async def cap_rule(
    context: ContextTypes.DEFAULT_TYPE, candidates: list[Offer], read: set[str]
) -> bool:
    """Alerts when a sector's price drops to that sector's fair cap."""
    alerted: dict[str, str] = STATE.setdefault("alerted", {})
    markup = current_markup()
    changed = False

    under_cap = set()
    for o in candidates:
        cap = config.price_cap(o.sector, o.category, markup)
        if cap is None or o.price > cap:
            continue
        under_cap.add(o.key)
        already = _dec(alerted.get(o.key))
        if already is not None and o.price >= already:
            continue  # we already warned at this price or a cheaper one
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
    """
    records: dict[str, str] = STATE.setdefault("record_lows", {})
    changed = False

    for session in config.SESSIONS:
        of_session = [o for o in candidates if o.session == session]
        if not of_session:
            continue
        cheapest = min(of_session, key=lambda o: o.price)
        low = _dec(records.get(session))
        if low is not None and cheapest.price >= low:
            continue
        if low is not None:
            await notify(context, record_message(cheapest, low))
        records[session] = str(cheapest.price)
        changed = True
    return changed


async def heartbeat(
    context: ContextTypes.DEFAULT_TYPE, candidates: list[Offer], read: set[str]
) -> bool:
    """Once a day, one line per night, so silence stays distinguishable from death."""
    now = time.time()
    last = STATE.get("last_heartbeat")
    if last is None:
        STATE["last_heartbeat"] = now  # first round: the startup message covers it
        return True
    if now - last < HEARTBEAT_EVERY or len(read) < len(config.SESSIONS):
        return False

    lines = [f"💓 Tudo certo por aqui. Mais barato de cada noite (teto +{current_markup()}%):"]
    for session in config.SESSIONS:
        of_session = [o for o in candidates if o.session == session]
        if of_session:
            cheapest = min(of_session, key=lambda o: o.price)
            lines.append(f"  {session}: {brl(cheapest.price)} · {cheapest.sector}")
        else:
            lines.append(f"  {session}: nada disponível")
    await notify(context, "\n".join(lines))
    STATE["last_heartbeat"] = now
    return True


async def record_failure(
    context: ContextTypes.DEFAULT_TYPE, reason: str, how_many: int
) -> None:
    STATE["failures"] = STATE.get("failures", 0) + 1
    if STATE["failures"] >= FAILURES_BEFORE_WARNING and not STATE.get("warned_failure"):
        minutes = STATE["failures"] * config.INTERVAL_SECONDS / 60
        await notify(
            context,
            f"⚠️ Não consigo ler {how_many} de {len(config.SESSIONS)} sessões "
            f"há ~{minutes:.0f} min.\n\n"
            f"Último erro: {reason}\n\n"
            "Vou continuar tentando. Se o site mudou, o parser precisa de ajuste.",
        )
        STATE["warned_failure"] = True
    save_state(STATE)


async def record_success(context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.get("warned_failure"):
        await notify(context, "✅ Voltei a ler o site normalmente.")
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
    await context.bot.send_message(
        config.CHAT_ID, text, disable_web_page_preview=True
    )


async def reply_long(update: Update, text: str, limit: int = 3800) -> None:
    """Replies in several messages: Telegram cuts off at 4096 chars.

    /status grows with the stock, so this is a question of when, not if.
    """
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            await update.message.reply_text(chunk, disable_web_page_preview=True)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        await update.message.reply_text(chunk, disable_web_page_preview=True)


def cap_table(markup: Decimal) -> str:
    """The caps a markup produces, one sector per line, full and half side by side."""
    lines = []
    for sector in config.FACE_VALUE:
        full = config.price_cap(sector, "Inteira", markup)
        half = config.price_cap(sector, HALF, markup)
        lines.append(f"  {sector}: {brl(full)} · meia {brl(half)}")
    return "\n".join(lines)


# --- commands ----------------------------------------------------------------

def best_deal(offers: list[Offer]) -> Offer | None:
    """The best offer of a night — cheapest RELATIVE TO FACE, not in reais.

    Sorting by price is actively misleading here. On 28/10 the cheapest listing
    was an Arquibancada meia at R$ 3,630 (+968%) while a Pista sat at R$ 4,178.90
    (+234%): the pricier ticket is the far better deal, and it is the one that
    will hit the cap first. Camarotes are skipped — no published face means no
    markup, so they can never alert and only add noise.
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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    offers, errors, *_ = await collect()
    if len(errors) == len(config.SESSIONS):
        await update.message.reply_text(f"⚠️ Não consegui ler o site.\n{errors[0]}")
        return

    candidates = eligible(offers)
    markup = current_markup()
    lines = [
        f"📊 Melhor de cada noite · teto +{markup}% "
        f"(Pista {brl(config.price_cap('Pista', 'Inteira', markup))})",
    ]
    for session in config.SESSIONS:
        best = best_deal([o for o in candidates if o.session == session])
        if best is None:
            lines += ["", f"{short_session(session)} — nada disponível"]
            continue
        half = "" if best.category == "Inteira" else " meia"
        lines += [
            "",
            f"{short_session(session)} — {brl(best.price)} · "
            f"{best.sector}{half} · +{markup_of(best)}% · {best.qty} un.",
            config.buy_link(best.id_ref, session),
        ]

    lines.append("")
    if errors:
        lines.append(f"⚠️ {len(errors)} sessão(ões) falharam nesta leitura.")
    if STATE.get("paused"):
        lines.append("⏸ Pausado (/voltar para religar).")
    elif (left := _cooldown_until - time.monotonic()) > 0:
        # Without this, /status would claim "a cada 30s" while the bot is in
        # fact holding still for ten minutes — the one lie that matters here.
        lines.append(f"🐢 Freando por mais {left / 60:.0f} min (o site reclamou).")
    else:
        lines.append(f"Checando a cada {config.INTERVAL_SECONDS} s · /agio muda o teto.")
    await reply_long(update, "\n".join(lines))


async def cmd_markup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            f"Ágio máximo atual: +{current_markup()}%\n\n"
            "Para mudar: /agio 36\n\n"
            "Tetos que isso gera:\n" + cap_table(current_markup())
        )
        return
    try:
        new = Decimal(context.args[0].replace("%", "").replace(",", "."))
        # is_finite() blocks inf and nan, which Decimal happily accepts and which
        # then blow up in price_cap's quantize() — the bot would die on every
        # check, with the poisoned value already written to state.json.
        if not new.is_finite() or not 0 <= new <= 1000:
            raise ValueError
    except (ValueError, ArithmeticError):
        await update.message.reply_text("Não entendi. Use assim: /agio 36")
        return

    STATE["markup"] = str(new)
    STATE["markup_base"] = str(config.MAX_MARKUP)  # so a .env edit can win later
    STATE["alerted"] = {}  # new cap, old alerts no longer apply
    save_state(STATE)
    await update.message.reply_text(
        f"✅ Ágio máximo agora é +{new}%.\n\n"
        + cap_table(new)
        + "\n\nChecando agora..."
    )
    await check(context, forced=True)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Checando as três noites...")
    await check(context, forced=True)
    await update.message.reply_text("Pronto. Se nada apareceu, nada bateu o teto.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATE["paused"] = True
    save_state(STATE)
    await update.message.reply_text("⏸ Pausado. /voltar quando quiser religar.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATE["paused"] = False
    save_state(STATE)
    await update.message.reply_text("▶️ Voltei a monitorar.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Vigio os ingressos do BTS no buyticketbrasil (28, 30 e 31/10) e aviso\n"
        "quando o ágio cair para um nível honesto.\n\n"
        "/status — o melhor negócio de cada noite, com link\n"
        "/agio 36 — define o ágio máximo aceitável (%)\n"
        "/checar — força uma checagem agora\n"
        "/pausar e /voltar\n\n"
        f"Categorias vigiadas: {', '.join(config.CATEGORIES)}.\n"
        "Meia-entrada tem teto próprio (metade da face), e exige comprovante\n"
        "na entrada — sem ele a entrada é negada."
    )


# --- startup -----------------------------------------------------------------

async def on_startup(app: Application) -> None:
    """Says hello. Guarded: an exception here dies before polling even starts,
    so a Telegram blip at boot would take the whole watcher down with it."""
    try:
        await app.bot.send_message(
            config.CHAT_ID,
            "🤖 No ar. Vigiando as três noites do BTS no Morumbis.\n"
            f"Ágio máximo: +{current_markup()}% · checagem a cada "
            f"{config.INTERVAL_SECONDS} s.\n\n/ajuda para os comandos.",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("could not send the startup message — check CHAT_ID")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    # UpdateType.MESSAGE excludes edited messages. Without it, editing an old
    # command re-runs the handler with update.message == None: the state changes
    # (a re-edited /pausar pauses the bot) and the reply blows up in AttributeError.
    our_chat_only = filters.Chat(chat_id=config.CHAT_ID) & filters.UpdateType.MESSAGE
    for name, handler in [
        ("status", cmd_status),
        ("agio", cmd_markup),
        ("checar", cmd_check),
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
