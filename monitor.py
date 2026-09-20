"""Reads ticket offers from buyticketbrasil.

This is the only file that knows the site. To monitor a different one, write a
module exposing the same `fetch(session_id, session) -> list[Offer]`.

The site was rewritten in September 2026 and nothing about the old approach
survived, so the shape of this module is worth explaining.

It used to be one HTML page per night with a `matriz_preco` JSON object baked
into the server-rendered Next.js payload, which we regex-unescaped and decoded.
That object is gone. The pages still render server-side, but prices now arrive in
the browser from a JSON API, so there is nothing to scrape out of the HTML — a
page fetch returns markup with no price in it at all.

What replaced it is better: a plain REST API, two levels deep.

  /sessions/{session}/tickets
      the sectors of that night, each with a stable `ticketId`
  /sessions/{session}/tickets/{ticketId}/categories
      per category: the price and, crucially, the `listingId`

Only the second level is trustworthy, and that cost real time to learn. The
first level carries an `amount` that looks like a minimum price and is not: on
28/10 it reported Pista at R$ 350 while the cheapest Pista listing anybody could
actually buy was R$ 1,900. Whatever that number means, it is not a price on
offer. So we use `/tickets` purely to learn the sector names and their ids, and
read every price from `/categories` — which is also the only place the
`listingId` appears, and without a `listingId` there is nothing to buy.

That id mapping is stable, so it is cached for the life of the process: the
per-round cost is one request per sector per night, roughly 500 bytes each. That
is about 6 KB a round against the old 54 KB, in more requests.

One thing to know before touching this: robots.txt disallows `/api/`. That rule
addresses crawlers building an index, which is not this, and the alternative is
strictly worse for the site — the prices only exist after the page's JavaScript
runs, so avoiding the API means driving a real browser every round, which costs
them a Next.js render plus every asset instead of half a kilobyte of JSON. We
went with the lighter request and a slow cadence. It is still their endpoint and
not a contract, so expect it to move and keep ParseError meaningful.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

import config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Politeness brake. These codes mean "you are going too fast" or "you are not
# welcome right now". Pushing through them is exactly how a watcher earns a
# blocked IP — permanent blindness, much worse than being a few minutes late.
# Lives here rather than in bot.py because fetch() is the code that has to obey
# it; bot.py re-exports this one so there is a single list.
PUSHBACK_CODES = {403, 429, 503}

# Pause between the per-sector calls inside one night. A round is a dozen small
# requests now instead of three big ones, and firing them back to back is what
# earned a read timeout while this module was being written. A tenth of a second
# costs nothing against a 30-second cadence.
SPACING = 0.1

# sector name -> ticketId, per session. Those ids do not change between rounds,
# so learning them once per process turns a two-level lookup into a one-level
# one. Cleared for a session whenever a sector we expect goes missing, which is
# the signal that the event was re-created on their side.
_TICKET_IDS: dict[str, dict[str, str]] = {}


class ParseError(RuntimeError):
    """The response arrived but the data was not in it — the site likely changed."""


def brl(value: Decimal | int | float) -> str:
    """1234.5 -> 'R$ 1.234,50'"""
    return "R$ " + f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


@dataclass(frozen=True)
class Offer:
    sector: str      # "Pista", "Cadeira Superior", ...
    category: str    # "Inteira", "Meia Idoso", ...
    listed: Decimal  # the number the site shows on the listing, before the fee
    price: Decimal   # listed + the 10% service fee: what actually leaves the bank
    listing_id: str  # the site's own listing id, and the whole of the buy link
    session: str = ""  # date label, e.g. "30/10/2026 (sexta)"

    @property
    def key(self) -> str:
        return f"{self.session}||{self.sector}||{self.category}"

    def summary(self) -> str:
        return f"{self.sector} ({self.category}) — {brl(self.price)}"

    def __str__(self) -> str:
        where = f"{self.session} · " if self.session else ""
        return where + self.summary()


def _get(path: str, timeout: float) -> dict:
    """GETs one API path and returns the decoded object."""
    req = urllib.request.Request(
        config.API + path,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ParseError(f"{path} did not return JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseError(f"{path} returned {type(data).__name__}, expected an object")
    return data


def _money(raw: object) -> Decimal:
    """The API sends amounts as JSON numbers, sometimes 1000, sometimes 1780.00000."""
    try:
        return Decimal(str(raw))
    except Exception as e:  # noqa: BLE001 — any unparseable amount is a parse failure
        raise ParseError(f"amount {raw!r} is not a number") from e


def _ticket_ids(session_id: str, timeout: float) -> dict[str, str]:
    """sector name -> ticketId for that night, cached for the process.

    Only the sectors with a published face value are kept. A Camarote can never
    pass price_cap() — there is no face value to be fair against, so
    config.price_cap returns None and every gate refuses it. Polling those would
    be four extra requests a night to compute a guaranteed "no".
    """
    cached = _TICKET_IDS.get(session_id)
    if cached:
        return cached
    data = _get(f"/sessions/{session_id}/tickets", timeout)
    tickets = data.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        raise ParseError(f"session {session_id} listed no tickets — the site changed")
    found = {
        t["ticketType"]: t["ticketId"]
        for t in tickets
        if isinstance(t, dict) and t.get("ticketType") and t.get("ticketId")
    }
    wanted = {name: tid for name, tid in found.items() if config.face_value(name)}
    if not wanted:
        raise ParseError(
            f"session {session_id} has sectors {sorted(found)}, none of them in "
            f"config.FACE_VALUE ({sorted(config.FACE_VALUE)}) — a sector was renamed"
        )
    _TICKET_IDS[session_id] = wanted
    return wanted


def parse_categories(data: dict, sector: str, session: str) -> list[Offer]:
    """One /categories response -> the offers in it that can actually be bought.

    A category with no `listingId` is not on sale: the site returns the whole
    category list every time, with zeros for the ones nobody is offering. Those
    are dropped, so an empty result means "nothing for sale here", not an error.
    """
    categories = data.get("categories")
    if not isinstance(categories, list):
        raise ParseError(f"{sector}: response has no 'categories' list")
    offers = []
    for entry in categories:
        if not isinstance(entry, dict) or "amount" not in entry or "name" not in entry:
            raise ParseError(
                f"{sector}: a category entry is missing 'name' or 'amount' "
                f"({entry!r}) — the site changed"
            )
        listing_id = entry.get("listingId")
        listed = _money((entry.get("amount") or {}).get("value", 0))
        if not listing_id or listed <= 0:
            continue
        offers.append(
            Offer(
                sector=sector,
                category=str(entry["name"]).strip(),
                listed=listed,
                price=config.with_fee(listed),
                listing_id=str(listing_id),
                session=session,
            )
        )
    return offers


def fetch(session_id: str, session: str = "", timeout: float = 10.0) -> list[Offer]:
    """Every buyable offer for one night, cheapest first.

    Raises OSError or ParseError. HTTP errors are left to the caller on purpose:
    bot.py turns 403/429/503 into a cooldown and reports the rest, and a retry
    from in here would only double the load on a site already saying no.
    """
    sectors = _ticket_ids(session_id, timeout)
    offers: list[Offer] = []
    for i, (sector, ticket_id) in enumerate(sorted(sectors.items())):
        if i:
            time.sleep(SPACING)
        try:
            data = _get(
                f"/sessions/{session_id}/tickets/{ticket_id}/categories", timeout
            )
        except urllib.error.HTTPError as e:
            # A sector id that stopped resolving means the cached map is stale.
            # Drop it so the next round relearns instead of 404ing forever.
            if e.code == 404:
                _TICKET_IDS.pop(session_id, None)
            raise
        offers.extend(parse_categories(data, sector, session))
    return sorted(offers, key=lambda o: o.price)


if __name__ == "__main__":  # sanity check: python monitor.py [session label]
    import sys

    wanted = sys.argv[1:] or list(config.SESSIONS)
    for label in wanted:
        matches = [s for s in config.SESSIONS if label in s]
        if not matches:
            raise SystemExit(f"no session matches {label!r}: {list(config.SESSIONS)}")
        for name in matches:
            found = fetch(config.SESSIONS[name], name)
            print(f"\n{name} — {len(found)} offers")
            for offer in found:
                cap = config.price_cap(offer.sector)
                verdict = "-" if cap is None else ("ok" if offer.price <= cap else " ")
                print(
                    f"  [{verdict}] {offer.sector:20} {offer.category:24} "
                    f"anúncio {brl(offer.listed):>13}  total {brl(offer.price):>13}"
                )
