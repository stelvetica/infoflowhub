"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { RuntimeStatus } from "@/lib/types";

type FetchNowPanelProps = {
  initialStatus: RuntimeStatus;
};

function shortTime(value: string): string {
  return value ? value.slice(5, 16) : "-";
}

function stateText(status: RuntimeStatus): string {
  if (status.fetch_state === "running") return "运行中";
  if (status.fetch_state === "error") return "最近一次失败";
  if (status.fetch_state === "success") return "最近一次完成";
  return "空闲";
}

export function FetchNowPanel({ initialStatus }: FetchNowPanelProps) {
  const router = useRouter();
  const [status, setStatus] = useState(initialStatus);
  const [isPending, startTransition] = useTransition();
  const [requesting, setRequesting] = useState(false);

  useEffect(() => {
    setStatus(initialStatus);
  }, [initialStatus]);

  useEffect(() => {
    if (status.fetch_state !== "running") return;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/api/subscriptions-fetch", { cache: "no-store" });
        if (!response.ok) return;
        const next = (await response.json()) as RuntimeStatus;
        setStatus(next);
        startTransition(() => router.refresh());
      } catch {
        return;
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [router, startTransition, status.fetch_state]);

  const actionLabel = useMemo(() => {
    if (requesting) return "提交中...";
    if (status.fetch_state === "running") return "后台抓取中...";
    return "立即抓取";
  }, [requesting, status.fetch_state]);

  async function handleStart() {
    if (requesting || status.fetch_state === "running") return;
    setRequesting(true);
    try {
      const response = await fetch("/api/subscriptions-fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      if (response.ok) {
        const next = (await response.json()) as RuntimeStatus;
        setStatus(next);
        startTransition(() => router.refresh());
      }
    } finally {
      setRequesting(false);
    }
  }

  return (
    <>
      <div className="settings-topbar">
        <div className="metric-strip metric-strip-5">
          <div className="metric-chip">
            <span className="label">运行状态</span>
            <strong>{stateText(status)}</strong>
          </div>
          <div className="metric-chip">
            <span className="label">本次开始</span>
            <strong>{shortTime(status.current_run_started_at)}</strong>
          </div>
          <div className="metric-chip">
            <span className="label">最近完成</span>
            <strong>{shortTime(status.last_run_at)}</strong>
          </div>
          <div className="metric-chip">
            <span className="label">成功源数</span>
            <strong>
              {status.last_success_sources}/{status.last_total_sources}
            </strong>
          </div>
          <div className="metric-chip">
            <span className="label">新增条目</span>
            <strong>{status.last_inserted_entries}</strong>
          </div>
        </div>
        <div className="settings-actions">
          <button className="btn" type="button" onClick={handleStart} disabled={requesting || status.fetch_state === "running"}>
            {actionLabel}
          </button>
          <a className="btn ghost" href="/api/restart-dev?returnTo=%2F%3Fview%3Dsettings">
            强制重启并清缓存
          </a>
        </div>
      </div>
      <div className="settings-error-block">
        <div className="subtle">错误摘要</div>
        <pre className="codebox compact">{status.last_error || "无"}</pre>
      </div>
    </>
  );
}
