# Experiment Notebook (`test.ipynb`)

`test.ipynb` is the scratchpad for trying out the Jev APIs (see [jev-apis.md](jev-apis.md)). It holds:

1. An OpenRouter SDK chat call to `typesafe/jev-router` that describes an image URL.
2. A decisions call with a `choice` question (attack / retreat / hide / heal) for a game-agent state.
3. A decisions call with a `noul` question ("Is the player in serious danger?").

## Caveat: cells depend on execution order

- Cell 1 uses `OpenRouter` and `os` without importing them (`from openrouter import OpenRouter`, `import os`).
- Cell 3 reuses `url` and `headers` defined in cell 2.

Run the import cell first, or add the missing imports, before running any cell on its own. Move stable code into Python modules once it works. Notebook checks don't replace module tests.
