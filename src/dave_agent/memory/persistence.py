"""Versioned JSON checkpoints for the learned world graph, plus a YAML inspection export.

A ``GraphStore`` (one graph per level) is a directory with one checkpoint per level,
``<dir>/<level_id>.json``. A store path given as ``X.json`` means the directory ``X/``; a legacy
combined checkpoint at ``X.json`` (one graph for every level) is split by level on load and
written back as a directory, so earlier learning is kept.

Saving is atomic: the new checkpoint is written to ``<path>.tmp`` and fsynced, the current
file is copied to ``<path>.bak``, and only then is the temp file renamed over ``<path>``. An
interrupted save therefore leaves the previous valid checkpoint in place. Loading rejects a
checkpoint whose schema, adapter, build, observation policy or execution mode differs from the
run's. A checkpoint written before the execution mode was recorded is ``paused_step``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from dave_agent.memory.graph import GraphStore, WorldGraph

GRAPH_SCHEMA_VERSION = 1
DEFAULT_EXECUTION_MODE = "paused_step"  # every checkpoint written before the field existed


class GraphCheckpointError(ValueError):
    """A graph checkpoint is missing, corrupt or incompatible; the message says what to do."""


def to_checkpoint(graph: WorldGraph) -> dict[str, Any]:
    return {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "adapter": graph.adapter,
        "build_id": graph.build_id,
        "observation_policy": graph.observation_policy,
        "execution_mode": graph.execution_mode,
        "level_id": graph.level_id,
        "scenarios": sorted(graph.scenarios),
        "lineage": graph.lineage,
        "parent_sha256": graph.parent_sha256,
        "evidence_limit": graph.evidence_limit,
        "topology_version": graph.topology_version,
        "unanchored": graph.unanchored,
        "counts": graph.counts(),
        "aliases": graph.aliases,
        "nodes": [{"id": n, **d} for n, d in graph.g.nodes(data=True)],
        "edges": [{"source": u, "target": v, "key": k, **d} for u, v, k, d in graph.g.edges(keys=True, data=True)],
        "suggestions": graph.suggestions,
    }


def from_checkpoint(data: dict[str, Any]) -> WorldGraph:
    graph = WorldGraph(data["adapter"], data["build_id"], data["observation_policy"], data["evidence_limit"],
                       data.get("level_id"), data.get("execution_mode", DEFAULT_EXECUTION_MODE))
    graph.scenarios = set(data["scenarios"])
    graph.lineage = list(data["lineage"])
    graph.parent_sha256 = data["parent_sha256"]
    graph.topology_version = data["topology_version"]
    graph.unanchored = data["unanchored"]
    graph.aliases = dict(data["aliases"])
    graph.suggestions = list(data["suggestions"])
    for node in data["nodes"]:
        attrs = dict(node)
        graph.g.add_node(attrs.pop("id"), **attrs)
    for edge in data["edges"]:
        attrs = dict(edge)
        graph.g.add_edge(attrs.pop("source"), attrs.pop("target"), key=attrs.pop("key"), **attrs)
    return graph


def _dumps(graph: WorldGraph) -> str:
    return json.dumps(to_checkpoint(graph), indent=1, sort_keys=True) + "\n"


def save_checkpoint(graph: WorldGraph, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(_dumps(graph))
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
    return path


def load_checkpoint(path: Path | str, adapter: str | None = None, build_id: str | None = None,
                    observation_policy: str | None = None, execution_mode: str | None = None) -> WorldGraph:
    """Load a checkpoint; any expectation given (adapter, build, policy, execution mode) must
    match exactly."""
    path = Path(path)
    if not path.exists():
        raise GraphCheckpointError(f"graph checkpoint not found: {path}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphCheckpointError(f"{path}: not valid JSON ({exc}); restore {path.name}.bak") from exc
    version = data.get("graph_schema_version")
    if version != GRAPH_SCHEMA_VERSION:
        raise GraphCheckpointError(
            f"{path}: graph schema version {version}, this code reads {GRAPH_SCHEMA_VERSION}; start a new checkpoint"
        )
    data.setdefault("execution_mode", DEFAULT_EXECUTION_MODE)
    for name, expected in (("adapter", adapter), ("build_id", build_id), ("observation_policy", observation_policy),
                           ("execution_mode", execution_mode)):
        if expected is not None and data.get(name) != expected:
            raise GraphCheckpointError(
                f"{path}: checkpoint {name} is {data.get(name)!r} but this run uses {expected!r}; "
                f"learned routes do not transfer, so use a separate checkpoint (--graph PATH)"
            )
    graph = from_checkpoint(data)
    graph.parent_sha256 = hashlib.sha256(raw).hexdigest()
    return graph


def export_yaml(graph: WorldGraph, path: Path | str) -> Path:
    """Human-readable export for inspection only; JSON remains the checkpoint format."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(to_checkpoint(graph), sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


