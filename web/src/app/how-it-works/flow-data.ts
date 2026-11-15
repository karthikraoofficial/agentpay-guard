// The nodes and wires of the "How AgentPay Guard works" diagram, in plain English.
// Coordinates are the centre of each node on a 1360 x 980 canvas.

export type Kind = "start" | "request" | "rules" | "ai" | "data" | "background" | "people" | "provider" | "stop";

export type FlowNode = {
  x: number;
  y: number;
  kind: Kind;
  name: string;
  short: string;
  about: string;
  code: string;
};

export type FlowEdge = {
  from: string;
  to: string;
  label?: string;
  waits?: boolean; // drawn dashed: waits on a timer or a person
  via?: number; // route around the other nodes at this height
  out?: number; // which output of the Answer node (-16, 0, 16)
  nudge?: [number, number];
};

export const KINDS: Record<Kind, string> = {
  start: "Starts the flow",
  request: "Request to the service",
  rules: "Fixed rules (plain code)",
  ai: "AI model",
  data: "Saved data",
  background: "Background job",
  people: "Person or business",
  provider: "Payment provider",
  stop: "Final answer",
};

export const CANVAS = { width: 1360, height: 980 };

export const LANES: { y: number; height: number; title: string }[] = [
  { y: 20, height: 430, title: "1 · Deciding on a payment, in under 150 ms" },
  { y: 470, height: 270, title: "2 · Asking the person, in the background" },
  { y: 760, height: 200, title: "3 · Handling a dispute" },
];

export const RULES_BOX = { x: 492, y: 60, width: 404, height: 300, title: "The decision · fixed rules, no AI inside" };

