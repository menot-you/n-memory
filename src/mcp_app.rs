//! Self-contained MCP App resources for nMEMORY generated views.
//!
//! Both views have no external dependencies and request no network, storage,
//! or device permissions. They are progressive enhancement: `memory_export`
//! and `memory_visual` keep returning their existing text payloads for hosts
//! without MCP Apps support.

/// MCP App resource attached to `memory_export`.
pub const DOCUMENT_URI: &str = "ui://nmemory/document";
/// MCP App resource attached to `memory_visual`.
pub const VISUAL_URI: &str = "ui://nmemory/visual";
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

/// A dependency-free MCP Apps document view for `memory_export`.
///
/// Stored bytes reach the DOM only through `textContent`; the app does not
/// interpret returned Markdown as HTML. The generated view remains read-only,
/// advisory data, and the exact Markdown stays available in the source panel.
pub const DOCUMENT_HTML: &str = r##"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
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
.source[open] summary::before { content: "−"; }
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
.edge-kind::before, .edge-kind::after { content: "—"; margin: 0 4px; color: var(--line); }
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
</style></head><body>
<div class="frame">
  <header class="topbar">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>nMEMORY</span><span class="brand-context">generated store</span></div>
    <span class="trust-chip">advisory data</span>
  </header>
  <div id="status" class="status-panel" role="status" aria-live="polite">
    <span class="status-dot" aria-hidden="true"></span>
    <span class="status-copy"><strong>Building the memory view</strong><span>Waiting for memory_export…</span></span>
  </div>
  <div id="app" hidden>
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
      <aside id="detail-shell" class="detail-pane" role="complementary" aria-labelledby="detail-title" hidden>
        <header class="detail-head">
          <div><p id="detail-label" class="detail-label">Memory</p><h2 id="detail-title" class="detail-title">Stored record</h2><p id="detail-context" class="detail-context"></p></div>
          <button id="detail-close" class="detail-close" type="button" aria-label="Close memory details">×</button>
        </header>
        <div id="detail-body" class="detail-body"></div>
      </aside>
    </div>
  </div>
