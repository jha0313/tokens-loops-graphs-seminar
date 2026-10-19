# measure.sh 템플릿

아래를 복사한 뒤 모든 <placeholder>를 바꾼다.

```bash
#!/usr/bin/env bash
# Measure <metric> N times. Prints the median and spread, and appends a row to results.tsv.
# Usage: N=3 bash measure.sh <label>   (label: baseline | candidate | accepted | rejected)
set -uo pipefail
LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="<workspace path>"
N="${N:-3}"
LABEL="${1:-candidate}"
cd "$WORKSPACE" || { echo "LOOP-BLOCKED cannot cd to $WORKSPACE"; exit 2; }

vals=()
for i in $(seq "$N"); do
  start=$(date +%s.%N)
  <metric command> >>"$LOOP_DIR/measure.log" 2>&1 || { echo "LOOP-BLOCKED metric command failed, see $LOOP_DIR/measure.log"; exit 2; }
  end=$(date +%s.%N)
  vals+=("$(python3 -c "print($end - $start)")")
done

python3 - "$LABEL" "$LOOP_DIR/results.tsv" "${vals[@]}" <<'EOF'
import statistics, sys, time
label, out, *v = sys.argv[1:]
v = [float(x) for x in v]
med, spread = statistics.median(v), max(v) - min(v)
with open(out, "a") as f:
    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{label}\t{med:.1f}\t{spread:.1f}\t{','.join(f'{x:.1f}' for x in v)}\n")
print(f"median={med:.1f}s spread={spread:.1f}s n={len(v)}")
EOF
```
