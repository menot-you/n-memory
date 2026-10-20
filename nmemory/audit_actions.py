"""The action names the journal records: a data contract independent of tool names.

Every audit event (`append_audit`) and every exported line (`"event"`) names its
action with one of these values. The journal is hash-chained and exports are
imported back, so a value here is part of the sealed data, and its readers (the
replay, the coverage leg, the importer) meet exactly these bytes. Renaming a
tool therefore changes what it is called on the wire and never what the
journal records: `nmemory_capsule_purge` records `FORGET`, exactly as
`memory_purge` recorded `memory_forget` before it.
"""

from __future__ import annotations

ALIAS = "memory_alias"
CLASSIFY = "memory_classify"
CONSOLIDATE = "memory_consolidate"
DOD = "memory_dod"
FORGET = "memory_forget"
IMPORT = "memory_import"
INDEX = "memory_index"
INGEST = "memory_ingest"
OUTCOME = "memory_outcome"
PIN = "memory_pin"
REANCHOR = "memory_reanchor"
RELATE = "memory_relate"
REVIEW = "memory_review"
SESSION_FINISH = "memory_session_finish"
SESSION_START = "memory_session_start"
