"""Find the Kalshi market that is the same bet as a Polymarket sports market.

US users can't trade Polymarket's main exchange, so a copy is only actionable
where Kalshi lists the same game. The game is found in the league's GAME
series (date code in the event ticker, both teams by code or name); spreads,
totals and first-inning runs live in sibling series whose events share the
game event's suffix (KXEPLGAME-26OCT18ARSCHE -> KXEPLTOTAL-26OCT18ARSCHE).

A match is a suggestion with its labels shown, never a trade: the alert prints
Kalshi's own market title next to Polymarket's so a wrong pairing is visible.
"""

from __future__ import annotations

import datetime as dt
import re

from polymarket_watch.poly import Http, fold, num

BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Polymarket's league prefix (the first part of an event slug like "lal-ala-val-2026-09-15") -> Kalshi stem.
LEAGUES = {
    "mlb": "MLB", "nfl": "NFL", "cfb": "NCAAF", "nba": "NBA", "nhl": "NHL", "wnba": "WNBA", "cbb": "NCAAMB",
    "mls": "MLS", "epl": "EPL", "lal": "LALIGA", "sea": "SERIEA", "bun": "BUNDESLIGA", "fl1": "LIGUE1",
    "ucl": "UCL", "uel": "UEL", "uecl": "UECL", "ere": "EREDIVISIE", "por": "LIGAPORTUGAL", "mex": "LIGAMX",
    "bra": "BRASILEIRO", "arg": "ARGPREMDIV", "elc": "EFLCHAMPIONSHIP", "tur": "SUPERLIG", "unl": "UEFANL",
    "col": "DIMAYOR", "kor": "KLEAGUE", "jap": "JLEAGUE", "spl": "SCOTTISHPREM", "bel": "BELGIANPL",
    "den": "DENSUPERLIGA", "nor": "ELITESERIEN", "swe": "ALLSVENSKAN", "aus": "ALEAGUE", "sau": "SAUDIPL",
    "lib": "CONMEBOLLIB", "sud": "CONMEBOLSUD", "ser": "SERIEB", "bun2": "BUNDESLIGA2", "fl2": "LIGUE2",
    "lal2": "LALIGA2", "efl1": "EFLL1", "fri": "INTLFRIENDLY", "wcq": "WC", "atp": "ATP", "wta": "WTA",
    "efl": "EFLCHAMPIONSHIP", "aut": "AUTBSL", "bra2": "BRASILEIROB", "es2": "LALIGA2", "fif": "INTLFRIENDLY",
    "cs2": "CS2", "lol": "LOL",
}
# Tried in turn for a soccer market whose league prefix is not in LEAGUES.
SOCCER_FALLBACK = ["EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL", "UEL", "EREDIVISIE", "LIGAPORTUGAL",
                   "LIGAMX", "BRASILEIRO", "MLS", "INTLFRIENDLY", "UEFANL", "AFCON", "WC"]
TENNIS = {"ATP": ["KXATPMATCH", "KXATPCHALLENGERMATCH"], "WTA": ["KXWTAMATCH", "KXWTACHALLENGERMATCH"]}
STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "us", "cd", "ud", "rc", "rcd", "sd", "sv", "vfb", "vfl", "tsg",
        "fk", "sk", "bk", "if", "club", "de", "del", "la", "the", "calcio", "balompie", "futbol", "cp", "sad", "bv",
        "09", "04", "05", "1899", "1846", "1900", "1910", "wins", "win", "and"}
# Words many teams share; a match on one of these alone must not pick a team ("Real Madrid" is not
# "Real Sociedad", "New York J" is not the Giants).
WEAK = {"real", "united", "city", "athletic", "atletico", "sporting", "inter", "racing", "deportivo", "union",
        "dynamo", "dinamo", "olympique", "borussia", "new", "york", "los", "angeles", "san", "state", "saint",
        "north", "south", "east", "west", "central", "university", "college", "academy", "town", "county", "rovers",
        "wanderers", "athletico", "independiente", "nacional", "olimpia", "universidad"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def taker_fee(price: float) -> float:
    """Kalshi's taker fee per contract, 7% of P*(1-P), before the per-order rounding up to the cent."""
    return 0.07 * price * (1 - price)


def date_code(day: dt.date) -> str:
    return f"{day:%y}{MONTHS[day.month - 1]}{day:%d}"


def tokens(s: str) -> list[str]:
    s = re.sub(r"\bst\b\.?", "state", fold(s).replace(".", " ").replace("&", " "))
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t and t not in STOP]


