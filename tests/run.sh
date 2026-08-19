#!/usr/bin/env bash
# Run the suite. `./tests/run.sh` for everything offline, `--live` to include
# the ones that hit a real model and cost tokens.
#
# The server must be running: ./restart.sh first. A stale process serving old
# code has burned multiple sessions, and it presents as a fix "not working".
set -uo pipefail
cd "$(dirname "$0")/.."

# test_ic_think and test_engine DO call a real local model despite living
# here — they are free rather than offline, and they are the two that can go
# red under contention. Everything else is genuinely offline.
OFFLINE=(test_frontend test_llm test_comfy test_api test_cards test_comfy_api
         test_library test_prompts test_scenarios test_engine test_ic_think
         test_blocks test_studio test_regex test_wizard test_chats test_gallery
         test_export test_vram_kcpp test_cast test_baton test_lore test_theme test_fixes)
# These call a real backend. test_fixes is in the offline list but is NOT
# harmless: it deletes data/coomkit.sqlite to prove the schema self-heal and
# restores it at exit. Do not run the suite over data you want.
LIVE=(test_vanish test_tool_e2e test_live_chat test_ui_smoke test_phase4)

want=("${OFFLINE[@]}")
[[ "${1:-}" == "--live" ]] && want+=("${LIVE[@]}")

failed=()
for t in "${want[@]}"; do
  printf "%-20s " "$t"
  if out=$(python3 "tests/$t.py" 2>&1); then
    echo "PASS"
  else
    echo "FAIL"
    echo "$out" | tail -8 | sed 's/^/    /'
    failed+=("$t")
  fi
done

echo
if ((${#failed[@]})); then
  echo "FAILED (${#failed[@]}): ${failed[*]}"
  exit 1
fi
echo "all ${#want[@]} passed"
