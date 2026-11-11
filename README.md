# Table Ledger Seat — a Werewolf agent for make.fun

An entry for [make.fun](https://make.fun) competition `werewolf-agent-01`: a
deterministic, rule-based seat at 8-player Werewolf, standard library only, no
model calls.

## What it does

Each trial it holds one seat at 30 games against seven house players, 15 as a
werewolf and 15 on the village side (5 seer, 5 doctor, 5 villager). The program
posts to the env at `MAKEFUN_ENV_URL`:

- `POST /observe {}` — every decision waiting on the seat, over every live game
- `POST /act {...}` — one answer: a statement, a vote, or a night action

It loops until `done`.

## How it plays

**Reading the table.** Every decision is taken from the game's own view: the
public log of statements, votes, exiles and dawn deaths, plus the seat's private
record (the seer's inspections, the doctor's last protection, the pack's night
votes). From that it builds a seer-claim list, an accusation count per seat, a
talk count and the running vote tally of the day.

**Werewolf.** The pack kill is the whole game: if the partner has already named a
seat the agent names the same one, because two different names kill nobody and an
abstention does not count. By day it never votes its partner; when the partner
leads the tally it throws its vote to force a tie, which exiles nobody, and
otherwise it joins whatever wagon is already carrying a non-wolf to a unique
plurality. It speaks only when accused, and then once, flatly, as a villager.

**Seer.** Inspects the most accused seat it has not checked. A wolf result is
announced plainly with the seat number and a call for the doctor, because a
revealed wolf is worth more than a hidden one. Clean results are shared only once
there are two, so the village can eliminate seats.

**Doctor.** Protects a standing seer claim, otherwise the loudest village voice.

**Villager.** Backs the seer claim it believes and votes that claim's target.

**Determinism.** No clock, no randomness, no model calls. Every answer is a
function of one game's view, and seat numbers break every tie, so the replay
serves the same requests and plays the same games byte for byte.

## Run

```sh
python3 -P -s main.py
```

Needs `MAKEFUN_ENV_URL`. Nothing is installed: standard library only.

## License

MIT.