# -- per-level stores ------------------------------------------------------------------------
def store_dir(path: Path | str) -> Path:
    """The directory of a graph store: ``X.json`` names the directory ``X``."""
    path = Path(path)
    return path.with_suffix("") if path.suffix == ".json" else path


def store_exists(path: Path | str) -> bool:
    path = Path(path)
    return store_dir(path).is_dir() or (path.suffix == ".json" and path.is_file())


def save_store(store: GraphStore, path: Path | str) -> Path:
    """One atomic checkpoint per level in the store's directory; returns the directory."""
    directory = store_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    for level_id, graph in sorted(store.levels.items()):
        save_checkpoint(graph, directory / f"{level_id}.json")
    return directory


def split_levels(graph: WorldGraph) -> GraphStore:
    """A legacy all-levels graph as one graph per level: nodes by their ``level_id``, edges only
    between nodes of the same level, suggestions by level; lineage and counters are copied."""
    store = GraphStore(graph.adapter, graph.build_id, graph.observation_policy, graph.evidence_limit,
                       graph.execution_mode)
    for node, data in graph.g.nodes(data=True):
        store.for_level(data["level_id"]).g.add_node(node, **data)
    for u, v, key, data in graph.g.edges(keys=True, data=True):
        level = graph.g.nodes[u]["level_id"]
        if graph.g.nodes[v]["level_id"] == level:
            store.levels[level].g.add_edge(u, v, key=key, **data)
    for level_id, part in store.levels.items():
        part.aliases = {a: t for a, t in graph.aliases.items() if a.startswith(f"{level_id}:")}
        part.suggestions = [s for s in graph.suggestions if s["level_id"] == level_id]
        part.scenarios, part.lineage = set(graph.scenarios), list(graph.lineage)
        part.parent_sha256, part.topology_version = graph.parent_sha256, graph.topology_version
    return store


def load_store(path: Path | str, adapter: str | None = None, build_id: str | None = None,
               observation_policy: str | None = None, execution_mode: str | None = None) -> GraphStore:
    """Load a store directory, or split a legacy combined checkpoint; expectations as for
    ``load_checkpoint``, checked on every level."""
    path = Path(path)
    directory = store_dir(path)
    if directory.is_dir():
        files = sorted(directory.glob("*.json"))
        if not files:
            raise GraphCheckpointError(f"graph store {directory} holds no level checkpoints")
        first = load_checkpoint(files[0], adapter, build_id, observation_policy, execution_mode)
        store = GraphStore(first.adapter, first.build_id, first.observation_policy, first.evidence_limit,
                           first.execution_mode)
        for file in files:
            graph = first if file == files[0] else load_checkpoint(file, adapter, build_id, observation_policy,
                                                                   execution_mode)
            level_id = graph.level_id or file.stem
            if graph.level_id is None:  # a single-level checkpoint written before levels were split
                graph.level_id = level_id
            store.levels[level_id] = graph
        return store
    if path.suffix == ".json" and path.is_file():
        return split_levels(load_checkpoint(path, adapter, build_id, observation_policy, execution_mode))
    raise GraphCheckpointError(f"graph store not found: {directory} (or a legacy {directory}.json)")


def export_store_yaml(store: GraphStore, path: Path | str) -> Path:
    """Human-readable export of every level, for inspection only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {level: to_checkpoint(graph) for level, graph in sorted(store.levels.items())}
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path
