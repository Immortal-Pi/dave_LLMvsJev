"""Phase 3: legal masks, revalidation, bounded phased execution and interruption (fixture only)."""

import pytest
from pydantic import ValidationError

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.skills import execute, generate_candidates, revalidate, stale_fallback
from dave_agent.schemas import Entity, PixelPos, StepResult

from ..conftest import LEVELS

CFG = ExecutorConfig(hazard_radius_px=48)
BUTTONS = frozenset({"left", "right", "jump"})


def spec(name, phases, kind="single", **kw):
    return SkillSpec.model_validate({"name": name, "kind": kind, "phases": phases, **kw})


WALK_RIGHT_10 = spec("walk_right", [{"buttons": ["right"], "ticks": 10}])
WAIT_2 = spec("wait", [{"buttons": [], "ticks": 2}])


class RecordingAdapter:
    """Wraps an adapter and records every step call."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[frozenset, int]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def step(self, buttons, frames):
        self.calls.append((buttons, frames))
        return self.inner.step(buttons, frames)


def test_candidate_ids_deterministic_and_in_catalog_order(adapter, config):
    skills = config.skills.for_adapter("fixture")
    obs = adapter.reset("fixture_l1", 0)
    a = generate_candidates(skills, BUTTONS, obs)
    b = generate_candidates(skills, BUTTONS, obs)
    assert a.ids == b.ids and a.digest() == b.digest()
    assert a.ids[0] == "c0_move_left" and [c.skill for c in a.candidates] == [s.name for s in skills]


def test_unavailable_field_masks_skill_instead_of_guessing(adapter):
    obs = adapter.reset("fixture_l1", 0)
    blind = obs.model_copy(update={"player_state": None, "unavailable_fields": frozenset({"player_state"})})
    skills = (spec("hop", [{"buttons": ["jump"], "ticks": 1}], preconditions=["on_ground"]), WAIT_2)
    offered = generate_candidates(skills, BUTTONS, blind)
    assert offered.ids == ("c0_wait",) and offered.masked == {"hop": "on_ground"}


def test_unsupported_buttons_are_masked(adapter):
    obs = adapter.reset("fixture_l1", 0)
    skills = (spec("shoot", [{"buttons": ["fire"], "ticks": 1}]), WAIT_2)
    offered = generate_candidates(skills, BUTTONS, obs)
    assert offered.ids == ("c0_wait",) and offered.masked["shoot"].startswith("unsupported_buttons")


def test_revalidate_rejects_failed_precondition_and_does_not_step(adapter):
    obs = adapter.reset("fixture_l1", 0)
    hop = spec("hop", [{"buttons": ["jump"], "ticks": 1}], preconditions=["has_gun"])
    candidate = generate_candidates((WAIT_2,), BUTTONS, obs).candidates[0].model_copy(
        update={"skill": "hop", "max_frames": 1}
    )
    assert revalidate(candidate, (hop,), obs) == "precondition:has_gun"
    rec = RecordingAdapter(adapter)
    run = execute(rec, candidate, (hop,), obs, CFG)
    assert run.outcome == "rejected" and rec.calls == []


def test_revalidate_rejects_tampered_duration(adapter):
    obs = adapter.reset("fixture_l1", 0)
    candidate = generate_candidates((WAIT_2,), BUTTONS, obs).candidates[0]
    assert revalidate(candidate.model_copy(update={"max_frames": 50}), (WAIT_2,), obs) == "max_frames_mismatch"


def test_phases_apply_exact_buttons_one_frame_at_a_time(adapter):
    obs = adapter.reset("fixture_l1", 0)
    hop_right = spec("hop_right", [{"buttons": ["jump", "right"], "ticks": 1}, {"buttons": [], "ticks": 2}],
                     kind="macro")
    rec = RecordingAdapter(adapter)
    candidate = generate_candidates((hop_right,), BUTTONS, obs).candidates[0]
    run = execute(rec, candidate, (hop_right,), obs, CFG)
    assert rec.calls == [(frozenset({"jump", "right"}), 1), (frozenset(), 1), (frozenset(), 1)]
    assert run.outcome == "completed" and run.input_ticks == 3 == candidate.max_frames
    kinds = [e.event_type for e in run.events]
    assert kinds[0] == "skill_started" and kinds[-1] == "skill_finished"


def test_death_interrupts_and_releases(adapter):
    obs = adapter.reset("fixture_l1", 0)  # start (1,4); fire at (4,4)
    rec = RecordingAdapter(adapter)
    candidate = generate_candidates((WALK_RIGHT_10,), BUTTONS, obs).candidates[0]
    run = execute(rec, candidate, (WALK_RIGHT_10,), obs, CFG)
    assert run.outcome == "interrupted" and run.reason == "death"
    assert run.input_ticks == 3 < candidate.max_frames
    # No further input after the interruption: the next skill starts from released keys.
    assert len(rec.calls) == 3
    finished = run.events[-1]
    assert finished.event_type == "skill_finished" and finished.payload["reason"] == "death"


def test_until_phase_times_out_within_budget(adapter):
    obs = adapter.reset("fixture_l1", 0)
    never = spec("wait_for_gun", [{"buttons": [], "until": "has_gun", "max_ticks": 4}])
    candidate = generate_candidates((never,), BUTTONS, obs).candidates[0]
    run = execute(adapter, candidate, (never,), obs, CFG)
    assert run.outcome == "failed" and run.reason == "phase0_timeout:has_gun"
    assert run.input_ticks == 4 == candidate.max_frames


def test_until_phase_ends_early_when_satisfied(adapter):
    obs = adapter.reset("fixture_l1", 0)
    hop = spec("hop", [{"buttons": ["jump"], "until": "jumping", "max_ticks": 5},
                       {"buttons": [], "until": "landed", "max_ticks": 20}], kind="macro")
    candidate = generate_candidates((hop,), BUTTONS, obs).candidates[0]
    run = execute(adapter, candidate, (hop,), obs, CFG)
    assert run.outcome == "completed" and run.input_ticks < candidate.max_frames
    assert run.observation.grounded


def test_new_hazard_nearby_interrupts(adapter):
    obs = adapter.reset("fixture_l1", 0)

    class SpawningAdapter(RecordingAdapter):
        def step(self, buttons, frames):
            result = super().step(buttons, frames)
            p = result.observation.player_position
            ghost = Entity(entity_id="monster0", entity_type="spider", position=PixelPos(x=p.x + 32, y=p.y),
                           last_observed_frame=result.observation.frame, visible=True, source="adapter")
            return StepResult(observation=result.observation.model_copy(update={"entities": (ghost,)}),
                              frames_advanced=result.frames_advanced, applied_buttons=result.applied_buttons,
                              events=result.events)

    skills = (spec("wait", [{"buttons": [], "ticks": 5}], interrupt_on=["death", "terminal", "new_hazard_nearby"]),)
    candidate = generate_candidates(skills, BUTTONS, obs).candidates[0]
    run = execute(SpawningAdapter(adapter), candidate, skills, obs, CFG)
    assert run.outcome == "interrupted" and run.reason == "new_hazard_nearby:monster0" and run.input_ticks == 1


def test_execution_is_deterministic_from_snapshot(adapter, config):
    skills = config.skills.for_adapter("fixture")
    adapter.reset("fixture_l1", 0)
    snap = adapter.save_snapshot()
    traces = []
    for _ in range(2):
        obs = adapter.load_snapshot(snap)
        candidate = generate_candidates(skills, BUTTONS, obs).candidates[4]  # jump_right
        run = execute(adapter, candidate, skills, obs, CFG)
        traces.append([s.observation.model_dump(exclude={"observation_id"}) for s in run.steps])
    assert traces[0] == traces[1]


def test_stale_fallback_is_the_wait(adapter, config):
    skills = config.skills.for_adapter("fixture")
    obs = adapter.reset("fixture_l1", 0)
    assert stale_fallback(generate_candidates(skills, BUTTONS, obs).candidates, skills).skill == "wait"


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "kind": "single", "phases": [{"buttons": [], "ticks": 1, "until": "landed"}]},
        {"name": "x", "kind": "single", "phases": [{"buttons": [], "until": "landed"}]},
        {"name": "x", "kind": "single", "phases": [{"buttons": [], "ticks": 1}, {"buttons": [], "ticks": 1}]},
        {"name": "x", "kind": "macro", "phases": [{"buttons": [], "ticks": 400}, {"buttons": [], "ticks": 400}]},
        {"name": "x", "kind": "single", "phases": []},
    ],
)
def test_invalid_skill_specs_rejected(bad):
    with pytest.raises(ValidationError):
        SkillSpec.model_validate(bad)


def test_fixture_adapter_reports_player_state(adapter):
    obs = adapter.reset("fixture_l1", 0)
    assert obs.player_state == "standing" and obs.facing == "right"
    obs = adapter.step(frozenset({"jump"}), 1).observation
    assert obs.player_state == "jumping"
    assert FixtureAdapter(LEVELS).reset("fixture_l1", 0).player_state == "standing"
