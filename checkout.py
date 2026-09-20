"""Takes one listing from "for sale" to a generated Pix charge, over HTTP.

No browser. That is the headline, and it is worth saying why, because this file
used to drive Chrome through Playwright and the reason it did no longer exists.

The old site was a Bubble.io app: every action went through one generic
`POST /workflow/start` carrying internal ids regenerated on each deploy, so there
was nothing stable to call and driving the rendered UI was the only thing that
survived. The rewrite in September 2026 replaced that with an ordinary REST API,
and the site's own browser code is a thin client over it — `fetch("/api/backend"
+ path)` for everything, `POST /api/auth/login` to sign in. Every call below was
read out of that bundle, not guessed — and then run against the live site on
20/09/2026, which is what the right-hand column records:

    POST /api/auth/login                       {document, password} -> cookies
        verified. 200 with a `user`. Every failure — wrong password, CPF that
        does not exist — answers an identical, bodyless `401 {}`, so this module
        cannot tell those apart and neither can you. Check it in the browser.
    GET  /marketplaces/users/me                the account's name and CPF
        verified. Carries NO address: name, e-mail, phone, CPF, birth date.
        The billing address has to come from the .env CEP, which is why the
        viacep lookup below is part of the flow and not a nicety.
    GET  /marketplaces/listings/{id}           the asking price, from the source
        verified. Requires the login: unauthenticated it is 401, which is why the
        watcher in monitor.py cannot check any of this and the seller check lives
        in the buy path. `amount` is {value, currency} and holds the SELLER's
        price, fee excluded — 4000 on a listing the board shows as R$ 4.400.
        `status` is "Active" while buyable and "LockedForSale" while somebody
        holds it, and `lockedAt`/`userId` say since when and by whom. This module
        reads neither yet: they would let it skip a listing already held.
        The seller is a bare `sellerId` UUID and there is NO name anywhere, on
        this or any other endpoint (/listings/{id}/seller, /sellers/{id},
        /users/{id} are all 404). What identifies the ticket is `proofUrl` plus
        the signed `proofDownloadUrl` — the receipt the seller uploaded, which is
        what the site shows on the listing page — and some listings simply do not
        have it. See `_vet_listing`.
    POST /marketplaces/listings/{id}/lock      reserve it
        verified live, 0.49s. One hold per account: a second one answers 422
        PLATFORM_MARKETPLACES_USER_ALREADY_HAS_LOCKED_LISTING, and a listing that
        already has an order answers 422 too, with a different code.
    POST /marketplaces/listings/{id}/unlock    give it back
        verified live, 0.49s, and it works even after an order was created on the
        listing — which is what makes the failure path in purchase() safe.
    POST /marketplaces/orders                  -> paymentResult.pixContent
        verified live, whole flow in 3.4s. It rejects an empty street,
        neighborhood, city or state with 400 PLATFORM_BAD_REQUEST, so all four
        are mandatory. Its `detail` reads "See Errors property for details" and
        the details live in `errors[]` — see `_reasons`, which exists because
        that sentence alone tells the operator nothing.
        The money: `amount` is the TOTAL, fee already inside it, and `fee` is
        {rate: 10, amount} breaking it down. See `_charged`.
        The Pix is dynamic — the BR Code carries no tag 54, so the amount the
        bank shows comes from the payload it fetches, not from the code.

Three consequences, all of them good:

- **Speed.** The whole flow is five small JSON calls. The browser version could
  not start faster than Bubble's own 6-8 second render, and that was the floor
  the entire design worked around.
- **The login is the CPF, not the e-mail.** That was not a choice; it is what the
  endpoint takes (their analytics even calls the method "document_password").
  A browser flow typing an e-mail into that form would never have worked.
- **The Pix code arrives as a field.** `paymentResult.pixContent` is the
  copy-and-paste BR Code. No screen scraping, nothing to truncate.

There is now also a real hold, which the old site did not have. `lock` reserves
the listing at that price for ten minutes before any money is committed, so the
race for a cheap ticket is won at the lock and not at the payment. This module
therefore locks first, and if anything after that fails it unlocks again rather
than sitting on a ticket somebody else could buy.

Where it stops: holding the Pix code. It never pays. No card number exists
anywhere in this repository, and `paymentMethod` is hard-coded to Pix — a card
would cost 3.6% more anyway.
"""

from __future__ import annotations

import asyncio
import base64
import http.cookiejar
import json
import logging
import os
import random
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import config
import monitor

ROOT = Path(__file__).resolve().parent

log = logging.getLogger(__name__)

# The logged-in session. Cookies, so a restart does not need a fresh login: the
# site issues bt_at (access), bt_rt (refresh) and bt_user. Kept out of git like
# every other secret here, and chmod 600 because it IS a credential — anything
# holding these cookies can buy on this account.
AUTH_DIR = ROOT / ".auth"
AUTH_FILE = AUTH_DIR / "cookies.txt"

