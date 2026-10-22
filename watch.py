#!/usr/bin/env python3
"""Doomsday watch: alerts you when Avengers: Doomsday showtimes go on sale at
Cineplex Junxion Erin Mills (Mississauga), and sends a quieter ping whenever the
theatre posts its next batch of showtimes, so you can learn when drops happen.

Each run does one check (GitHub Actions runs it every ~5 min). Locally you can
loop with --every SECONDS. Python 3.9+, standard library only.

Alert channels (set at least one):
  NTFY_TOPIC           your secret ntfy.sh topic -> push to the ntfy phone app
  DISCORD_WEBHOOK_URL  a Discord channel webhook
  DISCORD_USER_ID      optional: your Discord user id, so you get @-mentioned

Optional settings:
  WATCH_DATES       dates to watch, e.g. "2026-12-16..2026-12-20" or "2026-12-17,2026-12-18"
  MOVIE_PATTERN     regex for the film title (default: avengers.*doomsday)
  THEATRE_ID        Cineplex location id (default 7313 = Junxion Erin Mills)
  CINEPLEX_API_KEY  override Cineplex's public API key if it ever rotates
  WINDOW_ALERTS     "off" stops the new-showtimes-posted pings
  NTFY_SERVER, NTFY_TOKEN, FAIL_ALERT_AFTER (default 3)
  STATE_FILE        where it remembers what it already sent (default state.json on GitHub
                    Actions, state.local.json for local runs so the two never clash)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Toronto")
except Exception:  # Windows without the tzdata package: use the computer's own time zone
    TZ = None


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ) if TZ else dt.datetime.now().astimezone()

API_BASE = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"
# Not a secret: it's the static key cineplex.com's own frontend sends with every
# showtimes request. If Cineplex rotates it, the bot re-reads the new one from
# cineplex.com's JS bundle (technique from github.com/yinbri/cineplex-mcp).
BUNDLED_API_KEY = "dcdac5601d864addbc2675a2e96cb1f8"
CINEPLEX_HOME = "https://www.cineplex.com/"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BOT_UA = "doomsday-watch/1.0"  # Discord's CDN rejects Python's default user agent
DEFAULT_DATES = "2026-12-16..2026-12-20"  # early screenings + opening weekend (opens Fri Dec 18)
# Every seat at Junxion is a recliner; these labels add nothing to an alert.
NOISE_FORMATS = {"recliners", "recliner seating", "reserved seating", "regular", "standard"}

_sleep = time.sleep  # swapped out in tests


# --------------------------------------------------------------------------- config


def parse_dates(spec: str) -> list[dt.date]:
    dates: set[dt.date] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            start, end = (dt.date.fromisoformat(x.strip()) for x in part.split("..", 1))
            while start <= end:
                dates.add(start)
                start += dt.timedelta(days=1)
        else:
            dates.add(dt.date.fromisoformat(part))
    return sorted(dates)


@dataclass
class Config:
    theatre_id: str = "7313"
    theatre_name: str = "Junxion Erin Mills"
    theatre_url: str = "https://www.cineplex.com/theatre/cineplex-junxion-erin-mills"
    movie_pattern: re.Pattern = re.compile(r"avengers.*doomsday", re.I)
    dates: list = field(default_factory=lambda: parse_dates(DEFAULT_DATES))
    api_key: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    discord_webhook: str | None = None
    discord_user_id: str | None = None
    fail_alert_after: int = 3
    state_file: str = "state.json"
    window_alerts: bool = True

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        def get(name):
            value = (env.get(name) or "").strip()
            return value or None

        cfg = cls()
        cfg.theatre_id = get("THEATRE_ID") or cfg.theatre_id
        if get("THEATRE_ID") and get("THEATRE_ID") != "7313":
            cfg.theatre_name = f"Cineplex theatre {cfg.theatre_id}"
            cfg.theatre_url = "https://www.cineplex.com/theatres"
        if get("MOVIE_PATTERN"):
            cfg.movie_pattern = re.compile(get("MOVIE_PATTERN"), re.I)
        if get("WATCH_DATES"):
            cfg.dates = parse_dates(get("WATCH_DATES"))
        cfg.api_key = get("CINEPLEX_API_KEY")
        cfg.ntfy_server = get("NTFY_SERVER") or cfg.ntfy_server
        cfg.ntfy_topic = get("NTFY_TOPIC")
        if cfg.ntfy_topic and "/" in cfg.ntfy_topic:  # accept a pasted URL like ntfy.sh/mytopic
            server, _, cfg.ntfy_topic = cfg.ntfy_topic.rstrip("/").rpartition("/")
            if server and not get("NTFY_SERVER"):
                cfg.ntfy_server = server if "://" in server else f"https://{server}"
        cfg.ntfy_token = get("NTFY_TOKEN")
        cfg.discord_webhook = get("DISCORD_WEBHOOK_URL")
        cfg.discord_user_id = get("DISCORD_USER_ID")
        cfg.fail_alert_after = int(get("FAIL_ALERT_AFTER") or cfg.fail_alert_after)
        cfg.state_file = get("STATE_FILE") or (
            "state.json" if env.get("GITHUB_ACTIONS") == "true" else "state.local.json")
        cfg.window_alerts = (get("WINDOW_ALERTS") or "on").lower() not in ("off", "false", "0", "no")
        return cfg


# --------------------------------------------------------------------------- logging


def log(msg: str) -> None:
    print(f"[{now_local():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def warn(msg: str) -> None:
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "WARNING: "
    print(f"{prefix}{msg}", flush=True)


# --------------------------------------------------------------------------- HTTP


def http_request(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                 timeout: float = 20) -> tuple[int, bytes]:
    """Returns (status, body). Raises OSError on network failure."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, (err.read() if err.fp else b"")


