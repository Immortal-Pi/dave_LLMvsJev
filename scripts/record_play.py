"""Play Dave yourself and record every tick's keys, to compare with the agent's skills.

Opens the bridge's viewer window and steps the game one tick at a time with the keys you hold
(read with GetAsyncKeyState, so the game window can keep focus; Windows only):

    Left / Right (or A / D)   walk, air control      Up (or W)        jump
    Down (or S)               down                   Ctrl or Space    fire
    Alt                       jetpack (on press)     Esc              stop and save

The game is deterministic, so the key log alone reproduces the run:
scripts/compare_play.py replays it headless and compares your moves with the skill catalog.
Each line of the log is one tick: {"t", "keys", "x", "y", "state"} (position for a sanity check
on replay); the first line is a header with the scenario and build id.

Usage: uv run python scripts/record_play.py --scenario level4 [--delay 14] [--out FILE]
"""

import argparse
import ctypes
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.adapters.dave import BUTTON_KEYS
from dave_agent.config import load_config

VK = {
    "left": (0x25, 0x41), "right": (0x27, 0x44), "jump": (0x26, 0x57), "down": (0x28, 0x53),
    "fire": (0x11, 0x20), "jetpack": (0x12,),
}
ESC = 0x1B


def held(user32) -> set[str]:
    return {b for b, codes in VK.items() if any(user32.GetAsyncKeyState(c) & 0x8000 for c in codes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="level4")
    parser.add_argument("--config", type=Path, default=Path("configs/watch.yaml"))
    parser.add_argument("--delay", type=int, default=14, help="ms shown per tick (14 is game speed; more is slow motion)")
    parser.add_argument("--out", type=Path, help="default: artifacts/human/<scenario>-<time>.jsonl")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("record_play reads the keyboard with GetAsyncKeyState: Windows only", file=sys.stderr)
        return 2
    user32 = ctypes.windll.user32
    out = args.out or Path("artifacts/human") / f"{args.scenario}-{datetime.now():%Y%m%dT%H%M%S}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(args.config)
    adapter = create_adapter("dave", config.environment, watch=True, watch_delay_ms=args.delay)
    ticks = deaths = 0
    try:
        obs = adapter.reset(args.scenario, 0)
        with out.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"scenario": args.scenario, "build_id": obs.build_id,
                                "recorded": datetime.now().isoformat(timespec="seconds")}) + "\n")
            print(f"recording to {out}; click the game window, play, Esc to stop")
            time.sleep(1.0)
            was = set()
            while obs.terminal == "running":
                if user32.GetAsyncKeyState(ESC) & 0x8000:
                    break
                now = held(user32)
                # The jetpack is a key-down toggle: send it on the press only.
                keys = now - ({"jetpack"} & was)
                was = now
                result = adapter.step(frozenset(keys), 1)
                obs = result.observation
                p = obs.player_position
                deaths += any(e.event_type == "death" for e in result.events)
                f.write(json.dumps({"t": ticks, "keys": "".join(sorted(BUTTON_KEYS[k] for k in keys)) or "-",
                                    "frames": result.frames_advanced, "x": p and p.x, "y": p and p.y,
                                    "state": obs.player_state}) + "\n")
                ticks += 1
    finally:
        adapter.close()
    print(f"saved {ticks} ticks to {out}: terminal {obs.terminal}, deaths {deaths}, score {obs.score}")
    print(f"compare: uv run python scripts/compare_play.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