def name_score(kalshi_name: str, team: str) -> float:
    """How well a Kalshi label ("Los Angeles D", "Man City", "Alaves") fits a Polymarket name. Exact
    words first, then abbreviations against the words still unused."""
    q, t = tokens(kalshi_name), tokens(team)
    if not q or not t:
        return 0.0
    left, score = list(t), 0.0
    for w in q:
        if w in left:
            left.remove(w)
            score += 0.5 if w in WEAK else 2
    for w in q:
        if w in t and w not in left:
            continue
        hit = next((x for x in left if x.startswith(w) or (len(x) >= 4 and w.startswith(x))), None)
        if hit:
            left.remove(hit)
            score += 1
        else:
            score -= 1
    return score / len(q)


class Kalshi:
    """Open markets per series, fetched once per tick."""

    def __init__(self, http: Http):
        self.http, self.cache = http, {}

    def series(self, ticker: str) -> list[dict]:
        if ticker not in self.cache:
            out, cursor = [], None
            try:
                for _ in range(10):
                    page = self.http.get(f"{BASE}/markets", {"series_ticker": ticker, "status": "open", "limit": 1000,
                                                             **({"cursor": cursor} if cursor else {})})
                    out += page.get("markets") or []
                    cursor = page.get("cursor")
                    if not cursor or not page.get("markets"):
                        break
            except Exception as e:
                print(f"kalshi {ticker}: {str(e)[:160]}", flush=True)
            self.cache[ticker] = out
        return self.cache[ticker]

    def market(self, ticker: str) -> dict:
        return (self.http.get(f"{BASE}/markets/{ticker}") or {}).get("market") or {}


def ask(m: dict, side: str) -> float | None:
    if side == "yes":
        return num(m.get("yes_ask_dollars"))
    no = num(m.get("no_ask_dollars"))
    bid = num(m.get("yes_bid_dollars"))
    return no if no is not None else (round(1 - bid, 4) if bid is not None else None)


def label(m: dict) -> str:
    return str(m.get("yes_sub_title") or m.get("title") or m.get("ticker") or "")


def code_of(m: dict) -> str:
    return str(m.get("ticker") or "").rsplit("-", 1)[-1].upper()


def slug_parts(event_slug: str) -> tuple[str, list[str], dt.date | None]:
    """"lal-ala-val-2026-09-15" -> ("lal", ["ala", "val"], 2026-09-15). Side-market events carry a
    tail ("unl-eng-cze-2026-10-06-more-markets")."""
    m = re.match(r"^([a-z0-9]+)-(.+?)-(\d{4})-(\d{2})-(\d{2})(?:-[a-z-]+)?$", event_slug or "")
    if not m:
        return (event_slug or "").split("-")[0], [], None
    try:
        day = dt.date(int(m[3]), int(m[4]), int(m[5]))
    except ValueError:
        day = None
    return m[1], m[2].split("-"), day


def split_teams(text: str) -> list[str]:
    """"Deportivo Alavés vs. Valencia CF: O/U 2.5" -> ["Deportivo Alavés", "Valencia CF"]."""
    text = re.sub(r"^[^:]*\?:\s*", "", text)          # "Will there be a run ...?: A vs. B"
    text = re.sub(r"^[A-Za-z ]+(WTA|ATP)[^:]*:\s*", "", text)  # "US Open WTA: A vs B"
    text = re.split(r":\s*(O/U|1H|Total|Spread)", text)[0]
    parts = re.split(r"\s+vs\.?\s+|\s+@\s+|\s+at\s+", text, maxsplit=1)
    return [p.strip(" ?") for p in parts] if len(parts) == 2 else []


