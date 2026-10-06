"""Experience notes on candidates: what each skill did before when started from here.

Two sources, both appended to the candidate's description (the offered set and its digest are
computed before, so replay and parity checks are unaffected):

- this episode (every arm): working memory's per-(tile, skill) table, which survives respawns,
  so a move that killed Dave from this tile is marked when he is back on the tile;
- past runs (graph-enabled arms only): the learned graph's evidence for the segment Dave stands
  on, taken from the checkpoint as it was at episode start, so it never counts this episode twice;
  and its goal credit toward the active goal's target from there (control/credit.py): how many
  past goals used each skill from this platform and how many were achieved.

Notes only inform; nothing is masked and the controller still chooses.
"""

from __future__ import annotations

from typing import Any

from dave_agent.memory.working import Experience
from dave_agent.schemas import DESCRIPTION_MAX, SkillCandidate

MAX_DESCRIPTION = DESCRIPTION_MAX


def episode_note(e: Experience) -> str:
    parts = [f"{e.attempts}x"]
    if e.deaths:
        parts.append(f"{e.deaths} died" + (" (burned)" if e.burned == e.deaths else
                                           f" ({e.burned} burned)" if e.burned else ""))
    if e.no_move:
        parts.append(f"{e.no_move} did not move")
    if e.last_end is not None:
        parts.append(f"last end [{e.last_end.col},{e.last_end.row}]")
    return "this episode from here: " + ", ".join(parts)


def past_note(rec: dict[str, Any]) -> str:
    parts = [f"{rec['attempts']}x", f"{rec['successes']} ok"]
    if rec["fatal"]:
        parts.append(f"{rec['fatal']} fatal")
    if rec["lands"] is not None:
        row, lo, hi = rec["lands"]
        parts.append(f"lands row {row} cols {lo}-{hi}")
    return "past runs from this platform: " + ", ".join(parts)


def credit_note(rec: dict[str, int]) -> str:
    return (f"past goals like this one from this platform: {rec['reached']}/{rec['goals']} reached "
            f"({rec.get('closer', 0)}/{rec.get('tries', 0)} tries closer)")


def annotate_experience(candidates: list[SkillCandidate], here: dict[str, Experience],
                        past: dict[str, dict[str, Any]] | None = None,
                        credit: dict[str, dict[str, int]] | None = None) -> list[SkillCandidate]:
    """Each candidate with its experience notes appended; untried skills are unchanged.
    ``credit``: past goal credit toward the active goal's target from this platform (graph arms)."""
    out = []
    for c in candidates:
        notes = []
        if c.skill in here:
            notes.append(episode_note(here[c.skill]))
        if past and past.get(c.skill, {}).get("attempts"):
            notes.append(past_note(past[c.skill]))
        if credit and credit.get(c.skill, {}).get("goals"):
            notes.append(credit_note(credit[c.skill]))
        if notes:
            text = "; ".join([c.description, *notes]) if c.description else "; ".join(notes)
            c = c.model_copy(update={"description": text[:MAX_DESCRIPTION]})
        out.append(c)
    return out
