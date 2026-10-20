"""Shared byte and cardinality ceilings for untrusted wire and durable state.

the oracle's, verbatim: the first attempt's file carried numbers the oracle does
not have (identifier bytes 64 against the oracle's 512) and names it never
defined, so every constant here is quoted from the contract rather than remembered.
"""

MAX_DURABLE_ROW_BYTES = 64 * 1024
MAX_DURABLE_IDENTIFIER_BYTES = 512
MAX_DURABLE_PATH_BYTES = 4 * 1024
MAX_LAST_ITEM_BYTES = 240
MAX_DURABLE_OBSERVATION_BYTES = 16 * 1024

MAX_FILES_PER_ITEM = 256
MAX_METHOD_BYTES = 256
