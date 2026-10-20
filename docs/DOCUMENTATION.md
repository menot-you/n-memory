# Public documentation sources

The [export map](export-map.json) owns document destinations for all four repositories.
Each component has its own index, runbook, architecture, and reference.
The component README remains its canonical source and becomes the public root README.
The exporter resolves source links before it writes their final relative destinations.
It must refuse any absent source or local destination.
Never replace a missing local link with a private repository URL.

## Shared guides and community files

Installation, privacy, upgrade, troubleshooting, and roadmap pages apply to all four outputs.
Component runbooks own their specific setup, operation, backup, and restoration steps.
Community files apply at each repository root.
The public changelog contains only approved public entries.
It does not copy private campaign fragments or the full private history.
Citation metadata omits an unapproved release version and date.
The license comes from the existing AGPL-3.0 text.

## Presentation and proof

README figures use the NOTT mark, charcoal, parchment, and copper.
SVG files are editable sources with text alternatives and no external resource requests.
Architecture pages keep their editable Mermaid diagrams in Markdown.
Documentation claims need source declarations, exact inputs, and observed results.
No benchmark ranking or equal-client promise follows from source preparation.