class ApiError(Exception):
    pass


class KeyRejected(ApiError):
    """HTTP 401/403: the public API key rotated, or this network is being blocked."""


def fetch_showtimes(theatre_id: str, date: dt.date, key: str):
    query = urllib.parse.urlencode({"language": "en", "locationId": theatre_id, "date": date.isoformat()})
    url = f"{API_BASE}/showtimes?{query}"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Accept": "application/json",
        "User-Agent": BROWSER_UA,
        "Referer": CINEPLEX_HOME,
    }
    last_error: ApiError | None = None
    for attempt in range(3):
        if attempt:
            _sleep(2 * attempt)
        try:
            status, body = http_request("GET", url, headers)
        except OSError as err:
            last_error = ApiError(f"network error: {err}")
            continue
        if status in (401, 403):
            raise KeyRejected(f"HTTP {status}")
        if status in (204, 404):
            return []
        if status in (408, 429) or status >= 500:
            last_error = ApiError(f"HTTP {status}")
            continue
        if status != 200:
            raise ApiError(f"HTTP {status}")
        if not body.strip():
            return []
        try:
            return json.loads(body)
        except ValueError:
            raise ApiError("got a non-JSON response (blocked, or Cineplex changed its API)") from None
    raise last_error or ApiError("request failed")


def extract_api_key(source: str) -> str | None:
    """Find the theatrical-API key in a cineplex.com JS chunk. Anchored on the API
    URL because the bundle also carries unrelated decoy keys."""
    anchor = "apis.cineplex.com/prod/cpx/theatrical/api"
    pattern = re.compile(r'Ocp-Apim-Subscription-Key"?\s*:\s*"([0-9a-f]{32})"', re.I)
    at = source.find(anchor)
    while at != -1:
        match = pattern.search(source, at, at + 400) or pattern.search(source, max(0, at - 400), at)
        if match:
            return match.group(1)
        at = source.find(anchor, at + len(anchor))
    return None


def discover_api_key() -> str | None:
    try:
        status, html = http_request("GET", CINEPLEX_HOME, {"User-Agent": BROWSER_UA})
        if status != 200:
            return None
        refs = re.findall(r"""["'(]([^"'()]*_next/static/[^"')]*?\.js)""", html.decode("utf-8", "replace"))
        chunks = list(dict.fromkeys(urllib.parse.urljoin(CINEPLEX_HOME, ref) for ref in refs))
        for url in chunks[:25]:
            status, source = http_request("GET", url, {"User-Agent": BROWSER_UA})
            if status == 200:
                key = extract_api_key(source.decode("utf-8", "replace"))
                if key:
                    return key
    except OSError:
        return None
    return None


