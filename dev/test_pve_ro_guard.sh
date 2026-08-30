#!/bin/bash
# Regression tests for node-guard/pve-ro-guard. Run locally against a copy of
# the script (not on a real Proxmox node) -- most allowed verbs will
# themselves fail/error since there's no pvesh/qm here, but that's fine: this
# only checks what the GUARD decides (exit 126 = denied before exec, anything
# else = it was allowed through to `exec bash -c`).
#
# Usage: dev/test_pve_ro_guard.sh node-guard/pve-ro-guard
set -u
GUARD="${1:-node-guard/pve-ro-guard}"
pass=0; fail=0

check() {
  local desc="$1" cmd="$2" want="$3"  # want: allow | deny
  local out rc
  out="$(SSH_ORIGINAL_COMMAND="$cmd" bash "$GUARD" 2>&1)"
  rc=$?
  local got="deny"; [ "$rc" -ne 126 ] && got="allow"
  if [ "$got" = "$want" ]; then
    pass=$((pass+1)); echo "PASS - $desc"
  else
    fail=$((fail+1)); echo "FAIL - $desc (wanted $want, got $got) :: $out"
  fi
}

# --- the bug: a literal '|' inside quotes must not fragment the command ----
check "grep -E alternation with quoted pipe"           'journalctl --no-pager | grep -E "a|b|c"' allow
check "grep -i alternation (the exact failure hit)"    'journalctl -b -1 --no-pager | grep -i "142\|shutdown\|power"' allow
check "awk -F with a quoted pipe field separator"      "cat /etc/passwd | awk -F'|' '{print \$1}'" allow
check "double-quoted pipe survives escaped-quote case" 'echo "a\"b|c"' allow

# --- still denies real chaining --------------------------------------------
check "semicolon chaining"        'qm status 142; rm -rf /' deny
check "background/AND chaining"   'qm status 142 && rm -rf /' deny
check "OR chaining"               'qm status 142 || rm -rf /' deny
check "backtick substitution"     'echo `rm -rf /`' deny
check "dollar-paren substitution" 'echo $(rm -rf /)' deny
check "redirect out"              'qm status 142 > /etc/passwd' deny
check "an unquoted pipe to a write verb"  'qm status 142 | tee /etc/passwd' deny
check "unknown verb"              'rm -rf /' deny
check "empty command"             '' deny

# --- ordinary real-world read-only commands still work ---------------------
check "pvesh get"                 'pvesh get /cluster/resources' allow
check "pvesh get with stderr discard" 'pvesh get /cluster/resources 2>/dev/null' allow
check "qm status"                 'qm status 142' allow
check "qm config piped to grep"   'qm config 142 | grep onboot' allow
check "journalctl since/until with spaces" 'journalctl --since "2026-07-21 00:00:00" --until "2026-07-21 12:00:00"' allow
check "multi-stage pipe: cat | grep | wc" 'cat /var/log/syslog | grep error | wc -l' allow
check "sed without -i"            'echo foo | sed "s/foo/bar/"' allow
check "sed -i still denied inside a pipe" 'cat /etc/passwd | sed -i "s/foo/bar/"' deny
check "find -delete still denied" 'find /tmp -delete' deny
check "tee still denied"          'qm status 142 | tee /tmp/x' deny

echo ""
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ]
