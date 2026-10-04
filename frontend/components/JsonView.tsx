"use client";

import { useState } from "react";

export interface JsonTab {
  label: string;
  note?: string;
  value: unknown;
}

/** Tabs of exact request payloads. Strings are shown verbatim (system prompts), objects as JSON. */
export function JsonView({ tabs }: { tabs: JsonTab[] }) {
  const [active, setActive] = useState(0);
  const tab = tabs[active];
  const text = typeof tab.value === "string" ? tab.value : JSON.stringify(tab.value, null, 2);
  return (
    <div className="json-view">
      <div className="tabs" role="tablist">
        {tabs.map((t, i) => (
          <button key={t.label} role="tab" aria-selected={i === active} className={i === active ? "tab on" : "tab"}
                  onClick={() => setActive(i)}>
            {t.label}
          </button>
        ))}
        <button className="tab copy" onClick={() => navigator.clipboard?.writeText(text)}>
          Copy
        </button>
      </div>
      {tab.note && <p className="muted">{tab.note}</p>}
      <pre>{text}</pre>
    </div>
  );
}