# Where the site gets the address from the CEP. Not our choice of provider: it is
# the one their own checkout calls, so asking it the same question gives the same
# answer the form would have filled in. Free, no key, no account.
CEP_LOOKUP = "https://viacep.com.br/ws/{cep}/json/"

# Per-request timeout. The whole flow is five small calls; if one of them takes
# longer than this the site is in trouble and waiting does not help.
TIMEOUT = 15.0

# A Pix BR Code (EMV MPM) always starts with the payload-format indicator 000201
# and names the Central Bank's domain in its merchant account field.
PIX_PREFIX = "000201"
PIX_MARKER = "br.gov.bcb.pix"

# The order is created with the charge already generated, but the payment service
# is a third party, so allow a couple of re-reads before giving up on the code.
PIX_POLL_TRIES = 6
PIX_POLL_DELAY = 1.0

# Their own client retries an unlock twice before accepting that the hold is
# stuck. Same delays, same reason: a lock left behind blocks other buyers for ten
# minutes, and one of those other buyers might be us on the next round.
UNLOCK_RETRY_DELAYS = (3.0, 6.0)

# --- what kind of failure it was --------------------------------------------
# Every failure carries one of these, because two different readers need it. The
# retry logic needs to know whether trying again can possibly help, and the
# person in the Telegram group needs to know whether there is now something of
# theirs sitting on the site. One word answers both.
GONE = "gone"            # somebody else got it first — routine, not a fault
TRANSIENT = "transient"  # the network or the site wobbled — retrying is the fix
SETUP = "setup"          # .env, password, account — no retry will ever fix it
PRICE = "price"          # a price guard said no — working exactly as intended
PENDING = "pending"      # a reservation or order exists on the account RIGHT NOW
UNVERIFIED = "unverified"  # the listing has no seller proof — skip it, buy nothing

# How hard to retry, in the two places where retrying means different things.
#
# Before the lock the race is live: every millisecond spent sleeping is a
# millisecond somebody else is buying, so this is three quick tries and out.
# After the lock the ticket is ours for ten minutes, so the only thing at stake
# is the clock — being patient there costs nothing and saves the purchase.
LOCK_TRIES = 3
LOCK_DELAY = 0.4
ORDER_TRIES = 4
ORDER_DELAYS = (1.0, 2.0, 4.0)

# HTTP statuses worth trying again. 429 is in here because the site throttles
# per-IP and the watcher shares that IP; the rest are the site being briefly
# broken rather than saying no.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# --- saying where it is, while it is there -----------------------------------
# The four steps, in the words the group reads. They exist as data and not as
# strings scattered through purchase() because the message has to be able to show
# the whole list up front, with the current one marked: a progress display that
# only ever says "step 3" cannot show what step 4 is going to be.
STEPS = (
    "entrar na sua conta",
    "conferir o preço na fonte",
    "reservar o ingresso",
    "gerar o Pix",
)


class Progress:
    """How far the purchase got, readable from outside while it runs.

    purchase() runs in a worker thread and the message lives on the event loop,
    so this is deliberately the dumbest possible channel between them: two
    fields, assigned by one side and read by the other. No queue, no callback, no
    lock — an int and a str assignment cannot be torn, and the reader polling a
    stale value for 400ms costs nothing. A callback would have to be marshalled
    back onto the loop from the thread, which is real machinery for a display.
    """

    def __init__(self) -> None:
        self.done = 0    # steps finished
        self.note = ""   # what the step in flight is waiting on, if anything
        self.failed = False

    def finished(self, count: int) -> None:
        self.done = count
        self.note = ""

    def waiting(self, note: str) -> None:
        self.note = note


