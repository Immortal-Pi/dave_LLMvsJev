"""Experience notes: what a skill did before from here, appended to the candidate description."""

from dave_agent.control.experience import MAX_DESCRIPTION, annotate_experience
from dave_agent.memory.working import Experience
from dave_agent.schemas import SkillCandidate, TilePos


def cand(skill, description="jump right about 2 tiles, then drop straight down"):
    return SkillCandidate(candidate_id=f"c0_{skill}", skill=skill, description=description, max_frames=238)


def test_notes_for_tried_skills_only():
    here = {"jump_right_short": Experience(attempts=2, deaths=2, burned=2, last_end=TilePos(col=3, row=9)),
            "jump_up": Experience(attempts=1, no_move=1, last_end=TilePos(col=1, row=9))}
    past = {"jump_right_short": {"attempts": 5, "successes": 3, "fatal": 2, "lands": (7, 4, 9)},
            "move_right_1": {"attempts": 0, "successes": 0, "fatal": 0, "lands": None}}
    a, b, c = annotate_experience([cand("jump_right_short"), cand("jump_up", "jump straight up and land"),
                                   cand("move_right_1", "walk right about 1 tile")], here, past)
    assert a.description == ("jump right about 2 tiles, then drop straight down; this episode from here: 2x, "
                             "2 died (burned), last end [3,9]; past runs from this platform: 5x, 3 ok, 2 fatal, "
                             "lands row 7 cols 4-9")
    assert b.description == "jump straight up and land; this episode from here: 1x, 1 did not move, last end [1,9]"
    assert c.description == "walk right about 1 tile"  # never tried: unchanged
    assert a.candidate_id == "c0_jump_right_short" and a.max_frames == 238


def test_mixed_deaths_and_cap():
    here = {"s": Experience(attempts=12, deaths=3, burned=1, no_move=4, last_end=TilePos(col=12, row=10))}
    past = {"s": {"attempts": 120, "successes": 100, "fatal": 20, "lands": (10, 3, 14)}}
    (out,) = annotate_experience([cand("s", "x" * (MAX_DESCRIPTION - 50))], here, past)
    assert len(out.description) == MAX_DESCRIPTION
    (out,) = annotate_experience([cand("s", "")], here)
    assert out.description == "this episode from here: 12x, 3 died (1 burned), 4 did not move, last end [12,10]"
