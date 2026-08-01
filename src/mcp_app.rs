//! Self-contained MCP App resources for nMEMORY.
//!
//! Three resources, one shell. `ui://nmemory/document` reads a
//! `memory_export`, `ui://nmemory/visual` draws a `memory_visual`
//! projection, and `ui://nmemory/console` is the home surface over
//! `memory_digest`: handoff threads, the work dag, epic roots, the stored
//! memories, and the write verbs that move them.
//!
//! Every resource is a single HTML document with no external dependency and
//! requests no network, storage, or device permission. They are progressive
//! enhancement: the bound tools keep returning their existing text payloads
//! for hosts without MCP Apps support.
//!
//! The shared blocks below are `macro_rules!` rather than consts because
//! `concat!` splices literals only — a macro that expands to a string literal
//! composes, a `const` does not. One source per fact still holds: the token
//! block, the `ui/*` protocol handshake, the capsule detail renderer, and the
//! diagram renderer each exist exactly once.

/// MCP App resource attached to `memory_export`.
pub const DOCUMENT_URI: &str = "ui://nmemory/document";
/// MCP App resource attached to `memory_visual`.
pub const VISUAL_URI: &str = "ui://nmemory/visual";
/// MCP App resource attached to `memory_digest` — the home surface.
pub const CONSOLE_URI: &str = "ui://nmemory/console";
/// MCP Apps HTML resource MIME type.
pub const MIME_TYPE: &str = "text/html;profile=mcp-app";

/// One resource advertised through `resources/list` and served by
/// `resources/read`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AppResource {
    /// Stable `ui://` resource identifier.
    pub uri: &'static str,
    /// Machine-readable resource name.
    pub name: &'static str,
    /// Human-readable title.
    pub title: &'static str,
    /// Host-facing description.
    pub description: &'static str,
    /// Complete, self-contained HTML document.
    pub html: &'static str,
}

/// Document head up to the opening `<style>`.
macro_rules! app_head {
    () => {
        r##"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
"##
    };
}

/// The shared design tokens and every component every app shows: the frame,
/// the topbar, the loading/failure panel, memory cards, the detail pane, and
/// the responsive rules. Tokens are sampled from the canonical nMEMORY light
/// design system; the host may override any `--*` through `ui/initialize`.
macro_rules! app_shell_css {
    () => {
        r##":root {
  color-scheme: light;
  --canvas: #F6F1E8;
  --surface: #FAF6EE;
  --surface-subtle: #F0E9DE;
  --ink: #252422;
  --ink-soft: #4E4A45;
  --ink-muted: #746D65;
  --line: #BEB2A5;
  --line-soft: rgba(190, 178, 165, .62);
  --accent: #D9612F;
  --accent-dark: #A74726;
  --accent-wash: rgba(217, 97, 47, .09);
  --success: #4D7A5A;
  --waiting: #C88B32;
  --danger: #B42318;
  --focus: #D9612F;
  --ui: var(--font-sans, "Comic Neue", Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
  --mono: var(--font-mono, "JetBrains Mono", Menlo, Monaco, Consolas, monospace);
  --fast: 120ms;
  --default: 180ms;
  --ease: cubic-bezier(.22, 1, .36, 1);
}
* { box-sizing: border-box; }
html { min-width: 0; scroll-behavior: smooth; background: var(--canvas); }
body { margin: 0; min-width: 0; background: var(--canvas); color: var(--ink); font: 14px/1.5 var(--ui); overflow-wrap: anywhere; }
button { font: inherit; }
[hidden] { display: none !important; }
.frame { min-width: 0; overflow: clip; border: 1px solid var(--line); border-radius: 12px; background: var(--canvas); }
.topbar { min-height: 52px; display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 0 16px; border-bottom: 1px solid var(--line); background: var(--surface); }
.brand { display: flex; align-items: center; gap: 9px; min-width: 0; font: 800 13px/1 var(--mono); letter-spacing: .02em; }
.brand-mark { position: relative; flex: none; width: 22px; height: 22px; border: 1.5px solid var(--ink-soft); border-radius: 6px 8px 7px 5px; }
.brand-mark::before, .brand-mark::after { content: ""; position: absolute; background: var(--accent); }
.brand-mark::before { width: 7px; height: 7px; left: 6px; top: 3px; border-radius: 50%; }
.brand-mark::after { width: 1.5px; height: 7px; left: 9px; bottom: 3px; border-radius: 1px; }
.brand-context { color: var(--ink-muted); font-size: 10px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; white-space: nowrap; }
.topbar-side { display: flex; align-items: center; gap: 9px; min-width: 0; }
.trust-chip { flex: none; display: inline-flex; align-items: center; gap: 7px; min-height: 26px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-muted); font: 700 9px/1.15 var(--mono); letter-spacing: .07em; text-transform: uppercase; }
.trust-chip::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--success); }
.status-panel { margin: 18px; min-height: 92px; display: flex; align-items: center; gap: 14px; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink-soft); }
.status-dot { flex: none; width: 10px; height: 10px; border-radius: 50%; background: var(--accent); animation: pulse 1.4s ease-in-out infinite; }
.status-copy { display: grid; gap: 2px; }
.status-copy strong { color: var(--ink); font-size: 13px; }
.status-copy span { color: var(--ink-muted); font-size: 12px; }
.failure { border-color: var(--danger); color: var(--danger); }
.failure .status-dot { background: var(--danger); animation: none; }
@keyframes pulse { 50% { opacity: .35; transform: scale(.82); } }
.summary { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 22px; align-items: end; padding: 20px; border-bottom: 1px solid var(--line); }
.eyebrow, .section-kicker, .rail-title, .detail-section-label { margin: 0; color: var(--ink-muted); font: 700 10px/1.2 var(--mono); letter-spacing: .09em; text-transform: uppercase; }
.title { margin: 4px 0 0; font-size: 22px; line-height: 1.24; letter-spacing: -.015em; text-wrap: balance; }
.notice { max-width: 72ch; margin: 8px 0 0; color: var(--ink-soft); font-size: 12.5px; text-wrap: pretty; }
.generated-at { display: block; margin-top: 7px; color: var(--ink-muted); font: 10px/1.35 var(--mono); }
.meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0; margin: 0; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); list-style: none; overflow: hidden; }
.meta li { min-width: 76px; display: grid; gap: 2px; padding: 8px 10px; border-left: 1px solid var(--line-soft); }
.meta li:first-child { border-left: 0; }
.meta strong { font: 750 14px/1 var(--mono); }
.meta span { color: var(--ink-muted); font: 9px/1.2 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
.workspace { min-width: 0; display: grid; grid-template-columns: 176px minmax(0, 1fr); align-items: stretch; }
.workspace.has-detail { grid-template-columns: 176px minmax(300px, 1fr) minmax(340px, 430px); }
.rail { min-width: 0; padding: 14px 10px; border-right: 1px solid var(--line); background: var(--surface); }
.rail-title { padding: 0 8px 8px; }
.outline-list { display: flex; flex-direction: column; gap: 2px; margin: 0; padding: 0; list-style: none; }
.outline a { display: block; min-height: 34px; padding: 8px; border-radius: 7px; color: var(--ink-soft); font: 12px/1.35 var(--mono); text-decoration: none; }
.outline a:hover { background: var(--surface-subtle); color: var(--ink); }
.outline a:focus-visible, .source summary:focus-visible, .detail-close:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.source { margin: 14px 0 0; padding: 12px 8px 0; border-top: 1px solid var(--line-soft); }
.source summary { cursor: pointer; color: var(--ink-muted); font: 700 10px/1.3 var(--mono); list-style: none; }
.source summary::-webkit-details-marker { display: none; }
.source summary::before { content: "+"; display: inline-block; width: 17px; color: var(--accent); }
.source[open] summary::before { content: "\2212"; }
.source pre { max-height: 300px; margin: 9px 0 0; padding: 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--canvas); color: var(--ink-soft); overflow: auto; white-space: pre; font: 10px/1.5 var(--mono); }
.document { min-width: 0; }
.section { scroll-margin-top: 10px; border-bottom: 1px solid var(--line); }
.section:last-child { border-bottom: 0; }
.section-head { min-height: 52px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 11px 16px; border-bottom: 1px solid var(--line-soft); }
.section h2 { margin: 3px 0 0; font-size: 15px; line-height: 1.25; }
.section-count, .group-count { flex: none; padding: 2px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-muted); font: 10px/1.25 var(--mono); }
.group { border-top: 1px solid var(--line-soft); }
.group:first-of-type { border-top: 0; }
.group-head { min-height: 38px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 16px; background: var(--surface); }
.group h3 { margin: 0; color: var(--ink-soft); font: 700 10px/1.2 var(--mono); letter-spacing: .07em; text-transform: uppercase; }
.paragraph { max-width: 72ch; margin: 0; padding: 10px 16px; color: var(--ink-soft); }
.cards { display: flex; flex-direction: column; }
.memory-card { width: 100%; min-width: 0; min-height: 60px; appearance: none; display: grid; grid-template-columns: 36px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 9px 14px; border: 0; border-bottom: 1px solid var(--line-soft); border-radius: 0; background: transparent; color: var(--ink); text-align: left; cursor: pointer; transition: background var(--fast) var(--ease), color var(--fast) var(--ease); }
.memory-card:last-child { border-bottom: 0; }
.memory-card:hover { background: var(--surface); }
.memory-card:active { background: var(--surface-subtle); }
.memory-card:focus-visible { position: relative; z-index: 1; outline: 2px solid var(--focus); outline-offset: -2px; }
.memory-card[aria-expanded="true"] { background: var(--accent-wash); }
.card-glyph { flex: none; width: 34px; height: 34px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink-soft); font: 750 10px/1 var(--mono); letter-spacing: -.02em; }
.memory-card[aria-expanded="true"] .card-glyph { border-color: var(--accent); color: var(--accent); }
.card-copy { min-width: 0; display: grid; gap: 4px; }
.card-title { min-width: 0; display: -webkit-box; overflow: hidden; color: var(--ink); font-size: 13px; font-weight: 700; line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.card-meta { min-width: 0; display: flex; flex-wrap: wrap; gap: 4px 9px; color: var(--ink-muted); font: 10px/1.3 var(--mono); }
.card-meta span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-id { color: var(--accent-dark); font-weight: 700; }
.card-side { min-width: 74px; display: flex; align-items: center; justify-content: flex-end; gap: 9px; }
.tier { padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-muted); font: 700 9px/1.2 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
.tier.is-special { border-color: var(--accent); color: var(--accent-dark); }
.tier.is-ready { border-color: var(--success); color: var(--success); }
.tier.is-waiting { border-color: var(--waiting); color: var(--waiting); }
.card-arrow { color: var(--ink-muted); font: 18px/1 var(--ui); transition: transform var(--fast) var(--ease); }
.memory-card:hover .card-arrow, .memory-card[aria-expanded="true"] .card-arrow { color: var(--accent); transform: translateX(2px); }
.entry-generic { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 11px 16px; border-bottom: 1px solid var(--line-soft); color: var(--ink-soft); }
.entry-generic code { color: var(--accent-dark); font: 700 10px/1.4 var(--mono); }
.edge-list { margin: 0; padding: 0; list-style: none; }
.edge-card { min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 9px 16px; border-bottom: 1px solid var(--line-soft); color: var(--ink-soft); }
.edge-card:last-child { border-bottom: 0; }
.edge-flow { min-width: 0; display: flex; align-items: center; gap: 8px; font: 11px/1.4 var(--mono); }
.edge-node { padding: 3px 6px; border: 1px solid var(--line); border-radius: 5px; background: var(--surface); color: var(--ink); }
.edge-kind { color: var(--accent-dark); font-size: 9px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
.edge-kind::before, .edge-kind::after { content: "\2014"; margin: 0 4px; color: var(--line); }
.edge-time { flex: none; color: var(--ink-muted); font: 10px/1.3 var(--mono); }
.empty { margin: 20px; padding: 28px 18px; border: 1px dashed var(--line); border-radius: 8px; background: var(--surface); color: var(--ink-soft); text-align: center; }
.empty strong { display: block; margin-bottom: 4px; color: var(--ink); }
.detail-pane { min-width: 0; height: min(720px, calc(100vh - 8px)); position: sticky; top: 0; align-self: start; display: flex; flex-direction: column; border-left: 1px solid var(--line); background: var(--surface); overflow: hidden; }
.detail-head { flex: none; min-height: 76px; display: grid; grid-template-columns: minmax(0, 1fr) 32px; gap: 12px; align-items: start; padding: 13px 14px; border-bottom: 1px solid var(--line); }
.detail-label { margin: 0; color: var(--ink-muted); font: 700 9px/1.25 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
.detail-title { margin: 5px 0 0; font-size: 18px; line-height: 1.28; letter-spacing: -.01em; text-wrap: balance; }
.detail-context { margin: 5px 0 0; color: var(--ink-muted); font: 10px/1.35 var(--mono); }
.detail-close { width: 32px; height: 32px; appearance: none; border: 0; border-radius: 6px; background: transparent; color: var(--ink-muted); cursor: pointer; font-size: 20px; line-height: 1; }
.detail-close:hover { background: var(--surface-subtle); color: var(--ink); }
.detail-body { flex: 1; min-height: 0; padding: 14px; overflow: auto; }
.detail-foot { flex: none; display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 14px; border-top: 1px solid var(--line); background: var(--surface-subtle); }
.detail-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.detail-chip { padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-muted); font: 700 9px/1.2 var(--mono); letter-spacing: .05em; text-transform: uppercase; }
.detail-chip.accent { border-color: var(--accent); color: var(--accent-dark); }
.detail-section { margin-top: 17px; }
.detail-section:first-child { margin-top: 0; }
.detail-section-label { margin-bottom: 7px; }
.detail-content, .raw-object { margin: 0; padding: 11px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--canvas); color: var(--ink-soft); white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.55 var(--mono); }
.facts-grid { margin: 0; border-top: 1px solid var(--line-soft); }
.fact { display: grid; grid-template-columns: minmax(92px, .7fr) minmax(0, 1.3fr); gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
.fact dt { color: var(--ink-muted); font: 700 9px/1.35 var(--mono); letter-spacing: .05em; text-transform: uppercase; }
.fact dd { min-width: 0; margin: 0; color: var(--ink-soft); font-size: 12px; overflow-wrap: anywhere; }
.fact dd.mono { font-family: var(--mono); font-size: 10.5px; }
.detail-relations { margin: 0; padding: 0; border-top: 1px solid var(--line-soft); list-style: none; }
.detail-relations li { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 7px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); color: var(--ink-soft); font: 10px/1.4 var(--mono); }
.detail-relations strong { color: var(--accent-dark); font-size: 9px; }
.detail-loading { display: grid; gap: 9px; }
.skeleton { display: block; height: 12px; border-radius: 4px; background: linear-gradient(90deg, var(--surface-subtle), var(--canvas), var(--surface-subtle)); background-size: 220% 100%; animation: shimmer 1.4s linear infinite; }
.skeleton.block { height: 72px; }
@keyframes shimmer { to { background-position: -220% 0; } }
.detail-error { padding: 14px; border: 1px solid var(--danger); border-radius: 8px; color: var(--danger); background: var(--canvas); }
.btn { appearance: none; min-height: 30px; padding: 5px 11px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); color: var(--ink-soft); cursor: pointer; font: 700 10.5px/1.2 var(--mono); letter-spacing: .04em; text-transform: uppercase; }
.btn:hover { border-color: var(--ink-muted); color: var(--ink); }
.btn:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.btn.primary { border-color: var(--accent); background: var(--accent-wash); color: var(--accent-dark); }
.btn.danger { border-color: var(--danger); color: var(--danger); }
.btn[disabled] { opacity: .45; cursor: not-allowed; }
.field { display: grid; gap: 4px; margin-bottom: 11px; }
.field > span { color: var(--ink-muted); font: 700 9px/1.3 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
.field input, .field select, .field textarea { min-height: 32px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--canvas); color: var(--ink); font: 12px/1.45 var(--mono); }
.field textarea { min-height: 96px; resize: vertical; }
.field input:focus-visible, .field select:focus-visible, .field textarea:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.field-note { color: var(--ink-muted); font: 10px/1.4 var(--mono); }
.call-preview { margin: 0 0 11px; padding: 10px 11px; border: 1px solid var(--accent); border-radius: 8px; background: var(--canvas); color: var(--ink-soft); white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.5 var(--mono); }
.form-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.form-error { margin: 0 0 11px; color: var(--danger); font: 11px/1.45 var(--mono); }
.result-ok { padding: 11px 12px; border: 1px solid var(--success); border-radius: 8px; color: var(--ink-soft); background: var(--canvas); white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.5 var(--mono); }
@media (max-width: 1040px) {
  .summary { grid-template-columns: 1fr; }
  .meta { justify-content: flex-start; }
  .workspace.has-detail { grid-template-columns: 150px minmax(260px, 1fr) minmax(310px, 360px); }
}
@media (max-width: 840px) {
  .workspace, .workspace.has-detail { display: block; }
  .rail { padding: 11px 12px; border-right: 0; border-bottom: 1px solid var(--line); }
  .rail-title { padding: 0 3px 7px; }
  .outline-list { flex-direction: row; overflow-x: auto; padding-bottom: 3px; }
  .outline a { min-height: 30px; padding: 6px 8px; white-space: nowrap; }
  .source { margin: 8px 0 0; padding: 9px 3px 0; }
  .detail-pane { position: fixed; inset: 0; z-index: 20; width: auto; height: 100dvh; max-height: none; border: 0; }
  .detail-head { position: sticky; top: 0; z-index: 1; background: var(--surface); }
  body.detail-open { overflow: hidden; }
}
@media (max-width: 560px) {
  .frame { border-left: 0; border-right: 0; border-radius: 0; }
  .topbar { min-height: 48px; padding: 0 12px; }
  .brand-context { display: none; }
  .trust-chip { max-width: 140px; }
  .summary { padding: 16px 14px; }
  .meta { width: 100%; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .meta li { border-left: 0; border-top: 1px solid var(--line-soft); }
  .meta li:nth-child(-n+2) { border-top: 0; }
  .meta li:nth-child(even) { border-left: 1px solid var(--line-soft); }
  .section-head, .group-head { padding-left: 12px; padding-right: 12px; }
  .memory-card { grid-template-columns: 34px minmax(0, 1fr) auto; padding: 9px 11px; }
  .card-side { min-width: 18px; }
  .tier { display: none; }
  .edge-card { align-items: flex-start; flex-direction: column; }
  .edge-flow { flex-wrap: wrap; }
  .fact { grid-template-columns: 88px minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  .status-dot, .skeleton { animation: none; }
  .memory-card, .card-arrow { transition: none; }
}
"##
    };
}

