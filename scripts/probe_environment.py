"""Phase 2 acceptance probe against the real deadly-dave bridge.

Checks movement, a jump, a collectible pickup, death and respawn, and snapshot
restoration. Saves screenshots for human comparison of recorded positions.

Usage: uv run python scripts/probe_environment.py [--out artifacts/probe] [--watch [--watch-delay MS]]
"""

import argparse
import json
import sys
from pathlib import Path

from dave_agent.adapters.dave import DaveBridgeAdapter

ROOT = Path(__file__).resolve().parents[1]
RIGHT, LEFT, JUMP = frozenset({"right"}), frozenset({"left"}), frozenset({"jump"})


def hold(adapter, buttons, ticks):
    """Step one tick at a time so every event is captured; return (last result, events)."""
    events, result = [], None
    for _ in range(ticks):
        result = adapter.step(buttons, 1)
        events.extend(result.events)
        if result.observation.terminal != "running" or any(e.event_type == "death" for e in result.events):
            break
    return result, events


def comparable(obs):
    return obs.model_dump(exclude={"observation_id"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "probe")
    parser.add_argument("--watch", action="store_true", help="show the game in a window while probing")
    parser.add_argument("--watch-delay", type=int, default=14, metavar="MS")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    adapter = DaveBridgeAdapter(ROOT / "external" / "deadly-dave", watch=args.watch, watch_delay_ms=args.watch_delay)
    checks: dict[str, dict] = {}
    try:
        # 1. Movement
        start = adapter.reset("level1", 0)
        adapter.screenshot(args.out / "01_level1_start.bmp")
        moved, _ = hold(adapter, RIGHT, 30)
        adapter.screenshot(args.out / "02_level1_after_right.bmp")
        checks["movement"] = {
            "ok": moved.observation.player_position.x > start.player_position.x,
            "start": start.player_position.model_dump(),
            "after_30_ticks_right": moved.observation.player_position.model_dump(),
        }

        # 2. Jump: leaves the ground and rises
        adapter.reset("level1", 0)
        jumped, _ = hold(adapter, JUMP, 1)
        rise, _ = hold(adapter, frozenset(), 20)
        checks["jump"] = {
            "ok": rise.observation.player_position.y < start.player_position.y and not rise.observation.grounded,
            "y_start": start.player_position.y,
            "y_after_20_ticks": rise.observation.player_position.y,
        }

        # 3. Collectible: jump left onto the gem at tile (1, 7)
        adapter.reset("level1", 0)
        _, first = hold(adapter, frozenset({"jump", "left"}), 1)
        got, rest = hold(adapter, LEFT, 40)
        pickups = [e for e in first + rest if e.event_type == "item_collected"]
        adapter.screenshot(args.out / "03_level1_after_pickup.bmp")
        checks["collectible"] = {
            "ok": len(pickups) == 1 and got.observation.score == 100,
            "events": [{"item": e.payload["item"], "tile": e.location.model_dump(), "frame": e.frame} for e in pickups],
            "score": got.observation.score,
        }

        # 4. Snapshot restore reproduces the same future
        snap = adapter.save_snapshot()
        future_a, _ = hold(adapter, RIGHT, 25)
        restored = adapter.load_snapshot(snap)
        future_b, _ = hold(adapter, RIGHT, 25)
        checks["snapshot_restore"] = {
            "ok": restored.frame == snap.frame
            and comparable(future_a.observation) == comparable(future_b.observation),
            "snapshot_frame": snap.frame,
            "position_after_25_ticks": future_b.observation.player_position.model_dump(),
        }

        # 5. Death and respawn: level 2's floor is fire from column 3
        l2 = adapter.reset("level2", 0)
        dead, events = hold(adapter, RIGHT, 400)
        adapter.screenshot(args.out / "04_level2_after_respawn.bmp")
        types = [e.event_type for e in events]
        checks["death_respawn"] = {
            "ok": types[-2:] == ["death", "respawn"]
            and dead.observation.lives == l2.lives - 1
            and dead.observation.player_position == l2.player_position,
            "events": types,
            "lives": [l2.lives, dead.observation.lives],
            "respawn_position": dead.observation.player_position.model_dump(),
            "death_cause": next(e.payload["cause"] for e in events if e.event_type == "death") if "death" in types else None,
        }

        # 6. Local-observed filtering: nothing outside the 20-column viewport
        obs = adapter.observe()
        checks["local_observed"] = {
            "ok": all(obs.region.contains(t.pos) for t in obs.tiles)
            and obs.region.max.col - obs.region.min.col + 1 <= 20,
            "region": obs.region.model_dump(),
        }
        caps = adapter.capabilities()
    finally:
        adapter.close()

    report = {"adapter": "dave", "build_id": caps.build_id, "screenshots": str(args.out), "checks": checks}
    print(json.dumps(report, indent=2))
    return 0 if all(c["ok"] for c in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
