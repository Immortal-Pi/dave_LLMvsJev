"""Live viewer server: run one episode at a time and stream what happens to a browser.

``dave-agent live`` serves on 127.0.0.1 (stdlib ``http.server``, no extra dependencies):

| Route | |
| --- | --- |
| ``POST /start`` | ``{scenario, arm, planner: mock|live, tactical: mock|live, seed?, pause?}``: start an episode (``pause: false``: the game runs on while models think) |
| ``POST /stop`` | stop it at the next game tick (recorded as truncated / interrupted, like Ctrl-C) |
| ``GET /events`` | Server-Sent Events: ``run``, ``episode``, ``plan``, ``goal``, ``deciding``, ``decision``, ``outcome``, ``summary``, ``notice``, ``error``, ``idle`` (the current run's events are replayed first, so a reload catches up) |
| ``GET /frame`` | the latest game frame (BMP, 320x200 with the HUD) |
| ``GET /status`` | idle or running, the levels, arms and whether paid modes are allowed |

The episode runs exactly as ``play`` runs it (``run_trial``: same models, goal manager, store and
graph rules), so a live run can be inspected afterwards (``dave-agent inspect``). The only
differences are presentation: ``LiveAdapter`` paces the game at ``tick_ms`` per tick so a human
can follow it, and saves a frame every ``frame_every`` ticks; ``run_episode``'s write-only
``on_event`` hook feeds the event stream. Neither changes a decision. Paid models (``live``
planner or tactical) are refused unless the server was started with ``--allow-paid``.
See docs/live.md.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dave_agent.adapters import create_adapter
from dave_agent.config import AppConfig, ConfigError
from dave_agent.memory.episodes import EpisodeStore
from dave_agent.memory.persistence import save_store
from dave_agent.runner.session import budget_notice, build_models, episode_summary, open_graph, run_trial

log = logging.getLogger(__name__)

DAVE_LEVELS = tuple(f"level{i}" for i in range(1, 10))
LATEST_ONLY = frozenset({"graph"})  # a full snapshot each time (~10-20 KB): keep the newest only
MAX_EVENTS = 20000  # per run; older events are dropped from the replay buffer


class StopRequested(KeyboardInterrupt):
    """Raised inside the game loop when the viewer presses Stop; handled like Ctrl-C."""


class LiveHub:
    """Thread-safe store of the current run's events and the latest frame."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._frame: bytes | None = None
        self.frame_no = 0

    def reset(self) -> None:
        with self._cond:
            self._events = []
            self._frame = None
            self.frame_no = 0
            self._publish("reset", {})

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        with self._cond:
            self._publish(kind, data)

    def _publish(self, kind: str, data: dict[str, Any]) -> None:
        if kind in LATEST_ONLY:  # the viewer draws only the newest, so a replay needs no older copy
            self._events = [e for e in self._events if e["type"] != kind]
        self._seq += 1
        self._events.append({"seq": self._seq, "type": kind, "data": data})
        del self._events[:-MAX_EVENTS]
        self._cond.notify_all()

    def events_after(self, seq: int, timeout: float) -> list[dict[str, Any]]:
        """Events newer than ``seq`` (the whole current run when ``seq`` is older than it),
        waiting up to ``timeout`` seconds for one."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > seq, timeout)
            return [e for e in self._events if e["seq"] > seq]

    def set_frame(self, data: bytes, frame_no: int) -> None:
        with self._cond:
            self._frame, self.frame_no = data, frame_no

    @property
    def frame(self) -> bytes | None:
        with self._cond:
            return self._frame


class LiveAdapter:
    """A game adapter that runs at watchable speed and publishes frames. Everything else is the
    wrapped adapter's: observations and decisions are unchanged."""

    def __init__(self, inner, hub: LiveHub, tick_ms: float, frame_every: int, stop: threading.Event,
                 frame_dir: Path) -> None:
        self.inner, self.hub, self.tick_s, self.frame_every, self.stop = inner, hub, tick_ms / 1000, frame_every, stop
        self.frame_path = frame_dir / "frame.bmp"
        if " " in str(self.frame_path):
            raise ConfigError(f"frame path {self.frame_path} has a space; the bridge cannot read it")
        self._ticks = 0
        self._next = 0.0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def reset(self, scenario_id: str, seed: int):
        observation = self.inner.reset(scenario_id, seed)
        self._ticks, self._next = 0, time.monotonic()
        self.capture(observation.frame)
        return observation

    def step(self, buttons, frames: int = 1):
        if self.stop.is_set():
            raise StopRequested
        result = self.inner.step(buttons, frames)
        self._ticks += frames
        if self._ticks % self.frame_every == 0 or result.observation.terminal != "running":
            self.capture(result.observation.frame)
        now = time.monotonic()
        self._next = max(self._next + self.tick_s * frames, now)  # after a model pause, resume at pace
        if self._next > now:
            time.sleep(self._next - now)
        return result

    def capture(self, frame_no: int) -> None:
        """Publish the current frame (a no-op on adapters without screenshots)."""
        if not hasattr(self.inner, "screenshot"):
            return
        try:
            self.inner.screenshot(self.frame_path)
            self.hub.set_frame(self.frame_path.read_bytes(), frame_no)
        except Exception:  # a missing frame never stops the episode
            log.debug("frame capture failed", exc_info=True)


