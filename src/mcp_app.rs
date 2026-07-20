//! Self-contained MCP App resource for `memory_visual`.
//!
//! The view has no external dependencies and requests no network, storage, or
//! device permissions. It is progressive enhancement: `memory_visual` keeps
//! returning its existing text payload for hosts without MCP Apps support.

pub const VISUAL_URI: &str = "ui://nmemory/visual";
pub const MIME_TYPE: &str = "text/html;profile=mcp-app";

/// A small, dependency-free MCP Apps view. The host owns sandboxing; the view
/// only speaks JSON-RPC over `postMessage` and renders escaped text.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_resource_is_self_contained_and_protocol_shaped() {
        assert!(VISUAL_URI.starts_with("ui://"));
        assert_eq!(MIME_TYPE, "text/html;profile=mcp-app");
        assert!(VISUAL_HTML.starts_with("<!doctype html>"));
        assert!(VISUAL_HTML.contains("ui/initialize"));
        assert!(VISUAL_HTML.contains("ui/notifications/initialized"));
        assert!(VISUAL_HTML.contains("ui/notifications/tool-result"));
        assert!(!VISUAL_HTML.contains("<script src="));
        assert!(!VISUAL_HTML.contains("https://"));
        assert!(!VISUAL_HTML.contains("http://"));
    }
}
