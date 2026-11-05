// Copies the committed Eval report (../evals/report.json) into src/data/eval-report.json
// for the Ops console, with the release or commit it came from. Runs before dev, lint,
// typecheck and build. When the report isn't reachable (a checkout of web/ alone), it
// copies the fixture instead and the page says it is sample data.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const committed = join(web, "..", "evals", "report.json");
const fixture = join(web, "src", "data", "eval-report.fixture.json");
const out = join(web, "src", "data", "eval-report.json");

function release() {
  try {
    return execFileSync("git", ["describe", "--tags", "--always"], { cwd: web, encoding: "utf8" }).trim();
  } catch {
    const sha = process.env.VERCEL_GIT_COMMIT_SHA ?? process.env.GITHUB_SHA;
    return sha ? sha.slice(0, 7) : null;
  }
}

const sample = !existsSync(committed);
const report = JSON.parse(readFileSync(sample ? fixture : committed, "utf8"));
const source = { sample, release: sample ? null : release() };
writeFileSync(out, JSON.stringify({ source, report }, null, 2) + "\n");
console.log(`Ops console: ${sample ? "fixture (sample data)" : `evals/report.json at ${source.release}`}`);
