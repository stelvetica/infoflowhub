"use client";

import { useEffect, useRef, useState } from "react";

type EntriesSplitShellProps = {
  left: React.ReactNode;
  right: React.ReactNode;
  initialCollapsed?: boolean;
};

const COLLAPSED_STORAGE_KEY = "infoflowhub:laterhub-collapsed";
const WIDTH_STORAGE_KEY = "infoflowhub:laterhub-width";
const DEFAULT_WIDTH = 36;
const MIN_WIDTH = 22;
const MAX_WIDTH = 46;

export function EntriesSplitShell({ left, right, initialCollapsed = false }: EntriesSplitShellProps) {
  const [collapsed, setCollapsed] = useState(initialCollapsed);
  const [rightWidth, setRightWidth] = useState(DEFAULT_WIDTH);
  const [dragging, setDragging] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
      if (raw === null) return;
      setCollapsed(raw === "1");
    } catch {}
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(COLLAPSED_STORAGE_KEY, collapsed ? "1" : "0");
    } catch {}
  }, [collapsed]);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(WIDTH_STORAGE_KEY);
      if (!raw) return;
      const parsed = Number.parseFloat(raw);
      if (Number.isFinite(parsed)) {
        setRightWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, parsed)));
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(WIDTH_STORAGE_KEY, String(rightWidth));
    } catch {}
  }, [rightWidth]);

  useEffect(() => {
    if (!dragging) return;

    function handleMove(event: PointerEvent) {
      const root = rootRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      if (!rect.width) return;
      const nextWidth = ((rect.right - event.clientX) / rect.width) * 100;
      setCollapsed(false);
      setRightWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, nextWidth)));
    }

    function handleUp() {
      setDragging(false);
    }

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
    };
  }, [dragging]);

  return (
    <div ref={rootRef} className={`split-shell ${collapsed ? "laterhub-collapsed" : ""} ${dragging ? "split-shell-dragging" : ""}`} style={{ ["--laterhub-width" as string]: `${rightWidth}%` }}>
      <section className="split-pane split-pane-main">{left}</section>
      <button
        className="split-toggle"
        type="button"
        aria-label={collapsed ? "展开稍后处理" : "收起稍后处理"}
        aria-pressed={collapsed}
        onPointerDown={(event) => {
          if (event.pointerType === "mouse" || event.pointerType === "touch" || event.pointerType === "pen") {
            event.preventDefault();
            setDragging(true);
          }
        }}
        onClick={() => setCollapsed((current) => !current)}
      >
        <span className="split-toggle-line" />
        <span className="split-toggle-knob">{collapsed ? "‹" : "›"}</span>
      </button>
      <aside className="split-pane split-pane-side">{right}</aside>
    </div>
  );
}
