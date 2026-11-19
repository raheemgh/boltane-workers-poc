#!/usr/bin/env bash
# Collects the cf-worker files that belong in the Claude Project's Knowledge into ONE folder,
# already renamed to their flat "cfw_*" Knowledge names (see 00-file-map.md).
#
# Run from the cf-worker folder:
#     cd /workspaces/boltane-workers-poc/files12/cf-worker
#     bash collect_knowledge_files.sh
#
# It only COPIES files into ./knowledge_upload/ — nothing is changed or deleted, and it never
# touches .dev.vars (your secrets). Lines that say MISSING are files that are not in this
# folder; look for them in the downloads of the earlier stage chats.
# When you are done uploading, remove the folder:  rm -rf knowledge_upload

set -u
OUT="knowledge_upload"
mkdir -p "$OUT"
ok=0; missing=0

while read -r src flat; do
  [ -z "${src:-}" ] && continue
  if [ -f "$src" ]; then
    cp "$src" "$OUT/$flat"
    printf 'OK       %-52s -> %s\n' "$src" "$flat"
    ok=$((ok + 1))
  else
    printf 'MISSING  %-52s (would be %s)\n' "$src" "$flat"
    missing=$((missing + 1))
  fi
done <<'LIST'
pyproject.toml                                              cfw_pyproject.toml
wrangler.jsonc                                              cfw_wrangler.jsonc
src/entry.py                                                cfw_entry.py
src/app/main.py                                             cfw_main.py
src/app/config.py                                           cfw_config.py
src/app/routers/health.py                                   cfw_routers_health.py
src/app/routers/webhook.py                                  cfw_routers_webhook.py
src/app/routers/debug_settings.py                           cfw_routers_debug_settings.py
src/app/routers/debug_supabase.py                           cfw_routers_debug_supabase.py
src/app/routers/debug_meta.py                               cfw_routers_debug_meta.py
src/app/routers/debug_ai.py                                 cfw_routers_debug_ai.py
src/app/services/token_limits.py                            cfw_services_token_limits.py
src/app/services/pii_masking.py                             cfw_services_pii_masking.py
src/app/services/meta_client.py                             cfw_services_meta_client.py
src/app/services/supabase_client.py                         cfw_services_supabase_client.py
src/app/services/ai_client.py                               cfw_services_ai_client.py
.dev.vars.example                                           cfw_dev_vars_example.txt
tooling/local-test-support/workers.py                       cfw_tooling_workers_stub.py
tooling/local-test-support/memory_supabase.py               cfw_tooling_memory_supabase.py
test_stage0_ports.py                                        cfw_test_stage0_ports.py
test_stage1_settings.py                                     cfw_test_stage1_settings.py
test_stage2_supabase_unit.py                                cfw_test_stage2_supabase_unit.py
test_stage2_supabase_real.py                                cfw_test_stage2_supabase_real.py
test_stage3_meta.py                                         cfw_test_stage3_meta.py
test_stage3_offline.py                                      cfw_test_stage3_offline.py
test_pii_masking_general.py                                 cfw_test_pii_masking_general.py
test_stage4_offline.py                                      cfw_test_stage4_offline.py
test_stage4_real.py                                         cfw_test_stage4_real.py
probe_stage4_tokens.py                                      cfw_probe_stage4_tokens.py
LIST

echo
echo "copied: $ok   missing: $missing   (folder: $PWD/$OUT)"
echo "Not included on purpose: .dev.vars, stage4_*.json, stage4_report.txt"
