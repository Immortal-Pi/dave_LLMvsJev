"use client";

import { useState } from "react";
import type { DecisionBundle } from "@/lib/bundle";
import { OptionsTable } from "./OptionsTable";
import { TileGrid } from "./TileGrid";

/** The grid and the options table, linked: hovering an option draws its estimate and real path. */
export function Perspective({ decision, screenshot }: { decision: DecisionBundle; screenshot: string | null }) {
  const [selected, setSelected] = useState<string | null>(null);
  const { request } = decision;
  const showing = selected ?? decision.chosen;
  return (
    <>
      <section className="views">
        <figure className="panel">
          <figcaption>What the models see (the request&apos;s tile grid)</figcaption>
          <TileGrid request={request} outcomes={decision.outcomes} selected={selected} chosen={decision.chosen} />
          <p className="legend">
            <span className="key k-dave" /> Dave at px {request.player.px?.join(", ")}
            <span className="key k-wp" /> waypoint
            <span className="key k-est" /> estimated end of <b className="mono">{showing}</b>
            <span className="key k-path" /> real path
          </p>
        </figure>
        <figure className="panel">
          <figcaption>The game at this moment</figcaption>
          {screenshot ? (
            // A local BMP from the bridge; next/image does not optimise BMP.
            // eslint-disable-next-line @next/next/no-img-element
            <img className="screenshot" src={screenshot} alt={`Game screen at frame ${decision.frame}`} />
          ) : (
            <p className="muted">No screenshot in this bundle.</p>
          )}
        </figure>
      </section>
      <section className="panel">
        <h2>Options offered ({request.candidates.length})</h2>
        <p className="muted">Hover an option to draw its estimated end and its real path on the grid.</p>
        <OptionsTable
          candidates={request.candidates}
          chosen={decision.chosen}
          recorded={decision.recorded}
          asked={decision.asked}
          outcomes={decision.outcomes}
          selected={selected}
          onSelect={setSelected}
        />
      </section>
    </>
  );
}
