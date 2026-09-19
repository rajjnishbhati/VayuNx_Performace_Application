#!/usr/bin/env node
/**
 * vayunx-node run [--endpoint URL] [--service NAME] [--variant LABEL] [--run-id HEX32] [--slow-ms N] -- <command ...>
 *
 * Runs the command with NODE_OPTIONS=--require <this package>/register, so every Node.js process it
 * starts (including `next start`, `npm run ...` children) is profiled without code changes.
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

const FLAGS: Record<string, string> = {
  "--endpoint": "VAYUNX_ENDPOINT", "--service": "VAYUNX_SERVICE", "--variant": "VAYUNX_VARIANT",
  "--run-id": "VAYUNX_RUN_ID", "--slow-ms": "VAYUNX_SLOW_MS", "--phase": "VAYUNX_PHASE",
};

function usage(code: number): never {
  process.stderr.write("usage: vayunx-node run [--endpoint URL] [--service NAME] [--variant LABEL] [--run-id HEX32] "
    + "[--slow-ms N] [--phase baseline|remediated] -- <command ...>\n");
  process.exit(code);
}

export function childEnv(flags: Record<string, string>, base: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const register = path.join(__dirname, "register.js");
  const opt = `--require "${register.replace(/\\/g, "/")}"`;
  const env = { ...base, ...flags };
  env.NODE_OPTIONS = base.NODE_OPTIONS?.includes(opt) ? base.NODE_OPTIONS : [base.NODE_OPTIONS, opt].filter(Boolean).join(" ");
  return env;
}

/** Windows: npm/next/npx are .cmd shims, which only cmd.exe can start; everything else runs without a shell. */
function resolveCommand(command: string[]): [string, string[], boolean] {
  if (process.platform !== "win32") return [command[0], command.slice(1), false];
  const exts = (process.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";").filter(Boolean);
  const dirs = /[\\/]/.test(command[0]) ? [""] : ["", ...(process.env.PATH ?? "").split(path.delimiter)];
  for (const dir of dirs) {
    for (const ext of ["", ...exts]) {
      const candidate = path.join(dir, command[0] + ext);
      if (ext === "" && !path.extname(candidate)) continue;
      try {
        if (!fs.statSync(candidate).isFile()) continue;
      } catch {
        continue;
      }
      if (!/\.(cmd|bat)$/i.test(candidate)) return [candidate, command.slice(1), false];
      const quote = (a: string) => (/[\s"&|<>^]/.test(a) ? `"${a.replace(/"/g, '""')}"` : a);
      return [`"${[candidate, ...command.slice(1)].map(quote).join(" ")}"`, [], true];
    }
  }
  return [command[0], command.slice(1), false];
}

function main(argv: string[]): void {
  if (argv[0] !== "run") usage(2);
  const flags: Record<string, string> = {};
  let i = 1;
  for (; i < argv.length && argv[i] !== "--"; i += 2) {
    const env = FLAGS[argv[i]];
    if (!env || i + 1 >= argv.length) usage(2);
    flags[env] = argv[i + 1];
  }
  const command = argv.slice(i + 1);
  if (command.length === 0) usage(2);
  const [file, args, shell] = resolveCommand(command);
  const child = spawn(file, args, { stdio: "inherit", env: childEnv(flags), shell, windowsVerbatimArguments: shell });
  child.on("error", (e) => { process.stderr.write(`vayunx-node: ${e.message}\n`); process.exit(127); });
  child.on("exit", (code, signal) => process.exit(code ?? (signal ? 128 : 1)));
}

if (require.main === module) main(process.argv.slice(2));
