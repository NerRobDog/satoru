#!/bin/bash
# Run the suite on every Python this Mac has.
#
# Not a flourish: `python3` on the machines this project is developed on is 3.8,
# a clean macOS ships 3.9 with the Command Line Tools, and `tomllib` only arrives
# in 3.11. The hand-rolled TOML reader is therefore the production path for most
# users, and the differential test against tomllib only runs on 3.11+. A suite
# that passes on one interpreter has proved less than it looks.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

found=0
failed=0
for py in python3 /usr/bin/python3 python3.11 python3.12 python3.13; do
    command -v "$py" > /dev/null 2>&1 || continue
    ver=$("$py" --version 2>&1)
    case " $seen " in *" $ver "*) continue ;; esac
    seen="${seen:-} $ver"
    found=$((found + 1))
    out=$("$py" -m unittest discover -s . -p 'test_*.py' 2>&1)
    if [ $? -eq 0 ]; then
        printf '  %-16s %s\n' "$ver" "$(echo "$out" | tail -1)"
    else
        printf '  %-16s FAILED\n%s\n' "$ver" "$out"
        failed=$((failed + 1))
    fi
done

[ "$found" -gt 0 ] || { echo "no python3 found" >&2; exit 1; }
[ "$failed" -eq 0 ] || exit 1
echo "all green on $found interpreter(s)"
