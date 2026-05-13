import { NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { loadStatus, saveStatus } from "@/lib/data";
import { PYTHON_BIN, ROOT_DIR } from "@/lib/env";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(loadStatus(), { headers: { "Cache-Control": "no-store" } });
}

export async function POST() {
  const status = loadStatus();
  if (status.fetch_state !== "running") {
    saveStatus({
      ...status,
      fetch_state: "running",
      current_run_started_at: new Date().toISOString().slice(0, 19).replace("T", " ")
    });
    const child = spawn(PYTHON_BIN, ["scripts/web_bridge.py", "fetch-now-bg"], {
      cwd: ROOT_DIR,
      detached: true,
      stdio: "ignore"
    });
    child.unref();
  }
  return NextResponse.json(loadStatus(), { headers: { "Cache-Control": "no-store" } });
}
