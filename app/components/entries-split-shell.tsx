"use client";

import { useEffect, useState } from "react";

type EntriesSplitShellProps = {
  left: React.ReactNode;
  right: React.ReactNode;
  initialCollapsed?: boolean;
};

const STORAGE_KEY = "infoflowhub:laterhub-collapsed";

export function EntriesSplitShell({ left, right, initialCollapsed = false }: EntriesSplitShellProps) {
  const [collapsed, setCollapsed] = useState(initialCollapsed);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw === null) return;
      setCollapsed(raw === "1");
    } catch {}
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
    } catch {}
  }, [collapsed]);

  return (
    <div className={`split-shell ${collapsed ? "laterhub-collapsed" : ""}`}>
      <section className="split-pane split-pane-main">{left}</section>
      <button
        className="split-toggle"
        type="button"
        aria-label={collapsed ? "展开稍后处理" : "收起稍后处理"}
        aria-pressed={collapsed}
        onClick={() => setCollapsed((current) => !current)}
      >
        <span className="split-toggle-line" />
        <span className="split-toggle-knob">{collapsed ? "‹" : "›"}</span>
      </button>
      <aside className="split-pane split-pane-side">{right}</aside>
    </div>
  );
}
