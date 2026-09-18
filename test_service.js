"use strict";

const { spawnSync } = require("node:child_process");

function run(args) {
  return spawnSync("python3", args, { stdio: "inherit" });
}

// 1) 自动发现全部 test_*.py；2) 保留并运行既有健康检查契约 service_contract.py。
const commands = [
  ["-m", "unittest", "discover", "-p", "test_*.py", "-v"],
  ["-m", "unittest", "-v", "service_contract"],
];

for (const args of commands) {
  const result = run(args);
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}