# The API's error codes, in the operator's language, each with the kind of
# failure it is. Only the ones that can actually come back on a buy; anything
# else falls through to the raw response, which is more useful than a wrong
# translation.
ERRORS: dict[str, tuple[str, str]] = {
    "PLATFORM_MARKETPLACES_LISTING_STATUS_INVALID": (
        GONE,
        "esse anúncio não está mais ativo — alguém comprou antes.",
    ),
    "PLATFORM_MARKETPLACES_LISTING_NOT_LOCKABLE": (
        GONE,
        "o site não deixa reservar esse anúncio (provavelmente já foi).",
    ),
    "PLATFORM_MARKETPLACES_LISTING_ALREADY_LOCKED": (
        GONE,
        "outra pessoa reservou esse anúncio primeiro.",
    ),
    "PLATFORM_MARKETPLACES_USER_ALREADY_HAS_LOCKED_LISTING": (
        PENDING,
        "essa conta já tem OUTRO ingresso reservado. O site só permite um por vez:"
        " termine ou solte a reserva anterior.",
    ),
    "PLATFORM_MARKETPLACES_SELLER_CANNOT_LOCK_OWN_LISTING": (
        GONE,
        "esse anúncio é da própria conta.",
    ),
    "PLATFORM_MARKETPLACES_LISTING_LOCK_NOT_OWNED": (
        GONE,
        "a reserva desse anúncio é de outra pessoa.",
    ),
    "PLATFORM_MARKETPLACES_LISTING_NOT_LOCKED": (
        GONE,
        "a reserva expirou antes de eu conseguir gerar o Pix.",
    ),
    "PLATFORM_MARKETPLACES_LISTING_USER_BLOCKED": (
        SETUP,
        "essa conta está bloqueada pra comprar.",
    ),
    "PLATFORM_MARKETPLACES_ORDER_USER_BLOCKED": (
        SETUP,
        "essa conta está bloqueada pra comprar.",
    ),
    "PLATFORM_MARKETPLACES_ORDER_ALREADY_EXISTS_FOR_LISTING": (
        PENDING,
        "já existe um pedido pra esse anúncio.",
    ),
    "PLATFORM_MARKETPLACES_ORDER_ANTIFRAUD_DECLINED": (
        SETUP,
        "o antifraude do site recusou o pedido. Compre essa uma na mão pelo site;"
        " se recusar de novo, a conta precisa resolver isso com eles.",
    ),
    "PLATFORM_MARKETPLACES_ORDER_DELIVERY_EMAIL_REQUIRED": (
        SETUP,
        "o site exige o e-mail de quem recebe o ingresso — preencha RECEIVER_EMAIL"
        " no .env.",
    ),
    "PLATFORM_MARKETPLACES_USER_PHONE_NUMBER_CONFIRMATION_NOT_CONFIRMED": (
        SETUP,
        "o telefone da conta não está confirmado. Confirme uma vez no site, no"
        " celular, e depois isso não pede mais.",
    ),
    "PLATFORM_AUTHENTICATIONS_INVALID_PASSWORD": (
        SETUP,
        "senha errada (PASS no .env).",
    ),
    "PLATFORM_AUTHENTICATIONS_USER_NOT_FOUND": (
        SETUP,
        "não existe conta com esse CPF.",
    ),
    "PLATFORM_AUTHENTICATIONS_LIMIT_EXCEEDED": (
        SETUP,
        "o site bloqueou tentativas de login por excesso. Espere uns minutos.",
    ),
}


