import { spawnSync } from "node:child_process";
import { PYTHON_BIN, ROOT_DIR } from "@/lib/env";

export function runPythonBridge(command: string, payload: Record<string, unknown> = {}) {
  const result = spawnSync(PYTHON_BIN, ["scripts/web_bridge.py", command], {
    cwd: ROOT_DIR,
    input: JSON.stringify(payload),
    encoding: "utf-8",
    maxBuffer: 64 * 1024 * 1024
  });
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `bridge failed: ${command}`);
  }
  return result.stdout ? JSON.parse(result.stdout) : {};
}