export const NODES: Record<string, FlowNode> = {
  agent: {
    x: 90, y: 200, kind: "start", name: "AI agent", short: "wants to pay",
    about: "Software acting for a person, such as a shopping assistant. It can ask for permission to pay, but it can never approve its own request.",
    code: "api/routes/decisions.py",
  },
  intents: {
    x: 240, y: 110, kind: "request", name: "Cart check", short: "before checkout",
    about: "Before paying, the agent sends what the person asked for and what is in its cart. It gets back a reference to quote when it asks to pay.",
    code: "api/routes/intents.py",
  },
  decisions: {
    x: 240, y: 270, kind: "request", name: "Payment request", short: "can this go ahead?",
    about: "The agent asks whether it may pay. Sending the same request twice is safe, because it is only handled once.",
    code: "api/routes/decisions.py",
  },
  intentcheck: {
    x: 390, y: 110, kind: "ai", name: "AI compares cart", short: "Claude Haiku · 2.5 s limit",
    about: "A small AI model checks whether the cart matches what the person asked for. If it is slow, unsure or fails, the answer counts as unknown, and then the person is asked. The AI never makes the payment decision itself.",
    code: "core/signals/intent_check.py",
  },
  mandate: {
    x: 390, y: 270, kind: "data", name: "Spending rules", short: "set by the person",
    about: "The limits the person gave this agent: how much, how often and which kinds of shop. It also holds how much has been spent this month.",
    code: "core/domain/mandates.py",
  },
  signals: {
    x: 540, y: 270, kind: "rules", name: "Warning signs", short: "4 quick checks",
    about: "Four checks: too many payments in a short time, a shop the person has never used, a cart that doesn't match the request, and a price higher than quoted. A warning sign means the person is asked first. It can never block a payment on its own.",
    code: "core/domain/signals.py",
  },
  pack: {
    x: 540, y: 110, kind: "data", name: "Rulebook", short: "versioned and locked",
    about: "The written rules every decision follows. A released version never changes. A change means a new version with its own fingerprint.",
    code: "policy_packs/",
  },
  engine: {
    x: 690, y: 270, kind: "rules", name: "Decision maker", short: "fixed rules, no AI",
    about: "Plain code that applies the rulebook to the request, the spending rules and the warning signs. The same inputs always give the same answer, in under 5 milliseconds.",
    code: "core/policy/engine.py",
  },
  audit: {
    x: 840, y: 110, kind: "data", name: "Permanent record", short: "can't be edited",
    about: "Every decision is written down with everything it was based on. Records can be added but never changed, so any decision can be re-run later and shown to come out the same.",
    code: "core/domain/audit.py",
  },
  answer: {
    x: 840, y: 270, kind: "rules", name: "Answer", short: "yes · ask · no",
    about: "One of three answers: allow, ask the person first, or block. A person can turn an 'ask' into a yes, but nothing can undo a block.",
    code: "core/domain/decisions.py",
  },
  rail: {
    x: 1000, y: 150, kind: "provider", name: "Take payment", short: "test payments only",
    about: "If allowed, the money is collected through the payment provider: a built-in stand-in, or Razorpay in test mode. Card numbers are never stored.",
    code: "adapters/razorpay/rail.py",
  },
  question: {
    x: 1000, y: 270, kind: "data", name: "Question for person", short: "waiting for a reply",
    about: "If the answer is 'ask first', a question for the person is saved. A follow-up job is saved at the same moment.",
    code: "core/domain/consent.py",
  },
  block: {
    x: 1000, y: 372, kind: "stop", name: "Blocked", short: "with the reasons",
    about: "The agent is told no, with codes that say why. This answer is final.",
    code: "core/policy/engine.py",
  },
  paid: {
    x: 1160, y: 150, kind: "data", name: "Payment done", short: "or failed",
    about: "The result is saved, and the business that runs the agent is told.",
    code: "core/charges.py",
  },
  queue: {
    x: 90, y: 580, kind: "data", name: "Job queue", short: "saved with the change",
    about: "Follow-up jobs are saved together with the change that caused them. So a job exists only if the change really happened.",
    code: "core/relay.py",
  },
  worker: {
    x: 240, y: 580, kind: "background", name: "Background worker", short: "picks up jobs",
    about: "A separate program that takes jobs off the queue and starts them, so the payment request never has to wait for them.",
    code: "workflows/worker.py",
  },
  wait: {
    x: 390, y: 540, kind: "background", name: "Wait for answer", short: "up to 10 minutes",
    about: "Lets the person know, then waits for a reply or until time runs out. It keeps waiting even if the server restarts.",
    code: "workflows/consent.py",
  },
  notify: {
    x: 390, y: 660, kind: "background", name: "Notify business", short: "signed and retried",
    about: "Sends updates to the business that runs the agent. Each update is signed so the business can tell it's genuine, and it is sent again if the business doesn't respond.",
    code: "adapters/webhooks/sender.py",
  },
  business: {
    x: 540, y: 660, kind: "people", name: "The business", short: "e.g. a shopping app",
    about: "The company that runs the agent and uses AgentPay Guard. Updates carry IDs and statuses only, never the person's private details.",
    code: "api/routes/webhook_endpoint.py",
  },
  person: {
    x: 540, y: 540, kind: "people", name: "Person answers", short: "yes or no",
    about: "The person replies through a secure link, or inside the business's own app.",
    code: "web/src/app/consent",
  },
  recheck: {
    x: 690, y: 540, kind: "rules", name: "Check rules again", short: "at the moment of yes",
    about: "A yes doesn't skip the rules. The decision maker runs again with the latest limits, so a person can't approve past their own budget.",
    code: "core/policy/engine.py",
  },
  saved: {
    x: 840, y: 540, kind: "data", name: "Answer saved", short: "yes · no · timed out",
    about: "The outcome goes into the permanent record. If nobody answers in time, it is saved as timed out.",
    code: "core/domain/consent.py",
  },
  agentpays: {
    x: 1000, y: 540, kind: "request", name: "Agent pays or stops", short: "pays only on yes",
    about: "The agent checks the answer and pays only if the person said yes.",
    code: "api/routes/decisions.py",
  },
  dispute: {
    x: 90, y: 860, kind: "start", name: "Payment disputed", short: "'this was wrong'",
    about: "Later, the person may tell their bank that a payment was wrong.",
    code: "api/routes/disputes.py",
  },
  build: {
    x: 240, y: 860, kind: "background", name: "Build the case", short: "in the background",
    about: "A background job puts the evidence together and keeps track of the deadline to respond.",
    code: "workflows/dispute.py",
  },
  facts: {
    x: 390, y: 860, kind: "rules", name: "Collect the facts", short: "from the records",
    about: "Pulls the decision record, the person's answer and the payment details, and rates how strong the evidence is.",
    code: "core/evidence.py",
  },
  writer: {
    x: 540, y: 860, kind: "ai", name: "AI writes summary", short: "from the facts only",
    about: "An AI model writes a readable explanation using only the collected facts.",
    code: "agents/dispute_narrative.py",
  },
  factcheck: {
    x: 690, y: 860, kind: "rules", name: "Fact check", short: "every claim needs a source",
    about: "Each statement in the summary must point to a real fact. If one doesn't, the AI text is thrown away and plain code writes the summary instead.",
    code: "core/narrative_check.py",
  },
  evidence: {
    x: 840, y: 860, kind: "data", name: "Evidence pack", short: "numbered and sealed",
    about: "Each version of the case gets a number and a fingerprint, so any later change would show.",
    code: "core/domain/evidence.py",
  },
  signoff: {
    x: 1000, y: 860, kind: "people", name: "Human sign-off", short: "review and approve",
    about: "Someone at the business reads the pack, asks for a redo if needed, and signs it off before the deadline.",
    code: "web/src/app/dispute",
  },
  seal: {
    x: 1160, y: 860, kind: "rules", name: "Check the seal", short: "matches the sign-off",
    about: "Anyone can confirm later that the signed-off pack hasn't changed since.",
    code: "core/pack_verify.py",
  },
};