class CheckoutError(RuntimeError):
    """The flow did not end where it should have. Nothing was paid, ever.

    Carries the kind of failure it was, and — when the kind is PENDING — the page
    where the thing it left behind can be dealt with. Without those two fields the
    bot has one message for seven different situations, and the advice that is
    right for "somebody bought it first" (try again) is exactly wrong for "this
    account already holds a reservation" (go finish it).
    """

    def __init__(
        self, message: str, kind: str = TRANSIENT, url: str = "", status: int = 0
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status = status


@dataclass(frozen=True)
class PixCharge:
    """A generated Pix charge: the ticket is held, the money has not moved."""

    code: str            # the copy-and-paste BR Code
    intact: bool         # its own CRC checks out, so it is not truncated
    total: Decimal       # what the site will actually charge
    expires_in: str      # how long is left to pay, e.g. "09:45"
    seconds: float       # how long the whole flow took
    url: str = ""        # the order page, for a human who wants to look
    qr: bytes = b""      # the QR as a PNG, exactly as the site's own screen shows


# --- the Pix code checks itself ---------------------------------------------
def _crc16(payload: bytes) -> int:
    """CRC-16/CCITT-FALSE, the one the Pix BR Code spec uses."""
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def pix_code_is_intact(code: str) -> bool:
    """True when the BR Code's own checksum matches its contents.

    Worth the twelve lines even now that the code arrives in a JSON field: a code
    that got cut somewhere looks perfectly fine in a Telegram message and fails in
    the bank app, and by then most of the time to pay is gone. Every BR Code ends
    with tag 6304 plus a CRC16 over everything before it, so it can check itself.
    """
    at = code.rfind("6304")
    if at == -1 or len(code) - at != 8:
        return False
    try:
        declared = int(code[at + 4 :], 16)
    except ValueError:
        return False
    return _crc16(code[: at + 4].encode("utf-8")) == declared


# --- talking to the site ----------------------------------------------------
def _error_code(payload: object) -> str:
    """The API's own error code, from either shape it uses."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("code") or "")
        return str(payload.get("code") or "")
    return ""


def _reasons(payload: object) -> str:
    """The field-by-field reasons the API hides behind its `errors` list.

    Its own `detail` for a rejected body is "See Errors property for details" —
    a sentence that points at the very data this used to discard, leaving the
    operator with a failure that names nothing. The field list IS the message.
    """
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return ""
    said = [
        str(e["description"]).strip()
        for e in errors
        if isinstance(e, dict) and e.get("description")
    ]
    return " ".join(dict.fromkeys(said))  # dedup, order kept


def _explain(status: int, payload: object) -> tuple[str, str]:
    """An HTTP failure as (kind, words the operator can act on).

    The status decides the kind whenever the code does not, and that order
    matters: a 503 with no body is the site being briefly down, which a retry
    fixes, while a 400 with an unrecognised code is the site saying no for a
    reason that will still be true in four hundred milliseconds.
    """
    code = _error_code(payload)
    if code in ERRORS:
        return ERRORS[code]
    kind = TRANSIENT if status in RETRY_STATUS else SETUP
    said = _reasons(payload)
    if said:
        return kind, f"{said} (HTTP {status})"
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title") or payload.get("message")
        if detail:
            return kind, f"{detail} (HTTP {status}{', ' + code if code else ''})"
    if status in RETRY_STATUS:
        return kind, f"o site respondeu HTTP {status} — está instável agora."
    return kind, f"HTTP {status}: {str(payload)[:200]}"


class Client:
    """One logged-in session against the site's API.

    Deliberately synchronous and dependency-free — urllib with a cookie jar is
    the whole thing. buy() runs it in a thread so bot.py can await it.
    """

    def __init__(self) -> None:
        self.jar = http.cookiejar.MozillaCookieJar(str(AUTH_FILE))
        if AUTH_FILE.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except OSError:
                pass  # a corrupt or unreadable file just means logging in again
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.user: dict = {}

    # -- plumbing
    def call(self, url: str, body: object = None, method: str | None = None) -> object:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", monitor.UA)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Language", "pt-BR,pt;q=0.9")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            payload: object
            try:
                payload = json.loads(e.read() or b"null")
            except ValueError:
                payload = None
            kind, why = _explain(e.code, payload)
            raise CheckoutError(why, kind, status=e.code) from e
        except OSError as e:
            # Nothing was sent, or nothing came back — HTTPError is caught above,
            # so everything reaching here is the wire and not the site's answer.
            # URLError and the timeout are both OSError, so one clause covers them.
            raise CheckoutError(f"não consegui falar com o site: {e}", TRANSIENT) from e
        try:
            return json.loads(raw or b"null")
        except ValueError as e:
            raise CheckoutError(f"{url} não respondeu JSON", TRANSIENT) from e

    def api(self, path: str, body: object = None, method: str | None = None) -> dict:
        """One call to /api/backend, signing in again if the session lapsed."""
        try:
            result = self.call(config.BACKEND + path, body, method)
        except CheckoutError as e:
            if e.status != 401:
                raise
            self.login()
            result = self.call(config.BACKEND + path, body, method)
        if not isinstance(result, dict):
            raise CheckoutError(
                f"{path} respondeu {type(result).__name__}, não objeto", TRANSIENT
            )
        return result

    # -- session
    def login(self) -> dict:
        """Signs in with CPF + password and keeps the cookies for next time."""
        document = config.BILLING["document"]
        password = os.environ.get("PASS", "").strip()
        if not document or not password:
            raise CheckoutError(
                "faltam CPF e PASS no .env. Coloque você mesmo no arquivo — "
                "não mande por mensagem.",
                SETUP,
            )
        result = self.call(
            config.LOGIN, {"document": document, "password": password}, method="POST"
        )
        if not isinstance(result, dict) or not result.get("user"):
            raise CheckoutError("o login não devolveu usuário nenhum.", SETUP)
        AUTH_DIR.mkdir(exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        AUTH_FILE.chmod(0o600)
        self.user = result["user"]
        return self.user

    def me(self) -> dict:
        """The account, which is also where the payer's name and CPF come from.

        Doubles as the check that the saved cookies are still good: this is a
        cheap authenticated read, so it fails here rather than at the lock.
        """
        try:
            self.user = self.api("/marketplaces/users/me")
        except CheckoutError:
            self.login()
            self.user = self.api("/marketplaces/users/me")
        return self.user


# --- the address ------------------------------------------------------------
_ADDRESS_CACHE: dict[str, dict[str, str]] = {}


def address(cep: str) -> dict[str, str]:
    """CEP -> street, neighbourhood, city, state. Same lookup the site uses.

    Cached because it never changes and it is the one call in the flow that goes
    somewhere other than buyticketbrasil: on the round that matters it should
    already be answered.
    """
    cep = "".join(c for c in cep if c.isdigit())
    if len(cep) != 8:
        raise CheckoutError(f"POSTAL_CODE={cep!r} não é um CEP de 8 dígitos.", SETUP)
    if all(config.ADDRESS.values()):
        # Written down in the .env, so there is nothing to ask anybody. This is
        # the only step in the flow that talks to a host other than the site.
        return {"zipCode": cep, **config.ADDRESS}
    if cep in _ADDRESS_CACHE:
        return _ADDRESS_CACHE[cep]
    request = urllib.request.Request(
        CEP_LOOKUP.format(cep=cep), headers={"User-Agent": monitor.UA}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            found = json.loads(response.read())
    except (OSError, ValueError) as e:
        raise CheckoutError(
            f"não consegui consultar o CEP {cep}: {e}", TRANSIENT
        ) from e
    if not isinstance(found, dict) or found.get("erro") or not found.get("uf"):
        raise CheckoutError(
            f"o CEP {cep} não existe. Confira POSTAL_CODE no .env.", SETUP
        )
    result = {
        "zipCode": cep,
        "street": (found.get("logradouro") or "").strip(),
        "neighborhood": (found.get("bairro") or "").strip(),
        "city": (found.get("localidade") or "").strip(),
        "state": found.get("uf", "").strip(),
    }
    _ADDRESS_CACHE[cep] = result
    return result


# --- the order --------------------------------------------------------------
def _fingerprint() -> dict:
    """The antifraud device block, filled the way the site fills it with no SDK.

    Their checkout loads Konduto in the browser and sends its visitor id. There
    is no browser here, so this sends exactly what their own code sends when
    Konduto is not configured: a `no-fingerprint-…` id and a confidence of zero
    that says so. Honest, and a shape the backend already accepts.
    """
    noise = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return {
        "visitorId": f"no-fingerprint-{noise}"[:50],
        "confidence": {"score": 0, "comment": "Antifraud fingerprint not configured."},
    }


def _money(raw: object) -> Decimal:
    return Decimal(str((raw or {}).get("value", 0)))


def _left_to_pay(expires_at: object) -> str:
    """"09:45" — how long there is to pay, from the charge's own expiry."""
    if not isinstance(expires_at, str) or not expires_at:
        return ""
    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return ""
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _qr_png(payment: dict) -> bytes:
    """The QR as PNG bytes, out of the base64 the API sends. Never raises.

    The site puts this on its own payment screen, so sending it costs one field
    and no screenshot. It is a nicety, not the product — the copy-and-paste code
    is what gets paid, and a QR that failed to decode must never cost the group
    the code. Hence: any problem here returns empty and the flow carries on.
    """
    raw = str(payment.get("pixQrCode") or "").strip()
    if not raw:
        return b""
    if "base64," in raw:  # a data: URL, which is how their own <img> carries it
        raw = raw.split("base64,", 1)[1]
    try:
        png = base64.b64decode(raw, validate=False)
    except ValueError:  # binascii.Error is a ValueError
        return b""
    # Only a real PNG goes to Telegram; anything else would just be a failed
    # sendPhoto in the middle of the most important message in the bot.
    return png if png.startswith(b"\x89PNG\r\n\x1a\n") else b""


def _pix(order: dict) -> tuple[str, str, bytes]:
    """(copy-and-paste code, expiry, QR png) out of an order, or ("", "", b"")."""
    payment = order.get("paymentResult") or {}
    code = str(payment.get("pixContent") or "").strip()
    if not code.startswith(PIX_PREFIX) or PIX_MARKER not in code.lower():
        return "", "", b""
    return code, str(payment.get("pixExpiresAt") or ""), _qr_png(payment)


def _charged(order: dict, fallback: Decimal) -> Decimal:
    """What the site will really bill, straight from the order it just created.

    The order's `amount` is the total with the fee already inside it. Read off a
    real order on 20/09/2026: a listing the site quoted at 5000 came back as
    `amount 5500` with `fee {rate: 10, amount: 500}` — the 500 breaks the 5500
    down, it is not added to it. This used to add them, which billed the fee
    twice on paper and would have announced R$ 1.870 for a ticket costing 1.700.

    The rate is checked against SERVICE_FEE because every ceiling in config.py is
    computed from that constant before the order exists. If the site ever moves
    it, the number here stays honest — it comes from the site — but the caps that
    approved the purchase were computed with the wrong one, and that is silent.
    """
    rate = (order.get("fee") or {}).get("rate")
    if isinstance(rate, (int, float)) and Decimal(str(rate)) / 100 != config.SERVICE_FEE:
        log.warning(
            "o site cobrou taxa de %s%% e config.SERVICE_FEE é %s%% — "
            "os tetos foram calculados com a taxa errada.",
            rate,
            config.SERVICE_FEE * 100,
        )
    charged = _money(order.get("amount"))
    return charged.quantize(Decimal("0.01")) if charged > 0 else fallback


def _vet_listing(listing: dict, offer) -> Decimal:
    """Everything that has to be true about a listing before any money moves.

    Returns the seller's price as the SITE states it, which from here on is the
    only price that counts: the watcher's number is a memory of a page that may
    already have changed.

    Four refusals, and each one is a different kind on purpose — the person
    reading the group needs to know whether to try again, fix something, or do
    nothing at all.
    """
    listed = _money(listing.get("amount"))
    if listed <= 0:
        raise CheckoutError("o anúncio voltou sem preço — não compro às cegas.", GONE)
    if listed > offer.listed:
        raise CheckoutError(
            f"o preço subiu entre o alerta e agora: o anúncio dizia "
            f"R$ {offer.listed} e o site diz R$ {listed}. Parei sem reservar.",
            PRICE,
        )
    if listing.get("status") != "Active":
        raise CheckoutError(
            f"o anúncio está «{listing.get('status')}», não «Active» — já foi.",
            GONE,
        )
    # Who is selling this. The API never gives a buyer the seller's NAME — there
    # is no name field here and no endpoint that resolves sellerId to one (tried
    # /listings/{id}/seller, /sellers/{id} and /users/{id}: all 404). What it does
    # give is `proofUrl`, the purchase receipt the seller had to upload, which is
    # what the site shows on the listing page. Listings without it are live right
    # now: of the 16 on the board on 20/09/2026, two had no proofUrl at all, one
    # of them a Pista Inteira. Nobody can check that ticket, so the bot does not
    # buy it — the seat is the cheap part of this, the ticket being real is not.
    if not listing.get("sellerId") or not listing.get("proofUrl"):
        raise CheckoutError(
            "esse anúncio não tem comprovante do vendedor no site — sem isso não "
            "há como conferir de quem é o ingresso, então não reservei nada.",
            UNVERIFIED,
        )
    return listed


def _check_ceilings(sector: str, total: Decimal) -> None:
    """Both price gates. Called again in here on purpose.

    This is the function that spends, so it has to be safe to call from anywhere
    — including a /comprar typed by hand, or after the price moved between the
    alert and the click.
    """
    cap = config.price_cap(sector)
    if cap is None:
        raise CheckoutError(
            f"{sector} não tem valor original publicado, então não tem teto justo "
            f"pra comparar. Não compro às cegas.",
            PRICE,
        )
    if total > cap:
        raise CheckoutError(
            f"R$ {total} passa do teto de R$ {cap} pra {sector}.", PRICE
        )
    if total > config.HARD_CEILING:
        raise CheckoutError(
            f"R$ {total} passa do teto absoluto de R$ {config.HARD_CEILING}.", PRICE
        )


def _retry(
    call, what: str, tries: int, delays, progress: Progress | None = None
) -> object:
    """Runs `call`, trying again only for the failures a retry can actually fix.

    The discipline is in what it does NOT retry. "Alguém comprou antes" is a
    final answer — repeating the question three times just wastes the seconds the
    next listing needs. Only TRANSIENT comes back here, and TRANSIENT means the
    request never got a real answer: a dropped connection, a timeout, a 502, a
    429. Those are worth another try; a no is not.

    `delays` is indexed per attempt, so the caller decides whether to be quick
    (racing) or patient (already holding the ticket).
    """
    last: CheckoutError | None = None
    for attempt in range(tries):
        try:
            return call()
        except CheckoutError as e:
            if e.kind != TRANSIENT or attempt == tries - 1:
                raise
            last = e
            wait = delays[min(attempt, len(delays) - 1)]
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                what, attempt + 1, tries, e, wait,
            )
            if progress:
                # The group sees the retry happening rather than a message that
                # sat still for nine seconds and then said it failed.
                progress.waiting(f"o site não respondeu, tentativa {attempt + 2}/{tries}")
            time.sleep(wait)
    raise last or CheckoutError(f"{what} falhou sem dizer por quê", TRANSIENT)