class Cineplex:
    """Showtimes client: caches per date, recovers once per run from a rotated key."""

    def __init__(self, cfg: Config, state: dict):
        self.cfg = cfg
        self.state = state
        self.key = cfg.api_key or state.get("api_key") or BUNDLED_API_KEY
        self.tried_rediscovery = False
        self.cache: dict = {}

    def showtimes(self, date: dt.date):
        if date not in self.cache:
            self.cache[date] = self._fetch(date)
        return self.cache[date]

    def _fetch(self, date: dt.date):
        try:
            return fetch_showtimes(self.cfg.theatre_id, date, self.key)
        except KeyRejected as err:
            if self.cfg.api_key:
                raise ApiError(f"Cineplex refused your CINEPLEX_API_KEY secret ({err}); update it") from None
            if self.tried_rediscovery:
                raise ApiError(f"Cineplex refused the request ({err})") from None
            self.tried_rediscovery = True
            new_key = discover_api_key()
            if not new_key or new_key == self.key:
                raise ApiError(
                    f"Cineplex refused the request ({err}): either its API key rotated and no new one was found "
                    "on cineplex.com, or it is blocking this network"
                ) from None
            self.key = new_key
            try:
                data = fetch_showtimes(self.cfg.theatre_id, date, self.key)
            except KeyRejected as again:
                raise ApiError(f"Cineplex refused the key re-read from cineplex.com too ({again})") from None
            self.state["api_key"] = new_key
            log("Cineplex rotated its API key; picked up the new one from cineplex.com.")
            return data


# --------------------------------------------------------------------------- parsing


@dataclass
class Session:
    id: str
    start: dt.datetime
    formats: list
    sold_out: bool
    buy_url: str
    is_dbox_area: bool = False


def iter_movies(node):
    """Every movie object (a dict with a name and an experiences list) in a payload."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("title")
        if isinstance(name, str) and isinstance(node.get("experiences"), list):
            yield node
            return
        for value in node.values():
            yield from iter_movies(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_movies(value)


def _buy_url(theatre_id: str, raw: dict) -> tuple[str, bool]:
    """Cineplex's public seat-selection page for a showtime, plus whether it's the D-BOX seating area."""
    match = re.search(r"[?&]dbox=(\w+)", raw.get("seatMapUrl") or "")
    dbox = match.group(1) if match else "False"
    session_id = raw.get("vistaSessionId")
    if session_id is None:
        return raw.get("deeplinkUrl") or "https://www.cineplex.com/", dbox.lower() == "true"
    url = f"https://www.cineplex.com/ticketing/preview?theatreId={theatre_id}&showtimeId={session_id}&dbox={dbox}"
    return url, dbox.lower() == "true"


def parse_showtimes(payload, pattern: re.Pattern, theatre_id: str):
    """Returns (film titles in the payload, number of sessions of any film, sessions of the watched film)."""
    titles: list[str] = []
    total = 0
    found: dict[str, Session] = {}
    for movie in iter_movies(payload):
        name = movie.get("name") or movie.get("title")
        titles.append(name)
        is_target = bool(pattern.search(name))
        for experience in movie.get("experiences") or []:
            if not isinstance(experience, dict):
                continue
            types = [t for t in experience.get("experienceTypes") or [] if isinstance(t, str)]
            for raw in experience.get("sessions") or []:
                if not isinstance(raw, dict) or not raw.get("showStartDateTime"):
                    continue
                total += 1
                if not is_target or raw.get("isShowtimeEnabledOnline") is False:
                    continue  # not the film, or listed but not bookable online yet
                try:
                    start = dt.datetime.fromisoformat(str(raw["showStartDateTime"])[:19])
                except ValueError:
                    continue
                session_id = raw.get("vistaSessionId")
                key = str(session_id) if session_id is not None else f"{start.isoformat()}|{'/'.join(types)}"
                formats = [t for t in types if t.lower() not in NOISE_FORMATS]
                url, is_dbox = _buy_url(theatre_id, raw)
                sold_out = bool(raw.get("isSoldOut"))
                if key in found:  # same showing sold through several seating areas (e.g. D-BOX + regular)
                    existing = found[key]
                    existing.formats += [f for f in formats if f not in existing.formats]
                    existing.sold_out = existing.sold_out and sold_out
                    if existing.is_dbox_area and not is_dbox:
                        existing.buy_url, existing.is_dbox_area = url, False
                else:
                    found[key] = Session(key, start, formats, sold_out, url, is_dbox)
    return titles, total, sorted(found.values(), key=lambda s: s.start)


def films_with_sessions(payload) -> set:
    films = set()
    for movie in iter_movies(payload):
        for experience in movie.get("experiences") or []:
            sessions = experience.get("sessions") if isinstance(experience, dict) else None
            if any(isinstance(s, dict) and s.get("showStartDateTime") for s in sessions or []):
                films.add(movie.get("name") or movie.get("title"))
                break
    return films


# --------------------------------------------------------------------------- alerts


@dataclass
class Alert:
    title: str
    body: str
    priority: int = 3  # ntfy: 1 (min) .. 5 (urgent)
    click: str | None = None
    actions: list = field(default_factory=list)  # [(label, url)], max 3
    discord_body: str | None = None
    color: int = 0x5865F2
    mention: bool = False


def fit(text: str, limit: int) -> str:
    """Trim to a size limit on a line boundary, so links and labels aren't cut in half."""
    if len(text) <= limit:
        return text
    kept = []
    for line in text.split("\n"):
        if sum(len(k) + 1 for k in kept) + len(line) > limit - 20:
            break
        kept.append(line)
    return "\n".join(kept) + "\n…and more"