export const EDGES: FlowEdge[] = [
  { from: "agent", to: "intents", label: "request + cart", nudge: [-10, -8] },
  { from: "agent", to: "decisions", label: "amount", nudge: [0, 8] },
  { from: "intents", to: "intentcheck" },
  { from: "decisions", to: "mandate", label: "look up" },
  { from: "intentcheck", to: "signals", label: "match / no match / unknown", nudge: [-8, -6] },
  { from: "mandate", to: "signals" },
  { from: "pack", to: "engine", label: "rules" },
  { from: "signals", to: "engine", label: "warnings" },
  { from: "engine", to: "answer" },
  { from: "engine", to: "audit", label: "written down" },
  { from: "answer", to: "rail", label: "allow", out: -16 },
  { from: "answer", to: "question", label: "ask first", out: 0 },
  { from: "answer", to: "block", label: "block", out: 16 },
  { from: "rail", to: "paid", label: "collected" },
  { from: "question", to: "queue", label: "add a follow-up job", via: 460 },
  { from: "paid", to: "queue", via: 460 },
  { from: "queue", to: "worker", label: "pick up" },
  { from: "worker", to: "wait", label: "start" },
  { from: "worker", to: "notify", label: "send" },
  { from: "notify", to: "business", label: "signed update" },
  { from: "wait", to: "person", label: "let them know", waits: true },
  { from: "person", to: "recheck", label: "yes" },
  { from: "recheck", to: "saved" },
  { from: "wait", to: "saved", label: "no reply in 10 min", waits: true, via: 494 },
  { from: "saved", to: "agentpays", label: "result" },
  { from: "dispute", to: "build" },
  { from: "build", to: "facts" },
  { from: "facts", to: "writer", label: "facts" },
  { from: "writer", to: "factcheck", label: "draft" },
  { from: "factcheck", to: "evidence", label: "checked" },
  { from: "evidence", to: "signoff" },
  { from: "signoff", to: "seal", label: "signed" },
];
