# check.sh 템플릿

아래를 복사한 뒤 모든 <placeholder>를 바꾼다.

```bash
#!/usr/bin/env bash
# Verifier for loop <name>. The last line is exactly one of:
#   LOOP-DONE | LOOP-CONTINUE <reason> | LOOP-BLOCKED <reason>
set -uo pipefail
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="<workspace path>"
LOG="$LOOP_DIR/last_check.log"

blocked()  { echo "LOOP-BLOCKED $*";  exit 2; }
continue_() { echo "LOOP-CONTINUE $*"; exit 1; }

cd "$WORKSPACE" || blocked "cannot cd to $WORKSPACE"

# 1. Environment checks. A system problem is BLOCKED, not CONTINUE.
command -v <tool> >/dev/null 2>&1 || blocked "<tool> not found"

# 2. Run the check and keep the log.
<check command> >"$LOG" 2>&1
rc=$?

# 3. Classify. Environment errors stop the loop.
if grep -Eqi 'permission denied|unauthorized|not authorized|quota exceeded|INFRA_FAILURE|no space left|connection refused|certificate' "$LOG"; then
  blocked "environment error, see $LOG"
fi
[ "$rc" -eq 0 ] || continue_ "check failed (rc=$rc), see $LOG"

# 4. Hill-climb only: also require the metric target.
# best=$(awk -F'\t' '$2=="accepted"{print $3}' "$LOOP_DIR/results.tsv" | sort -n | head -1)
# [ -n "$best" ] && awk -v b="$best" -v t="<target>" 'BEGIN{exit !(b<=t)}' || continue_ "best=${best:-none} target=<target>"

echo "LOOP-DONE"
```
