"use client";
import { useEffect, useRef, useState } from "react";
import { LIVE_URL, type LiveState } from "@/lib/live";

/** The real game frame (320x200, HUD included), fetched from the live server about 20 times a
 * second while a run is going. */
export function GameView({ live }: { live: LiveState }) {
  const [src, setSrc] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);
  const running = live.running;

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const res = await fetch(`${LIVE_URL}/frame`, { cache: "no-store" });
        if (res.status === 200) {
          const url = URL.createObjectURL(await res.blob());
          if (urlRef.current) URL.revokeObjectURL(urlRef.current);
          urlRef.current = url;
          setSrc(url);
        }
      } catch {
        // server not up yet: keep the last frame
      }
      if (!stop && running) timer = setTimeout(tick, 45);
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [running]);

  useEffect(() => () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
  }, []);

  const thinking = live.thinking;
  const who = live.run?.arm_config.tactical === "jev" ? "Jev" : "LLM";
  return (
    <figure className="panel game-view">
      <figcaption>
        Game{live.run ? <span className="muted"> · {live.run.scenario}</span> : null}
        {live.now ? (
          <span className="muted small">
            {" "}
            · frame {live.now.frame} · score {live.now.score ?? "—"} · lives {live.now.lives ?? "—"} · deaths {live.deaths}
          </span>
        ) : null}
      </figcaption>
      <div className="screen">
        {src ? (
          // eslint-disable-next-line @next/next/no-img-element -- a live BMP blob, not a static asset
          <img src={src} alt="Dangerous Dave, live" width={960} height={600} />
        ) : (
          <div className="screen-empty muted">Choose a level and press Start.</div>
        )}
        {thinking ? (
          <div className="thinking">
            {who} is choosing among {thinking.options} skills…{" "}
            <span className="muted">{live.run?.pause === false ? "(game running, Dave stands still)" : "(game paused)"}</span>
          </div>
        ) : null}
        {live.finished ? <div className="thinking done">{live.finished.outcome}</div> : null}
      </div>
    </figure>
  );
}
