"""Versioned JSON checkpoints for the learned world graph, plus a YAML inspection export.

Saving is atomic: the new checkpoint is written to ``<path>.tmp`` and fsynced, the current
file is copied to ``<path>.bak``, and only then is the temp file renamed over ``<path>``. An
interrupted save therefore leaves the previous valid checkpoint in place. Loading rejects a
checkpoint whose schema, adapter, build or observation policy differs from the run's.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from dave_agent.memory.graph import WorldGraph

GRAPH_SCHEMA_VERSION = 1


class GraphCheckpointError(ValueError):
    """A graph checkpoint is missing, corrupt or incompatible; the message says what to do."""


def to_checkpoint(graph: WorldGraph) -> dict[str, Any]:
    return {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "adapter": graph.adapter,
        "build_id": graph.build_id,
        "observation_policy": graph.observation_policy,
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
    graph = WorldGraph(data["adapter"], data["build_id"], data["observation_policy"], data["evidence_limit"])
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
                    observation_policy: str | None = None) -> WorldGraph:
    """Load a checkpoint; any expectation given (adapter, build, policy) must match exactly."""
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
    for name, expected in (("adapter", adapter), ("build_id", build_id), ("observation_policy", observation_policy)):
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
