#!/usr/bin/env node
// Thin launcher: Whale Agent is a Python package. This finds uv or pipx and hands
// every argument to the real `whale` command. It downloads nothing itself.
"use strict";

const { spawnSync } = require("node:child_process");

const PACKAGE = process.env.WHALE_INSTALL_SOURCE || "whale-agent";
const CURL = "curl -fsSL https://whale-agent.com/install | sh";

function has(cmd) {
  const probe = process.platform === "win32" ? "where" : "which";
  return spawnSync(probe, [cmd], { stdio: "ignore" }).status === 0;
}

function plan(args, exists = has) {
  if (exists("uvx")) return ["uvx", ["--from", PACKAGE, "whale", ...args]];
  if (exists("uv")) return ["uv", ["tool", "run", "--from", PACKAGE, "whale", ...args]];
  if (exists("pipx")) return ["pipx", ["run", "--spec", PACKAGE, "whale", ...args]];
  return null;
}

function main() {
  const step = plan(process.argv.slice(2));
  if (!step) {
    console.error("Whale Agent needs uv or pipx, and neither is on your PATH.");
    console.error("Install everything in one step with:");
    console.error("");
    console.error("  " + CURL);
    return 1;
  }
  const res = spawnSync(step[0], step[1], { stdio: "inherit" });
  if (res.error) {
    console.error(`could not run ${step[0]}: ${res.error.message}`);
    return 1;
  }
  return res.status === null ? 1 : res.status;
}

if (require.main === module) process.exit(main());
module.exports = { plan };