def fmt_time(t: dt.datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def fmt_day(d: dt.date) -> str:
    return f"{d:%a %b} {d.day}"


def describe_sessions(sessions: list, markdown: bool = False) -> str:
    by_day: dict = {}
    for s in sorted(sessions, key=lambda s: s.start):
        by_day.setdefault(s.start.date(), []).append(s)
    lines = []
    for day, items in by_day.items():
        parts = []
        for s in items:
            label = f"[{fmt_time(s.start)}]({s.buy_url})" if markdown else fmt_time(s.start)
            if s.formats:
                label += f" {'/'.join(s.formats)}" if markdown else f" ({'/'.join(s.formats)})"
            if s.sold_out:
                label += " SOLD OUT"
            parts.append(label)
        day_label = f"**{fmt_day(day)}**" if markdown else fmt_day(day)
        lines.append(f"{day_label}: " + (" · " if markdown else ", ").join(parts))
    return "\n".join(lines)


def on_sale_alert(cfg: Config, sessions: list, first: bool) -> Alert:
    if first:
        title = f"🎟️ Doomsday tickets are ON SALE at {cfg.theatre_name}"
    else:
        title = f"🎟️ New Doomsday showtimes at {cfg.theatre_name}"
    earliest = next((s for s in sessions if not s.sold_out), sessions[0])
    return Alert(
        title=title,
        body=describe_sessions(sessions) + "\n\nTap to open Cineplex.",
        priority=5 if first else 4,
        click=cfg.theatre_url,
        actions=[(f"Book {fmt_day(earliest.start.date())} {fmt_time(earliest.start)}", earliest.buy_url),
                 ("All showtimes", cfg.theatre_url)],
        discord_body=describe_sessions(sessions, markdown=True),
        color=0xE62429,
        mention=True,
    )


def notify(cfg: Config, alert: Alert, dry_run: bool = False) -> bool:
    """Send to every configured channel; True if at least one delivered."""
    if dry_run:
        log(f"[dry run] would alert: {alert.title}\n{alert.body}")
        return True
    delivered = False
    if cfg.ntfy_topic:
        delivered |= send_ntfy(cfg, alert)
    if cfg.discord_webhook:
        delivered |= send_discord(cfg, alert)
    return delivered


def _post_json(url: str, payload: dict, headers: dict, channel: str) -> bool:
    try:
        status, body = http_request("POST", url, {"Content-Type": "application/json", "User-Agent": BOT_UA, **headers},
                                    json.dumps(payload).encode())
    except OSError as err:
        warn(f"{channel} alert failed: {err}")
        return False
    if 200 <= status < 300:
        return True
    warn(f"{channel} alert failed: HTTP {status} {body[:200]!r}")
    return False


def send_ntfy(cfg: Config, alert: Alert) -> bool:
    payload = {"topic": cfg.ntfy_topic, "title": alert.title, "message": fit(alert.body, 3800),
               "priority": alert.priority}
    if alert.click:
        payload["click"] = alert.click
    if alert.actions:
        payload["actions"] = [{"action": "view", "label": label[:40], "url": url, "clear": True}
                              for label, url in alert.actions[:3]]
    headers = {"Authorization": f"Bearer {cfg.ntfy_token}"} if cfg.ntfy_token else {}
    return _post_json(cfg.ntfy_server.rstrip("/") + "/", payload, headers, "ntfy")


def send_discord(cfg: Config, alert: Alert) -> bool:
    content, allowed = alert.title, {"parse": []}
    if alert.mention and cfg.discord_user_id:
        content = f"<@{cfg.discord_user_id}> {content}"
        allowed = {"users": [cfg.discord_user_id]}
    embed = {"title": alert.title[:256], "description": fit(alert.discord_body or alert.body, 4000),
             "color": alert.color}
    if alert.click:
        embed["url"] = alert.click
    payload = {"username": "Doomsday Watch", "content": content[:2000], "embeds": [embed],
               "allowed_mentions": allowed}
    return _post_json(cfg.discord_webhook, payload, {}, "Discord")


# --------------------------------------------------------------------------- schedule drops


def schedule_end(client: Cineplex, start: dt.date, max_days: int = 21) -> dt.date | None:
    """Last day of the regular schedule running from `start`. A regular day has 2+ films,
    so a lone early presale (one big film weeks ahead) doesn't count. One empty day is
    tolerated; two in a row end the run. None if nothing is posted from `start`."""
    last, empty, day = None, 0, start
    for _ in range(max_days):
        if len(films_with_sessions(client.showtimes(day))) >= 2:
            last, empty = day, 0
        else:
            empty += 1
            if empty == 2:
                break
        day += dt.timedelta(days=1)
    return last


def track_schedule_window(cfg: Config, client: Cineplex, state: dict, now: dt.datetime, dry_run: bool) -> str:
    """Ping when the theatre posts its next batch of showtimes, with when it was spotted.
    Returns a one-line summary for the first-run message instead of pinging separately."""
    today = now.date()
    known = state.get("window_through")
    known = dt.date.fromisoformat(known) if known else None
    try:
        if known is None or known < today:  # first run, or the bot was off long enough to miss drops
            end = schedule_end(client, today + dt.timedelta(days=1))
            if end is None:
                return ""
            state["window_through"] = end.isoformat()
            summary = f"Showtimes there are posted through {fmt_day(end)}; you'll also get a ping each time the next batch drops."
            if known is None and state.get("initialized"):
                notify(cfg, Alert(f"📅 {cfg.theatre_name} has showtimes up through {fmt_day(end)}",
                                  "You'll get a ping with the date and time each time the next batch drops.",
                                  click=cfg.theatre_url, color=0x5865F2), dry_run)
            return summary
        end = schedule_end(client, known + dt.timedelta(days=1))
    except ApiError as err:
        warn(f"Skipped the new-showtimes check: {err}")
        return ""
    if end is None:
        return ""

    added = known + dt.timedelta(days=1)
    new_dates = fmt_day(added) if added == end else f"{fmt_day(added)} – {fmt_day(end)}"
    drops = state.setdefault("window_drops", [])
    body = f"New: {new_dates}.\nSpotted {fmt_day(today)}, {fmt_time(now)} (the bot checks every ~5–15 min)."
    if drops:
        earlier = [dt.datetime.fromisoformat(d["at"]) for d in drops[-4:]]
        body += "\nEarlier drops: " + "; ".join(f"{fmt_day(t.date())} {fmt_time(t)}" for t in reversed(earlier))
    state["window_through"] = end.isoformat()
    drops.append({"at": now.isoformat(timespec="minutes"), "through": end.isoformat()})
    del drops[:-10]
    alert = Alert(f"📅 {cfg.theatre_name} posted showtimes through {fmt_day(end)}", body,
                  click=cfg.theatre_url, color=0x5865F2)
    log(f"{alert.title}\n{body}")
    if not notify(cfg, alert, dry_run):
        warn("The new-showtimes ping wasn't delivered; it's still recorded under 'Earlier drops'.")
    return ""


# --------------------------------------------------------------------------- the check


def run_once(cfg: Config, state: dict, now: dt.datetime | None = None, dry_run: bool = False) -> None:
    now = now or now_local()
    today = now.date()
    dates = [d for d in cfg.dates if d >= today]
    if not dates:
        log("Every watch date is in the past. Nothing to do; disable the workflow.")
        return

    client = Cineplex(cfg, state)
    sessions: dict[str, Session] = {}
    errors: list[str] = []
    anything_scheduled = False
    for date in dates:
        try:
            _, total, found = parse_showtimes(client.showtimes(date), cfg.movie_pattern, cfg.theatre_id)
        except ApiError as err:
            errors.append(f"{date}: {err}")
            continue
        anything_scheduled |= total > 0
        for s in found:
            sessions.setdefault(s.id, s)
    failed_dates = len(errors)

    # Future dates are often empty, which looks the same as a broken parser. Tomorrow's
    # regular schedule proves the API key, endpoint and response format all still work.
    api_ok = anything_scheduled
    tomorrow_note = ""
    if not api_ok or dry_run:
        tomorrow = today + dt.timedelta(days=1)
        try:
            titles, total, _ = parse_showtimes(client.showtimes(tomorrow), cfg.movie_pattern, cfg.theatre_id)
            if total:
                api_ok = True
                tomorrow_note = f"{len(titles)} films / {total} showtimes tomorrow"
                if dry_run:
                    log(f"Sanity check, tomorrow at {cfg.theatre_name}: {', '.join(titles)}")
            else:
                errors.append(f"{tomorrow} (sanity check): no showtimes found; Cineplex may have changed its API")
        except ApiError as err:
            errors.append(f"{tomorrow} (sanity check): {err}")
    for error in errors:
        warn(f"Cineplex check problem: {error}")

    if not api_ok or (failed_dates == len(dates) and not sessions):
        state["failures"] = state.get("failures", 0) + 1
        threshold = cfg.fail_alert_after if state.get("initialized") else 1
        log(f"Check failed ({state['failures']} in a row).")
        if state["failures"] >= threshold and not state.get("failure_alerted"):
            alert = Alert(
                title="⚠️ Doomsday watch can't read Cineplex",
                body="\n".join(errors[:4]) + "\n\nIt'll keep retrying. Check the Actions log; if the API key "
                     "rotated, set the CINEPLEX_API_KEY secret (see README).",
                priority=4, color=0xF0B232,
            )
            if notify(cfg, alert, dry_run):
                state["failure_alerted"] = True
        return

    if state.get("failure_alerted"):
        notify(cfg, Alert("✅ Doomsday watch is reading Cineplex again", "Back to normal.", priority=2,
                          color=0x23A55A), dry_run)
    state["failures"] = 0
    state["failure_alerted"] = False

    window_summary = track_schedule_window(cfg, client, state, now, dry_run) if cfg.window_alerts else ""

    seen: dict = state.setdefault("seen", {})
    new = sorted((s for s in sessions.values() if s.id not in seen), key=lambda s: s.start)
    if new:
        first = not state.get("on_sale")
        shown = sorted(sessions.values(), key=lambda s: s.start) if first else new
        alert = on_sale_alert(cfg, shown, first)
        log(f"{alert.title}\n{alert.body}")
        if notify(cfg, alert, dry_run):
            for s in new:
                seen[s.id] = s.start.isoformat()
            state["on_sale"] = True
            state["initialized"] = True
        else:
            warn("No alert channel accepted the message; will retry on the next run.")
        return

    span = f"{fmt_day(dates[0])}–{fmt_day(dates[-1])}" if len(dates) > 1 else fmt_day(dates[0])
    if not state.get("initialized"):
        alert = Alert(
            title=f"👀 Watching {cfg.theatre_name} for Doomsday",
            body=f"Not on sale there yet. Checking {span} every few minutes; you'll get an urgent alert "
                 f"the moment showtimes appear." + (f"\n{window_summary}" if window_summary else "")
                 + (f"\n(Cineplex API OK: {tomorrow_note}.)" if tomorrow_note else ""),
            priority=3, click=cfg.theatre_url, color=0x23A55A,
        )
        if notify(cfg, alert, dry_run):
            state["initialized"] = True
        return

    if state.get("on_sale"):
        log(f"On sale; no new showtimes ({len(sessions)} known).")
    else:
        log(f"Not on sale at {cfg.theatre_name} yet ({span}).")


# --------------------------------------------------------------------------- CLI


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        warn(f"Couldn't read {path} ({err}); starting fresh.")
        return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Alert when Doomsday showtimes appear at Cineplex Junxion Erin Mills.")
    parser.add_argument("--dry-run", action="store_true", help="check and print only: no alerts, no state changes")
    parser.add_argument("--test-notify", action="store_true", help="send a test alert and exit")
    parser.add_argument("--every", type=int, metavar="SECONDS", help="keep running locally, checking every N seconds")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if not args.dry_run and not (cfg.ntfy_topic or cfg.discord_webhook):
        print("Set NTFY_TOPIC and/or DISCORD_WEBHOOK_URL (or use --dry-run).", file=sys.stderr)
        return 2
    if args.test_notify:
        ok = notify(cfg, Alert("✅ Test from Doomsday watch", "Alerts are working.", click=cfg.theatre_url))
        print("Delivered." if ok else "Not delivered; see warnings above.")
        return 0 if ok else 1

    while True:
        state = load_state(cfg.state_file)
        before = json.dumps(state, sort_keys=True)
        run_once(cfg, state, dry_run=args.dry_run)
        if not args.dry_run and json.dumps(state, sort_keys=True) != before:
            save_state(cfg.state_file, state)
        if not args.every:
            return 0
        _sleep(max(30, args.every))


if __name__ == "__main__":
    sys.exit(main())
