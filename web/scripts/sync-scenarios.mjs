// Copies the Scenario catalogue (../demo/scenarios/*.json) into src/data/scenarios.json, and
// the playlists (../demo/playlists/*.json) into src/data/playlists.json, for the /demo page
// and the home page, after checking every file against its committed schema
// (../demo/scenario.schema.json and ../demo/playlist.schema.json, generated from
// demo/scenario.py and demo/playlist.py) and each playlist's journeys against the
// catalogue. Runs before dev, lint,
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
const playlistFolder = join(demo, "playlists");
const playlistOut = join(web, "src", "data", "playlists.json");

if (!existsSync(folder)) {
  writeFileSync(out, JSON.stringify({ scenarios: [] }, null, 2) + "\n");
  writeFileSync(playlistOut, JSON.stringify({ playlists: [] }, null, 2) + "\n");
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

// Playlists: each file against its schema, and every journey it names in the catalogue.
const validatePlaylist = ajv.compile(JSON.parse(readFileSync(join(demo, "playlist.schema.json"), "utf8")));
const known = new Set(scenarios.map((s) => s.id));
const playlists = [];
const names = existsSync(playlistFolder)
  ? readdirSync(playlistFolder).filter((n) => n.endsWith(".json")).sort()
  : [];
for (const name of names) {
  let playlist;
  try {
    playlist = JSON.parse(readFileSync(join(playlistFolder, name), "utf8"));
  } catch (error) {
    problems.push(`${name}: not JSON (${error.message})`);
    continue;
  }
  if (!validatePlaylist(playlist)) {
    for (const e of validatePlaylist.errors ?? []) problems.push(`${name}: ${e.instancePath || "/"} ${e.message}`);
    continue;
  }
  if (`${playlist.id}.json` !== name) {
    problems.push(`${name}: holds playlist ${playlist.id}; name it ${playlist.id}.json`);
    continue;
  }
  for (const journey of playlist.journeys) {
    if (!known.has(journey)) problems.push(`${name}: names ${journey}, which has no Scenario file`);
  }
  playlists.push(playlist);
}
if (problems.length > 0) {
  console.error("Playlists: malformed files\n  " + problems.join("\n  "));
  process.exit(1);
}
writeFileSync(playlistOut, JSON.stringify({ playlists }, null, 2) + "\n");
console.log(`Playlists: ${playlists.length} from demo/playlists`);