class LiveServer:
    def __init__(self, config: AppConfig, adapter_name: str, store_path: Path | None = None,
                 allow_paid: bool = False, tick_ms: float = 14.0, frame_every: int = 3,
                 adapter_factory=create_adapter) -> None:
        self.config, self.adapter_name, self.allow_paid = config, adapter_name, allow_paid
        self.store_path = store_path or config.memory.episode_store.parent / "live.sqlite"
        self.tick_ms, self.frame_every, self.adapter_factory = tick_ms, frame_every, adapter_factory
        self.hub = LiveHub()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.run_id: str | None = None
        self._frame_dir = Path(tempfile.mkdtemp(prefix="dave-live-"))

    # -- control ---------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def levels(self) -> tuple[str, ...]:
        return DAVE_LEVELS if self.adapter_name == "dave" else (self.config.scenario,)

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "run_id": self.run_id, "adapter": self.adapter_name,
                "levels": list(self.levels()), "allow_paid": self.allow_paid,
                "arms": {name: arm.model_dump(mode="json") for name, arm in sorted(self.config.arms.items())},
                "max_frames": self.config.benchmark.max_episode_frames}

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate, build the models (credentials first) and start the episode thread.
        Raises ``ConfigError`` with a message for the viewer."""
        scenario = str(request.get("scenario") or self.levels()[0])
        arm = str(request.get("arm") or "B")
        planner, tactical = request.get("planner", "mock"), request.get("tactical", "mock")
        seed = int(request.get("seed", self.config.benchmark.seed))
        pause = request.get("pause", True)
        if not isinstance(pause, bool):
            raise ConfigError("pause is true or false")
        if not pause and self.tick_ms <= 0:
            raise ConfigError("the game runs on while models think only at a paced speed; "
                              "restart the server without --tick-ms 0")
        if scenario not in self.levels():
            raise ConfigError(f"unknown level {scenario!r}; choose one of {list(self.levels())}")
        if arm not in self.config.arms:
            raise ConfigError(f"unknown arm {arm!r}; configured arms: {sorted(self.config.arms)}")
        if planner not in ("mock", "live") or tactical not in ("mock", "live"):
            raise ConfigError("planner and tactical are 'mock' or 'live'")
        paid = planner == "live" or tactical == "live"
        if paid and not self.allow_paid:
            raise ConfigError("live models are paid calls; restart the server with --allow-paid to use them")
        with self._lock:
            if self.running:
                raise ConfigError(f"run {self.run_id} is still going; stop it first")
            models = build_models(self.config, arm, planner == "live", tactical == "live", seed)
            self.run_id = f"live-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
            self._stop.clear()
            self.hub.reset()
            # Pause off: real-time mode for this run only (recorded in the run's config).
            config = self.config if pause else self.config.model_copy(update={
                "environment": self.config.environment.model_copy(update={"execution_mode": "real_time"})})
            info = {"run_id": self.run_id, "scenario": scenario, "arm": arm, "seed": seed, "mode": models.mode,
                    "pause": pause,
                    "planner": f"{models.planner.provider}:{models.planner.model}",
                    "tactical": models.controller.model, "arm_config": self.config.arms[arm].model_dump(mode="json")}
            self.hub.publish("run", info)
            if paid:
                self.hub.publish("notice", budget_notice(self.config, models.mode))
            self._thread = threading.Thread(target=self._run, args=(models, scenario, arm, seed, config), daemon=True,
                                            name=f"live-{self.run_id}")
            self._thread.start()
            return info

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop.set()
        return True

    def close(self) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=10)

    # -- the episode -----------------------------------------------------------------------------
    def _run(self, models, scenario: str, arm: str, seed: int, config: AppConfig | None = None) -> None:
        config = config or self.config
        run_id = self.run_id
        assert run_id is not None
        adapter = None
        store = EpisodeStore(self.store_path)
        graph, graph_path, learn, recorder = None, None, False, None
        try:
            adapter = self.adapter_factory(self.adapter_name, config.environment)
            live = LiveAdapter(adapter, self.hub, self.tick_ms, self.frame_every, self._stop, self._frame_dir)
            # The arm's own store, next to the live episode store (by default the same place as
            # play's: artifacts/graphs/arm-<ARM>/<adapter>/), unless memory.graph_checkpoint is set.
            graph, graph_path, learn = open_graph(
                config, arm, adapter, self.adapter_name,
                config.memory.graph_checkpoint
                or self.store_path.parent / "graphs" / f"arm-{arm}" / f"{self.adapter_name}.json")
            def on_event(kind: str, data: dict[str, Any]) -> None:
                if kind == "deciding":
                    live.capture(data["frame"])  # the frame the models decide on
                self.hub.publish(kind, data)

            result, recorder = run_trial(config, arm, models, live, self.adapter_name, scenario, seed, store,
                                         run_id, "live", graph, learn, on_event=on_event)
            summary = episode_summary(result, mode=models.mode, run_id=run_id, episode_key=recorder.episode_key,
                                      store=store.path, arm=arm, models=models, graph=graph, graph_path=graph_path,
                                      learn=learn)
            self.hub.publish("summary", summary)
        except KeyboardInterrupt as exc:  # Stop (or Ctrl-C): the recorder already finished the episode
            recorder = getattr(exc, "episode_recorder", None)
            self.hub.publish("episode", {"status": "stopped", "run_id": run_id})
        except Exception as exc:
            log.exception("live run %s failed", run_id)
            recorder = getattr(exc, "episode_recorder", recorder)
            self.hub.publish("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            if learn and recorder is not None:
                graph.add_lineage(run_id, recorder.episode_key, arm, scenario)
                save_store(graph, graph_path)
            if adapter is not None:
                adapter.close()
            store.close()
            models.close()
            self.hub.publish("idle", {"run_id": run_id, "store": str(self.store_path),
                                      "episode_key": recorder and recorder.episode_key})


def make_handler(server: LiveServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # quiet: one line per frame otherwise
            log.debug("%s " + fmt, self.address_string(), *args)

        def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode()
            self._headers(status, "application/json", len(body))
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # CORS preflight for POST with a JSON body
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/status":
                self._json(200, server.status())
            elif path == "/frame":
                frame = server.hub.frame
                if frame is None:
                    self._headers(204, "image/bmp", 0)
                else:
                    self._headers(200, "image/bmp", len(frame))
                    self.wfile.write(frame)
            elif path == "/events":
                self._events()
            else:
                self._json(404, {"error": f"no route {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "body is not JSON"})
                return
            if path == "/start":
                try:
                    self._json(200, server.start(body))
                except ConfigError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/stop":
                self._json(200, {"stopping": server.stop()})
            else:
                self._json(404, {"error": f"no route {path}"})

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = int(self.headers.get("Last-Event-ID") or 0)
            try:
                while True:
                    batch = server.hub.events_after(last, timeout=15)
                    if not batch:
                        self.wfile.write(b": keepalive\n\n")
                    for e in batch:
                        data = json.dumps(e["data"], default=str)
                        self.wfile.write(f"id: {e['seq']}\nevent: {e['type']}\ndata: {data}\n\n".encode())
                        last = e["seq"]
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    return Handler


def serve(server: LiveServer, host: str = "127.0.0.1", port: int = 8765,
          ready=lambda address: None) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    httpd.daemon_threads = True
    ready(httpd.server_address)
    try:
        httpd.serve_forever()
    finally:
        server.close()
        httpd.server_close()
