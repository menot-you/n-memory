"""The CLI's command handlers, one module per noun.

`nmemory.cli` owns the argv and the `COMMANDS` table; each handler here,
`cmd_<noun>_<verb>(argv)`, parses the words that follow its command with the one
grammar in `commands.common` and ends the process with `sys.exit` when it fails.
`mcp serve` stays in `cli`, because it is the one command that builds the MCP door.
Nothing here imports `cli` or `server`.
"""