</div>
<script>
(()=>{'use strict';
let nextId=1,initializeId=0,resizeFrame=0,selectedCard=null,detailToken=0;
const pending=new Map();
const status=document.getElementById('status');
const app=document.getElementById('app');
const title=document.getElementById('document-title');
const notice=document.getElementById('notice');
const generatedAt=document.getElementById('generated-at');
const meta=document.getElementById('meta');
const outline=document.getElementById('outline');
const documentView=document.getElementById('document');
const raw=document.getElementById('raw');
const source=document.getElementById('source');
const workspace=document.getElementById('workspace');
const detailShell=document.getElementById('detail-shell');
const detailClose=document.getElementById('detail-close');
const detailLabel=document.getElementById('detail-label');
const detailTitle=document.getElementById('detail-title');
const detailContext=document.getElementById('detail-context');
const detailBody=document.getElementById('detail-body');
const post=message=>window.parent.postMessage(message,'*');
const notify=(method,params={})=>post({jsonrpc:'2.0',method,params});
const make=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node};
const resize=()=>{if(resizeFrame)cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>notify('ui/notifications/size-changed',{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}))};
const request=(method,params)=>new Promise((resolve,reject)=>{const id=nextId++;const timer=window.setTimeout(()=>{pending.delete(id);reject(new Error('The host did not answer the request.'))},10000);pending.set(id,{resolve,reject,timer});post({jsonrpc:'2.0',id,method,params})});
const parse=markdown=>{const model={title:'nMEMORY store — generated view',intro:[],sections:[],loose:[],empty:false};let section=null,group=null;for(const rawLine of markdown.split(/\r?\n/)){const line=rawLine.trim();if(!line)continue;if(line.startsWith('# ')){model.title=line.slice(2).trim();continue}if(line.startsWith('## ')){section={heading:line.slice(3).trim(),groups:[],items:[],paragraphs:[]};model.sections.push(section);group=null;continue}if(line.startsWith('### ')){if(!section)continue;group={heading:line.slice(4).trim(),items:[],paragraphs:[]};section.groups.push(group);continue}if(line.startsWith('> ')){model.intro.push(line.slice(2).trim());continue}if(line.startsWith('- ')){const target=group?group.items:section?section.items:model.loose;target.push(line.slice(2).trim());continue}if(line==='_store is empty_'){model.empty=true;continue}const target=group?group.paragraphs:section?section.paragraphs:model.intro;target.push(line)}return model};
const pretty=value=>String(value||'unclassified').replaceAll('_',' ').replaceAll('-',' ');
const kindMark=kind=>({decision:'D',task:'T',fact:'F',constraint:'C',procedure:'P',epic:'EP',brainstorm:'B',doc:'DOC',capability:'CAP',failure_pattern:'!',evidence:'E',journal:'J',lifecycle:'L',unclassified:'M'}[kind]||'M');
const shortDate=value=>{if(!value)return'';const date=new Date(value);if(Number.isNaN(date.getTime()))return value;return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',year:'numeric'}).format(date)};
const parseEntry=(text,project,kind)=>{const bold=text.match(/^\*\*([^*]+)\*\*\s*(.*)$/),plain=text.match(/^((?:cap|out|sess)-\d+)\b\s*(.*)$/),match=bold||plain;if(!match)return{id:null,headline:text,project,kind};const entry={id:match[1],headline:match[2].trim().replace(/^—\s*/,''),project,kind,confidence:'',authority:'',taint:'',source:'',anchor:'',validFrom:'',validTo:'',tier:'active'};const rest=match[2].trim(),closing=rest.lastIndexOf('" · conf ');if(rest.startsWith('"')&&closing>0){entry.headline=rest.slice(1,closing).replaceAll('\\"','"').replaceAll('\\n',' ↵ ').replaceAll('\\r','');const fields=rest.slice(closing+4).split(' · ');for(const field of fields){if(field.startsWith('conf '))entry.confidence=field.slice(5);else if(field.startsWith('taint:'))entry.taint=field.slice(6);else if(field.startsWith('tier '))entry.tier=field.slice(5);else if(field.includes(' @ ')){const at=field.lastIndexOf(' @ ');entry.source=field.slice(0,at);entry.anchor=field.slice(at+3)}else if(field.includes(' → ')){const arrow=field.indexOf(' → ');entry.validFrom=field.slice(0,arrow);entry.validTo=field.slice(arrow+3)}else if(!entry.authority)entry.authority=field}}else if(/tombstoned/i.test(entry.headline))entry.tier='tombstoned';else if(/superseded/i.test(entry.headline))entry.tier='superseded';return entry};
const parseRelation=text=>{const match=text.match(/^((?:cap|out)-\d+)\s+--([a-z_]+)-->\s+((?:cap|out)-\d+)\s+·\s+at\s+(.+)$/);return match?{from:match[1],kind:match[2],to:match[3],at:match[4]}:null};
const isNarrowDetail=()=>window.matchMedia('(max-width:840px)').matches;
const syncDetailMode=()=>{detailShell.setAttribute('role',isNarrowDetail()?'dialog':'complementary');if(isNarrowDetail())detailShell.setAttribute('aria-modal','true');else detailShell.removeAttribute('aria-modal')};
const closeDetail=()=>{if(detailShell.hidden)return;detailToken+=1;detailShell.hidden=true;workspace.classList.remove('has-detail');document.body.classList.remove('detail-open');if(selectedCard){selectedCard.setAttribute('aria-expanded','false');selectedCard.focus({preventScroll:true});selectedCard=null}resize()};
const renderLoading=()=>{detailBody.replaceChildren();detailBody.setAttribute('aria-busy','true');const loading=make('div','detail-loading');loading.append(make('span','skeleton block'),make('span','skeleton'),make('span','skeleton'),make('span','skeleton'));detailBody.append(loading)};
const fact=(label,value,mono=false)=>{const item=make('div','fact'),term=make('dt','',label),description=make('dd',mono?'mono':'',value===undefined||value===null||value===''?'—':String(value));item.append(term,description);return item};
const detailSection=label=>{const section=make('section','detail-section');section.append(make('p','detail-section-label',label));return section};
const decodeToolResult=result=>{if(!result)throw new Error('memory_get returned no result.');const textItem=Array.isArray(result.content)?result.content.find(item=>item&&item.type==='text'&&typeof item.text==='string'):null;if(result.isError)throw new Error(textItem?textItem.text:'memory_get returned an error.');if(result.structuredContent&&typeof result.structuredContent==='object')return result.structuredContent;if(!textItem)throw new Error('memory_get returned no readable payload.');try{return JSON.parse(textItem.text)}catch(_error){throw new Error('memory_get returned malformed JSON.')}};
const renderDetail=data=>{detailBody.removeAttribute('aria-busy');detailBody.replaceChildren();if(!data||!data.capsule){const section=detailSection('Stored marker');section.append(make('p','detail-content','This record does not expose live capsule content.'));section.append(make('pre','raw-object',JSON.stringify(data,null,2)));detailBody.append(section);resize();return}const capsule=data.capsule,classification=data.classification||{},provenance=capsule.provenance||{},freshness=capsule.freshness||{},scope=capsule.scope||{};const chips=make('div','detail-chips');chips.append(make('span','detail-chip accent',pretty(classification.kind||'unclassified')),make('span','detail-chip',pretty(data.tier||'active')),make('span','detail-chip',pretty(capsule.authority_class)));if(capsule.instruction_taint)chips.append(make('span','detail-chip','tainted'));if(data.expired)chips.append(make('span','detail-chip','expired'));detailBody.append(chips);const content=detailSection('Full content');content.append(make('p','detail-content',capsule.content));detailBody.append(content);const properties=detailSection('Memory properties'),grid=make('dl','facts-grid');grid.append(fact('Project',scope.project_id),fact('Confidence',typeof capsule.confidence==='number'?Math.round(capsule.confidence*100)+'%':capsule.confidence),fact('Authority',pretty(capsule.authority_class)),fact('Created',data.created_at),fact('Valid from',freshness.valid_from),fact('Valid to',freshness.valid_to||'Open'),fact('Lifecycle',pretty(data.tier||'active')),fact('Sequence',data.seq));properties.append(grid);detailBody.append(properties);const provenanceSection=detailSection('Provenance'),provenanceGrid=make('dl','facts-grid');provenanceGrid.append(fact('Source',provenance.source,true),fact('Anchor',provenance.anchor,true),fact('Source hash',provenance.source_hash,true));provenanceSection.append(provenanceGrid);detailBody.append(provenanceSection);if(classification.reason||classification.scope){const classificationSection=detailSection('Classification'),classificationGrid=make('dl','facts-grid');classificationGrid.append(fact('Kind',pretty(classification.kind)),fact('Scope',pretty(classification.scope)),fact('Reason',classification.reason));classificationSection.append(classificationGrid);detailBody.append(classificationSection)}if(Array.isArray(data.relations)&&data.relations.length){const relationSection=detailSection('Graph edges'),list=make('ul','detail-relations');for(const relation of data.relations){const row=make('li','');row.append(make('span','',relation.from),make('strong','',pretty(relation.kind).toUpperCase()),make('span','',relation.to));list.append(row)}relationSection.append(list);detailBody.append(relationSection)}if(data.epistemics){const epistemicSection=detailSection('Epistemics'),epistemicGrid=make('dl','facts-grid');epistemicGrid.append(fact('Evidence state',pretty(data.epistemics.evidence_state)),fact('Recorded',data.epistemics.at),fact('Proof hint',data.epistemics.proof_hint,true),fact('Stale if',data.epistemics.stale_if));epistemicSection.append(epistemicGrid);detailBody.append(epistemicSection)}if(data.last_mutation){const auditSection=detailSection('Last mutation'),auditGrid=make('dl','facts-grid');auditGrid.append(fact('Event',pretty(data.last_mutation.event)),fact('Actor',data.last_mutation.actor,true),fact('At',data.last_mutation.at));auditSection.append(auditGrid);detailBody.append(auditSection)}resize()};
const renderDetailError=error=>{detailBody.removeAttribute('aria-busy');detailBody.replaceChildren(make('div','detail-error',error instanceof Error?error.message:'Unable to open this memory.'));resize()};
const openDetail=(card,entry)=>{if(!entry.id||!/^cap-\d+$/.test(entry.id))return;if(selectedCard&&selectedCard!==card)selectedCard.setAttribute('aria-expanded','false');selectedCard=card;card.setAttribute('aria-expanded','true');detailLabel.textContent='Memory '+entry.id;detailTitle.textContent=entry.headline||entry.id;detailContext.textContent=[entry.project,pretty(entry.kind),entry.source].filter(Boolean).join(' · ');detailShell.hidden=false;syncDetailMode();workspace.classList.add('has-detail');document.body.classList.add('detail-open');detailBody.scrollTop=0;renderLoading();detailClose.focus({preventScroll:true});resize();const token=++detailToken;request('tools/call',{name:'memory_get',arguments:{id:entry.id}}).then(decodeToolResult).then(data=>{if(token===detailToken)renderDetail(data)}).catch(error=>{if(token===detailToken)renderDetailError(error)})};
const addMemoryCard=(container,text,project,kind)=>{const entry=parseEntry(text,project,kind);if(!entry.id||!/^cap-\d+$/.test(entry.id)){const generic=make('div','entry-generic'),match=text.match(/^(\S+)\s*(.*)$/);if(match)generic.append(make('code','',match[1]),make('span','',match[2]));else generic.append(make('span','',text));container.append(generic);return}const card=make('button','memory-card');card.type='button';card.setAttribute('aria-expanded','false');card.setAttribute('aria-controls','detail-shell');card.setAttribute('aria-label','Open '+entry.id+': '+entry.headline);const glyph=make('span','card-glyph',kindMark(kind));glyph.setAttribute('aria-hidden','true');const copy=make('span','card-copy'),headline=make('span','card-title',entry.headline),cardMeta=make('span','card-meta');cardMeta.append(make('span','card-id',entry.id),make('span','',pretty(kind)));if(project)cardMeta.append(make('span','',project));if(entry.source)cardMeta.append(make('span','',entry.source));if(entry.confidence)cardMeta.append(make('span','',Math.round(Number(entry.confidence)*100)+'% confidence'));copy.append(headline,cardMeta);const side=make('span','card-side'),tier=make('span','tier'+(entry.tier==='active'?'':' is-special'),pretty(entry.tier)),arrow=make('span','card-arrow','›');arrow.setAttribute('aria-hidden','true');side.append(tier,arrow);card.append(glyph,copy,side);card.addEventListener('click',()=>openDetail(card,entry));container.append(card)};
const addEdge=(list,text)=>{const relation=parseRelation(text),item=make('li','edge-card');if(relation){const flow=make('span','edge-flow');flow.append(make('span','edge-node',relation.from),make('span','edge-kind',pretty(relation.kind)),make('span','edge-node',relation.to));item.append(flow,make('span','edge-time',shortDate(relation.at)))}else item.append(make('span','',text));list.append(item)};
const renderItems=(items,parent,context)=>{if(!items.length)return;if(context.cards){const cards=make('div','cards');for(const item of items)addMemoryCard(cards,item,context.project,context.kind);parent.append(cards)}else{const list=make('ul','edge-list');for(const item of items)addEdge(list,item);parent.append(list)}};
const renderParagraphs=(paragraphs,parent)=>{for(const text of paragraphs)parent.append(make('p','paragraph',text))};
const headingParts=heading=>heading.startsWith('project ')?{kicker:'Project',title:heading.slice(8),project:heading.slice(8),cards:true}:{kicker:heading==='relations'?'Store graph':'Lifecycle',title:heading,project:'',cards:heading==='superseded + tombstoned'};
const renderMeta=intro=>{meta.replaceChildren();const digest=intro.find(line=>line.startsWith('store digest:')),generated=intro.find(line=>line.startsWith('generated_at:'));generatedAt.textContent=generated?'Generated '+shortDate(generated.slice(13).trim()):'Stable, unstamped export';if(!digest)return;const values={};for(const token of digest.slice(13).split(' · ')[0].split(/\s+/)){const pair=token.split('=');if(pair.length===2)values[pair[0]]=pair[1]}const labels=[['capsules','Memories'],['projects','Projects'],['relations','Connections'],['live','Live']];if(Number(values.superseded)>0)labels.push(['superseded','Superseded']);if(Number(values.tombstoned)>0)labels.push(['tombstoned','Tombstoned']);for(const [key,label] of labels){if(values[key]===undefined)continue;const item=make('li','');item.append(make('strong','',values[key]),make('span','',label));meta.append(item)}};
const render=result=>{const data=result&&result.structuredContent;if(!data||typeof data.markdown!=='string'){status.className='status-panel failure';status.replaceChildren(make('span','status-dot'),make('span','status-copy','No structured Markdown result was provided.'));status.hidden=false;app.hidden=true;resize();return}closeDetail();const model=parse(data.markdown);title.textContent=model.title.replace(' — generated view','');notice.textContent=model.intro.find(line=>line.includes('GENERATED VIEW'))||'Generated view — regenerate from nMEMORY; never hand-edit.';renderMeta(model.intro);outline.replaceChildren();documentView.replaceChildren();raw.textContent=data.markdown;model.sections.forEach((section,index)=>{const id='section-'+index,parts=headingParts(section.heading),navItem=make('li',''),link=make('a','',parts.title);link.href='#'+id;navItem.append(link);outline.append(navItem);const node=make('section','section');node.id=id;const sectionHead=make('header','section-head'),heading=make('div','');heading.append(make('p','section-kicker',parts.kicker),make('h2','',parts.title));const total=section.items.length+section.groups.reduce((sum,group)=>sum+group.items.length,0);sectionHead.append(heading,make('span','section-count',total+' '+(total===1?'item':'items')));node.append(sectionHead);renderParagraphs(section.paragraphs,node);if(parts.cards){const cards=make('div','cards');for(const item of section.items)addMemoryCard(cards,item,parts.project,parts.project?'unclassified':'lifecycle');for(const group of section.groups)for(const item of group.items)addMemoryCard(cards,item,parts.project,group.heading);if(cards.childElementCount)node.append(cards)}else{renderItems(section.items,node,{cards:false,project:parts.project,kind:'unclassified'});for(const group of section.groups){const groupNode=make('div','group'),groupHead=make('div','group-head');groupHead.append(make('h3','',pretty(group.heading)),make('span','group-count',group.items.length+' '+(group.items.length===1?'memory':'memories')));groupNode.append(groupHead);renderParagraphs(group.paragraphs,groupNode);renderItems(group.items,groupNode,{cards:false,project:parts.project,kind:group.heading});node.append(groupNode)}}documentView.append(node)});if(model.empty||(!model.sections.length&&!model.loose.length)){const empty=make('div','empty');empty.append(make('strong','','No memories yet'),document.createTextNode('Capture a memory, then run memory_export again.'));documentView.append(empty)}renderItems(model.loose,documentView,{cards:false,project:'',kind:'unclassified'});status.hidden=true;app.hidden=false;resize()};
const applyHostStyles=result=>{const vars=result&&result.hostContext&&result.hostContext.styles&&result.hostContext.styles.variables;if(vars)for(const [key,value] of Object.entries(vars))if(typeof value==='string'&&key.startsWith('--'))document.documentElement.style.setProperty(key,value)};
const observer=typeof ResizeObserver==='function'?new ResizeObserver(resize):null;
if(observer)observer.observe(document.documentElement);
source.addEventListener('toggle',resize);
detailClose.addEventListener('click',closeDetail);
document.addEventListener('keydown',event=>{if(detailShell.hidden)return;if(event.key==='Escape'){event.preventDefault();closeDetail();return}if(event.key==='Tab'&&isNarrowDetail()){const focusable=[...detailShell.querySelectorAll('button,[href],[tabindex]:not([tabindex="-1"])')].filter(node=>!node.disabled&&!node.hidden);if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});
window.addEventListener('resize',()=>{syncDetailMode();resize()});
window.addEventListener('message',event=>{const message=event.data;if(!message||message.jsonrpc!=='2.0')return;if(message.id===initializeId&&message.result){applyHostStyles(message.result);notify('ui/notifications/initialized');resize();return}if(message.id!=null&&pending.has(message.id)){const requestState=pending.get(message.id);pending.delete(message.id);window.clearTimeout(requestState.timer);if(message.error)requestState.reject(new Error(message.error.message||'Host request failed.'));else requestState.resolve(message.result);return}if(message.method==='ui/notifications/tool-result'){render(message.params)}else if(message.method==='ui/resource-teardown'&&message.id!=null){if(observer)observer.disconnect();for(const requestState of pending.values()){window.clearTimeout(requestState.timer);requestState.reject(new Error('The app was closed.'))}pending.clear();post({jsonrpc:'2.0',id:message.id,result:{}})}});
initializeId=nextId++;
post({jsonrpc:'2.0',id:initializeId,method:'ui/initialize',params:{protocolVersion:'2026-01-26',appInfo:{name:'nmemory-document',version:'0.3.0'},appCapabilities:{availableDisplayModes:['inline']}}});
})();
</script></body></html>"##;
/// A small, dependency-free MCP Apps view for `memory_visual`. The host owns
/// sandboxing; the view only speaks JSON-RPC over `postMessage` and renders
/// escaped text.
///
/// Styling is the nMEMORY light design system (tokens sampled from the
/// canonical `assets/*.svg`): cream canvas `#F5F1E9`, panel `#FAF7F1`, ink
/// `#36332F`/`#4A4640`, muted `#8E8982`, border `#BBB3A8`, green `#698664`,
/// amber `#C88B32`, red `#E45A43`, mono type. The header carries the brand
/// mark — the capsule glyph with the subscript n — as inline SVG (no xmlns:
/// inline SVG in HTML needs none, and the resource stays URL-free).
pub const VISUAL_HTML: &str = r##"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light;--bg:#F5F1E9;--panel:#FAF7F1;--fg:#36332F;--strong:#4A4640;--muted:#8E8982;--border:#BBB3A8;--green:#698664;--amber:#C88B32;--red:#E45A43;--mono:"DejaVu Sans Mono","Noto Sans Mono",ui-monospace,monospace}
*{box-sizing:border-box}body{margin:0;padding:14px;background:var(--bg);color:var(--fg);font:13px/1.5 var(--mono)}
header{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:10px}
.brand{display:flex;align-items:flex-end;gap:8px}.brand svg{display:block}
h1{font:600 14px/1 var(--mono);letter-spacing:.08em;margin:0;color:var(--fg)}h1 sub{font-size:10px;color:var(--strong)}
.label{color:var(--muted);font:11px/1 var(--mono);letter-spacing:.06em}
pre{margin:0;padding:14px;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:auto;font:12px/1.5 var(--mono);white-space:pre;color:var(--fg)}.error{color:var(--red)}
</style></head><body><header><div class="brand"><svg width="30" height="19" viewBox="0 0 30 19" aria-hidden="true"><rect x="1.5" y="1.5" width="27" height="16" rx="8" fill="none" stroke="#4A4640" stroke-width="2"/><line x1="8" y1="9.5" x2="22" y2="9.5" stroke="#4A4640" stroke-width="2" stroke-linecap="round"/></svg><h1><sub>n</sub>MEMORY visual</h1></div><span class="label">ADVISORY_NOT_AUTHORITY</span></header><pre id="view">Waiting for memory_visual…</pre>
<script>
(()=>{'use strict';let nextId=1;const view=document.getElementById('view');
const post=m=>window.parent.postMessage(m,'*');
const notify=(method,params={})=>post({jsonrpc:'2.0',method,params});
const render=result=>{const data=result&&result.structuredContent;view.className='';if(data&&typeof data.mermaid==='string'){view.textContent=data.mermaid}else{view.className='error';view.textContent='No structured Mermaid result was provided.'}resize()};
const resize=()=>notify('ui/notifications/size-changed',{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight});
window.addEventListener('message',event=>{const m=event.data;if(!m||m.jsonrpc!=='2.0')return;if(m.id===1&&m.result){const vars=m.result.hostContext&&m.result.hostContext.styles&&m.result.hostContext.styles.variables;if(vars)for(const [k,v] of Object.entries(vars))if(typeof v==='string'&&k.startsWith('--'))document.documentElement.style.setProperty(k,v);notify('ui/notifications/initialized');resize()}else if(m.method==='ui/notifications/tool-result'){render(m.params)}else if(m.method==='ui/resource-teardown'&&m.id!=null){post({jsonrpc:'2.0',id:m.id,result:{}})}});
post({jsonrpc:'2.0',id:nextId++,method:'ui/initialize',params:{protocolVersion:'2026-01-26',appInfo:{name:'nmemory-visual',version:'0.1.0'},appCapabilities:{availableDisplayModes:['inline']}}});
})();
</script></body></html>"##;

/// Closed resource set advertised by the server.
pub const APP_RESOURCES: &[AppResource] = &[
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
        description: "Interactive view for memory_visual Mermaid projections",
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
        assert!(!html.contains("http://"));
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
    fn visual_resource_remains_self_contained_and_protocol_shaped() {
        assert!(VISUAL_URI.starts_with("ui://"));
        assert_protocol_shape(VISUAL_HTML, "mermaid");
    }

    #[test]
    fn advertised_resources_are_unique_and_resolvable() {
        assert_eq!(APP_RESOURCES.len(), 2);
        assert_ne!(APP_RESOURCES[0].uri, APP_RESOURCES[1].uri);
        for resource in APP_RESOURCES {
            assert_eq!(resource_for_uri(resource.uri), Some(resource));
            assert_eq!(
                resource_for_uri(resource.uri).map(|resolved| resolved.html.len()),
                Some(resource.html.len())
            );
        }
        assert!(resource_for_uri("ui://nmemory/missing").is_none());
    }
}
