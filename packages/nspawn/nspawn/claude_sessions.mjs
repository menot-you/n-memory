// Read Claude Code sessions through the Agent SDK's own session functions.
// argv: <sdk.mjs> list <limit> <budgetBytes> | info <sessionId> |
//       messages <sessionId> <maxMessages> <maxBytes>
// It writes JSON to stdout. It writes no transcript text to stderr.
import { pathToFileURL } from "node:url";

const [sdkPath, verb, ...rest] = process.argv.slice(2);
const TITLE = 200;
// One stdout line must stay under nSPAWN's 8 MiB line ceiling (pump.py).
const LINE = 4 * 1024 * 1024;

function usage(messages) {
  let model = null;
  const last = new Map();
  for (const item of messages) {
    const message = item.type === "assistant" ? item.message : null;
    if (!message) continue;
    if (typeof message.model === "string") model = message.model;
    if (message.usage) last.set(message.id ?? item.uuid, message.usage);
  }
  if (last.size === 0) return { model, tokens: null };
  const tokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
  for (const u of last.values()) {
    tokens.input += u.input_tokens ?? 0;
    tokens.output += u.output_tokens ?? 0;
    tokens.cacheRead += u.cache_read_input_tokens ?? 0;
    tokens.cacheWrite += u.cache_creation_input_tokens ?? 0;
  }
  tokens.total = tokens.input + tokens.output + tokens.cacheRead + tokens.cacheWrite;
  return { model, tokens };
}

function row(info, counted) {
  return {
    sessionId: info.sessionId,
    cwd: info.cwd,
    title: (info.summary ?? "").slice(0, TITLE),
    updatedAt: info.lastModified,
    model: counted ? counted.model : null,
    tokens: counted ? counted.tokens : null,
  };
}

function tail(messages, maxMessages, maxBytes) {
  const kept = [];
  let bytes = 0;
  for (let at = messages.length - 1; at >= 0 && kept.length < maxMessages; at--) {
    const line = JSON.stringify(messages[at]);
    if (line.length > LINE || bytes + line.length > maxBytes) break;
    kept.push(line);
    bytes += line.length;
  }
  kept.reverse();
  // A tail starts on the owner's prompt, never on a tool result.
  let start = 0;
  while (start < kept.length && !prompt(JSON.parse(kept[start]))) start++;
  return { kept: kept.slice(start), omitted: messages.length - kept.length + start };
}

function prompt(item) {
  if (item.type !== "user") return false;
  const content = item.message?.content;
  return typeof content === "string" ||
    (Array.isArray(content) && !content.some((block) => block?.type === "tool_result"));
}

try {
  const sdk = await import(pathToFileURL(sdkPath).href);
  if (verb === "list") {
    const [limit, budget] = rest.map(Number);
    const listed = (await sdk.listSessions({ limit, includeProgrammatic: false }))
      .filter((info) => typeof info.cwd === "string" && info.cwd);
    let spent = 0;
    const threads = [];
    for (const info of listed) {
      // A session larger than what is left is skipped, not the end: the newest is
      // often a live session that is too large, and the older ones still fit.
      let counted = null;
      if (spent + (info.fileSize ?? 0) <= budget) {
        spent += info.fileSize ?? 0;
        counted = usage(await sdk.getSessionMessages(info.sessionId, { dir: info.cwd }));
      }
      threads.push(row(info, counted));
    }
    process.stdout.write(JSON.stringify({ threads }) + "\n");
  } else if (verb === "info") {
    const info = await sdk.getSessionInfo(rest[0]);
    const thread = info && info.cwd
      ? row(info, null)
      : null;
    process.stdout.write(JSON.stringify({ thread }) + "\n");
  } else if (verb === "messages") {
    const [maxMessages, maxBytes] = rest.slice(1).map(Number);
    const { kept, omitted } = tail(await sdk.getSessionMessages(rest[0]), maxMessages, maxBytes);
    // One message per line, then the count: no line passes the pump's ceiling.
    for (const line of kept) process.stdout.write(line + "\n");
    process.stdout.write(JSON.stringify({ omitted }) + "\n");
  } else {
    process.stderr.write("claude_sessions: unknown verb\n");
    process.exit(2);
  }
} catch {
  process.stderr.write("claude_sessions: the SDK could not read the sessions\n");
  process.exit(1);
}
