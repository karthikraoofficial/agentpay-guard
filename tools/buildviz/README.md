# buildviz

A small, zero-dependency tool that shows your app's **module-by-module compile pipeline as a branching node graph** (think n8n): each module is a node, dependencies are the wires, and each node shows its status. Click a node to see its command and output, or re-run it.

- Needs only **Node 18+**. No `npm install`.
- Reusable: copy this folder into any project (e.g. `tools/buildviz/`).
- Independent modules build **in parallel**; if a module fails, only the modules that depend on it are skipped.
- Status is saved to `.buildviz/state.json`, so builds run from a terminal (or by Claude Code) show up in the browser view when you hit **Refresh**.

## 60-second start

```bash
cp -r buildviz tools/buildviz            # into your project
cd your-project
node tools/buildviz/buildviz.js init     # writes buildviz.config.json — edit the modules
node tools/buildviz/buildviz.js          # serves http://localhost:4477
```

In VS Code: `Ctrl/Cmd+Shift+P` → **Simple Browser: Show** → paste `http://localhost:4477`. Drag that tab beside your editor.

Try the demo first: `node buildviz.js --config demo/buildviz.config.json`

## Describing your modules

`buildviz.config.json` (in your project root):

```json
{
  "project": "My web app",
  "modules": [
    { "id": "shared",  "label": "Shared library", "phase": "Phase 1", "cmd": "npm run build --workspace=packages/shared" },
    { "id": "auth",    "label": "Auth service",   "phase": "Phase 2", "cmd": "npm run build --workspace=services/auth", "dependsOn": ["shared"] }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique, 1–64 chars: letters, digits, `_ . : -` |
| `label` | no | Name shown on the node (defaults to `id`) |
| `phase` | no | Groups modules into the phase chips under the header; click a chip to focus that phase |
| `cmd` | no | Shell command that compiles/builds the module. Exit code 0 = done. Empty = a milestone node that completes when its dependencies do |
| `dependsOn` | no | Ids that must be **done** first. Several modules can share a dependency, so the graph can branch and merge |
| `cwd` | no | Working directory, relative to the config file (default `.`) |
| `env` | no | Extra environment variables for this command |
| `timeoutSec` | no | Kill the command after this many seconds |
| `description` | no | Shown when you hover over the node and in the detail panel. Keep it to one short, plain sentence. Without it, hovering shows `cmd` |
| top-level `concurrency` | no | Max parallel builds (default 4) |

The config is re-read whenever it changes, so adding a module while the page is open just works (the graph re-lays out on the next refresh).

## Using the graph

| You want to… | Do this |
|---|---|
| Build everything | **Run all** |
| Rebuild one module | Click its node → **Run this module** (also builds any unfinished modules it depends on) |
| Rebuild a module and everything after it | Click its node → **Run + downstream** |
| See why something failed | Click the red node → **Output** shows the full log |
| Cancel | **Stop** |
| Refresh a snapshot | **Refresh** (turn **Auto-refresh** off if you want a pure snapshot; the page still updates while a build you started from it is running) |
| Navigate | Drag to pan, Ctrl/⌘ + scroll to zoom, **Fit** to reset |

Status is shown with an icon **and** a word on every node (Pending, Queued, Running, Done, Failed, Skipped), not colour alone.

## From the terminal

```bash
node buildviz.js run                     # build all, in dependency order
node buildviz.js run auth                # one module (plus unfinished dependencies)
node buildviz.js run auth --downstream   # and everything that depends on it
node buildviz.js status                  # last known status
```

`run` exits non-zero if anything failed, and writes logs to `.buildviz/logs/<id>.log`.

## Optional: start it automatically in VS Code

`.vscode/tasks.json`:

```json
{
  "version": "2.0.0",
  "tasks": [{
    "label": "buildviz",
    "type": "shell",
    "command": "node tools/buildviz/buildviz.js",
    "isBackground": true,
    "problemMatcher": [],
    "runOptions": { "runOn": "folderOpen" }
  }]
}
```

(VS Code asks once whether to allow automatic tasks.) Add `.buildviz/` to `.gitignore`.

## Using it with Claude Code

Add this to your project's `CLAUDE.md`, so every module Claude Code builds appears in the graph and gets verified:

```md
## Build visualiser (buildviz)
- Every module you create must be registered in `buildviz.config.json` with: `id`, `label`, `phase`,
  `cmd` (the module's compile/build command) and `dependsOn` (ids of modules it needs).
- After finishing a module, run `node tools/buildviz/buildviz.js run <module-id>` and fix any failure
  before starting the next module. Read `.buildviz/logs/<module-id>.log` for the error output.
- Do not edit anything inside `.buildviz/`.
```

## How it works, and what to know

- Status comes from **process exit codes**. If your build command exits 0 despite errors, the node will show Done.
- Two builds cannot run at once; the second is refused with a message.
- The server listens on `127.0.0.1` only, answers only requests addressed to `localhost`/`127.0.0.1`, and only runs commands that are already in your config file (the browser can never send a command). Mutating requests need a custom header that other websites cannot send.
- On Windows, commands run through `cmd.exe`; on macOS/Linux through `sh`.
- If you view it through a forwarded host (Codespaces, remote dev), start it with `--allow-host your-forwarded-hostname`.
- `Stop` ends the whole process tree of running modules; modules that were queued or stopped go back to Pending.
