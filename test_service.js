"use strict";

const { spawnSync } = require("node:child_process");

const runs = [
  ["-m", "unittest", "-v", "service_contract"],
  ["-m", "unittest", "discover", "-v", "-s", "tests"],
];

for (const args of runs) {
  const result = spawnSync("python3", args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}
process.exit(0);
