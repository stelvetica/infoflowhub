import path from "node:path";

export const ROOT_DIR = process.cwd();
export const PYTHON_BIN = process.env.INFOFLOW_PYTHON || path.join(ROOT_DIR, "..", "anaconda3", "python.exe");
