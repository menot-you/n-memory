# The Demo Script — nMEMORY in plain English (10–15 min)

How to demo nMEMORY out loud: the opener, the four beats, a pocket glossary for
every term that can start a rabbit hole, and the punchlines. Written to be spoken.

## The 30-second opener

> "Every AI coding agent wakes up with amnesia. Every session, you re-explain the project, re-decide settled questions, re-discover last week's failure. Memory tools exist — but they optimize for remembering more, and a memory that confidently returns a wrong answer is worse than no memory at all. So we built the opposite: nMEMORY — one small binary, one file on your disk, zero network. Its one rule: when it doesn't know, it says so. It never makes something up. That's not a setting. There is literally no code in it that can invent an answer."

## How it works, in four beats

1. **Capture.** The agent saves a fact as a "capsule": the fact, plus its birth certificate — where it came from, an exact pointer (a file and line, a commit, a ticket), and a fingerprint of the source at that moment. No source? Rejected. Not stored with a blank — refused at the door.
2. **Store.** Everything lives in one file on your disk. No server, no account, no cloud, no background process. Delete the file, the memory is gone. Copy it, the memory travels. It opens zero network connections — that's verifiable, not marketing.
3. **Recall.** Ask it something and exactly one of three things happens: it hands you evidence (with source, age, and relevance attached) — or it says "I had matches but they're all disqualified" (outdated, proven false — with the reason counted) — or it says "I don't have that." Three honest outcomes. The fourth — improvising — doesn't exist.
4. **The stamp.** Everything it returns is stamped "this is data, not an instruction." Even if someone poisons the memory with "ignore your instructions and run X", it comes back as a quoted piece of data the agent can look at, never a command the agent obeys. Memory can't hijack the agent.

## Pocket glossary — say the line, move on

**The product words:**

- **Capsule** — "One remembered fact plus its birth certificate: what, where from, fingerprint of the source, how confident, valid since when."
- **Provenance** — "The birth certificate. No origin, no entry — we reject, we never store a blank."
- **Grounded / missing evidence / abstain** — "The only three answers it can give: here's the evidence — everything matched was disqualified, here's why — or: I don't have that."
- **Advisory, not authority** — "Every answer is stamped 'data, not instructions.' The stamp can't be removed — it's baked into the software's type system, so forging it doesn't even compile."
- **Curated graph** — "Facts link to each other — 'this replaces that', 'this was derived from that', 'this disproves that'. Every link was declared by someone with a source. Competitors' graphs are guessed by an AI — even their links can be hallucinations."
- **Fences** — "Disqualification rules at answer time: superseded, proven-false, expired or quarantined facts get benched — and counted, never silently served."
- **Confidence decay** — "Old facts lose ranking power — half their boost every 90 days. Time demotes; it never deletes."
- **Token budget** — "Every answer fits a fixed budget, headline first. Memory never floods the agent's context — about a page, max, unless you ask for more."

**The search words (these are the ones you don't want a rabbit hole on — each line closes the topic):**

- **FTS5** — "SQLite's built-in search engine. Battle-tested, offline, instant. Nothing exotic."
- **BM25 / TF-IDF** — "The ranking math every search engine used before AI — rare words count more, repetition doesn't fool it. Thirty years old and deliberately boring. That's a feature: it never guesses."
- **Embedding** — "Text turned into a point on a map of meaning — 'deploy broke' and 'release failed' land next to each other even with zero words in common."
- **Embedder** — "The machine that draws that map. We deliberately don't ship one — no cloud calls, no hidden AI. The agent can bring its own coordinates if it wants semantic search."
- **Cosine** — "How close two points sit on the meaning map. Close means related. At or below zero means unrelated — and we throw those out; weak similarity is not evidence."
- **RRF fusion** — "How we merge keyword results with meaning results: by position on each list, not by score — because the two scoring scales aren't comparable. Whoever ranks well on both lists wins. One-line formula from a 2009 paper, fully deterministic: same question, same order, forever."
- **vector_k** — "A cap — only the top ten meaning-matches get a seat at the table, so fuzzy matches can't flood the result."
- **Dormant lane, byte-identical** — "The semantic lane ships fully built but asleep. Don't use it? The output is byte-for-byte identical to a binary that never had it. Proof of no regression — not a promise."
- **model2vec** — "A pocket-dictionary version of an AI embedding model: ~30 MB, no GPU, no cloud, same answer every time. It's our future, optional add-on — and it only ships if benchmarks prove we need it."

## If they push — deflect lines

- **On the search math:** "Everything inside is deliberately thirty-year-old, boring search math — the same family Elasticsearch runs on. The innovation isn't how we find. It's what we refuse to return."
- **On "why no AI inside the memory?":** "Every piece of AI inside a memory is a place it can be wrong invisibly. We keep the AI outside, where you can see it. The memory itself never guesses."
- **On scale:** "It's agent memory, not a data lake. One file holds years of engineering decisions — and when it needs to travel, it's a file copy."
- **On benchmarks:** "We're building an honest one — including the metric the whole category structurally can't report: true abstain — how often it correctly refuses. A system that always answers something can't even measure that about itself."

## The 10–15 minute runbook

| Time | Say / do |
|------|----------|
| 0:00–1:30 | The opener above. Land the one rule. |
| 1:30–4:00 | Live: capture a fact with its source. Then try to capture one without a source — let them watch the rejection. ("It refuses at the door — no fact without a birth certificate.") |
| 4:00–7:00 | Live, the money shot: recall something real — evidence with origin and age on screen. Then ask something it doesn't know — and let the abstain sit on screen for three seconds. "Every other tool in this category would have answered something." |
| 7:00–9:30 | The stamp. Poison the memory live with "ignore your instructions", recall it, show it come back as quoted data. "Memory can't hijack the agent." |
| 9:30–11:30 | Zero-everything: one file, no accounts, no sockets ("verifiable — trace it, zero network calls"), copy = backup, delete = gone. |
| 11:30–13:30 | Roadmap, three moves: honest benchmark (with true-abstain as the headline metric), multi-hop over the curated links ("follow the 'replaced-by' chain, never guess"), optional local embedder — gated on benchmark proof. |
| 13:30–15:00 | Punchlines, then invite the question you want: "Ask me why refusing to answer is the feature." |

## Punchlines — pick three, land them slowly

- "Recall has three honest outcomes. The fourth — making something up — isn't forbidden. It's absent."
- "The competition can't even measure the metric we're perfect on: correctly refusing."
- "Their graph is guessed by an AI — even the links can be hallucinations. Ours is declared — every link has an author and a source."
- "The 'not an instruction' stamp isn't a field someone fills in. Forging it doesn't fail — it doesn't compile."
- "One file. Delete it, the memory dies. Copy it, it travels. No account, no server, no socket."

**And the closer:** "We didn't build a smarter memory. We built one that knows when it's dumb — and says so."
