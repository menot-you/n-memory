# shellcheck shell=bash
# The variables the plugin's shell hooks read under a new name, each old name spelled
# here once. Source it, then `renamed_warn NEW...` before reading NEW.
#
# A hook does NOT refuse, unlike every other reader of a renamed variable: the hooks are
# advisory, run in every live session from the main checkout, and must never take a
# session down. So when an old spelling is set, a hook prints one line naming the new
# variable and runs as it does without a value; the old value is never read.
# `tools/organization.py` holds this table to [map.env].

renamed_old() {
  case $1 in
    NOTT_NMEMORY_BIN) echo NMEMORY_BIN ;;
    NOTT_NMEMORY_PROJECT) echo NMEMORY_PROJECT ;;
    NOTT_SYSTEM1_API_KEY) echo NOTT_TYPESAFE_API_KEY ;;
    NOTT_SYSTEM1_AUDIT) echo NOTT_JEV_AUDIT ;;
  esac
}

# Old spellings retired with no new name of their own, keyed by the variable read in
# their place (RETIRED in renamed.py).
renamed_retired() {
  case $1 in
    NOTT_SYSTEM1_API_KEY) echo TYPESAFE_AI_API_KEY ;;
  esac
}

# renamed_warn NEW... — one line on stderr for each NEW whose old spelling is exported.
# Always status 0: the hook goes on.
renamed_warn() {
  local new old
  for new in "$@"; do
    for old in $(renamed_old "$new") $(renamed_retired "$new"); do
      if printenv "$old" >/dev/null 2>&1; then
        printf 'nott hook: %s was renamed to %s: set %s and unset %s\n' "$old" "$new" "$new" "$old" >&2
      fi
    done
  done
  return 0
}
