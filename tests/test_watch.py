"""Offline tests: Cineplex, ntfy and Discord are faked. Payloads use the real field
names of Cineplex's showtimes API. Run: python3 -m unittest discover tests"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watch  # noqa: E402

NOW = dt.datetime(2026, 10, 21, 23, 0, tzinfo=watch.TZ)
TOMORROW = "2026-10-22"
NTFY = "https://ntfy.sh/"
DISCORD = "https://discord.com/api/webhooks/123/abc"


# ---- Cineplex-shaped payload builders -----------------------------------------------

def session(vid, start, *, dbox=False, area="0000000001", sold_out=False, online=True):
    return {
        "seatMapUrl": f"https://www.cineplex.com/en-Mobile/ticketing/preview?theatreId=7313&showtimeId={vid}"
                      f"&dbox={'True' if dbox else 'False'}",
        "deeplinkUrl": f"https://apis.cineplex.com/prod/cpx/theatrical/deeplink?s={vid}&a={area}&l=7313"
                       f"&m=avengers-doomsday&ss=False",
        "vistaSessionId": vid, "areaCode": area, "showStartDateTime": start,
        "isInThePast": False, "isReservedSeating": True, "isShowtimeEnabledOnline": online,
        "seatsRemaining": 0 if sold_out else 120, "isSoldOut": sold_out, "auditorium": "AVX #1",
    }


def experience(types, sessions):
    return {"experienceTypes": types, "order": 1, "sessions": sessions}


def movie(mid, name, experiences):
    return {"id": mid, "name": name, "runtimeInMinutes": 165, "genres": ["Action"], "experiences": experiences}


def showtimes(date, movies):
    return [{"theatre": "Cineplex Junxion Erin Mills", "theatreId": 7313,
             "dates": [{"startDate": f"{date}T00:00:00", "movies": movies}]}]


def regular_day(date):
    """A normal schedule with other films only (used as tomorrow's sanity check)."""
    return showtimes(date, [
        movie(1, "Street Fighter", [experience(["UltraAVX", "Recliners"], [session(9001, f"{date}T19:00:00")])]),
        movie(2, "Clayface", [experience(["Recliners"], [session(9002, f"{date}T21:30:00")])]),
    ])


def big_day(date, n=6):
    """A busier normal day: n films."""
    return showtimes(date, [movie(100 + i, f"Film {chr(65 + i)}",
                                  [experience(["Recliners"], [session(7000 + i, f"{date}T19:00:00")])])
                            for i in range(n)])


def doomsday_dec17():
    return showtimes("2026-12-17", [
        movie(40000, "Avengers: Doomsday", [
            experience(["UltraAVX", "D-BOX", "Recliners"], [session(500, "2026-12-17T19:00:00", dbox=True,
                                                                     area="0000000004")]),
            experience(["UltraAVX", "Recliners"], [session(500, "2026-12-17T19:00:00"),
                                                    session(501, "2026-12-17T22:45:00", sold_out=True)]),
            experience(["Recliners"], [session(502, "2026-12-17T18:30:00", online=False)]),
        ]),
        movie(1, "Street Fighter", [experience(["Recliners"], [session(9003, "2026-12-17T17:00:00")])]),
    ])


# ---- fake network --------------------------------------------------------------------

class FakeNet:
    def __init__(self):
        self.schedule = {TOMORROW: regular_day(TOMORROW)}  # date -> payload
        self.valid_keys = {watch.BUNDLED_API_KEY}
        self.cineplex_status = None  # force an HTTP status for showtimes calls
        self.notify_status = {NTFY: 200, DISCORD: 204}
        self.homepage = b""
        self.chunks = {}
        self.sent = []  # (url, payload) for notifications
        self.showtime_calls = 0

    def __call__(self, method, url, headers=None, body=None, timeout=20):
        headers = headers or {}
        if method == "POST":
            self.sent.append((url, json.loads(body)))
            return self.notify_status.get(url, 404), b"{}"
        if url.startswith(watch.API_BASE + "/showtimes"):
            self.showtime_calls += 1
            if self.cineplex_status:
                return self.cineplex_status, b"error"
            if headers.get("Ocp-Apim-Subscription-Key") not in self.valid_keys:
                return 401, b'{"message":"Access denied due to invalid subscription key."}'
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert query["locationId"] == ["7313"] and query["language"] == ["en"]
            return 200, json.dumps(self.schedule.get(query["date"][0], [])).encode()
        if url == watch.CINEPLEX_HOME:
            return 200, self.homepage
        if url in self.chunks:
            return 200, self.chunks[url]
        raise AssertionError(f"unexpected request: {method} {url}")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self._real = (watch.http_request, watch._sleep)
        watch.http_request = self.net
        watch._sleep = lambda s: None
        self.cfg = watch.Config(ntfy_topic="doomsday-test", discord_webhook=DISCORD, discord_user_id="42")
        self.state = {}

    def tearDown(self):
        watch.http_request, watch._sleep = self._real

    def run_check(self, now=NOW):
        before = len(self.net.sent)
        watch.run_once(self.cfg, self.state, now=now)
        return self.net.sent[before:]

    def ntfy(self, sent):
        return [p for url, p in sent if url == NTFY]

    # ---- happy paths ----

    def test_first_run_not_on_sale_sends_watching_ping(self):
        sent = self.run_check()
        self.assertEqual(len(self.ntfy(sent)), 1)
        msg = self.ntfy(sent)[0]
        self.assertIn("Watching Junxion Erin Mills", msg["title"])
        self.assertIn("Checking Wed Dec 16–Sun Dec 20", msg["message"])
        self.assertIn("2 films / 2 showtimes tomorrow", msg["message"])
        self.assertTrue(self.state["initialized"])
        self.assertEqual(self.run_check(), [])  # quiet afterwards

    def test_on_sale_alert_lists_bookable_showtimes_with_formats_and_links(self):
        self.run_check()
        self.net.schedule["2026-12-17"] = doomsday_dec17()
        sent = self.run_check()
        msg = self.ntfy(sent)[0]
        self.assertEqual(msg["priority"], 5)
        self.assertIn("ON SALE at Junxion Erin Mills", msg["title"])
        # 7:00 PM appears once with D-BOX merged in; 6:30 PM isn't bookable online so it's left out.
        self.assertIn("Thu Dec 17: 7:00 PM (UltraAVX/D-BOX), 10:45 PM (UltraAVX) SOLD OUT", msg["message"])
        self.assertNotIn("6:30", msg["message"])
        self.assertNotIn("Recliners", msg["message"])
        # "Book" button goes to the regular-seating area of the earliest showing that isn't sold out.
        self.assertEqual(msg["actions"][0]["url"],
                         "https://www.cineplex.com/ticketing/preview?theatreId=7313&showtimeId=500&dbox=False")
        self.assertEqual(msg["actions"][0]["label"], "Book Thu Dec 17 7:00 PM")
        discord = [p for url, p in sent if url == DISCORD][0]
        self.assertTrue(discord["content"].startswith("<@42> "))
        self.assertEqual(discord["allowed_mentions"], {"users": ["42"]})
        self.assertIn("[7:00 PM](https://www.cineplex.com/ticketing/preview?theatreId=7313&showtimeId=500&dbox=False)",
                      discord["embeds"][0]["description"])
        self.assertEqual(sorted(self.state["seen"]), ["500", "501"])
        self.assertEqual(self.run_check(), [])  # no repeat alert

    def test_already_on_sale_on_very_first_run(self):
        self.net.schedule["2026-12-17"] = doomsday_dec17()
        msg = self.ntfy(self.run_check())[0]
        self.assertIn("ON SALE", msg["title"])
        self.assertTrue(self.state["initialized"])

    def test_later_additions_alert_only_new_showtimes(self):
        self.net.schedule["2026-12-17"] = doomsday_dec17()
        self.run_check()
        self.net.schedule["2026-12-18"] = showtimes("2026-12-18", [
            movie(40000, "Avengers: Doomsday (Early Access Fan Event)",
                  [experience(["UltraAVX"], [session(600, "2026-12-18T12:15:00")])])])
        msg = self.ntfy(self.run_check())[0]
        self.assertEqual(msg["priority"], 4)
        self.assertIn("New Doomsday showtimes", msg["title"])
        self.assertTrue(msg["message"].startswith("Fri Dec 18: 12:15 PM (UltraAVX)"))
        self.assertNotIn("Dec 17", msg["message"])

    def test_failed_delivery_is_retried_next_run(self):
        self.net.schedule["2026-12-17"] = doomsday_dec17()
        self.net.notify_status = {NTFY: 500, DISCORD: 500}
        self.run_check()
        self.assertEqual(self.state.get("seen", {}), {})
        self.assertFalse(self.state.get("on_sale"))
        self.net.notify_status = {NTFY: 200, DISCORD: 204}
        self.assertIn("ON SALE", self.ntfy(self.run_check())[0]["title"])

    def test_one_channel_failing_still_counts_as_delivered(self):
        self.net.notify_status = {NTFY: 200, DISCORD: 500}
        self.run_check()
        self.assertTrue(self.state["initialized"])

    # ---- resilience ----

    def test_rotated_api_key_is_rediscovered_and_saved(self):
        new_key = "0123456789abcdef0123456789abcdef"
        self.net.valid_keys = {new_key}
        chunk_url = "https://www.cineplex.com/next-static-files/_next/static/chunks/9026-abc.js"
        self.net.homepage = f'<script src="{chunk_url}"></script>'.encode()
        decoy = 'banner:{url:"https://x"},headers:{"Ocp-Apim-Subscription-Key":"ffffffffffffffffffffffffffffffff"}'
        real = ('base:"https://apis.cineplex.com/prod/cpx/theatrical/api/v1",headers:{"Ocp-Apim-Subscription-Key":"'
                + new_key + '"}')
        self.net.chunks[chunk_url] = (decoy + ";" + real).encode()
        sent = self.run_check()
        self.assertEqual(self.state["api_key"], new_key)
        self.assertIn("Watching", self.ntfy(sent)[0]["title"])

    def test_extract_key_ignores_decoy_before_anchor(self):
        src = ('"Ocp-Apim-Subscription-Key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ... '
               'apis.cineplex.com/prod/cpx/theatrical/api/v1",{headers:{Ocp-Apim-Subscription-Key:"'
               'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}')
        self.assertIsNone(watch.extract_api_key("no anchor here"))
        self.assertEqual(watch.extract_api_key(src), "b" * 32)

    def test_first_run_failure_alerts_immediately(self):
        self.net.cineplex_status = 503
        sent = self.run_check()
        self.assertIn("can't read Cineplex", self.ntfy(sent)[0]["title"])
        self.assertIn("HTTP 503", self.ntfy(sent)[0]["message"])

    def test_failure_alert_after_threshold_then_recovery(self):
        self.run_check()  # initialized
        self.net.cineplex_status = 500
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.run_check(), [])
        self.assertIn("can't read Cineplex", self.ntfy(self.run_check())[0]["title"])  # 3rd failure
        self.assertEqual(self.run_check(), [])  # alerted once only
        self.net.cineplex_status = None
        self.assertIn("reading Cineplex again", self.ntfy(self.run_check())[0]["title"])
        self.assertEqual(self.state["failures"], 0)

    def test_empty_sanity_check_counts_as_failure(self):
        self.net.schedule[TOMORROW] = []  # API answers but nothing parses: format changed?
        sent = self.run_check()
        self.assertIn("can't read Cineplex", self.ntfy(sent)[0]["title"])
        self.assertIn("no showtimes found", self.ntfy(sent)[0]["message"])

    def test_non_json_response_is_a_failure_not_a_crash(self):
        real = self.net.__call__

        def html_instead(method, url, headers=None, body=None, timeout=20):
            if url.startswith(watch.API_BASE):
                return 200, b"<html>Access Denied</html>"
            return real(method, url, headers, body, timeout)

        watch.http_request = html_instead
        sent = self.run_check()
        self.assertIn("non-JSON", self.ntfy(sent)[0]["message"])

    def test_network_error_is_retried(self):
        calls = {"n": 0}
        real = self.net.__call__

        def flaky(method, url, headers=None, body=None, timeout=20):
            if url.startswith(watch.API_BASE) and calls["n"] < 2:
                calls["n"] += 1
                raise OSError("connection reset")
            return real(method, url, headers, body, timeout)

        watch.http_request = flaky
        self.assertIn("Watching", self.ntfy(self.run_check())[0]["title"])

    def test_past_dates_are_skipped_and_finished_window_is_quiet(self):
        self.net.schedule["2026-12-20"] = doomsday_dec17()
        self.assertEqual(self.run_check(now=dt.datetime(2026, 12, 21, 9, 0, tzinfo=watch.TZ)), [])
        self.assertEqual(self.net.showtime_calls, 0)

    def test_dates_and_config_from_env(self):
        cfg = watch.Config.from_env({"WATCH_DATES": "2026-12-17, 2026-12-19..2026-12-20", "NTFY_TOPIC": " t ",
                                     "DISCORD_WEBHOOK_URL": "", "MOVIE_PATTERN": "secret wars"})
        self.assertEqual([d.isoformat() for d in cfg.dates], ["2026-12-17", "2026-12-19", "2026-12-20"])
        self.assertEqual(cfg.ntfy_topic, "t")
        self.assertIsNone(cfg.discord_webhook)
        self.assertTrue(cfg.movie_pattern.search("Avengers: Secret Wars"))
        self.assertEqual([d.isoformat() for d in watch.Config.from_env({}).dates],
                         ["2026-12-16", "2026-12-17", "2026-12-18", "2026-12-19", "2026-12-20"])
        self.assertEqual(watch.Config.from_env({"GITHUB_ACTIONS": "true"}).state_file, "state.json")
        self.assertEqual(watch.Config.from_env({}).state_file, "state.local.json")  # local runs never touch the bot's
        for pasted in ("ntfy.sh/my-topic", "https://ntfy.sh/my-topic/", "my-topic"):
            cfg = watch.Config.from_env({"NTFY_TOPIC": pasted})
            self.assertEqual((cfg.ntfy_server, cfg.ntfy_topic), ("https://ntfy.sh", "my-topic"), pasted)

    # ---- new-showtimes-posted pings ----

    def post_days(self, first, last):
        day, end = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
        while day <= end:
            self.net.schedule[day.isoformat()] = regular_day(day.isoformat())
            day += dt.timedelta(days=1)

    def lone_presale(self, date):
        self.net.schedule[date] = showtimes(date, [
            movie(7, "Dune: Part Three", [experience(["UltraAVX"], [session(800, f"{date}T19:00:00")])])])

    def test_drop_ping_says_what_was_added_and_when_with_history(self):
        self.post_days("2026-10-22", "2026-10-29")
        self.lone_presale("2026-12-15")
        first = self.ntfy(self.run_check())[0]
        self.assertIn("posted through Thu Oct 29", first["message"])  # folded into the first-run message
        self.assertEqual(self.run_check(dt.datetime(2026, 10, 26, 9, 0, tzinfo=watch.TZ)), [])

        self.post_days("2026-10-30", "2026-11-05")
        msg = self.ntfy(self.run_check(dt.datetime(2026, 10, 27, 14, 13, tzinfo=watch.TZ)))[0]
        self.assertEqual(msg["title"], "📅 Junxion Erin Mills posted showtimes through Thu Nov 5")
        self.assertEqual(msg["priority"], 3)
        self.assertTrue(msg["message"].startswith("New: Fri Oct 30 – Thu Nov 5.\nSpotted Tue Oct 27, 2:13 PM"))
        self.assertNotIn("Earlier drops", msg["message"])

        self.post_days("2026-11-06", "2026-11-12")
        msg = self.ntfy(self.run_check(dt.datetime(2026, 11, 3, 2, 5, tzinfo=watch.TZ)))[0]
        self.assertIn("Spotted Tue Nov 3, 2:05 AM", msg["message"])
        self.assertIn("Earlier drops: Tue Oct 27 2:13 PM", msg["message"])
        self.assertEqual(self.state["window_through"], "2026-11-12")

    def test_lone_presale_far_ahead_is_not_a_drop(self):
        self.state = {"initialized": True, "window_through": "2026-12-13"}
        self.post_days("2026-12-11", "2026-12-13")
        self.lone_presale("2026-12-15")
        self.assertEqual(self.run_check(dt.datetime(2026, 12, 10, 12, 0, tzinfo=watch.TZ)), [])
        self.assertEqual(self.state["window_through"], "2026-12-13")

    def test_existing_setup_gets_a_one_time_baseline_ping(self):
        self.state = {"initialized": True}
        self.post_days("2026-10-22", "2026-10-29")
        msg = self.ntfy(self.run_check())[0]
        self.assertEqual(msg["title"], "📅 Junxion Erin Mills has showtimes up through Thu Oct 29")
        self.assertEqual(self.run_check(), [])

    def test_resyncs_quietly_after_downtime(self):
        self.state = {"initialized": True, "window_through": "2026-10-10"}
        self.post_days("2026-10-22", "2026-10-29")
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.state["window_through"], "2026-10-29")
        self.assertNotIn("window_drops", self.state)

    def test_drop_pings_can_be_turned_off(self):
        self.cfg.window_alerts = False
        self.post_days("2026-10-22", "2026-10-29")
        self.assertNotIn("posted through", self.ntfy(self.run_check())[0]["message"])
        self.assertNotIn("window_through", self.state)
        self.assertFalse(watch.Config.from_env({"WINDOW_ALERTS": "off"}).window_alerts)
        self.assertTrue(watch.Config.from_env({}).window_alerts)

    def test_two_film_presale_is_not_a_batch_when_regular_days_are_bigger(self):
        self.state = {"initialized": True, "window_through": "2026-10-29"}
        for day in range(22, 30):
            self.net.schedule[f"2026-10-{day}"] = big_day(f"2026-10-{day}")
        self.net.schedule["2026-10-30"] = regular_day("2026-10-30")  # Friday openers on presale
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.state["window_through"], "2026-10-29")
        self.assertEqual(self.state["run_log"][0]["current_last_bookable_day"], "October 30")  # but it is bookable

        for day in ("2026-10-30", "2026-10-31", "2026-11-01"):
            self.net.schedule[day] = big_day(day)
        msg = self.ntfy(self.run_check())[0]
        self.assertEqual(msg["title"], "📅 Junxion Erin Mills posted showtimes through Sun Nov 1")
        self.assertTrue(msg["message"].startswith("New: Fri Oct 30 – Sun Nov 1."))

    # ---- run log in state.json ----

    def test_every_run_adds_a_line_to_the_run_log_newest_first(self):
        self.lone_presale("2026-12-15")  # far-off premiere: outside the scan, not listed
        self.run_check()
        self.post_days("2026-10-23", "2026-10-23")
        self.run_check(dt.datetime(2026, 10, 22, 9, 5, tzinfo=watch.TZ))
        self.assertEqual(self.state["run_log"], [
            {"time_run": "October 22 2026, 9:05am", "current_on_sale": "Clayface, Street Fighter",
             "current_last_bookable_day": "October 23"},
            {"time_run": "October 21 2026, 11:00pm", "current_on_sale": "Clayface, Street Fighter",
             "current_last_bookable_day": "October 22"},
        ])
        self.assertNotIn("time_run", self.state)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            watch.save_state(path, self.state)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertEqual(json.loads(text), self.state)  # valid JSON, nothing lost
        lines = text.splitlines()
        self.assertEqual(lines[1], '  "run_log": [')
        self.assertTrue(lines[2].startswith('    {"time_run": "October 22 2026, 9:05am"'))  # one line per run
        self.assertTrue(lines[3].startswith('    {"time_run": "October 21 2026, 11:00pm"'))
        keys = list(json.loads(text))
        self.assertEqual(keys[0], "run_log")
        self.assertEqual(keys[1:], sorted(keys[1:]))

    def test_run_log_is_capped_and_old_one_run_format_is_kept(self):
        self.state = {"initialized": True, "time_run": "October 20 2026, 1:00pm", "current_on_sale": "Old",
                      "current_last_bookable_day": "October 27",
                      "run_log": [{"time_run": str(i)} for i in range(watch.MAX_RUN_LOG)]}
        self.run_check()
        run_log = self.state["run_log"]
        self.assertEqual(len(run_log), watch.MAX_RUN_LOG)
        self.assertEqual(run_log[0]["time_run"], "October 21 2026, 11:00pm")
        self.assertEqual(run_log[1]["current_on_sale"], "Old")
        self.assertEqual(run_log[2]["time_run"], "0")

    def test_finished_watch_adds_nothing_to_the_log(self):
        self.run_check(dt.datetime(2026, 12, 21, 9, 0, tzinfo=watch.TZ))
        self.assertNotIn("run_log", self.state)

    def test_save_state_without_a_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            for state in ({}, {"failures": 1}, {"run_log": []}, {"run_log": [], "failures": 0}):
                watch.save_state(path, state)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh), state)

    def test_on_sale_list_skips_past_sold_out_and_offline_showtimes(self):
        self.net.schedule[TOMORROW] = showtimes(TOMORROW, [
            movie(1, "Bookable", [experience(["Recliners"], [session(1, f"{TOMORROW}T19:00:00")])]),
            movie(2, "Sold Out", [experience(["Recliners"], [session(2, f"{TOMORROW}T19:00:00", sold_out=True)])]),
            movie(3, "Not Online", [experience(["Recliners"], [session(3, f"{TOMORROW}T19:00:00", online=False)])]),
        ])
        self.net.schedule["2026-10-21"] = showtimes("2026-10-21", [  # tonight; NOW is 11 PM
            movie(4, "Already Started", [experience(["Recliners"], [session(4, "2026-10-21T21:00:00")])])])
        self.run_check()
        self.assertEqual(self.state["run_log"][0]["current_on_sale"], "Bookable")

    def test_failed_check_logs_unknown(self):
        self.net.cineplex_status = 503
        self.run_check()
        self.assertEqual(self.state["run_log"], [{"time_run": "October 21 2026, 11:00pm",
                                                  "current_on_sale": watch.UNKNOWN,
                                                  "current_last_bookable_day": watch.UNKNOWN}])

    def test_dead_api_gives_up_after_two_failed_requests(self):
        self.net.cineplex_status = 503
        sent = self.run_check()
        self.assertEqual(self.net.showtime_calls, 6)  # 2 dates x 3 attempts, then the rest are skipped
        self.assertIn("can't read Cineplex", self.ntfy(sent)[0]["title"])

    # ---- CLI ----

    def test_cli_requires_a_channel_and_saves_state(self):
        today = watch.now_local().date()  # main() uses the real clock
        self.post_days(today.isoformat(), (today + dt.timedelta(days=3)).isoformat())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            env = {"STATE_FILE": path}
            old = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(env)
                self.assertEqual(watch.main([]), 2)
                self.assertEqual(watch.main(["--dry-run"]), 0)
                self.assertFalse(os.path.exists(path))  # dry runs never write state
                os.environ["NTFY_TOPIC"] = "doomsday-test"
                self.assertEqual(watch.main([]), 0)
                with open(path) as fh:
                    self.assertTrue(json.load(fh)["initialized"])
                self.assertEqual(watch.main(["--test-notify"]), 0)
            finally:
                os.environ.clear()
                os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
