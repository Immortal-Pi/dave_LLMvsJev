"""Check the threat prediction against the real game, tick by tick.

Runs a fixed sequence of catalog skills (as scripts/try_skills.py does) and, for each one,
compares what control/threats.py and control/reach.py predicted when it started with what the
game did on every frame it ran: Dave's position, each monster's position and its plasma. Prints
the first divergence over ``--tolerance`` pixels per skill, and the threat screen's verdict on
every candidate at that point (what would have been masked, and why).

With ``--patient``, a skill the threat screen masks is not run yet: Dave waits (``wait_short``,
or the screen's first kept skill when it masks the wait too) and tries again, up to
``--max-waits`` times, as the timing notes advise.

Usage: uv run python scripts/audit_threats.py --scenario level4 move_right_3 jump_right ...
       [--from-file skills.txt] [--quiet]   # --quiet: only skills with a divergence or a death
       [--patient]
"""

import argparse
import sys
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.config import load_config
from dave_agent.control.reach import ReachMap, trace_in_jump, trace_skill
from dave_agent.control.skills import execute, generate_candidates
from dave_agent.control.threats import FACING, assess, deadzone, screen, simulate, threats


def learn(cells: dict, obs) -> None:
    r = obs.region
    for col in range(r.min.col, r.max.col + 1):
        for row in range(r.min.row, r.max.row + 1):
            cells.setdefault((col, row), "empty")
    for t in obs.tiles:
        cells[(t.pos.col, t.pos.row)] = t.kind


def predicted_path(reach: ReachMap, obs, spec) -> list[tuple[int, int]]:
    pos = obs.player_position
    if obs.player_state == "jumping" and obs.jump_tick is not None:
        return trace_in_jump(reach, pos.x, pos.y, obs.jump_tick, spec)
    facing = FACING.get(obs.facing or "", 0) if obs.player_state == "freefalling" else 0
    return trace_skill(reach, pos.x, pos.y, spec, facing, cooldown=obs.jump_cooldown or 0)


def far(a, b, tol: int) -> bool:
    return a is None or b is None or abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol


def audit(reach: ReachMap, obs, spec, run, tol: int) -> list[str]:
    """The first divergence of Dave, and of each monster and its plasma, as text lines."""
    frames = [s.observation for s in run.steps]
    path = predicted_path(reach, obs, spec)
    out = []
    for i, o in enumerate(frames):
        want = path[min(i, len(path) - 1)]
        got = (o.player_position.x, o.player_position.y) if o.player_position else None
        if o.player_state in ("burning", "dead"):
            break
        if far(want, got, tol):
            out.append(f"    dave: tick {i + 1} predicted {want} actual {got}")
            break
    zone = deadzone(obs)
    xs = [p[0] for p in path]
    for t in threats(obs):
        if t.motion is None:
            continue
        f = simulate(t, xs, len(frames), reach.cells, zone)
        n = t.entity_id[len("monster"):]
        for i, o in enumerate(frames):
            seen = {e.entity_id: (e.position.x, e.position.y) for e in o.entities if e.visible}
            if far(f.monster[i], seen.get(t.entity_id), tol):
                out.append(f"    {t.entity_id}: tick {i + 1} predicted {f.monster[i]} actual {seen.get(t.entity_id)}")
                break
        for i, o in enumerate(frames):
            seen = {e.entity_id: (e.position.x, e.position.y) for e in o.entities if e.visible}
            want, got = f.shots[i], seen.get(f"plasma{n}")
            if (want is None) != (got is None) or (want is not None and far(want, got, tol)):
                out.append(f"    plasma{n} (shot {f.shot_no[i]}): tick {i + 1} predicted {want} actual {got}")
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skills", nargs="*", help="catalog skill names, run in order")
    parser.add_argument("--from-file", type=Path, help="whitespace-separated skill names, run before the others")
    parser.add_argument("--scenario", default="level4")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path, default=Path("configs/watch.yaml"))
    parser.add_argument("--tolerance", type=int, default=2, help="pixels of divergence allowed")
    parser.add_argument("--quiet", action="store_true", help="print only skills with a divergence or a death")
    parser.add_argument("--patient", action="store_true", help="wait while the next skill is masked")
    parser.add_argument("--max-waits", type=int, default=40)
    args = parser.parse_args()
    names = (args.from_file.read_text(encoding="utf-8").split() if args.from_file else []) + args.skills

    config = load_config(args.config)
    catalog = config.skills.for_adapter("dave")
    specs = {s.name: s for s in catalog}
    rcfg = config.skills.reach["dave"]
    tcfg = config.skills.executor.threats
    adapter = create_adapter("dave", config.environment)
    cells: dict = {}
    deaths = waits = 0
    try:
        obs = adapter.reset(args.scenario, args.seed)
        buttons = adapter.capabilities().buttons
        for i, name in enumerate(names):
            for _ in range(args.max_waits if args.patient else 0):
                learn(cells, obs)
                offered = generate_candidates(catalog, buttons, obs).candidates
                kept, masked = screen(offered, assess(obs, ReachMap(cells, rcfg), offered, specs, tcfg))
                if not any(c.skill == name and c.candidate_id in masked for c in offered):
                    break
                # Wait if the screen keeps a wait; when it does not, Dave is in a trap: take what it
                # keeps (the latest contact) and come back to the planned skill after it.
                step = next((c for c in kept if c.skill == "wait_short"), kept[0])
                if step.skill != "wait_short":
                    why = next(masked[c.candidate_id] for c in offered if c.skill == name)
                    print(f"    escape: {step.skill} instead of {name} ({why}; kept {[c.skill for c in kept]})")
                obs = execute(adapter, step, catalog, obs, config.skills.executor).observation
                waits += 1
            learn(cells, obs)
            reach = ReachMap(cells, rcfg)
            offered = generate_candidates(catalog, buttons, obs).candidates
            candidate = next((c for c in offered if c.skill == name), None)
            if candidate is None:
                print(f"{i:3d} {name:18s} NOT LEGAL")
                continue
            contacts = assess(obs, reach, offered, specs, tcfg)
            start = obs
            run = execute(adapter, candidate, catalog, obs, config.skills.executor)
            obs = run.observation
            lines = audit(reach, start, specs[name], run, args.tolerance)
            died = obs.player_state in ("burning", "dead") or any(e.event_type == "death" for e in run.events)
            deaths += died
            if args.quiet and not lines and not died:
                continue
            p, q = start.player_position, obs.player_position
            print(f"{i:3d} {name:18s} {run.outcome:11s} {run.frames:3d}f px ({p.x},{p.y})->({q.x},{q.y}) "
                  f"cooldown {start.jump_cooldown} state {obs.player_state}"
                  f"{'  reason ' + run.reason if run.reason else ''}{'  DIED' if died else ''}")
            mine = contacts.get(candidate.candidate_id)
            if died and (mine is None or not mine.blocks):
                print("    FORECAST MISS: predicted safe, but Dave died (control/goals.py records these as forecast_miss)")
            print(f"    screen: this skill {'safe' if mine is None else mine.note()}; masked "
                  f"{sorted(c.split('_', 1)[1] for c, h in contacts.items() if h is not None)}")
            for line in lines:
                print(line)
            if obs.terminal != "running":
                print(f"terminal: {obs.terminal}")
                break
    finally:
        adapter.close()
    p = obs.player_position
    print(f"end: px ({p.x},{p.y}) screen from col {obs.region.min.col}, frame {obs.frame}, deaths {deaths}, "
          f"waits {waits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
