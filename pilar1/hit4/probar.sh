#!/usr/bin/env bash
# Compara ./md5 contra md5sum: los ejemplos del RFC 1321 y los largos donde cambia el relleno.
cd "$(dirname "$0")"

casos=(
    "" "a" "abc" "message digest" "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "12345678901234567890123456789012345678901234567890123456789012345678901234567890"
    "$(printf 'x%.0s' {1..55})" "$(printf 'x%.0s' {1..56})"
    "$(printf 'x%.0s' {1..64})" "$(printf 'x%.0s' {1..65})"
    "ñandú"
)

fallas=0
for t in "${casos[@]}"; do
    gpu=$(./md5 "$t")
    cpu=$(printf '%s' "$t" | md5sum | cut -d' ' -f1)
    if [ "$gpu" = "$cpu" ]; then estado=OK; else estado=ERROR; fallas=$((fallas + 1)); fi
    printf '%-5s %s  largo %3d  "%.30s"\n' "$estado" "$gpu" "$(printf '%s' "$t" | wc -c)" "$t"
done
echo "$((${#casos[@]} - fallas))/${#casos[@]} iguales a md5sum"
exit $((fallas > 0))
