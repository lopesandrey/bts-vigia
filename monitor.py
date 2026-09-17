"""Reads ticket offers from buyticketbrasil.

This is the only file that knows the site. To monitor a different one, write a
module exposing the same `fetch(url, session) -> list[Offer]`.

How it works: the page is server-rendered (Next.js) and embeds a `matriz_preco`
object keyed by "Sector||Category". We read that JSON instead of scraping the
rendered text, which is far more stable.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ParseError(RuntimeError):
    """The HTML arrived but the data was not in it — the site likely changed."""


def brl(value: Decimal | int | float) -> str:
    """1234.5 -> 'R$ 1.234,50'"""
    return "R$ " + f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


@dataclass(frozen=True)
class Offer:
    sector: str      # "Pista", "Cadeira Superior", ...
    category: str    # "Inteira", "Meia Estudante", "Meia PCD", ...
    price: Decimal   # in reais
    qty: int
    id_ref: str      # the site's own listing id, used to build a deep link
    session: str = ""  # date label, e.g. "30/10/2026 (sexta)"

    @property
    def key(self) -> str:
        return f"{self.session}||{self.sector}||{self.category}"

    def summary(self) -> str:
        return f"{self.sector} ({self.category}) — {brl(self.price)} · {self.qty} un."

    def __str__(self) -> str:
        where = f"{self.session} · " if self.session else ""
        return where + self.summary()


def _find_json(html: str, field: str) -> dict:
    """Extracts the JSON object at `field` from inside the Next.js payload.

    The payload arrives escaped (`\\"key\\":`), so we unescape one level and let
    the decoder find where the object ends.
    """
    for marker in (f'\\"{field}\\":', f'"{field}":'):
        i = html.find(marker)
        if i != -1:
            escaped = marker.startswith("\\")
            break
    else:
        raise ParseError(f"field {field!r} not found in the HTML")

    chunk = html[i + len(marker):]
    if escaped:
        # Unescape pair by pair, left to right: `\\` -> `\` and `\"` -> `"`.
        # A blind replace of `\"` with `"` would close the string in the wrong
        # place at the first escaped quote in any field, and would also corrupt
        # the block that arrives unescaped.
        chunk = re.sub(r"\\(.)", lambda m: m[1] if m[1] in '"\\' else m[0], chunk)
    try:
        obj, _ = json.JSONDecoder().raw_decode(chunk.lstrip())
    except json.JSONDecodeError as e:
        raise ParseError(f"field {field!r} is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ParseError(f"field {field!r} is not an object: {type(obj).__name__}")
    return obj


def parse(html: str, session: str = "") -> list[Offer]:
    """HTML -> available offers, cheapest first.

    Sold-out entries (qty 0 or price 0) are dropped: there is nothing to alert.
    """
    matrix = _find_json(html, "matriz_preco")
    offers = []
    recognized = 0
    for key, data in matrix.items():
        if "||" not in key or not isinstance(data, dict):
            continue
        if "disponivel" not in data or "preco_min" not in data:
            continue
        recognized += 1
        sector, category = (p.strip() for p in key.split("||", 1))
        qty = int(data.get("disponivel") or 0)
        cents = int(data.get("preco_min") or 0)
        if qty <= 0 or cents <= 0:
            continue
        offers.append(
            Offer(sector, category, Decimal(cents) / 100, qty,
                  str(data.get("id_ref", "")), session)
        )
    # A populated matrix with no entry in the expected shape means the site
    # changed. Without this the bot would return [] and the check would count as
    # a success: it would go blind in silence, the worst way for a watcher to
    # fail. Real sold-out keeps the keys with 0 values, so no false alarm.
    if matrix and not recognized:
        raise ParseError(
            f"matriz_preco has {len(matrix)} entries, none shaped like "
            f"'Sector||Category' with 'disponivel'/'preco_min' — the site changed"
        )
    return sorted(offers, key=lambda o: o.price)


def _get(url: str, timeout: float, rsc: bool) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}
    if rsc:
        headers["RSC"] = "1"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch(url: str, session: str = "", timeout: float = 10.0) -> list[Offer]:
    """Downloads the page and returns its offers. Raises OSError or ParseError.

    Asks for the RSC payload first: it is the same data Next.js sends when you
    navigate inside the app — identical offers, ~40% fewer bytes (18.7 KB vs
    31 KB gzipped, measured). At one round every 30 seconds that is the polite
    thing to do. RSC is an internal Next.js protocol though, so if it ever stops
    carrying the data we fall back to the plain HTML: a framework upgrade on
    their side must not blind the bot.

    The fallback deliberately does NOT catch HTTPError — retrying a 429 would
    hammer the site exactly when it is asking us to stop.
    """
    try:
        return parse(_get(url, timeout, rsc=True), session)
    except ParseError:
        return parse(_get(url, timeout, rsc=False), session)


if __name__ == "__main__":  # sanity check: python monitor.py <url|file.html>
    import sys

    target = sys.argv[1]
    if target.startswith("http"):
        found = fetch(target)
    else:
        with open(target, encoding="utf-8") as f:
            found = parse(f.read())
    print(f"{len(found)} offers\n")
    for offer in found:
        print(" ", offer)
