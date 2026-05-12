import { spawn } from "node:child_process";
import path from "node:path";
import { NextResponse } from "next/server";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const returnTo = url.searchParams.get("returnTo") || "/?view=settings";
  const scriptPath = path.join(process.cwd(), "scripts", "restart_infoflow_dev.ps1");

  spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", scriptPath], {
    cwd: process.cwd(),
    detached: true,
    stdio: "ignore",
    windowsHide: true
  }).unref();

  return NextResponse.redirect(new URL(returnTo, url));
}
