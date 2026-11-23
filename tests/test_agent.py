"""Tests for the werewolf seat agent.

Every decision is a pure function of one view, so a view is the only fixture
needed. These tests build small synthetic tables and pin what the agent claims
about itself in its docstring: same view, same action, byte for byte, with seat
numbers as the only tie break.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as w  # noqa: E402


def player(seat, name, alive=True):
    return {"seat": seat, "name": name, "alive": alive}


def view(me=1, role="villager", players=None, log=None, partner=None,
         seer_results=None, wolf_votes=None, day=1, phase="day"):
    players = players or [player(i, "Seat%d" % i) for i in range(1, 8)]
    return {
        "you": {"seat": me, "role": role, "partner": partner},
        "players": players,
        "log": log or [],
        "seerResults": seer_results or [],
        "wolfVotes": wolf_votes or [],
        "day": day,
        "phase": phase,
    }


def speech(seat, text, day=1):
    return {"kind": "speech", "seat": seat, "text": text, "day": day}


def vote(seat, target, day=1):
    return {"kind": "vote", "seat": seat, "target": target, "day": day}


class ReadTableTest(unittest.TestCase):
    def test_a_seer_claim_is_recognised_in_its_common_phrasings(self):
        for text in ("I am the seer", "im seer", "I'm a seer", "me the seer"):
            claims, *_ = w.read_table(view(log=[speech(3, text)]))
            self.assertEqual(claims, [3], text)

    def test_a_wolfish_word_alone_is_not_an_accusation(self):
        _, accused, *_ = w.read_table(view(log=[speech(4, "this table is full of wolves")]))
        self.assertEqual(sum(accused.values()), 0.0)

    def test_a_seat_reference_in_an_accusation_lands_on_that_seat(self):
        _, accused, *_ = w.read_table(view(log=[speech(4, "seat 6 is a liar")]))
        self.assertEqual(accused[6], 1.0)

    def test_a_name_reference_in_an_accusation_lands_on_that_seat(self):
        players = [player(1, "Ana"), player(2, "Budi"), player(3, "Citra")]
        _, accused, *_ = w.read_table(view(players=players, log=[speech(1, "budi looks shady")]))
        self.assertEqual(accused[2], 1.0)

    def test_a_name_inside_a_longer_name_accuses_only_the_seat_named(self):
        # "Budiman" contains "Budi"; only the seat that was actually named was
        # accused, and crediting both moves the vote read by a whole point.
        players = [player(1, "Ana"), player(2, "Budi"), player(3, "Budiman")]
        _, accused, *_ = w.read_table(
            view(players=players, log=[speech(1, "budiman looks shady")])
        )
        self.assertEqual(accused[3], 1.0)
        self.assertEqual(accused[2], 0.0)

    def test_a_name_inside_an_ordinary_word_accuses_nobody(self):
        # "aliens" is not a mention of Ali.
        players = [player(1, "Ana"), player(4, "Ali")]
        _, accused, *_ = w.read_table(
            view(players=players, log=[speech(1, "the aliens are shady")])
        )
        self.assertEqual(accused[4], 0.0)

    def test_a_seat_never_accuses_itself(self):
        _, accused, *_ = w.read_table(view(log=[speech(5, "seat 5 is the wolf")]))
        self.assertEqual(accused[5], 0.0)

    def test_a_loud_accuser_reads_a_shade_cleaner(self):
        log = [speech(4, "seat 6 is a liar")]
        _, accused, *_ = w.read_table(view(log=log))
        self.assertEqual(accused[4], -0.25)

    def test_speech_is_counted_per_seat(self):
        log = [speech(2, "hello"), speech(2, "again"), speech(3, "hi")]
        _, _, talk, *_ = w.read_table(view(log=log))
        self.assertEqual(talk[2], 2)
        self.assertEqual(talk[3], 1)

    def test_votes_are_grouped_by_day(self):
        log = [vote(1, 2, day=1), vote(3, 2, day=1), vote(1, 4, day=2)]
        _, _, _, votes, *_ = w.read_table(view(log=log))
        self.assertEqual(votes[1], {1: 2, 3: 2})
        self.assertEqual(votes[2], {1: 4})

    def test_exiles_and_deaths_carry_their_role(self):
        log = [{"kind": "exile", "seat": 2, "role": "werewolf", "day": 1},
               {"kind": "dawn", "died": 5, "role": "villager", "day": 2}]
        _, _, _, _, exiled, died = w.read_table(view(log=log))
        self.assertEqual(exiled, [(2, "werewolf")])
        self.assertEqual(died, [(5, "villager")])

    def test_a_malformed_dawn_entry_is_skipped(self):
        log = [{"kind": "dawn", "died": None, "role": "villager", "day": 2}]
        *_, died = w.read_table(view(log=log))
        self.assertEqual(died, [])

    def test_a_two_digit_seat_can_be_accused(self):
        # A 12-seat table: seats 10-12 are votable, so an accusation aimed at
        # one has to score like any other.
        players = [player(i, "Seat%d" % i) for i in range(1, 13)]
        for seat in (10, 11, 12):
            _, accused, *_ = w.read_table(
                view(players=players, log=[speech(5, "seat %d is a wolf" % seat)])
            )
            self.assertEqual(accused[seat], 1.0, seat)

    def test_a_seat_reference_does_not_swallow_a_longer_number(self):
        # "seat 1" is a prefix of "seat 11"; only the seat actually named may
        # be credited.
        players = [player(i, "Seat%d" % i) for i in range(1, 13)]
        _, accused, *_ = w.read_table(
            view(players=players, log=[speech(5, "seat 11 is a wolf")])
        )
        self.assertEqual(accused[1], 0.0)
        self.assertEqual(accused[11], 1.0)


class KnowledgeTest(unittest.TestCase):
    def test_a_wolf_knows_itself_and_its_partner(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"})
        self.assertEqual(w.known_wolves(v, 2, "werewolf"), {2, 5})

    def test_a_wolf_without_a_partner_knows_only_itself(self):
        v = view(me=2, role="werewolf")
        self.assertEqual(w.known_wolves(v, 2, "werewolf"), {2})

    def test_a_seer_result_marks_a_wolf(self):
        v = view(seer_results=[{"seat": 6, "werewolf": True}])
        self.assertIn(6, w.known_wolves(v, 1, "villager"))

    def test_a_dead_or_exiled_wolf_is_known_to_everyone(self):
        log = [{"kind": "exile", "seat": 3, "role": "werewolf", "day": 1},
               {"kind": "dawn", "died": 4, "role": "werewolf", "day": 2}]
        self.assertEqual(w.known_wolves(view(log=log), 1, "villager"), {3, 4})

    def test_a_villager_knows_itself_clean(self):
        self.assertIn(1, w.known_clean(view(me=1, role="villager"), 1, "villager"))

    def test_a_wolf_does_not_count_itself_clean(self):
        self.assertNotIn(2, w.known_clean(view(me=2, role="werewolf"), 2, "werewolf"))

    def test_a_seer_result_marks_a_clean_seat(self):
        v = view(seer_results=[{"seat": 6, "werewolf": False}])
        self.assertIn(6, w.known_clean(v, 1, "seer"))

    def test_an_exiled_villager_is_known_clean(self):
        log = [{"kind": "exile", "seat": 3, "role": "villager", "day": 1}]
        self.assertIn(3, w.known_clean(view(log=log), 1, "villager"))


class TallyTest(unittest.TestCase):
    def test_only_todays_votes_are_tallied(self):
        log = [vote(1, 2, day=1), vote(3, 2, day=1), vote(4, 5, day=2)]
        self.assertEqual(w.tally_so_far(view(day=1, log=log), [2, 5]), {2: 2})

    def test_an_abstention_is_not_counted(self):
        log = [vote(1, None, day=1), vote(3, 2, day=1)]
        self.assertEqual(w.tally_so_far(view(day=1, log=log), [2]), {2: 1})


class VillageVoteTest(unittest.TestCase):
    def test_a_proven_wolf_is_the_vote(self):
        log = [{"kind": "exile", "seat": 6, "role": "werewolf", "day": 1}]
        self.assertEqual(w.pick_vote(view(log=log), 1, "villager", [3, 5, 6]), 6)

    def test_the_lowest_seat_wins_between_two_proven_wolves(self):
        log = [{"kind": "exile", "seat": 6, "role": "werewolf", "day": 1},
               {"kind": "exile", "seat": 3, "role": "werewolf", "day": 1}]
        self.assertEqual(w.pick_vote(view(log=log), 1, "villager", [3, 6]), 3)

    def test_a_seer_votes_against_a_rival_seer_claim(self):
        log = [speech(5, "I am the seer")]
        self.assertEqual(w.pick_vote(view(me=1, role="seer", log=log), 1, "seer", [3, 5, 7]), 5)

    def test_a_villager_backs_the_target_of_a_trusted_seer(self):
        log = [speech(4, "I am the seer"), vote(4, 7, day=1)]
        self.assertEqual(w.pick_vote(view(log=log, day=2), 1, "villager", [3, 7]), 7)

    def test_a_wagon_of_two_is_joined(self):
        log = [vote(2, 5), vote(3, 5)]
        self.assertEqual(w.pick_vote(view(me=1, log=log), 1, "villager", [4, 5]), 5)

    def test_a_clean_seat_is_not_exiled_on_a_hunch(self):
        log = [{"kind": "exile", "seat": 4, "role": "villager", "day": 1}]
        chosen = w.pick_vote(view(log=log), 1, "villager", [4, 5])
        self.assertNotEqual(chosen, 4)

    def test_no_options_means_no_vote(self):
        self.assertIsNone(w.pick_vote(view(), 1, "villager", []))

    def test_the_vote_is_always_one_of_the_options(self):
        log = [speech(4, "I am the seer"), vote(4, 7), vote(2, 7)]
        for options in ([3, 5], [2, 5, 7], [7]):
            self.assertIn(w.pick_vote(view(log=log), 1, "villager", options), options)


class WolfVoteTest(unittest.TestCase):
    def test_a_wolf_never_votes_its_own_pack(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"})
        self.assertEqual(w.pick_vote(v, 2, "werewolf", [5]), None)

    def test_a_wolf_ties_the_vote_to_save_its_partner(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"},
                 log=[vote(3, 5)])
        self.assertEqual(w.pick_vote(v, 2, "werewolf", [4, 5]), 4)

    def test_a_wolf_joins_a_wagon_carrying_a_villager(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"},
                 log=[vote(3, 6), vote(7, 6)])
        self.assertEqual(w.pick_vote(v, 2, "werewolf", [4, 5, 6]), 6)


class NightTest(unittest.TestCase):
    def test_the_pack_agreement_is_always_honoured(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"},
                 wolf_votes=[{"seat": 5, "night": 1, "target": 3}])
        self.assertEqual(w.pick_kill(v, 2, "werewolf", [3, 4]), 3)

    def test_a_stale_pack_vote_is_ignored(self):
        # The partner voted on an earlier night for a seat that is no longer offered.
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"},
                 wolf_votes=[{"seat": 5, "night": 0, "target": 3}])
        self.assertIn(w.pick_kill(v, 2, "werewolf", [4, 6]), [4, 6])

    def test_a_claimed_seer_is_the_kill(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"},
                 log=[speech(4, "I am the seer")])
        self.assertEqual(w.pick_kill(v, 2, "werewolf", [3, 4, 6]), 4)

    def test_a_wolf_never_kills_its_partner(self):
        v = view(me=2, role="werewolf", partner={"seat": 5, "name": "Seat5"})
        self.assertNotEqual(w.pick_kill(v, 2, "werewolf", [5]), 5)

    def test_the_doctor_protects_a_seer_claim(self):
        v = view(me=1, role="doctor", log=[speech(4, "I am the seer")])
        self.assertEqual(w.pick_protect(v, 1, "doctor", [3, 4, 6]), 4)

    def test_the_doctor_protects_someone_from_the_options(self):
        v = view(me=1, role="doctor")
        self.assertIn(w.pick_protect(v, 1, "doctor", [3, 4]), [3, 4])

    def test_the_seer_does_not_inspect_a_seat_twice(self):
        v = view(me=1, role="seer", seer_results=[{"seat": 3, "werewolf": False}])
        self.assertNotEqual(w.pick_inspect(v, 1, "seer", [3, 4]), 3)

    def test_the_seer_inspects_a_rival_claimant_first(self):
        v = view(me=1, role="seer", log=[speech(5, "I am the seer")])
        self.assertEqual(w.pick_inspect(v, 1, "seer", [4, 5, 6]), 5)


class SpeechTest(unittest.TestCase):
    def test_a_seer_with_a_wolf_names_it(self):
        v = view(me=1, role="seer", seer_results=[{"seat": 6, "werewolf": True}])
        text = w.say(v, 1, "seer")
        self.assertIn("seat 6", text.lower())
        self.assertIn("werewolf", text.lower())

    def test_a_seer_with_only_clean_reads_says_so(self):
        v = view(me=1, role="seer", seer_results=[{"seat": 3, "werewolf": False},
                                                  {"seat": 4, "werewolf": False}])
        self.assertIn("clean", w.say(v, 1, "seer").lower())

    def test_a_seer_with_nothing_says_nothing(self):
        self.assertIsNone(w.say(view(me=1, role="seer"), 1, "seer"))

    def test_a_wolf_stays_quiet_when_nobody_pushes_it(self):
        self.assertIsNone(w.say(view(me=2, role="werewolf"), 2, "werewolf"))

    def test_a_wolf_answers_an_accusation_aimed_at_it(self):
        v = view(me=2, role="werewolf", log=[speech(4, "seat 2 is a liar")])
        self.assertIsNotNone(w.say(v, 2, "werewolf"))

    def test_a_villager_backs_a_seer_claim(self):
        v = view(me=1, role="villager", log=[speech(4, "I am the seer")])
        self.assertIn("seat 4", w.say(v, 1, "villager").lower())

    def test_a_villager_with_no_seer_claim_says_nothing(self):
        self.assertIsNone(w.say(view(me=1, role="villager"), 1, "villager"))

    def test_no_statement_exceeds_the_cap(self):
        v = view(me=1, role="seer", seer_results=[{"seat": 3, "werewolf": False},
                                                  {"seat": 4, "werewolf": False},
                                                  {"seat": 5, "werewolf": False},
                                                  {"seat": 6, "werewolf": False}])
        self.assertLessEqual(len(w.say(v, 1, "seer")), 500)


class AnswerTest(unittest.TestCase):
    def test_a_silent_seat_passes_rather_than_sending_an_empty_line(self):
        p = {"game": "g", "decision": "speak", "view": view(me=1, role="villager")}
        self.assertEqual(w.answer(p), {"game": "g", "decision": "speak", "pass": True})

    def test_the_character_cap_from_the_prompt_is_respected(self):
        p = {"game": "g", "decision": "speak", "max_chars": 40,
             "view": view(me=1, role="seer",
                          seer_results=[{"seat": 3, "werewolf": False},
                                        {"seat": 4, "werewolf": False}])}
        self.assertLessEqual(len(w.answer(p)["text"]), 40)

    def test_a_target_outside_the_options_is_never_sent(self):
        # A proven wolf that is not in the offered list falls through to the
        # table read, and whatever comes back must still be an offered seat.
        p = {"game": "g", "decision": "vote", "options": [3, 4],
             "view": view(me=1, role="villager",
                          log=[{"kind": "exile", "seat": 6, "role": "werewolf", "day": 1}])}
        self.assertIn(w.answer(p)["target"], [3, 4])

    def test_an_unknown_decision_sends_no_target(self):
        p = {"game": "g", "decision": "dance", "options": [3], "view": view()}
        self.assertIsNone(w.answer(p)["target"])

    def test_the_same_view_answers_the_same_way(self):
        p = {"game": "g", "decision": "vote", "options": [3, 4, 5],
             "view": view(me=1, role="villager", log=[vote(2, 4), vote(3, 4)])}
        self.assertEqual(w.answer(p), w.answer(p))

    def test_every_decision_kind_is_answered(self):
        for kind in ("vote", "kill", "protect", "inspect"):
            p = {"game": "g", "decision": kind, "options": [3, 4],
                 "view": view(me=1, role="villager")}
            out = w.answer(p)
            self.assertEqual(out["decision"], kind)
            self.assertIn(out["target"], [3, 4, None])


def stub_env():
    """A local /observe that answers `pending: []` until the caller says done."""
    state = {"n": 0, "done": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            state["n"] += 1
            body = json.dumps({"done": state["done"], "pending": []}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


class RunLoopTest(unittest.TestCase):
    """The observe loop: what it does with an env that has nothing waiting.

    Both cases here are the same mistake — a `while True` around one HTTP call
    with no pause — so both are bounded by the same measurement: how many
    requests the loop makes in a window it should be sleeping through.
    """

    def setUp(self):
        self._pause = w.POLL_PAUSE
        self.addCleanup(setattr, w, "POLL_PAUSE", self._pause)
        w.POLL_PAUSE = 0.02
        old_env = w.ENV
        self.addCleanup(setattr, w, "ENV", old_env)
        # main() refuses to start without one; the address itself is never used
        # here because each test replaces the transport underneath it.
        w.ENV = "http://127.0.0.1:9"

    @staticmethod
    def _run_quietly():
        # main() reports its decision count on stdout; the tests only care
        # about the exit code.
        with contextlib.redirect_stdout(io.StringIO()):
            return w.main()

    def test_an_idle_env_is_not_polled_in_a_hot_loop(self):
        srv, state = stub_env()
        self.addCleanup(srv.shutdown)
        old_env = w.ENV
        self.addCleanup(setattr, w, "ENV", old_env)
        w.ENV = "http://127.0.0.1:%d" % srv.server_address[1]

        rc = {}
        t = threading.Thread(
            target=lambda: rc.setdefault("v", self._run_quietly()), daemon=True
        )
        t.start()
        time.sleep(0.3)
        seen = state["n"]
        state["done"] = True
        t.join(5)

        # 0.02 s per pause is ~15 calls in 0.3 s. The shipped loop answered 606
        # observes in one second against an env with nothing pending.
        self.assertLessEqual(seen, 25, "%d observes in 0.3 s" % seen)
        self.assertEqual(rc.get("v"), 0)

    def test_a_transport_failure_is_retried_with_a_pause(self):
        calls = {"n": 0}
        stop = {"v": False}

        def dead_env(path, body, timeout=30):
            calls["n"] += 1
            return (0, {}) if not stop["v"] else (200, {"done": True})

        old_post = w.post
        self.addCleanup(setattr, w, "post", old_post)
        w.post = dead_env

        rc = {}
        t = threading.Thread(
            target=lambda: rc.setdefault("v", self._run_quietly()), daemon=True
        )
        t.start()
        time.sleep(0.3)
        seen = calls["n"]
        stop["v"] = True
        t.join(5)

        self.assertLessEqual(seen, 25, "%d retries in 0.3 s" % seen)
        self.assertEqual(rc.get("v"), 0)


if __name__ == "__main__":
    unittest.main()
