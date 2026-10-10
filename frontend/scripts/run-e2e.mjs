import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(scriptDir, "..");
const repositoryRoot = path.resolve(frontendRoot, "..");
const healthUrl = "http://127.0.0.1:8011/api/health";
const pythonCommand = process.env.E2E_PYTHON || "python";
const playwrightCli = path.join(frontendRoot, "node_modules", "@playwright", "test", "cli.js");
const playwrightArgs = process.argv.slice(2);
const lastRunPath = path.join(frontendRoot, "test-results", ".last-run.json");

let server = null;
let tests = null;
let stopping = false;

async function healthy() {
  try {
    const response = await fetch(healthUrl, { signal: AbortSignal.timeout(750) });
    return response.ok;
  } catch {
    return false;
  }
}

// 首次运行要在隔离目录里完整播种 6 个虚构案例（渲染 12 份 PDF、解析、抽表），
// 30 秒不够，冷启动会误报「后端未就绪」。

// 真实参考项目（中邮、北投）的素材是**外部给定**的公开材料，`startup()` 不会生成；
// 缺了它们 `seed_real_reference_projects()` 直接跳过，相关用例会假失败。补齐方式与
// `tests/conftest.py::_copy_materials` 一致：只读正式目录里的 demo_materials。
// 注意：不要复制虚构案例（demo-bond-*）的 PDF——那些由 `startup()` 自己渲染，
// 复制旧渲染产物反而会触发陈旧材料的重建路径。
const REFERENCE_FIXTURE_DIRS = ["real-bond-zhongyou-2026-02", "demo-北投"];

function copyReferenceFixtures(e2eDataDir) {
  const official = path.resolve(repositoryRoot, ".data");
  const target = path.resolve(repositoryRoot, e2eDataDir);
  if (target === official) return;
  // 放在子进程里做：素材缺失、被占用或被运行环境拒绝时降级为软失败，
  // 与 `tests/conftest.py::_copy_materials` 一样不阻断其余用例。
  const script = `
    const fs = require("node:fs"); const path = require("node:path");
    const [official, target] = process.argv.slice(1);
    for (const name of ${JSON.stringify(REFERENCE_FIXTURE_DIRS)}) {
      const from = path.join(official, "demo_materials", name);
      const to = path.join(target, "demo_materials", name);
      if (!fs.existsSync(from) || fs.existsSync(to)) continue;
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.cpSync(from, to, { recursive: true });
      process.stdout.write("已补入真实参考素材: " + name + "\\n");
    }`;
  const result = spawnSync(process.execPath, ["-e", script, official, target], { encoding: "utf8", windowsHide: true });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.error || result.status !== 0) {
    process.stderr.write("[e2e] 未补入真实参考素材，真实参考项目相关用例会因缺少素材而失败\n");
  }
}

async function waitForHealth(timeoutMs = 180_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (server && server.exitCode !== null) {
      throw new Error(`E2E backend exited before becoming healthy (code ${server.exitCode})`);
    }
    if (await healthy()) return;
    await delay(250);
  }
  throw new Error(`E2E backend did not become healthy within ${timeoutMs}ms`);
}

function waitForExit(child, timeoutMs) {
  if (!child || child.exitCode !== null) return Promise.resolve(true);
  return Promise.race([
    new Promise((resolve) => child.once("exit", () => resolve(true))),
    delay(timeoutMs).then(() => false),
  ]);
}

async function stopProcessTree(child) {
  if (!child || child.exitCode !== null) return;
  const pid = child.pid;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
  } else {
    child.kill("SIGTERM");
    if (!(await waitForExit(child, 5_000))) child.kill("SIGKILL");
  }
  await waitForExit(child, 5_000);
}

async function stopChildren() {
  if (stopping) return;
  stopping = true;
  await stopProcessTree(tests);
  await stopProcessTree(server);
}

async function waitForTestResult(timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const payload = JSON.parse(readFileSync(lastRunPath, "utf8"));
      if (payload.status === "passed" || payload.status === "failed") return payload;
    } catch {
      // Playwright writes the result atomically near the end of the run.
    }
    await delay(250);
  }
  throw new Error(`Playwright did not produce ${lastRunPath} within ${timeoutMs}ms`);
}

function installSignalHandler(signal, exitCode) {
  process.once(signal, () => {
    void stopChildren().finally(() => process.exit(exitCode));
  });
}

installSignalHandler("SIGINT", 130);
installSignalHandler("SIGTERM", 143);

let testExitCode = 1;
try {
  if (await healthy()) {
    throw new Error("Port 8011 is already serving an application; stop it before running E2E tests");
  }
  // E2E 与 pytest 用同一条纪律：绝不读写正式 .data。启动时会把演示项目重新播种，
  // 直接跑会把真实库的 runs/report_sections 一起清掉。未显式指定时落到独立目录。
  const e2eDataDir = process.env.APP_DATA_DIR || ".data-e2e";
  if (path.resolve(repositoryRoot, e2eDataDir) === path.resolve(repositoryRoot, ".data")) {
    throw new Error("拒绝在正式库 .data 上运行 E2E：请设置 APP_DATA_DIR 指向独立目录");
  }
  copyReferenceFixtures(e2eDataDir);
  process.stdout.write(`[e2e] 测试数据目录: ${e2eDataDir}
`);
  server = spawn(
    pythonCommand,
    ["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8011", "--log-level", "warning"],
    {
      cwd: repositoryRoot,
      stdio: "inherit",
      windowsHide: true,
      env: { ...process.env, APP_DATA_DIR: e2eDataDir },
    },
  );
  server.once("error", (error) => {
    process.stderr.write(`Failed to start E2E backend: ${error.message}\n`);
  });
  await waitForHealth();

  rmSync(lastRunPath, { force: true });
  tests = spawn(process.execPath, [playwrightCli, "test", ...playwrightArgs], {
    cwd: frontendRoot,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    env: { ...process.env, E2E_MANAGED_SERVER: "1" },
  });
  const exitPromise = new Promise((resolve, reject) => {
    tests.once("error", reject);
    tests.once("exit", (code) => resolve(code ?? 1));
  });
  let outputTail = "";
  let resolveSummary;
  const summaryPromise = new Promise((resolve) => {
    resolveSummary = resolve;
  });
  const observeOutput = (chunk, destination) => {
    destination.write(chunk);
    outputTail = `${outputTail}${chunk.toString("utf8")}`.slice(-20_000);
    const plain = outputTail.replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "");
    if (/\b\d+\s+passed\s+\([^)]+\)/.test(plain)) resolveSummary();
  };
  tests.stdout.on("data", (chunk) => observeOutput(chunk, process.stdout));
  tests.stderr.on("data", (chunk) => observeOutput(chunk, process.stderr));
  const first = await Promise.race([
    exitPromise.then((code) => ({ kind: "exit", code })),
    summaryPromise.then(() => ({ kind: "summary" })),
  ]);
  if (first.kind === "exit") {
    testExitCode = first.code;
  } else {
    const result = await waitForTestResult();
    const gracefulExit = await Promise.race([
      exitPromise,
      delay(5_000).then(() => null),
    ]);
    if (gracefulExit === null) {
      process.stderr.write("Playwright finished but did not exit within 5s; terminating its process tree\n");
      await stopProcessTree(tests);
      testExitCode = result.status === "passed" && result.failedTests?.length === 0 ? 0 : 1;
    } else {
      testExitCode = gracefulExit;
    }
  }
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  testExitCode = 1;
} finally {
  await stopChildren();
}

process.exit(testExitCode);