def what(m: dict, outcome: str, codes: list[str], slug: str) -> dict | None:
    """The bet in plain terms: kind (winner/draw/spread/total/rfi), the team it is about and its
    opponent (name and slug code), the line, and whether it is the YES side of that question."""
    q, kind = m.get("question") or "", m.get("type") or ""
    outs = m.get("outcomes") or []
    # Half-time, exact-score and handicap markets have no full-game twin; never map them to one.
    if kind not in ("", "moneyline", "spreads", "totals", "nrfi") or re.search(
            r"\b1H\b|first half|exact score|handicap|\bmap\b", q, re.I):
        return None
    teams = split_teams(m.get("event_title") or "") or split_teams(q)
    if kind == "nrfi" or "run scored in the first inning" in q.lower():
        return {"kind": "rfi", "teams": teams, "codes": codes, "yes": outcome.lower() == "yes"}
    if kind == "totals" or "O/U" in q:
        line = m.get("line") or num((re.search(r"O/U\s*([\d.]+)", q) or [None, None])[1])
        if line is None or outcome.lower() not in ("over", "under"):
            return None
        return {"kind": "total", "teams": teams, "codes": codes, "line": line, "yes": outcome.lower() == "over"}
    if kind == "spreads" or q.startswith("Spread:"):
        sm = re.match(r"Spread:\s*(.+?)\s*\(([-+]?[\d.]+)\)", q)
        if not sm or len(outs) != 2:
            return None
        fav, line = sm[1], abs(float(sm[2]))
        fav_i = max(range(2), key=lambda i: name_score(outs[i], fav))
        return {"kind": "spread", "team": outs[fav_i], "opp": outs[1 - fav_i],
                "code": codes[fav_i] if len(codes) == 2 else "", "opp_code": codes[1 - fav_i] if len(codes) == 2 else "",
                "line": line, "yes": outcome == outs[fav_i]}
    if "draw" in q.lower() and outcome.lower() in ("yes", "no"):
        return {"kind": "draw", "teams": teams, "codes": codes, "yes": outcome.lower() == "yes"}
    wm = re.match(r"Will (.+?) win\b", q)
    if wm and outcome.lower() in ("yes", "no"):
        team = wm[1]
        code = slug.rsplit("-", 1)[-1] if slug and not re.search(r"\d{2}$", slug) else ""
        opp = next((t for t in teams if name_score(t, team) <= 0), "")
        opp_code = next((c for c in codes if c != code), "") if code in codes else ""
        return {"kind": "winner", "team": team, "opp": opp, "code": code, "opp_code": opp_code,
                "yes": outcome.lower() == "yes"}
    if len(outs) == 2 and outcome in outs:
        i = outs.index(outcome)
        return {"kind": "winner", "team": outs[i], "opp": outs[1 - i],
                "code": codes[i] if len(codes) == 2 else "", "opp_code": codes[1 - i] if len(codes) == 2 else "",
                "yes": True}
    return None


def fit(m: dict, name: str, code: str) -> float:
    """How well a Kalshi game market fits one team: a code match is decisive, a name match counts."""
    if code and code_of(m) == code.upper():
        return 3.0
    return name_score(label(m), name) if name else 0.0


def find_game(markets: list[dict], days: set[str], teams: list[tuple[str, str]]) -> tuple[str, dict] | None:
    """(event ticker, {team index: market}) of the one game on these days that fits every team given."""
    by_event = {}
    for m in markets:
        et = str(m.get("event_ticker") or "")
        if et.split("-", 1)[-1][:7] in days:
            by_event.setdefault(et, []).append(m)
    scored = []
    for et, ms in by_event.items():
        sides = [x for x in ms if fold(label(x)) not in ("tie", "draw")]
        picks, total = {}, 0.0
        for i, (name, code) in enumerate(teams):
            if not name and not code:
                continue
            best = max(sides, key=lambda x: fit(x, name, code), default=None)
            if best is None or fit(best, name, code) <= 0 or best in picks.values():
                break
            picks[i], total = best, total + fit(best, name, code)
        else:
            if picks:
                scored.append((total, et, picks, ms))
    scored.sort(key=lambda s: -s[0])
    if not scored or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1], {"picks": scored[0][2], "markets": scored[0][3]}