def _release(client: Client, listing_id: str) -> bool:
    """Gives the ticket back. Never raises; returns whether it worked.

    Called when anything after the lock goes wrong. A hold left behind blocks
    every other buyer for ten minutes, and on a cheap listing one of those other
    buyers is us on the next round — the site allows one reservation per account.

    The return value is the part that reaches a human: if all three attempts
    fail, the account is still holding a ticket nobody told the group about, and
    that turns the failure into a PENDING one with something to go and do.
    """
    for delay in (0.0, *UNLOCK_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            client.api(f"/marketplaces/listings/{listing_id}/unlock", method="POST")
            return True
        except CheckoutError as e:
            log.warning("unlock of %s failed: %s", listing_id, e)
    return False


def _wait_for_pix(
    client: Client, order: dict, order_id: str, progress: Progress | None = None
) -> tuple[str, str, bytes]:
    """The charge, re-reading the order until the payment service fills it in.

    Almost always present on the first read. It is polled anyway because the
    charge is generated by a third party: an empty `pixContent` on a created
    order means "not yet", not "never". The ticket is already reserved while this
    loops, so the wait costs nothing but the ten-minute clock.
    """
    code, expires_at, qr = _pix(order)
    for attempt in range(PIX_POLL_TRIES):
        if code:
            return code, expires_at, qr
        if progress:
            progress.waiting(
                f"o pagamento ainda não devolveu o código, "
                f"tentativa {attempt + 1}/{PIX_POLL_TRIES}"
            )
        time.sleep(PIX_POLL_DELAY)
        order = client.api(f"/marketplaces/orders/{order_id}")
        code, expires_at, qr = _pix(order)
    if not code:
        raise CheckoutError(
            f"o pedido foi criado e o ingresso está reservado, mas o Pix não veio "
            f"em {PIX_POLL_TRIES} tentativas. Abra o pedido e pague por lá — a "
            f"reserva está de pé.",
            PENDING,
            url=f"{config.SITE}/checkout/pix?order={order_id}",
        )
    return code, expires_at, qr


def purchase(
    offer, client: Client | None = None, progress: Progress | None = None
) -> PixCharge:
    """Listing -> Pix charge, synchronously. Reserves the ticket; never pays.

    The order of the steps is the whole safety argument:

    1. read the listing from the API, so the price being checked is the site's
       own number and not the one the watcher happened to see;
    2. check both ceilings against that number;
    3. lock — from here the ticket is ours for ten minutes at that price;
    4. create the order with Pix, which is what generates the charge;
    5. anything wrong after step 3 unlocks again.

    Steps 1, 3 and 4 retry, and the budget changes at step 3 because the meaning
    of time changes there. Before the lock a retry is competing with every other
    buyer, so it is three tries 400ms apart and then give up and let the next
    round find another listing. After the lock the ticket is already ours for ten
    minutes, so a retry competes with nothing: four tries backing off to four
    seconds, because at that point giving up is the expensive option.
    """
    started = time.monotonic()
    blank = config.missing_billing()
    if blank:
        raise CheckoutError(
            f"falta preencher no .env: {', '.join(blank)}. "
            f"Coloque você mesmo no arquivo.",
            SETUP,
        )

    progress = progress or Progress()
    client = client or Client()
    account = client.me()
    where = address(config.BILLING["zip_code"])
    progress.finished(1)

    listing = _retry(
        lambda: client.api(f"/marketplaces/listings/{offer.listing_id}"),
        "listing read", LOCK_TRIES, (LOCK_DELAY,), progress,
    )
    listed = _vet_listing(listing, offer)
    total = config.with_fee(listed)
    _check_ceilings(offer.sector, total)
    progress.finished(2)

    _retry(
        lambda: client.api(
            f"/marketplaces/listings/{offer.listing_id}/lock", method="POST"
        ),
        "lock", LOCK_TRIES, (LOCK_DELAY,), progress,
    )
    progress.finished(3)

    body = {
        "listingId": offer.listing_id,
        "paymentInformation": {
            "name": config.BILLING["name"] or account.get("name", ""),
            "document": config.BILLING["document"],
            "address": {
                **where,
                "number": config.BILLING["number"],
                **(
                    {"complement": config.BILLING["complement"]}
                    if config.BILLING["complement"]
                    else {}
                ),
            },
            # Hard-coded, never read from config: a card would cost 3.6% more and
            # would be paid the instant this call returns, with no human in
            # between. Pix is the only method this bot uses.
            "paymentMethod": "Pix",
            "creditCard": None,
        },
        "deliveryInformation": {"email": config.receiver_email()},
        "device": _fingerprint(),
    }
    try:
        order = _retry(
            lambda: client.api("/marketplaces/orders", body, method="POST"),
            "order create", ORDER_TRIES, ORDER_DELAYS, progress,
        )
    except CheckoutError as e:
        if _release(client, offer.listing_id):
            raise
        # The order failed AND the ticket did not come back. Nobody would ever
        # guess that from the original message, so it says so and stops being a
        # failure you retry — it is a failure you go and look at.
        raise CheckoutError(
            f"{e} E não consegui soltar a reserva depois, então essa conta está "
            f"segurando esse ingresso por até 10 min.",
            PENDING,
            url=f"{config.SITE}/checkout?listing={offer.listing_id}",
        ) from e

    method = (order.get("paymentInformation") or {}).get("paymentMethod")
    order_id = str(order.get("id") or "")
    if method != "Pix":
        # Cannot happen from the body above, which is exactly why it is checked:
        # if it ever does, something else decided how this account pays.
        raise CheckoutError(
            f"o pedido saiu como «{method}» e não Pix. NÃO pague nada até olhar.",
            PENDING,
            url=f"{config.SITE}/tickets/{order_id}",
        )

    code, expires_at, qr = _wait_for_pix(client, order, order_id, progress)
    progress.finished(4)

    return PixCharge(
        code=code,
        intact=pix_code_is_intact(code),
        total=_charged(order, fallback=total),
        expires_in=_left_to_pay(expires_at),
        seconds=time.monotonic() - started,
        url=f"{config.SITE}/checkout/pix?order={order_id}",
        qr=qr,
    )


def blockers() -> list[str]:
    """Everything that would make buy() fail before it touched the site.

    Exists so /armar can refuse now instead of at the moment it matters. There is
    no browser to check for any more — the whole flow is urllib — so what is left
    is the .env and the CEP.

    The CEP check is a real network call, on purpose, and it is the only one in
    this function. /armar and /comprar are the only callers and both are somebody
    typing a command, so the cost is paid where it is invisible; and because
    address() caches, the answer is already in memory when a ticket shows up. A
    CEP that cannot be resolved is otherwise a failure that waits for the worst
    possible moment to happen.
    """
    blank = config.missing_billing()
    if blank:
        return [f"falta preencher no .env: {', '.join(blank)}"]
    try:
        address(config.BILLING["zip_code"])
    except CheckoutError as e:
        return [str(e)]
    return []


def can_check() -> bool:
    """Whether there is a login to check a listing with. No network, no cost.

    blockers() is the thorough answer and it resolves a CEP, which is far too
    expensive to ask once every thirty seconds. This is the cheap one, for the
    watcher: without a CPF and a password there is nothing to check with, and the
    alert has to go out unchecked rather than not at all.
    """
    return bool(config.BILLING.get("document") and os.environ.get("PASS", "").strip())


async def inspect(listing_id: str) -> str:
    """"" if this listing is worth telling the group about, else why it is not.

    One authenticated read, and it exists because the public list lies by omission.
    On 20/09/2026 a Pista Inteira showed up at R$ 650 — below face value — and the
    group got an alert; by the time anybody tapped the link it was `LockedForSale`,
    locked in the same second it appeared, with `proofUrl: null` and created
    eighteen days earlier. From the /categories list, which carries only a price
    and an id, that listing is indistinguishable from the bargain of the year.
    It cost the reader a wasted tap and, worse, taught them to distrust the alert.

    Cheap where it matters: only listings that already passed the price cap get
    here, which on this event is a handful a week. Never raises — an unreachable
    site must not silence an alert, so anything unexpected means "go ahead", and
    purchase() checks all of this again before a single centavo moves.
    """
    def read() -> str:
        listing = Client().api(f"/marketplaces/listings/{listing_id}")
        if listing.get("status") != "Active":
            return f"o site diz «{listing.get('status')}» — alguém já pegou"
        if not listing.get("sellerId") or not listing.get("proofUrl"):
            return "não tem comprovante do vendedor"
        return ""

    try:
        return await asyncio.to_thread(read)
    except Exception:  # noqa: BLE001 — a failed check must never eat the alert
        log.warning("could not inspect listing %s before alerting", listing_id)
        return ""


# How often the saved session is touched so it never goes stale. Ten minutes is
# far more often than the cookies need — they last days — and that is the point:
# the failure this guards against is not the cookie expiring, it is discovering
# at the worst possible moment that the password changed or the account got
# logged out everywhere. Ten minutes means the bot finds that out while nothing
# is happening, and tells somebody.
KEEP_ALIVE_SECONDS = 600


async def keep_alive() -> str:
    """Touches the session so the login is already good when a ticket appears.

    "" while the account is fine, otherwise the sentence to show a human.

    Two jobs. It keeps the cookie jar warm, so the buy path does not spend its
    first second on a login while somebody else races for the same ticket; and it
    is the only thing that notices a broken login before there is money on the
    table. Client.me() re-logs in by itself on a 401, so one cheap authenticated
    read covers both.
    """
    def touch() -> str:
        try:
            Client().me()
        except CheckoutError as e:
            # A 401 here has exactly one meaning and the site's own answer for it
            # is a byte-empty body, so "HTTP 401: {}" is what a human would get
            # shown. Say the thing instead.
            if e.status == 401:
                return (
                    "o CPF e a senha do .env não entram mais nessa conta — "
                    "senha trocada, ou conta bloqueada"
                )
            return str(e)
        except Exception as e:  # noqa: BLE001 — a keep-alive must never crash the bot
            return f"não consegui conferir o login: {e}"
        return ""

    return await asyncio.to_thread(touch)


async def buy(offer, progress: Progress | None = None) -> PixCharge:
    """Listing -> Pix charge. Reserves the ticket; never pays for it.

    `progress` is written from the worker thread and read by whoever is drawing
    the message, so the group watches the four steps happen instead of staring at
    one line for however long the site takes.
    """
    return await asyncio.to_thread(purchase, offer, None, progress)

async def login() -> str:
    """Signs in and saves the cookies. Handy to run once by hand."""
    client = Client()
    user = await asyncio.to_thread(client.login)
    return str(user.get("name") or user.get("email") or "?")


if __name__ == "__main__":
    import sys

    def _find(listing_id: str, session: str):
        """Looks the listing up live, so the CLI cannot be told a wrong price."""
        for offer in monitor.fetch(config.SESSIONS[session], session):
            if offer.listing_id == listing_id:
                return offer
        raise SystemExit(f"não achei o anúncio {listing_id} em {session}.")

    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        print(f"Entrou como {asyncio.run(login())}. Cookies em {AUTH_FILE}.")
    elif len(sys.argv) > 1 and sys.argv[1] == "--whoami":
        account = Client().me()
        print(f"{account.get('name')} · CPF {account.get('document')}")
    elif len(sys.argv) > 2 and sys.argv[1] == "--cep":
        print(address(sys.argv[2]))
    elif len(sys.argv) > 3 and sys.argv[1] == "--buy":
        target = _find(sys.argv[2], sys.argv[3])
        print(f"comprando: {target}")
        charge = asyncio.run(buy(target))
        print(f"\nPix em {charge.seconds:.1f}s — total R$ {charge.total}")
        print(f"QR: {len(charge.qr)} bytes de PNG" if charge.qr else "QR: não veio")
        print(f"expira em {charge.expires_in or '?'} · {charge.url}")
        print(f"checksum {'ok' if charge.intact else 'NÃO CONFERE'}")
        print(f"\n{charge.code}")
    else:
        print("uso: python checkout.py --login")
        print("     python checkout.py --whoami")
        print("     python checkout.py --cep <cep>")
        print("     python checkout.py --buy <listing_id> <sessão>   # GERA COBRANÇA")
