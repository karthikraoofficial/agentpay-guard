// Copies the Scenario catalogue (../demo/scenarios/*.json) into src/data/scenarios.json for
// the /demo page, after checking every file against the committed Scenario schema
// (../demo/scenario.schema.json, generated from demo/scenario.py). Runs before dev, lint,
// typecheck and build, so a malformed Scenario file fails the web build as well as CI.
// When the catalogue isn't reachable (a checkout of web/ alone), it writes an empty one
// and the page says so.
import Ajv2020 from "ajv/dist/2020.js";
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const demo = join(web, "..", "demo");
const folder = join(demo, "scenarios");
const out = join(web, "src", "data", "scenarios.json");

if (!existsSync(folder)) {
  writeFileSync(out, JSON.stringify({ scenarios: [] }, null, 2) + "\n");
  console.log("Scenario catalogue: not found, wrote an empty one");
  process.exit(0);
}

// strict: false, because Pydantic's schema carries an OpenAPI-style `discriminator`
// beside each `oneOf`, which Ajv would otherwise refuse as an unknown keyword.
const ajv = new Ajv2020({ allErrors: true, strict: false });
const validate = ajv.compile(JSON.parse(readFileSync(join(demo, "scenario.schema.json"), "utf8")));

const scenarios = [];
const problems = [];
for (const name of readdirSync(folder).filter((n) => n.endsWith(".json")).sort()) {
  let scenario;
  try {
    scenario = JSON.parse(readFileSync(join(folder, name), "utf8"));
  } catch (error) {
    problems.push(`${name}: not JSON (${error.message})`);
    continue;
  }
  if (!validate(scenario)) {
    for (const e of validate.errors ?? []) problems.push(`${name}: ${e.instancePath || "/"} ${e.message}`);
    continue;
  }
  if (`${scenario.id}.json` !== name) {
    problems.push(`${name}: holds Scenario ${scenario.id}; name it ${scenario.id}.json`);
    continue;
  }
  scenarios.push(scenario);
}

if (problems.length > 0) {
  console.error("Scenario catalogue: malformed files\n  " + problems.join("\n  "));
  process.exit(1);
}
writeFileSync(out, JSON.stringify({ scenarios }, null, 2) + "\n");
console.log(`Scenario catalogue: ${scenarios.length} Scenario(s) from demo/scenarios`);