def match(k: Kalshi, m: dict, outcome: str, event_slug: str, slug: str) -> dict:
    """The Kalshi side of a Polymarket bet: ticker, side, Kalshi's label and ask, or a note saying why not."""
    prefix, codes, day = slug_parts(event_slug or m.get("event_slug") or "")
    if day is None and m.get("game_start"):
        day = dt.datetime.fromtimestamp(m["game_start"], dt.timezone.utc).date()
    bet = what(m, outcome, codes, slug)
    if not bet:
        return {"note": "bet type not mapped to Kalshi"}
    if day is None:
        return {"note": "no game date"}
    days = {date_code(day + dt.timedelta(days=d)) for d in (-1, 0, 1)}
    stem = LEAGUES.get(prefix)
    if stem in TENNIS:
        game_series = TENNIS[stem]
    elif stem:
        game_series = [f"KX{stem}GAME"]
    elif m.get("category") == "sports" and bet["kind"] in ("winner", "draw", "total", "spread"):
        game_series = [f"KX{s}GAME" for s in SOCCER_FALLBACK]
    else:
        return {"note": f"league '{prefix}' not on the Kalshi map"}

    if bet["kind"] in ("winner", "spread"):
        teams = [(bet["team"], bet.get("code", "")), (bet.get("opp", ""), bet.get("opp_code", ""))]
    else:
        names = bet.get("teams") or ["", ""]
        cs = bet.get("codes") if len(bet.get("codes") or []) == 2 else ["", ""]
        teams = list(zip(names + [""] * (2 - len(names)), cs))
    found, series = None, None
    for s in game_series:
        found = find_game(k.series(s), days, teams)
        if found:
            series = s
            break
    if not found:
        return {"note": "no matching Kalshi game"}
    event, game = found
    suffix = event.split("-", 1)[1]

    if bet["kind"] == "winner":
        mk, side = game["picks"][0], "yes" if bet["yes"] else "no"
    elif bet["kind"] == "draw":
        mk = next((x for x in game["markets"] if fold(label(x)) in ("tie", "draw")), None)
        side = "yes" if bet["yes"] else "no"
    else:
        stem_series = series[:-4] if series.endswith("GAME") else series
        sibling = {"spread": f"{stem_series}SPREAD", "total": f"{stem_series}TOTAL", "rfi": f"{stem_series}RFI"}[
            bet["kind"]]
        ladder = [x for x in k.series(sibling) if x.get("event_ticker") == f"{sibling}-{suffix}"]
        if bet["kind"] == "rfi":
            mk, side = (ladder[0] if len(ladder) == 1 else None), "yes" if bet["yes"] else "no"
        else:
            if bet["kind"] == "spread":
                fav = game["picks"][0]
                ladder = [x for x in ladder if code_of(x).startswith(code_of(fav)) or
                          name_score(re.split(r"\s+wins\b", label(x))[0], bet["team"]) > 0]
            exact = [x for x in ladder if num(x.get("floor_strike")) == bet["line"]]
            mk, side = (exact[0] if len(exact) == 1 else None), "yes" if bet["yes"] else "no"
            if mk is None and ladder:
                near = sorted({num(x.get("floor_strike")) for x in ladder} - {None})
                return {"note": f"no {bet['line']:g} line on Kalshi (has {', '.join(f'{x:g}' for x in near)})",
                        "event": f"{sibling}-{suffix}"}
    if mk is None:
        return {"note": "Kalshi game found but not this market", "event": event}
    price = ask(mk, side)
    return {"ticker": mk["ticker"], "side": side, "label": label(mk), "ask": price,
            "payout": round(1 / (price + taker_fee(price)), 2) if price and 0 < price < 1 else None}


def settle(k: Kalshi, ticker: str, side: str) -> str | None:
    """"win" or "loss" once Kalshi has settled the market, else None."""
    try:
        result = k.market(ticker).get("result")
    except Exception as e:
        print(f"kalshi {ticker}: {str(e)[:120]}", flush=True)
        return None
    if result not in ("yes", "no"):
        return None
    return "win" if result == side else "loss"