/// Diagram-stage CSS — the drawn projection shared by the console's DAG tab
/// and the visual resource.
macro_rules! app_stage_css {
    () => {
        r##".stage-wrap { min-width: 0; overflow: auto; padding: 16px; background: var(--canvas); }
.stage { position: relative; }
/* An svg is a REPLACED element: inset:0 alone leaves it at its intrinsic
   300x150 and silently CLIPS every edge beyond that box. The explicit size is
   load-bearing, and drawStage also stamps width/height to the stage. */
.stage-edges { position: absolute; left: 0; top: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
.stage-node { position: absolute; width: 210px; min-height: 58px; appearance: none; display: grid; gap: 5px; align-content: center; padding: 8px 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); color: var(--ink); text-align: left; cursor: pointer; font: 12px/1.35 var(--ui); }
.stage-node.is-flat { cursor: default; }
.stage-node:hover { border-color: var(--ink-muted); }
.stage-node:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.stage-node[aria-expanded="true"] { border-width: 2px; border-color: var(--accent); }
.stage-node-id { color: var(--ink-muted); font: 700 10px/1.2 var(--mono); letter-spacing: .04em; }
.stage-node-label { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.stage-edge-label { fill: var(--ink-muted); font: 9px var(--mono); }
.stage-group { position: absolute; border: 1px dashed var(--line); border-radius: 11px; }
.stage-group-name { position: absolute; top: -9px; left: 12px; padding: 0 6px; background: var(--canvas); color: var(--ink-muted); font: 700 9px/1.2 var(--mono); letter-spacing: .07em; text-transform: uppercase; }
.stage-banner { margin: 0 16px 0; padding: 12px 14px; border: 2px solid var(--danger); border-radius: 8px; background: var(--surface); color: var(--danger); font: 11.5px/1.5 var(--mono); }
.stage-legend { display: flex; flex-wrap: wrap; gap: 12px; padding: 10px 16px; border-top: 1px solid var(--line-soft); color: var(--ink-muted); font: 10px/1.3 var(--mono); }
.stage-legend b { display: inline-flex; align-items: center; gap: 5px; font-weight: 700; }
.stage-legend i { width: 9px; height: 9px; border-radius: 3px; border: 1px solid var(--line); }
"##
    };
}

/// End of the style block, opening of the body.
macro_rules! app_body_open {
    () => {
        r##"</style></head><body>
"##
    };
}

/// The loading / failure panel every app boots into.
macro_rules! app_status_html {
    ($waiting:literal) => {
        concat!(
            r##"  <div id="status" class="status-panel" role="status" aria-live="polite">
    <span class="status-dot" aria-hidden="true"></span>
    <span class="status-copy"><strong>Building the memory view</strong><span>Waiting for "##,
            $waiting,
            r##"…</span></span>
  </div>
"##
        )
    };
}

/// The capsule detail pane — one DOM contract for every app.
macro_rules! app_detail_html {
    () => {
        r##"      <aside id="detail-shell" class="detail-pane" role="complementary" aria-labelledby="detail-title" hidden>
        <header class="detail-head">
          <div><p id="detail-label" class="detail-label">Memory</p><h2 id="detail-title" class="detail-title">Stored record</h2><p id="detail-context" class="detail-context"></p></div>
          <button id="detail-close" class="detail-close" type="button" aria-label="Close memory details">×</button>
        </header>
        <div id="detail-body" class="detail-body"></div>
        <div id="detail-foot" class="detail-foot" hidden></div>
      </aside>
"##
    };
}

/// Opening of the app script.
macro_rules! app_script_open {
    () => {
        r##"<script>
(()=>{'use strict';
"##
    };
}

/// The shared runtime: the `ui/*` JSON-RPC handshake over `postMessage`,
/// DOM helpers, the memory-card builder, and the capsule detail renderer.
///
/// Stored bytes reach the DOM only through `textContent`; no app interprets
/// stored content as markup.
macro_rules! app_runtime_js {
    () => {
        r##"let nextId=1,initializeId=0,resizeFrame=0,selectedCard=null,detailToken=0,onToolResult=null,onTeardown=null,onCapsuleOpened=null;
const pending=new Map();
const byId=id=>document.getElementById(id);
const status=byId('status'),app=byId('app'),workspace=byId('workspace');
const detailShell=byId('detail-shell'),detailClose=byId('detail-close'),detailLabel=byId('detail-label'),detailTitle=byId('detail-title'),detailContext=byId('detail-context'),detailBody=byId('detail-body'),detailFoot=byId('detail-foot');
const post=message=>window.parent.postMessage(message,'*');
const notify=(method,params={})=>post({jsonrpc:'2.0',method,params});
const make=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node};
const resize=()=>{if(resizeFrame)cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>notify('ui/notifications/size-changed',{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}))};
const request=(method,params)=>new Promise((resolve,reject)=>{const id=nextId++;const timer=window.setTimeout(()=>{pending.delete(id);reject(new Error('The host did not answer the request.'))},20000);pending.set(id,{resolve,reject,timer});post({jsonrpc:'2.0',id,method,params})});
const pretty=value=>String(value||'unclassified').replaceAll('_',' ').replaceAll('-',' ');
const kindMark=kind=>({decision:'D',task:'T',fact:'F',constraint:'C',procedure:'P',epic:'EP',brainstorm:'B',doc:'DOC',capability:'CAP',failure_pattern:'!',evidence:'E',journal:'J',lifecycle:'L',ready:'>',blocked:'||',done:'OK',unclassified:'M'}[kind]||'M');
const shortDate=value=>{if(!value)return'';const date=new Date(value);if(Number.isNaN(date.getTime()))return value;return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',year:'numeric'}).format(date)};
const clip=(text,max)=>{const value=String(text===undefined||text===null?'':text);return value.length>max?value.slice(0,max-1)+'…':value};
const isCapsuleId=value=>typeof value==='string'&&/^cap-\d+$/.test(value);
const fact=(label,value,mono=false)=>{const item=make('div','fact'),term=make('dt','',label),description=make('dd',mono?'mono':'',value===undefined||value===null||value===''?'—':String(value));item.append(term,description);return item};
const detailSection=label=>{const section=make('section','detail-section');section.append(make('p','detail-section-label',label));return section};
const decodeToolResult=result=>{if(!result)throw new Error('The tool returned no result.');const textItem=Array.isArray(result.content)?result.content.find(item=>item&&item.type==='text'&&typeof item.text==='string'):null;if(result.isError)throw new Error(textItem?textItem.text:'The tool returned an error.');if(result.structuredContent&&typeof result.structuredContent==='object')return result.structuredContent;if(!textItem)throw new Error('The tool returned no readable payload.');try{return JSON.parse(textItem.text)}catch(_error){throw new Error('The tool returned malformed JSON.')}};
const callTool=(name,args)=>request('tools/call',{name,arguments:args}).then(decodeToolResult);
const isNarrowDetail=()=>window.matchMedia('(max-width:840px)').matches;
const syncDetailMode=()=>{detailShell.setAttribute('role',isNarrowDetail()?'dialog':'complementary');if(isNarrowDetail())detailShell.setAttribute('aria-modal','true');else detailShell.removeAttribute('aria-modal')};
const closeDetail=()=>{if(detailShell.hidden)return;detailToken+=1;detailShell.hidden=true;detailFoot.hidden=true;detailFoot.replaceChildren();if(workspace)workspace.classList.remove('has-detail');document.body.classList.remove('detail-open');if(selectedCard){selectedCard.setAttribute('aria-expanded','false');selectedCard.focus({preventScroll:true});selectedCard=null}resize()};
const renderLoading=()=>{detailBody.replaceChildren();detailBody.setAttribute('aria-busy','true');const loading=make('div','detail-loading');loading.append(make('span','skeleton block'),make('span','skeleton'),make('span','skeleton'),make('span','skeleton'));detailBody.append(loading)};
const renderDetail=data=>{detailBody.removeAttribute('aria-busy');detailBody.replaceChildren();if(!data||!data.capsule){const section=detailSection('Stored marker');section.append(make('p','detail-content','This record does not expose live capsule content.'));section.append(make('pre','raw-object',JSON.stringify(data,null,2)));detailBody.append(section);resize();return}const capsule=data.capsule,classification=data.classification||{},provenance=capsule.provenance||{},freshness=capsule.freshness||{},scope=capsule.scope||{};const chips=make('div','detail-chips');chips.append(make('span','detail-chip accent',pretty(classification.kind||'unclassified')),make('span','detail-chip',pretty(data.tier||'active')),make('span','detail-chip',pretty(capsule.authority_class)));if(capsule.instruction_taint)chips.append(make('span','detail-chip','tainted'));if(data.expired)chips.append(make('span','detail-chip','expired'));detailBody.append(chips);const content=detailSection('Full content');content.append(make('p','detail-content',capsule.content));detailBody.append(content);const properties=detailSection('Memory properties'),grid=make('dl','facts-grid');grid.append(fact('Project',scope.project_id),fact('Confidence',typeof capsule.confidence==='number'?Math.round(capsule.confidence*100)+'%':capsule.confidence),fact('Authority',pretty(capsule.authority_class)),fact('Created',data.created_at),fact('Valid from',freshness.valid_from),fact('Valid to',freshness.valid_to||'Open'),fact('Lifecycle',pretty(data.tier||'active')),fact('Sequence',data.seq));properties.append(grid);detailBody.append(properties);const provenanceSection=detailSection('Provenance'),provenanceGrid=make('dl','facts-grid');provenanceGrid.append(fact('Source',provenance.source,true),fact('Anchor',provenance.anchor,true),fact('Source hash',provenance.source_hash,true));provenanceSection.append(provenanceGrid);detailBody.append(provenanceSection);if(classification.reason||classification.scope){const classificationSection=detailSection('Classification'),classificationGrid=make('dl','facts-grid');classificationGrid.append(fact('Kind',pretty(classification.kind)),fact('Scope',pretty(classification.scope)),fact('Reason',classification.reason));classificationSection.append(classificationGrid);detailBody.append(classificationSection)}if(Array.isArray(data.relations)&&data.relations.length){const relationSection=detailSection('Graph edges'),list=make('ul','detail-relations');for(const relation of data.relations){const row=make('li','');row.append(make('span','',relation.from),make('strong','',pretty(relation.kind).toUpperCase()),make('span','',relation.to));list.append(row)}relationSection.append(list);detailBody.append(relationSection)}if(data.epistemics){const epistemicSection=detailSection('Epistemics'),epistemicGrid=make('dl','facts-grid');epistemicGrid.append(fact('Evidence state',pretty(data.epistemics.evidence_state)),fact('Recorded',data.epistemics.at),fact('Proof hint',data.epistemics.proof_hint,true),fact('Stale if',data.epistemics.stale_if));epistemicSection.append(epistemicGrid);detailBody.append(epistemicSection)}if(Array.isArray(data.taint_findings)&&data.taint_findings.length){const taintSection=detailSection('Taint findings'),taintList=make('dl','facts-grid');data.taint_findings.forEach((finding,index)=>taintList.append(fact('Rule '+(index+1),finding,true)));taintSection.append(taintList);detailBody.append(taintSection)}if(data.last_mutation){const auditSection=detailSection('Last mutation'),auditGrid=make('dl','facts-grid');auditGrid.append(fact('Event',pretty(data.last_mutation.event)),fact('Actor',data.last_mutation.actor,true),fact('At',data.last_mutation.at));auditSection.append(auditGrid);detailBody.append(auditSection)}resize()};
const renderDetailError=error=>{detailBody.removeAttribute('aria-busy');detailBody.replaceChildren(make('div','detail-error',error instanceof Error?error.message:'Unable to open this memory.'));resize()};
const openShell=(card,label,title,context)=>{if(selectedCard&&selectedCard!==card)selectedCard.setAttribute('aria-expanded','false');selectedCard=card||null;if(card)card.setAttribute('aria-expanded','true');detailLabel.textContent=label;detailTitle.textContent=title;detailContext.textContent=context;detailShell.hidden=false;detailFoot.hidden=true;detailFoot.replaceChildren();syncDetailMode();if(workspace)workspace.classList.add('has-detail');document.body.classList.add('detail-open');detailBody.scrollTop=0;detailClose.focus({preventScroll:true});resize();return ++detailToken};
const openCapsule=(card,entry)=>{if(!isCapsuleId(entry.id))return;const token=openShell(card,'Memory '+entry.id,entry.headline||entry.id,[entry.project,pretty(entry.kind),entry.source].filter(Boolean).join(' · '));renderLoading();request('tools/call',{name:'memory_get',arguments:{id:entry.id}}).then(decodeToolResult).then(data=>{if(token!==detailToken)return;renderDetail(data);if(typeof onCapsuleOpened==='function')onCapsuleOpened(entry.id,data,token)}).catch(error=>{if(token===detailToken)renderDetailError(error)})};
const memoryCard=(container,entry)=>{const card=make('button','memory-card');card.type='button';card.setAttribute('aria-expanded','false');card.setAttribute('aria-controls','detail-shell');card.setAttribute('aria-label','Open '+entry.id+': '+(entry.headline||entry.id));const glyph=make('span','card-glyph',kindMark(entry.badge||entry.kind));glyph.setAttribute('aria-hidden','true');const copy=make('span','card-copy'),headline=make('span','card-title',entry.headline||entry.id),cardMeta=make('span','card-meta');cardMeta.append(make('span','card-id',entry.id),make('span','',pretty(entry.kind)));if(entry.project)cardMeta.append(make('span','',entry.project));if(entry.source)cardMeta.append(make('span','',entry.source));if(entry.note)cardMeta.append(make('span','',entry.note));if(entry.confidence)cardMeta.append(make('span','',Math.round(Number(entry.confidence)*100)+'% confidence'));copy.append(headline,cardMeta);const side=make('span','card-side');if(entry.tier){const tone=entry.tone==='ready'?' is-ready':entry.tone==='waiting'?' is-waiting':entry.tier==='active'?'':' is-special';side.append(make('span','tier'+tone,pretty(entry.tier)))}const arrow=make('span','card-arrow','›');arrow.setAttribute('aria-hidden','true');side.append(arrow);card.append(glyph,copy,side);card.addEventListener('click',()=>openCapsule(card,entry));container.append(card);return card};
const genericRow=(container,text)=>{const generic=make('div','entry-generic'),match=String(text).match(/^(\S+)\s*(.*)$/);if(match)generic.append(make('code','',match[1]),make('span','',match[2]));else generic.append(make('span','',String(text)));container.append(generic);return generic};
const emptyBlock=(container,strongText,text)=>{const empty=make('div','empty');empty.append(make('strong','',strongText),document.createTextNode(text));container.append(empty);return empty};
const failStatus=message=>{status.className='status-panel failure';status.replaceChildren(make('span','status-dot'),make('span','status-copy',message));status.hidden=false;app.hidden=true;resize()};
const applyHostStyles=result=>{const vars=result&&result.hostContext&&result.hostContext.styles&&result.hostContext.styles.variables;if(vars)for(const [key,value] of Object.entries(vars))if(typeof value==='string'&&key.startsWith('--'))document.documentElement.style.setProperty(key,value)};
const observer=typeof ResizeObserver==='function'?new ResizeObserver(resize):null;
if(observer)observer.observe(document.documentElement);
detailClose.addEventListener('click',closeDetail);
document.addEventListener('keydown',event=>{if(detailShell.hidden)return;if(event.key==='Escape'){event.preventDefault();closeDetail();return}if(event.key==='Tab'&&isNarrowDetail()){const focusable=[...detailShell.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter(node=>!node.disabled&&!node.hidden);if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});
window.addEventListener('resize',()=>{syncDetailMode();resize()});
window.addEventListener('message',event=>{const message=event.data;if(!message||message.jsonrpc!=='2.0')return;if(message.id===initializeId&&message.result){applyHostStyles(message.result);notify('ui/notifications/initialized');resize();return}if(message.id!=null&&pending.has(message.id)){const requestState=pending.get(message.id);pending.delete(message.id);window.clearTimeout(requestState.timer);if(message.error)requestState.reject(new Error(message.error.message||'Host request failed.'));else requestState.resolve(message.result);return}if(message.method==='ui/notifications/tool-result'){if(typeof onToolResult==='function')onToolResult(message.params)}else if(message.method==='ui/resource-teardown'&&message.id!=null){if(observer)observer.disconnect();if(typeof onTeardown==='function')onTeardown();for(const requestState of pending.values()){window.clearTimeout(requestState.timer);requestState.reject(new Error('The app was closed.'))}pending.clear();post({jsonrpc:'2.0',id:message.id,result:{}})}});
const boot=appName=>{initializeId=nextId++;post({jsonrpc:'2.0',id:initializeId,method:'ui/initialize',params:{protocolVersion:'2026-01-26',appInfo:{name:appName,version:'"##
    };
}

/// Close of the boot call — the crate version is spliced between the two
/// halves so the app reports the same version as the server binary.
macro_rules! app_runtime_js_tail {
    () => {
        r##"'},appCapabilities:{availableDisplayModes:['inline']}}})};
"##
    };
}

/// The Mermaid reader and the diagram renderer. `memory_visual` emits a
/// deterministic, documented grammar (quoted node declarations, `-->` edges
/// with an optional `|kind|` label, `subgraph`/`end` groups, `classDef` and
/// `class` styling); this parses exactly that and draws it. Labels arrive
/// entity-encoded by the server's own encoder and are decoded back for
/// display — `#35;` last, because it is encoded first.
macro_rules! app_dag_js {
    () => {
        r##"const NODE_W=210,NODE_H=58,COL_GAP=62,ROW_GAP=18,PAD=14;
const decodeLabel=text=>String(text).replaceAll('#quot;','"').replaceAll('#91;','[').replaceAll('#93;',']').replaceAll('#123;','{').replaceAll('#125;','}').replaceAll('#40;','(').replaceAll('#41;',')').replaceAll('#lt;','<').replaceAll('#gt;','>').replaceAll('#124;','|').replaceAll('#96;','`').replaceAll('#35;','#');
const parseMermaid=text=>{const model={header:'',view:'',counts:'',nodes:new Map(),order:[],edges:[],groups:[],classes:new Map(),banner:''};const lines=String(text).split(/\r?\n/);let group=null;for(let index=0;index<lines.length;index+=1){const line=lines[index].trim();if(!line)continue;if(index===0&&/^(graph|flowchart)\b/.test(line)){model.header=line;continue}if(line.startsWith('%%')){const view=line.match(/view=([a-z]+)/),counts=line.match(/·\s*([a-z]+=[^·]+)·/);if(view)model.view=view[1];if(counts)model.counts=counts[1].trim();continue}if(line==='end'){group=null;continue}const groupMatch=line.match(/^subgraph\s+(\S+)\["(.*)"\]$/);if(groupMatch){group={key:groupMatch[1],label:decodeLabel(groupMatch[2]),members:[]};model.groups.push(group);continue}const classDef=line.match(/^classDef\s+(\S+)\s+(.+);$/);if(classDef){const style={};for(const pair of classDef[2].split(',')){const at=pair.indexOf(':');if(at>0)style[pair.slice(0,at).trim()]=pair.slice(at+1).trim()}model.classes.set(classDef[1],style);continue}const classLine=line.match(/^class\s+(\S+)\s+(\S+);$/);if(classLine){for(const key of classLine[1].split(','))if(model.nodes.has(key))model.nodes.get(key).cls=classLine[2];continue}const labelled=line.match(/^(\w+)\s*-->\|([^|]*)\|\s*(\w+)$/);if(labelled){model.edges.push({from:labelled[1],to:labelled[3],label:decodeLabel(labelled[2])});continue}const plain=line.match(/^(\w+)\s*-->\s*(\w+)$/);if(plain){model.edges.push({from:plain[1],to:plain[2],label:''});continue}const node=line.match(/^(\w+)\["(.*)"\]$/);if(node){const label=decodeLabel(node[2]);if(node[1]==='cycle_banner'){model.banner=label;continue}const idMatch=label.match(/^((?:cap|out)-\d+)(?::\s*(.*))?$/);const entry={key:node[1],label:idMatch&&idMatch[2]?idMatch[2]:label,id:idMatch?idMatch[1]:null,cls:'',group:group?group.key:null};model.nodes.set(node[1],entry);model.order.push(node[1]);if(group)group.members.push(node[1])}}return model};
const depthMap=model=>{const incoming=new Map(),outgoing=new Map();for(const key of model.nodes.keys()){incoming.set(key,0);outgoing.set(key,[])}for(const edge of model.edges){if(!model.nodes.has(edge.from)||!model.nodes.has(edge.to))continue;incoming.set(edge.to,incoming.get(edge.to)+1);outgoing.get(edge.from).push(edge.to)}const depth=new Map();const queue=[];for(const [key,count] of incoming)if(count===0){depth.set(key,0);queue.push(key)}let head=0;while(head<queue.length){const key=queue[head++];for(const next of outgoing.get(key)||[]){depth.set(next,Math.max(depth.get(next)||0,(depth.get(key)||0)+1));incoming.set(next,incoming.get(next)-1);if(incoming.get(next)===0)queue.push(next)}}for(const key of model.nodes.keys())if(!depth.has(key))depth.set(key,0);return depth};
const layout=model=>{const places=new Map();if(model.groups.length){let x=PAD;for(const group of model.groups){let y=PAD+26;for(const key of group.members){places.set(key,{x:x+14,y});y+=NODE_H+ROW_GAP}group.box={x,y:PAD,w:NODE_W+28,h:Math.max(y-PAD, NODE_H+40)};x+=NODE_W+28+COL_GAP}return places}if(!model.edges.length){const perRow=Math.max(1,Math.min(4,model.order.length));model.order.forEach((key,index)=>{places.set(key,{x:PAD+(index%perRow)*(NODE_W+ROW_GAP),y:PAD+Math.floor(index/perRow)*(NODE_H+ROW_GAP)})});return places}const depth=depthMap(model),rows=new Map();for(const key of model.order){const column=depth.get(key)||0,row=rows.get(column)||0;rows.set(column,row+1);places.set(key,{x:PAD+column*(NODE_W+COL_GAP),y:PAD+row*(NODE_H+ROW_GAP)})}return places};
const svgEl=name=>document.createElementNS('http://www.w3.org/2000/svg',name);
const drawStage=(container,model,onNode)=>{container.replaceChildren();if(model.banner)container.append(make('p','stage-banner',model.banner));const wrap=make('div','stage-wrap'),stage=make('div','stage');const places=layout(model);let maxX=0,maxY=0;for(const place of places.values()){maxX=Math.max(maxX,place.x+NODE_W+PAD);maxY=Math.max(maxY,place.y+NODE_H+PAD)}for(const group of model.groups)if(group.box){maxX=Math.max(maxX,group.box.x+group.box.w+PAD);maxY=Math.max(maxY,group.box.y+group.box.h+PAD)}const stageW=Math.max(maxX,320),stageH=Math.max(maxY,140);stage.style.width=stageW+'px';stage.style.height=stageH+'px';const svg=svgEl('svg');svg.setAttribute('class','stage-edges');svg.setAttribute('aria-hidden','true');svg.setAttribute('width',String(stageW));svg.setAttribute('height',String(stageH));svg.setAttribute('viewBox','0 0 '+stageW+' '+stageH);const defs=svgEl('defs'),marker=svgEl('marker');marker.setAttribute('id','stage-arrow');marker.setAttribute('viewBox','0 0 8 8');marker.setAttribute('refX','7');marker.setAttribute('refY','4');marker.setAttribute('markerWidth','7');marker.setAttribute('markerHeight','7');marker.setAttribute('orient','auto-start-reverse');const head=svgEl('path');head.setAttribute('d','M0.5 0.8 L7.2 4 L0.5 7.2');head.setAttribute('fill','none');head.setAttribute('stroke','currentColor');head.setAttribute('stroke-width','1.3');head.setAttribute('stroke-linecap','round');marker.append(head);defs.append(marker);svg.append(defs);svg.style.color='var(--ink-muted)';for(const group of model.groups){if(!group.box)continue;const box=make('div','stage-group');box.style.left=group.box.x+'px';box.style.top=group.box.y+'px';box.style.width=group.box.w+'px';box.style.height=group.box.h+'px';box.append(make('span','stage-group-name',group.label));stage.append(box)}for(const edge of model.edges){const from=places.get(edge.from),to=places.get(edge.to);if(!from||!to)continue;const x1=from.x+NODE_W,y1=from.y+NODE_H/2,x2=to.x-7,y2=to.y+NODE_H/2;const path=svgEl('path');const midX=(x1+x2)/2;path.setAttribute('d','M'+x1+' '+y1+' C'+midX+' '+y1+' '+midX+' '+y2+' '+x2+' '+y2);path.setAttribute('fill','none');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','1.3');path.setAttribute('marker-end','url(#stage-arrow)');if(x2<x1)path.setAttribute('stroke-dasharray','4 3');svg.append(path);if(edge.label){const text=svgEl('text');text.setAttribute('class','stage-edge-label');text.setAttribute('x',String(midX));text.setAttribute('y',String((y1+y2)/2-4));text.setAttribute('text-anchor','middle');text.textContent=edge.label;svg.append(text)}}stage.append(svg);for(const key of model.order){const place=places.get(key);if(!place)continue;const entry=model.nodes.get(key),style=model.classes.get(entry.cls)||{};const node=make('button','stage-node'+(entry.id?'':' is-flat'));node.type='button';node.style.left=place.x+'px';node.style.top=place.y+'px';if(style.fill)node.style.background=style.fill;if(style.stroke)node.style.borderColor=style.stroke;if(entry.id){node.setAttribute('aria-expanded','false');node.setAttribute('aria-controls','detail-shell');node.setAttribute('aria-label','Open '+entry.id);node.append(make('span','stage-node-id',entry.id+(entry.cls?' · '+entry.cls:'')));node.append(make('span','stage-node-label',clip(entry.label===entry.id?'':entry.label,90)));node.addEventListener('click',()=>onNode(node,entry))}else{node.disabled=true;node.append(make('span','stage-node-label',clip(entry.label,140)))}stage.append(node)}wrap.append(stage);container.append(wrap);const legend=make('div','stage-legend');for(const [name,style] of model.classes){const item=make('b','',''),swatch=make('i','');if(style.fill)swatch.style.background=style.fill;if(style.stroke)swatch.style.borderColor=style.stroke;item.append(swatch,document.createTextNode(name));legend.append(item)}if(model.counts)legend.append(make('b','',model.counts));if(legend.childElementCount)container.append(legend);resize()};
"##
    };
}

/// Close of the app script and the document.
macro_rules! app_tail {
    () => {
        r##"})();
</script></body></html>"##
    };
}

/// The MCP Apps document view for `memory_export` — a readable master-detail
/// projection of the generated store document, with the exact Markdown kept
/// in a source panel. Read-only, advisory data.
pub const DOCUMENT_HTML: &str = concat!(
    app_head!(),
    app_shell_css!(),
    app_body_open!(),
    r##"<div class="frame">
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>nMEMORY</span><span class="brand-context">generated store</span></div>
    <span class="trust-chip">advisory data</span>
  </header>
"##,
    app_status_html!("memory_export"),
    r##"  <div id="app" hidden>
    <section class="summary" aria-labelledby="document-title">
      <div>
        <p class="eyebrow">Store projection</p>
        <h1 id="document-title" class="title"></h1>
        <p id="notice" class="notice"></p>
        <span id="generated-at" class="generated-at"></span>
      </div>
      <ul id="meta" class="meta" aria-label="Store summary"></ul>
    </section>
    <div id="workspace" class="workspace">
      <nav class="rail outline" aria-label="Store collections">
        <p class="rail-title">Collections</p>
        <ol id="outline" class="outline-list"></ol>
        <details id="source" class="source"><summary>Exact Markdown source</summary><pre id="raw"></pre></details>
      </nav>
      <main id="document" class="document"></main>
"##,
    app_detail_html!(),
    r##"    </div>
  </div>
</div>
"##,
    app_script_open!(),
    app_runtime_js!(),
    env!("CARGO_PKG_VERSION"),
    app_runtime_js_tail!(),
    r##"const title=byId('document-title'),notice=byId('notice'),generatedAt=byId('generated-at'),meta=byId('meta'),outline=byId('outline'),documentView=byId('document'),raw=byId('raw'),source=byId('source');
const parse=markdown=>{const model={title:'nMEMORY store — generated view',intro:[],sections:[],loose:[],empty:false};let section=null,group=null;for(const rawLine of markdown.split(/\r?\n/)){const line=rawLine.trim();if(!line)continue;if(line.startsWith('# ')){model.title=line.slice(2).trim();continue}if(line.startsWith('## ')){section={heading:line.slice(3).trim(),groups:[],items:[],paragraphs:[]};model.sections.push(section);group=null;continue}if(line.startsWith('### ')){if(!section)continue;group={heading:line.slice(4).trim(),items:[],paragraphs:[]};section.groups.push(group);continue}if(line.startsWith('> ')){model.intro.push(line.slice(2).trim());continue}if(line.startsWith('- ')){const target=group?group.items:section?section.items:model.loose;target.push(line.slice(2).trim());continue}if(line==='_store is empty_'){model.empty=true;continue}const target=group?group.paragraphs:section?section.paragraphs:model.intro;target.push(line)}return model};
const parseEntry=(text,project,kind)=>{const bold=text.match(/^\*\*([^*]+)\*\*\s*(.*)$/),plain=text.match(/^((?:cap|out|sess)-\d+)\b\s*(.*)$/),match=bold||plain;if(!match)return{id:null,headline:text,project,kind};const entry={id:match[1],headline:match[2].trim().replace(/^—\s*/,''),project,kind,confidence:'',authority:'',taint:'',source:'',anchor:'',validFrom:'',validTo:'',tier:'active'};const rest=match[2].trim(),closing=rest.lastIndexOf('" · conf ');if(rest.startsWith('"')&&closing>0){entry.headline=rest.slice(1,closing).replaceAll('\\"','"').replaceAll('\\n',' ↵ ').replaceAll('\\r','');const fields=rest.slice(closing+4).split(' · ');for(const field of fields){if(field.startsWith('conf '))entry.confidence=field.slice(5);else if(field.startsWith('taint:'))entry.taint=field.slice(6);else if(field.startsWith('tier '))entry.tier=field.slice(5);else if(field.includes(' @ ')){const at=field.lastIndexOf(' @ ');entry.source=field.slice(0,at);entry.anchor=field.slice(at+3)}else if(field.includes(' → ')){const arrow=field.indexOf(' → ');entry.validFrom=field.slice(0,arrow);entry.validTo=field.slice(arrow+3)}else if(!entry.authority)entry.authority=field}}else if(/tombstoned/i.test(entry.headline))entry.tier='tombstoned';else if(/superseded/i.test(entry.headline))entry.tier='superseded';return entry};
const parseRelation=text=>{const match=text.match(/^((?:cap|out)-\d+)\s+--([a-z_]+)-->\s+((?:cap|out)-\d+)\s+·\s+at\s+(.+)$/);return match?{from:match[1],kind:match[2],to:match[3],at:match[4]}:null};
const addMemoryCard=(container,text,project,kind)=>{const entry=parseEntry(text,project,kind);if(!isCapsuleId(entry.id)){genericRow(container,text);return}entry.kind=kind;memoryCard(container,entry)};
const addEdge=(list,text)=>{const relation=parseRelation(text),item=make('li','edge-card');if(relation){const flow=make('span','edge-flow');flow.append(make('span','edge-node',relation.from),make('span','edge-kind',pretty(relation.kind)),make('span','edge-node',relation.to));item.append(flow,make('span','edge-time',shortDate(relation.at)))}else item.append(make('span','',text));list.append(item)};
const renderItems=(items,parent,context)=>{if(!items.length)return;if(context.cards){const cards=make('div','cards');for(const item of items)addMemoryCard(cards,item,context.project,context.kind);parent.append(cards)}else{const list=make('ul','edge-list');for(const item of items)addEdge(list,item);parent.append(list)}};
const renderParagraphs=(paragraphs,parent)=>{for(const text of paragraphs)parent.append(make('p','paragraph',text))};
const headingParts=heading=>heading.startsWith('project ')?{kicker:'Project',title:heading.slice(8),project:heading.slice(8),cards:true}:{kicker:heading==='relations'?'Store graph':'Lifecycle',title:heading,project:'',cards:heading==='superseded + tombstoned'};
const renderMeta=intro=>{meta.replaceChildren();const digest=intro.find(line=>line.startsWith('store digest:')),generated=intro.find(line=>line.startsWith('generated_at:'));generatedAt.textContent=generated?'Generated '+shortDate(generated.slice(13).trim()):'Stable, unstamped export';if(!digest)return;const values={};for(const token of digest.slice(13).split(' · ')[0].split(/\s+/)){const pair=token.split('=');if(pair.length===2)values[pair[0]]=pair[1]}const labels=[['capsules','Memories'],['projects','Projects'],['relations','Connections'],['live','Live']];if(Number(values.superseded)>0)labels.push(['superseded','Superseded']);if(Number(values.tombstoned)>0)labels.push(['tombstoned','Tombstoned']);for(const [key,label] of labels){if(values[key]===undefined)continue;const item=make('li','');item.append(make('strong','',values[key]),make('span','',label));meta.append(item)}};
onToolResult=result=>{const data=result&&result.structuredContent;if(!data||typeof data.markdown!=='string'){failStatus('No structured Markdown result was provided.');return}closeDetail();const model=parse(data.markdown);title.textContent=model.title.replace(' — generated view','');notice.textContent=model.intro.find(line=>line.includes('GENERATED VIEW'))||'Generated view — regenerate from nMEMORY; never hand-edit.';renderMeta(model.intro);outline.replaceChildren();documentView.replaceChildren();raw.textContent=data.markdown;model.sections.forEach((section,index)=>{const id='section-'+index,parts=headingParts(section.heading),navItem=make('li',''),link=make('a','',parts.title);link.href='#'+id;navItem.append(link);outline.append(navItem);const node=make('section','section');node.id=id;const sectionHead=make('header','section-head'),heading=make('div','');heading.append(make('p','section-kicker',parts.kicker),make('h2','',parts.title));const total=section.items.length+section.groups.reduce((sum,group)=>sum+group.items.length,0);sectionHead.append(heading,make('span','section-count',total+' '+(total===1?'item':'items')));node.append(sectionHead);renderParagraphs(section.paragraphs,node);if(parts.cards){const cards=make('div','cards');for(const item of section.items)addMemoryCard(cards,item,parts.project,parts.project?'unclassified':'lifecycle');for(const group of section.groups)for(const item of group.items)addMemoryCard(cards,item,parts.project,group.heading);if(cards.childElementCount)node.append(cards)}else{renderItems(section.items,node,{cards:false,project:parts.project,kind:'unclassified'});for(const group of section.groups){const groupNode=make('div','group'),groupHead=make('div','group-head');groupHead.append(make('h3','',pretty(group.heading)),make('span','group-count',group.items.length+' '+(group.items.length===1?'memory':'memories')));groupNode.append(groupHead);renderParagraphs(group.paragraphs,groupNode);renderItems(group.items,groupNode,{cards:false,project:parts.project,kind:group.heading});node.append(groupNode)}}documentView.append(node)});if(model.empty||(!model.sections.length&&!model.loose.length))emptyBlock(documentView,'No memories yet','Capture a memory, then run memory_export again.');renderItems(model.loose,documentView,{cards:false,project:'',kind:'unclassified'});status.hidden=true;app.hidden=false;resize()};
source.addEventListener('toggle',resize);
boot('nmemory-document');
"##,
    app_tail!()
);

/// The MCP Apps view for `memory_visual` — the projection DRAWN, not dumped:
/// nodes are laid out by contract depth with `blocks` edges as arrows, and
/// the exact deterministic Mermaid stays available in a source panel. Nodes
/// that name a capsule open it in the shared detail pane.
pub const VISUAL_HTML: &str = concat!(
    app_head!(),
    app_shell_css!(),
    app_stage_css!(),
    app_body_open!(),
    r##"<div class="frame">
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>nMEMORY</span><span class="brand-context">visual</span></div>
    <span class="trust-chip">advisory data</span>
  </header>
"##,
    app_status_html!("memory_visual"),
    r##"  <div id="app" hidden>
    <div id="workspace" class="workspace">
      <nav class="rail" aria-label="Projection">
        <p class="rail-title">Projection</p>
        <ol id="outline" class="outline-list"></ol>
        <details id="source" class="source"><summary>Exact Mermaid source</summary><pre id="raw"></pre></details>
      </nav>
      <main id="diagram" class="document"></main>
"##,
    app_detail_html!(),
    r##"    </div>
  </div>
</div>
"##,
    app_script_open!(),
    app_runtime_js!(),
    env!("CARGO_PKG_VERSION"),
    app_runtime_js_tail!(),
    app_dag_js!(),
    r##"const diagram=byId('diagram'),raw=byId('raw'),source=byId('source'),outline=byId('outline');
const VIEWS=['dag','relations','tiers','sessions'];
let currentView='dag';
const renderRail=()=>{outline.replaceChildren();for(const view of VIEWS){const item=make('li',''),button=make('button','btn'+(view===currentView?' primary':''),view);button.type='button';button.style.width='100%';button.addEventListener('click',()=>{currentView=view;renderRail();diagram.replaceChildren();const head=make('header','section-head');head.append(make('h2','','Loading '+view+'…'));diagram.append(head);callTool('memory_visual',{view}).then(data=>draw(data)).catch(error=>{diagram.replaceChildren(make('div','detail-error',error.message))})});item.append(button);outline.append(item)}};
const draw=data=>{if(!data||typeof data.mermaid!=='string'){failStatus('No structured Mermaid result was provided.');return}const model=parseMermaid(data.mermaid);if(model.view)currentView=model.view;raw.textContent=data.mermaid;renderRail();diagram.replaceChildren();const head=make('header','section-head'),heading=make('div','');heading.append(make('p','section-kicker','Projection'),make('h2','',model.view||'diagram'));head.append(heading,make('span','section-count',String(model.nodes.size)+' nodes · '+String(model.edges.length)+' edges'));diagram.append(head);const stageHost=make('div','');drawStage(stageHost,model,(node,entry)=>openCapsule(node,{id:entry.id,headline:entry.label,project:'',kind:entry.cls||'unclassified'}));diagram.append(stageHost);if(!model.nodes.size)emptyBlock(diagram,'Nothing to draw','This projection is empty in scope.');status.hidden=true;app.hidden=false;resize()};
onToolResult=result=>{closeDetail();draw(result&&result.structuredContent)};
source.addEventListener('toggle',resize);
boot('nmemory-visual');
"##,
    app_tail!()
);

/// The MCP Apps console for `memory_digest` — the home surface. Five tabs
/// over one store: the handoff threads and store shape (home), the blocks
/// dag as ready/blocked/done work (work), the epic roots of the mission
/// spine (epics), the drawn projection (dag), and the stored memories with
/// recall (memories).
///
/// Every write is explicit: the form collects the arguments, a review step
/// shows the exact `tools/call` before it is sent, and the result or the
/// server's own error is displayed verbatim. Destructive verbs
/// (`memory_forget`, `memory_merge`, `memory_consolidate`) are NOT reachable
/// from this app — they stay in the conversation, where the blast radius is
/// visible.
pub const CONSOLE_HTML: &str = concat!(
    app_head!(),
    app_shell_css!(),
    app_stage_css!(),
    r##".nav-list { display: flex; flex-direction: column; gap: 2px; margin: 0; padding: 0; list-style: none; }
.nav-item { width: 100%; min-height: 34px; appearance: none; display: flex; align-items: center; gap: 9px; padding: 7px 9px; border: 0; border-radius: 7px; background: transparent; color: var(--ink-soft); cursor: pointer; text-align: left; font: 12px/1.35 var(--mono); }
.nav-item:hover { background: var(--surface-subtle); color: var(--ink); }
.nav-item:focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; }
.nav-item[aria-pressed="true"] { background: var(--accent-wash); color: var(--accent-dark); font-weight: 700; }
.nav-item em { font-style: normal; margin-left: auto; color: var(--ink-muted); font-size: 11px; }
.nav-item[aria-pressed="true"] em { color: var(--accent-dark); }
.next { display: grid; gap: 6px; padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--surface); }
.next-body { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.next-text { min-width: 0; flex: 1; color: var(--ink); font-size: 13px; font-weight: 700; line-height: 1.4; }
.nudges { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 10px 16px; border-bottom: 1px solid var(--line-soft); list-style: none; }
.nudge { padding: 3px 8px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-muted); font: 700 9.5px/1.25 var(--mono); letter-spacing: .05em; text-transform: uppercase; }
.nudge.warn { border-color: var(--waiting); color: var(--waiting); }
.nudge.bad { border-color: var(--danger); color: var(--danger); }
.board { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; }
.board > .lane { min-width: 0; border-right: 1px solid var(--line-soft); }
.board > .lane:last-child { border-right: 0; }
.lane-head { min-height: 40px; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 12px; border-bottom: 1px solid var(--line-soft); background: var(--surface); }
.lane-head h3 { margin: 0; color: var(--ink-soft); font: 700 10px/1.2 var(--mono); letter-spacing: .07em; text-transform: uppercase; }
.searchbar { display: flex; flex-wrap: wrap; gap: 7px; padding: 12px 16px; border-bottom: 1px solid var(--line-soft); }
.searchbar input, .searchbar select { min-height: 32px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); color: var(--ink); font: 12px/1.4 var(--mono); }
.searchbar input { flex: 1; min-width: 160px; }
.searchbar input:focus-visible, .searchbar select:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
@media (max-width: 840px) { .board { grid-template-columns: 1fr; } .board > .lane { border-right: 0; border-bottom: 1px solid var(--line-soft); } }
"##,
    app_body_open!(),
    r##"<div class="frame">
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>nMEMORY</span><span class="brand-context">console</span></div>
    <div class="topbar-side">
      <button id="capture" class="btn" type="button">Capture</button>
      <button id="refresh" class="btn" type="button">Refresh</button>
      <span class="trust-chip">advisory data</span>
    </div>
  </header>
"##,
    app_status_html!("memory_digest"),
    r##"  <div id="app" hidden>
    <section class="summary" aria-labelledby="console-title">
      <div>
        <p class="eyebrow">Store state</p>
        <h1 id="console-title" class="title">nMEMORY console</h1>
        <p id="notice" class="notice">Advisory data — this app locates evidence, it never carries authority.</p>
        <span id="generated-at" class="generated-at"></span>
      </div>
      <ul id="meta" class="meta" aria-label="Store summary"></ul>
    </section>
    <div id="workspace" class="workspace">
      <nav class="rail" aria-label="Console tabs">
        <p class="rail-title">Surfaces</p>
        <ul id="nav" class="nav-list"></ul>
      </nav>
      <main id="view" class="document"></main>
"##,
    app_detail_html!(),
    r##"    </div>
  </div>
</div>
"##,
    app_script_open!(),
    app_runtime_js!(),
    env!("CARGO_PKG_VERSION"),
    app_runtime_js_tail!(),
    app_dag_js!(),
    r##"const nav=byId('nav'),view=byId('view'),meta=byId('meta'),generatedAt=byId('generated-at'),refreshButton=byId('refresh'),captureButton=byId('capture');
const KINDS=['fact','procedure','decision','task','epic','brainstorm','doc','constraint','capability','failure_pattern'];
const EDGE_KINDS=['blocks','witnesses','grounded_in','part_of','about','derived_from','proposes','supersedes','falsifies'];
const TABS=[{id:'home',label:'Home'},{id:'work',label:'Work'},{id:'epics',label:'Epics'},{id:'dag',label:'DAG'},{id:'memories',label:'Memories'}];
const state={digest:null,index:new Map(),indexed:false,tab:'home',visual:null,visualView:'dag',search:'',listKind:'',lastCapsule:null};
const indexOf=id=>state.index.get(id)||null;
const entryFor=(id,extra)=>{const row=indexOf(id)||{};return Object.assign({id,headline:row.headline||'',project:row.project_id||'',kind:row.kind||'unclassified',tier:row.tier||(row.superseded?'superseded':'active'),note:row.headline?'':'outside the index window'},extra||{})};
const headlineEntry=(row,extra)=>Object.assign({id:row.id,headline:row.headline||'',project:row.project_id||'',kind:row.kind||'unclassified',tier:row.tier||(row.superseded?'superseded':'active')},extra||{});
const countFor=tab=>{const digest=state.digest;if(!digest)return'';if(tab==='home')return String(digest.handoff_total||(digest.handoff?digest.handoff.length:0));if(tab==='work'){const dag=digest.dag||{};return dag.status==='cycle'?'!':String((dag.ready_total||0)+(dag.blocked_total||0))}if(tab==='epics'){const mission=digest.mission;if(!mission)return'0';return mission.status==='cycle'?'!':String(mission.roots?mission.roots.length:0)}if(tab==='memories')return String(digest.total||0);return''};
const renderNav=()=>{nav.replaceChildren();for(const tab of TABS){const item=make('li',''),button=make('button','nav-item');button.type='button';button.setAttribute('aria-pressed',String(state.tab===tab.id));button.append(make('span','',tab.label));const count=countFor(tab.id);if(count!=='')button.append(make('em','',count));button.addEventListener('click',()=>{state.tab=tab.id;renderNav();renderView()});item.append(button);nav.append(item)}};
const sectionHead=(kicker,heading,count)=>{const head=make('header','section-head'),copy=make('div','');copy.append(make('p','section-kicker',kicker),make('h2','',heading));head.append(copy);if(count!==undefined&&count!==null)head.append(make('span','section-count',String(count)));return head};
const cardList=(parent,entries,emptyStrong,emptyText)=>{if(!entries.length){emptyBlock(parent,emptyStrong,emptyText);return}const cards=make('div','cards');for(const entry of entries)memoryCard(cards,entry);parent.append(cards)};
const renderMeta=()=>{const digest=state.digest||{};meta.replaceChildren();const tiers=digest.tiers||{};const rows=[[digest.total,'Memories'],[digest.by_project?digest.by_project.length:0,'Projects'],[digest.relations,'Edges'],[tiers.active,'Active'],[tiers.archived,'Archived'],[tiers.quarantined,'Quarantined'],[digest.audit_events,'Audit']];for(const [value,label] of rows){if(value===undefined||value===null)continue;const item=make('li','');item.append(make('strong','',String(value)),make('span','',label));meta.append(item)}generatedAt.textContent=state.indexed?'Headline index hydrated from memory_list':'Hydrating headline index…'};
const nextAction=()=>{const digest=state.digest||{},dag=digest.dag||{};if(dag.status==='cycle')return{text:'Blocks-cycle fail-closed: '+(dag.cycle||[]).join(' → ')+'. Supersede, forget, or witness a member, then refresh.',id:(dag.cycle||[])[0],tab:'work'};if(Array.isArray(dag.ready)&&dag.ready.length){const id=dag.ready[0];return{text:'Pick up '+id+(indexOf(id)?': '+clip(indexOf(id).headline,110):''),id,tab:'work'}}if(Array.isArray(digest.handoff)&&digest.handoff.length){const row=digest.handoff[0];return{text:'Continue '+row.id+': '+clip(row.headline,110),id:row.id,tab:'home'}}if(Array.isArray(digest.newest)&&digest.newest.length)return{text:'No open work item. Newest record is '+digest.newest[0].id+'.',id:digest.newest[0].id,tab:'memories'};return{text:'The store is empty in scope. Capture the first memory.',id:null,tab:'memories'}};
const renderNudges=parent=>{const digest=state.digest||{},list=make('ul','nudges');const add=(text,tone)=>list.append(make('li','nudge'+(tone?' '+tone:''),text));if(digest.unanchored)add(digest.unanchored+' unanchored','warn');if(digest.open_sessions)add(digest.open_sessions+' open session'+(digest.open_sessions===1?'':'s'),'warn');if(digest.staged&&digest.staged.proposed)add(digest.staged.proposed+' proposed','warn');if(digest.staged&&digest.staged.stale_proposals)add(digest.staged.stale_proposals+' stale proposals','bad');if(digest.archive_candidates)add(digest.archive_candidates+' archive candidates');if(digest.recall_misses)add(digest.recall_misses+' recall misses');const journal=digest.journal||{};if(journal.chain&&journal.chain!=='ok')add('journal chain '+journal.chain,'bad');if(journal.out_of_band)add(journal.out_of_band+' out of band','warn');if(list.childElementCount)parent.append(list)};
const renderHome=()=>{const digest=state.digest||{};const next=nextAction();const band=make('section','next');band.append(make('p','eyebrow','Next action'));const body=make('div','next-body');body.append(make('span','next-text',next.text));if(next.id){const open=make('button','btn primary','Open '+next.id);open.type='button';open.addEventListener('click',()=>openCapsule(null,entryFor(next.id)));body.append(open)}band.append(body);view.append(band);renderNudges(view);const threads=make('section','section');threads.append(sectionHead('Handoff','ACTIVE threads',(digest.handoff?digest.handoff.length:0)+' of '+(digest.handoff_total||0)));cardList(threads,(digest.handoff||[]).map(row=>headlineEntry(row,{badge:'lifecycle'})),'No handoff in scope','Close a thread with an ACTIVE(<thread>) capture and it leads this list.');view.append(threads);if(Array.isArray(digest.pinned)&&digest.pinned.length){const pinned=make('section','section');pinned.append(sectionHead('Pinned','Decay-exempt',digest.pinned_total||digest.pinned.length));cardList(pinned,digest.pinned.map(row=>headlineEntry(row)),'','');view.append(pinned)}const newest=make('section','section');newest.append(sectionHead('Append order','Newest',(digest.newest?digest.newest.length:0)+' of '+(digest.newest_total||0)));cardList(newest,(digest.newest||[]).map(row=>headlineEntry(row)),'Nothing captured yet','Use Capture to record the first memory.');view.append(newest);if(Array.isArray(digest.most_recalled)&&digest.most_recalled.length){const recalled=make('section','section');recalled.append(sectionHead('Usage','Most recalled',(digest.most_recalled.length)+' of '+(digest.most_recalled_total||0)));cardList(recalled,digest.most_recalled.map(row=>headlineEntry(row,{note:row.recall_count+' recalls'})),'','');view.append(recalled)}};
const renderWork=()=>{const dag=(state.digest||{}).dag||{};const section=make('section','section');if(dag.status==='cycle'){section.append(sectionHead('Blocks dag','Cycle — fail closed',dag.entangled_total+' entangled'));section.append(make('p','stage-banner','No ready/blocked answer is fabricated. One concrete cycle: '+(dag.cycle||[]).join(' → ')+' → '+((dag.cycle||[])[0]||'')+'. Supersede, forget, or witness a member, then refresh.'));cardList(section,(dag.cycle||[]).map(id=>entryFor(id,{badge:'blocked',tone:'waiting',tier:'in cycle'})),'','');view.append(section);return}section.append(sectionHead('Blocks dag','Work',(dag.ready_total||0)+' ready · '+(dag.blocked_total||0)+' blocked · '+(dag.done_total||0)+' done'));view.append(section);const board=make('div','board');const lanes=[['Ready',dag.ready||[],dag.ready_total||0,'ready','ready','Nothing is unblocked','Add a blocks edge, or witness a blocker to release its dependents.'],['Blocked',dag.blocked||[],dag.blocked_total||0,'blocked','waiting','Nothing is gated','A blocked item waits on at least one live blocker.'],['Done',dag.done||[],dag.done_total||0,'done','','Nothing witnessed yet','A witnesses edge closes an item with proof — it leaves ready and blocked but stays recallable.']];for(const [name,ids,total,badge,tone,emptyStrong,emptyText] of lanes){const lane=make('div','lane'),head=make('div','lane-head');head.append(make('h3','',name),make('span','section-count',ids.length+' of '+total));lane.append(head);cardList(lane,ids.map(id=>entryFor(id,{badge,tone,tier:badge})),emptyStrong,emptyText);board.append(lane)}view.append(board)};
const renderEpics=()=>{const mission=(state.digest||{}).mission;const section=make('section','section');if(!mission){section.append(sectionHead('Mission spine','No epic root',0));section.append(make('p','paragraph','Nothing grounds into an epic yet. Capture an epic, then add a grounded_in edge from the work it carries — the spine appears here.'));view.append(section);return}if(mission.status==='cycle'){section.append(sectionHead('Mission spine','Cycle — fail closed',mission.entangled_total+' entangled'));section.append(make('p','stage-banner','No root list is fabricated. One concrete grounded_in cycle: '+(mission.cycle||[]).join(' → ')+'. Supersede or forget a member, then refresh.'));cardList(section,(mission.cycle||[]).map(id=>entryFor(id,{badge:'epic',tier:'in cycle'})),'','');view.append(section);return}section.append(sectionHead('Mission spine','Epic roots',(mission.roots||[]).length));cardList(section,(mission.roots||[]).map(root=>headlineEntry(root,{badge:'epic',note:root.children+' grounded'})),'No epic root','An epic with no live grounded_in parent is a root. Capture one and ground work into it.');view.append(section)};
const renderDag=()=>{const section=make('section','section');section.append(sectionHead('Projection',state.visualView,state.visual?String(state.visual.nodes.size)+' nodes · '+String(state.visual.edges.length)+' edges':'loading'));const switcher=make('div','searchbar');for(const name of ['dag','relations','tiers','sessions']){const button=make('button','btn'+(name===state.visualView?' primary':''),name);button.type='button';button.addEventListener('click',()=>{state.visualView=name;state.visual=null;renderView();loadVisual()});switcher.append(button)}section.append(switcher);view.append(section);const host=make('div','');if(state.visual)drawStage(host,state.visual,(node,entry)=>openCapsule(node,entryFor(entry.id,{badge:entry.cls||'unclassified'})));else host.append(make('div','detail-loading',''),make('p','paragraph','Drawing '+state.visualView+'…'));view.append(host);if(state.visual){const details=make('details','source'),summary=make('summary','','Exact Mermaid source');details.append(summary);const pre=make('pre','',state.visual.raw||'');details.append(pre);details.addEventListener('toggle',resize);const wrap=make('div','');wrap.style.padding='0 16px 16px';wrap.append(details);view.append(wrap)}};
const renderMemories=()=>{const section=make('section','section');section.append(sectionHead('Store',state.search?'Recall':'Index',state.results?state.results.length:''));const bar=make('div','searchbar');const input=make('input','');input.type='search';input.placeholder='Terms — OR across terms, AND inside one';input.value=state.search;input.setAttribute('aria-label','Recall terms');const kindPick=make('select','');kindPick.setAttribute('aria-label','Kind filter');const anyKind=make('option','','any kind');anyKind.value='';kindPick.append(anyKind);for(const kind of KINDS){const option=make('option','',kind);option.value=kind;if(kind===state.listKind)option.selected=true;kindPick.append(option)}const go=make('button','btn primary','Recall');go.type='button';const clear=make('button','btn','Index');clear.type='button';go.addEventListener('click',()=>{state.search=input.value.trim();state.listKind=kindPick.value;runRecall()});clear.addEventListener('click',()=>{state.search='';state.listKind=kindPick.value;runList()});input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();state.search=input.value.trim();state.listKind=kindPick.value;runRecall()}});bar.append(input,kindPick,go,clear);section.append(bar);if(state.resultNote)section.append(make('p','paragraph',state.resultNote));view.append(section);const host=make('section','section');cardList(host,(state.results||[]).map(row=>headlineEntry(row,{note:row.note})),'Nothing to show','Type terms and Recall, or press Index for the newest rows.');view.append(host)};
const RENDER={home:renderHome,work:renderWork,epics:renderEpics,dag:renderDag,memories:renderMemories};
const renderView=()=>{view.replaceChildren();renderMeta();(RENDER[state.tab]||renderHome)();resize()};
const loadVisual=()=>callTool('memory_visual',{view:state.visualView}).then(data=>{if(!data||typeof data.mermaid!=='string')throw new Error('memory_visual returned no Mermaid.');const model=parseMermaid(data.mermaid);model.raw=data.mermaid;state.visual=model;if(state.tab==='dag')renderView()}).catch(error=>{state.visual=null;if(state.tab==='dag'){view.replaceChildren();renderMeta();view.append(make('div','detail-error',error.message))}});
const runList=()=>{const args={limit:60};if(state.listKind)args.kind=state.listKind;state.resultNote='Newest rows from memory_list'+(state.listKind?' · kind '+state.listKind:'')+'.';callTool('memory_list',args).then(data=>{state.results=(data.entries||[]).slice().reverse();if(state.tab==='memories')renderView()}).catch(error=>{state.results=[];state.resultNote=error.message;if(state.tab==='memories')renderView()})};
const runRecall=()=>{const terms=state.search.split(/\s*,\s*|\s{2,}/).map(term=>term.trim()).filter(Boolean);if(!terms.length){runList();return}state.resultNote='Recalling…';if(state.tab==='memories')renderView();callTool('memory_retrieve',{terms,token_budget:4000}).then(data=>{const rows=(data.results||[]).map(row=>({id:row.id,headline:row.headline,project_id:row.project_id||(indexOf(row.id)||{}).project_id,kind:(indexOf(row.id)||{}).kind,tier:(indexOf(row.id)||{}).tier,note:'relevance '+row.relevance}));state.results=rows;const excluded=data.excluded?Object.entries(data.excluded).map(([reason,count])=>reason+' '+count).join(' · '):'';state.resultNote='Outcome '+data.outcome+(excluded?' · excluded: '+excluded:'')+' · '+terms.length+' term'+(terms.length===1?'':'s')+'.';if(state.tab==='memories')renderView()}).catch(error=>{state.results=[];state.resultNote=error.message;if(state.tab==='memories')renderView()})};
const hydrate=()=>callTool('memory_list',{limit:400}).then(data=>{state.index=new Map();for(const row of data.entries||[])state.index.set(row.id,row);state.indexed=true;renderNav();renderView()}).catch(()=>{state.indexed=false});
const refresh=()=>{refreshButton.disabled=true;return callTool('memory_digest',{headlines:14}).then(data=>{state.digest=data;return hydrate()}).catch(error=>{failStatus(error.message)}).then(()=>{refreshButton.disabled=false})};
"##,
    // --- write verbs: form, review the exact call, send, show the answer ---
    r##"const field=(label,control,note)=>{const wrap=make('label','field');wrap.append(make('span','',label),control);if(note)wrap.append(make('span','field-note',note));return wrap};
const textInput=(value,placeholder)=>{const input=make('input','');input.type='text';input.value=value||'';if(placeholder)input.placeholder=placeholder;return input};
const areaInput=(value,placeholder)=>{const area=make('textarea','');area.value=value||'';if(placeholder)area.placeholder=placeholder;return area};
const selectInput=(options,value)=>{const select=make('select','');for(const option of options){const node=make('option','',String(option));node.value=String(option);if(String(option)===String(value))node.selected=true;select.append(node)}return select};
const showResult=(host,name,payload)=>{host.replaceChildren();host.append(make('p','detail-section-label',name+' answered'));host.append(make('pre','result-ok',JSON.stringify(payload,null,2)));const back=make('button','btn primary','Refresh the console');back.type='button';back.addEventListener('click',()=>{closeDetail();refresh()});const actions=make('div','form-actions');actions.append(back);host.append(actions)};
const reviewAndSend=(host,name,args,rebuild)=>{host.replaceChildren();host.append(make('p','detail-section-label','Review the exact call'));host.append(make('pre','call-preview','tools/call '+name+'\n'+JSON.stringify(args,null,2)));const actions=make('div','form-actions'),send=make('button','btn primary','Send'),back=make('button','btn','Back');send.type='button';back.type='button';back.addEventListener('click',rebuild);send.addEventListener('click',()=>{send.disabled=true;back.disabled=true;callTool(name,args).then(payload=>showResult(host,name,payload)).catch(error=>{host.replaceChildren();host.append(make('p','detail-section-label',name+' was rejected'));host.append(make('div','detail-error',error.message));const again=make('button','btn','Back');again.type='button';again.addEventListener('click',rebuild);const wrap=make('div','form-actions');wrap.append(again);host.append(wrap)})});actions.append(send,back);host.append(actions)};
const formPin = (host,id,data)=>{const build=()=>{host.replaceChildren();host.append(make('p','detail-section-label','Pin or unpin '+id));const mode=selectInput(['pin','unpin'],'pin'),reason=areaInput('','Why this capsule is decay-exempt — audited, and never written for you');const error=make('p','form-error','');error.hidden=true;host.append(field('Verdict',mode),field('Reason',reason,'memory_pin refuses an empty reason: a pin is a witnessed act.'),error);const actions=make('div','form-actions'),next=make('button','btn primary','Review call'),cancel=make('button','btn','Cancel');next.type='button';cancel.type='button';cancel.addEventListener('click',()=>renderCapsuleActions(id,data));next.addEventListener('click',()=>{if(!reason.value.trim()){error.textContent='Type the reason first.';error.hidden=false;return}reviewAndSend(host,'memory_pin',{id,pinned:mode.value==='pin',reason:reason.value.trim()},build)});actions.append(next,cancel);host.append(actions)};build()};
const formClassify=(host,id,data)=>{const capsule=(data&&data.capsule)||{};const current=(data&&data.classification&&data.classification.kind)||'';const build=()=>{host.replaceChildren();host.append(make('p','detail-section-label','Classify '+id));const kind=selectInput(KINDS,current||'task'),evidence=selectInput(['','observed','inferred','unverified'],(data&&data.epistemics&&data.epistemics.evidence_state)||''),proof=textInput((data&&data.epistemics&&data.epistemics.proof_hint)||'','Command that re-proves the claim'),stale=textInput((data&&data.epistemics&&data.epistemics.stale_if)||'','Condition under which it expires');host.append(field('Kind',kind,current?'Currently '+current+'. A different kind replaces the label — last write wins.':'This capsule has no persisted kind yet.'),field('Evidence state',evidence,'Optional. Closed set: observed, inferred, unverified.'),field('Proof hint',proof,'Advisory string — stored verbatim, NEVER executed.'),field('Stale if',stale,'Advisory string — stored verbatim, NEVER evaluated.'));const actions=make('div','form-actions'),next=make('button','btn primary','Review call'),cancel=make('button','btn','Cancel');next.type='button';cancel.type='button';cancel.addEventListener('click',()=>renderCapsuleActions(id,data));next.addEventListener('click',()=>{const args={content:capsule.content||'',kind:kind.value,capsule_id:id};if(evidence.value)args.evidence_state=evidence.value;if(proof.value.trim())args.proof_hint=proof.value.trim();if(stale.value.trim())args.stale_if=stale.value.trim();reviewAndSend(host,'memory_classify',args,build)});actions.append(next,cancel);host.append(actions)};build()};
const formRelate=(host,id,data,preset)=>{const build=()=>{host.replaceChildren();host.append(make('p','detail-section-label','Relate '+id));const kind=selectInput(EDGE_KINDS,(preset&&preset.kind)||'blocks'),from=textInput((preset&&preset.from)||id,'cap-<n>'),to=textInput((preset&&preset.to)||'','cap-<n>');const error=make('p','form-error','');error.hidden=true;host.append(field('Kind',kind,'blocks builds the dag · witnesses closes with proof · grounded_in anchors to an epic · part_of groups an effort · about tags a topic.'),field('From',from,'from --kind--> to'),field('To',to),error);const actions=make('div','form-actions'),next=make('button','btn primary','Review call'),cancel=make('button','btn','Cancel');next.type='button';cancel.type='button';cancel.addEventListener('click',()=>renderCapsuleActions(id,data));next.addEventListener('click',()=>{const fromId=from.value.trim(),toId=to.value.trim();if(!fromId||!toId){error.textContent='Both endpoints are required.';error.hidden=false;return}if(fromId===toId){error.textContent='An edge to itself is never a relation.';error.hidden=false;return}reviewAndSend(host,'memory_relate',{kind:kind.value,from:fromId,to:toId},build)});actions.append(next,cancel);host.append(actions)};build()};
const formCapture=(host,preset,onCancel)=>{const build=()=>{host.replaceChildren();host.append(make('p','detail-section-label',(preset&&preset.title)||'Capture a memory'));const content=areaInput((preset&&preset.content)||'','What became true — one fact, stated plainly'),kind=selectInput(KINDS,(preset&&preset.kind)||'task'),project=textInput((preset&&preset.project)||'','Leave empty for the server default'),source=textInput((preset&&preset.source)||'','Where this came from — mandatory'),anchor=textInput((preset&&preset.anchor)||'','path:line, url, or id that grounds it — mandatory'),evidence=selectInput(['','observed','inferred','unverified'],(preset&&preset.evidence)||'observed');const error=make('p','form-error','');error.hidden=true;host.append(field('Content',content),field('Kind',kind),field('Project',project),field('Source',source,'memory_ingest rejects a capture with no provenance.'),field('Anchor',anchor),field('Evidence state',evidence),error);const actions=make('div','form-actions'),next=make('button','btn primary','Review call'),cancel=make('button','btn','Cancel');next.type='button';cancel.type='button';cancel.addEventListener('click',onCancel);next.addEventListener('click',()=>{if(!content.value.trim()||!source.value.trim()||!anchor.value.trim()){error.textContent='Content, source, and anchor are all mandatory.';error.hidden=false;return}const args={content:content.value.trim(),source:source.value.trim(),anchor:anchor.value.trim(),kind:kind.value};if(project.value.trim())args.project_id=project.value.trim();if(evidence.value)args.evidence_state=evidence.value;if(preset&&preset.supersedes)args.supersedes=preset.supersedes;reviewAndSend(host,'memory_ingest',args,build)});actions.append(next,cancel);host.append(actions)};build()};
const formWitness=(host,id,data)=>{const build=()=>{host.replaceChildren();host.append(make('p','detail-section-label','Witness '+id+' — two acts, never one button'));host.append(make('p','detail-content','A work item closes only when evidence attests it. Step 1 captures the observation as its own capsule. Step 2 records the witnesses edge that takes '+id+' out of ready and blocked. Nothing here certifies itself.'));const actions=make('div','form-actions'),step1=make('button','btn primary','1 · Capture the evidence'),step2=make('button','btn','2 · Record witnesses edge'),cancel=make('button','btn','Cancel');step1.type='button';step2.type='button';cancel.type='button';step1.addEventListener('click',()=>formCapture(host,{title:'Capture the evidence for '+id,kind:'fact',evidence:'observed',content:'',source:'',anchor:''},build));step2.addEventListener('click',()=>formRelate(host,id,data,{kind:'witnesses',from:'',to:id}));cancel.addEventListener('click',()=>renderCapsuleActions(id,data));actions.append(step1,step2,cancel);host.append(actions)};build()};
const renderCapsuleActions=(id,data)=>{detailBody.replaceChildren();renderDetail(data);detailFoot.replaceChildren();const host=make('section','detail-section');const act=(label,run)=>{const button=make('button','btn',label);button.type='button';button.addEventListener('click',()=>{detailBody.replaceChildren(host);host.scrollIntoView({block:'nearest'});run(host,id,data);resize()});return button};detailFoot.append(act('Pin',formPin),act('Classify',formClassify),act('Relate',formRelate),act('Witness',formWitness));const supersede=make('button','btn','Supersede');supersede.type='button';supersede.addEventListener('click',()=>{detailBody.replaceChildren(host);formCapture(host,{title:'Supersede '+id,content:'',kind:(data&&data.classification&&data.classification.kind)||'fact',project:(data&&data.capsule&&data.capsule.scope&&data.capsule.scope.project_id)||'',source:'',anchor:'',supersedes:id},()=>renderCapsuleActions(id,data));resize()});detailFoot.append(supersede);detailFoot.hidden=false;resize()};
onCapsuleOpened=(id,data,token)=>{if(token!==detailToken)return;state.lastCapsule={id,data};renderCapsuleActions(id,data)};
captureButton.addEventListener('click',()=>{const token=openShell(null,'Capture','New memory','memory_ingest — provenance is mandatory');detailBody.replaceChildren();const host=make('section','detail-section');detailBody.append(host);formCapture(host,{title:'Capture a memory'},()=>{if(token===detailToken)closeDetail()})});
refreshButton.addEventListener('click',()=>refresh());
onToolResult=result=>{const data=result&&result.structuredContent;if(!data||typeof data.total!=='number'){failStatus('No structured memory_digest result was provided.');return}closeDetail();state.digest=data;status.hidden=true;app.hidden=false;renderNav();renderView();if(!state.indexed)hydrate();if(!state.results)runList();if(!state.visual)loadVisual()};
renderNav();
boot('nmemory-console');
"##,
    app_tail!()
);

/// Closed resource set advertised by the server.
pub const APP_RESOURCES: &[AppResource] = &[
    AppResource {
        uri: CONSOLE_URI,
        name: "nmemory_console",
        title: "nMEMORY console",
        description: "Home surface for memory_digest: handoffs, work dag, epics, memories, and the write verbs",
        html: CONSOLE_HTML,
    },
    AppResource {
        uri: DOCUMENT_URI,
        name: "nmemory_document",
        title: "nMEMORY document",
        description: "Readable generated-store document for memory_export",
        html: DOCUMENT_HTML,
    },
    AppResource {
        uri: VISUAL_URI,
        name: "nmemory_visual",
        title: "nMEMORY visual",
        description: "Drawn projection for memory_visual, with the exact Mermaid source",
        html: VISUAL_HTML,
    },
];

/// Resolve an advertised resource by its exact URI.
#[must_use]
pub fn resource_for_uri(uri: &str) -> Option<&'static AppResource> {
    APP_RESOURCES.iter().find(|resource| resource.uri == uri)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_protocol_shape(html: &str, structured_field: &str) {
        assert!(html.starts_with("<!doctype html>"));
        assert!(html.contains("ui/initialize"));
        assert!(html.contains("ui/notifications/initialized"));
        assert!(html.contains("ui/notifications/tool-result"));
        assert!(html.contains("ui/notifications/size-changed"));
        assert!(html.contains("ui/resource-teardown"));
        assert!(html.contains("structuredContent"));
        assert!(html.contains(structured_field));
        assert!(!html.contains("<script src="));
        assert!(!html.contains("https://"));
        // The SVG namespace literal is the ONE permitted exception: inline
        // SVG built through createElementNS needs it, and it is never fetched.
        let without_svg_ns = html.replace("http://www.w3.org/2000/svg", "");
        assert!(!without_svg_ns.contains("http://"));
    }

    #[test]
    fn document_resource_is_self_contained_safe_and_protocol_shaped() {
        assert!(DOCUMENT_URI.starts_with("ui://"));
        assert_eq!(MIME_TYPE, "text/html;profile=mcp-app");
        assert_protocol_shape(DOCUMENT_HTML, "markdown");
        assert!(DOCUMENT_HTML.contains("textContent"));
        assert!(!DOCUMENT_HTML.contains("innerHTML"));
        assert!(DOCUMENT_HTML.contains("Exact Markdown source"));
        assert!(DOCUMENT_HTML.contains("make('button','memory-card')"));
        assert!(DOCUMENT_HTML.contains("aria-controls','detail-shell"));
        assert!(DOCUMENT_HTML.contains("request('tools/call'"));
        assert!(DOCUMENT_HTML.contains("name:'memory_get'"));
        assert!(DOCUMENT_HTML.contains("decodeToolResult"));
        assert!(DOCUMENT_HTML.contains("event.key==='Tab'"));
    }

    #[test]
    fn visual_resource_draws_the_projection_and_stays_self_contained() {
        assert!(VISUAL_URI.starts_with("ui://"));
        assert_protocol_shape(VISUAL_HTML, "mermaid");
        assert!(!VISUAL_HTML.contains("innerHTML"));
        // The projection is DRAWN, not dumped: the parser and the stage
        // renderer are both present, and the exact source stays reachable.
        assert!(VISUAL_HTML.contains("parseMermaid"));
        assert!(VISUAL_HTML.contains("drawStage"));
        assert!(VISUAL_HTML.contains("Exact Mermaid source"));
        assert!(VISUAL_HTML.contains("createElementNS"));
    }

    #[test]
    fn console_resource_is_self_contained_safe_and_protocol_shaped() {
        assert!(CONSOLE_URI.starts_with("ui://"));
        assert_protocol_shape(CONSOLE_HTML, "memory_digest");
        assert!(CONSOLE_HTML.contains("textContent"));
        assert!(!CONSOLE_HTML.contains("innerHTML"));
        // The five surfaces the console exists to carry.
        for surface in [
            "renderHome",
            "renderWork",
            "renderEpics",
            "renderDag",
            "renderMemories",
        ] {
            assert!(CONSOLE_HTML.contains(surface), "missing {surface}");
        }
        // Home law: a ready-set, ONE next action, and the fail-closed
        // cycle wording rather than a fabricated ready/blocked answer.
        assert!(CONSOLE_HTML.contains("Next action"));
        assert!(CONSOLE_HTML.contains("No ready/blocked answer is fabricated"));
        assert!(CONSOLE_HTML.contains("No root list is fabricated"));
        // Every permitted write verb is reachable, and each one is
        // reviewed as an exact tools/call before it is sent.
        for verb in [
            "memory_pin",
            "memory_classify",
            "memory_relate",
            "memory_ingest",
            "memory_retrieve",
            "memory_list",
            "memory_visual",
        ] {
            assert!(CONSOLE_HTML.contains(verb), "missing {verb}");
        }
        assert!(CONSOLE_HTML.contains("Review the exact call"));
        assert!(CONSOLE_HTML.contains("reviewAndSend"));
        // Closure needs a witness: the app never offers a one-click done.
        assert!(CONSOLE_HTML.contains("two acts, never one button"));
        assert!(CONSOLE_HTML.contains("kind:'witnesses'"));
    }

    #[test]
    fn destructive_verbs_are_unreachable_from_every_app() {
        for resource in APP_RESOURCES {
            for verb in ["memory_forget", "memory_merge", "memory_consolidate"] {
                assert!(
                    !resource.html.contains(verb),
                    "{} must not reach {verb}",
                    resource.uri
                );
            }
        }
    }

    #[test]
    fn advertised_resources_are_unique_and_resolvable() {
        assert_eq!(APP_RESOURCES.len(), 3);
        let mut uris: Vec<&str> = APP_RESOURCES.iter().map(|resource| resource.uri).collect();
        uris.sort_unstable();
        let count = uris.len();
        uris.dedup();
        assert_eq!(uris.len(), count, "resource uris must be unique");
        for resource in APP_RESOURCES {
            assert_eq!(resource_for_uri(resource.uri), Some(resource));
            assert_eq!(
                resource_for_uri(resource.uri).map(|resolved| resolved.html.len()),
                Some(resource.html.len())
            );
        }
        assert!(resource_for_uri("ui://nmemory/missing").is_none());
    }

    #[test]
    fn every_resource_reports_the_crate_version_to_the_host() {
        let expected = format!("version:'{}'", env!("CARGO_PKG_VERSION"));
        for resource in APP_RESOURCES {
            assert!(
                resource.html.contains(&expected),
                "{} must report {expected}",
                resource.uri
            );
        }
    }
}
