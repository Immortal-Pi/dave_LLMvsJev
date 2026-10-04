# Dave Inspector (frontend)

A local, read-only viewer for the inspection bundles written by `dave-agent inspect`. It shows what the LLM and Jev saw at each decision of a recorded episode. See `../docs/inspector.md`.

```bash
# from the repository root: build a bundle (free; --ask is paid)
UV_PROJECT_ENVIRONMENT=jev uv run dave-agent inspect --store artifacts/benchmark-dave.sqlite --run-id demo-C-L2-1

# then, in frontend/
npm install
npm run dev          # http://localhost:3000
```

`INSPECT_DIR` overrides the bundle directory (default `../artifacts/inspect`). The app never calls a model and never writes files. `npm run lint` and `npm run build` check it.
