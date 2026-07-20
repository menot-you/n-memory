//! Self-contained MCP App resource for `memory_visual`.
//!
//! The view has no external dependencies and requests no network, storage, or
//! device permissions. It is progressive enhancement: `memory_visual` keeps
//! returning its existing text payload for hosts without MCP Apps support.

pub const VISUAL_URI: &str = "ui://nmemory/visual";
pub const MIME_TYPE: &str = "text/html;profile=mcp-app";

/// A small, dependency-free MCP Apps view. The host owns sandboxing; the view
/// only speaks JSON-RPC over `postMessage` and renders escaped text.
pub const VISUAL_HTML: &str = r##"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark;--bg:var(--color-background-primary,#fff);--fg:var(--color-text-primary,#171717);--muted:var(--color-text-secondary,#666);--border:var(--color-border-primary,#ddd);--mono:var(--font-mono,ui-monospace,monospace)}
*{box-sizing:border-box}body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:14px/1.45 var(--font-sans,system-ui,sans-serif)}
header{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:8px}h1{font-size:15px;margin:0}.label{color:var(--muted);font-size:12px}
pre{margin:0;padding:12px;border:1px solid var(--border);border-radius:var(--border-radius-md,8px);overflow:auto;font:12px/1.45 var(--mono);white-space:pre}.error{color:var(--color-text-danger,#b42318)}
</style></head><body><header><h1>nMEMORY visual</h1><span class="label">ADVISORY_NOT_AUTHORITY</span></header><pre id="view">Waiting for memory_visual…</pre>
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
