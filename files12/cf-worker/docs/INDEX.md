# فهرس الوحدات

مولَّد آلياً بواسطة `python3 tooling/conventions/check_conventions.py --write-docs`؛ لا يُحرَّر يدوياً.

| المسار | الغرض | الطبقة | المرحلة | الأعلام | ملف الاختبار |
|---|---|---|---|---|---|
| src/app/v2/adapters/supabase_rest.py | calls Supabase PostgREST functions over HTTPS and maps every failure to a V2 error code. | adapters | V2-02B | none | tests/v2/adapters/test_supabase_rest.py |
| src/app/v2/adapters/worker_env.py | reads the Worker env into typed V2 settings: the feature flags and the Supabase connection. | adapters | V2-02B | v2_pipeline | tests/v2/adapters/test_worker_env.py |
| src/app/v2/core/errors.py | the single V2 error taxonomy: stable codes, categories, retry and alert hints. | core | V2-01 | none | tests/v2/core/test_errors.py |
| src/app/v2/core/events.py | the typed result of reading one Meta webhook delivery: events, account events, issues. | core | V2-02A | none | tests/v2/core/test_events.py |
| src/app/v2/core/flags.py | the feature-flag registry and the resolver (global list, allowlist, per-store override). | core | V2-01 | none | tests/v2/core/test_flags.py |
| src/app/v2/core/log_schema.py | the one structured log line per message: allowed fields, value checks, JSON text. | core | V2-01 | none | tests/v2/core/test_log_schema.py |
| src/app/v2/core/safe.py | small readers for untrusted JSON that never raise: dict, list, text, int, number. | core | V2-02A | none | tests/v2/core/test_safe.py |
| src/app/v2/core/store_config.py | validates the per-store settings columns added by migration 001. | core | V2-01 | none | tests/v2/core/test_store_config.py |
| src/app/v2/inbound/content.py | reads what one Meta message carries: text, reply title, location, media, reaction, system, ... | domain | V2-02A | none | tests/v2/inbound/test_content.py |
| src/app/v2/inbound/identity.py | reads who sent a message or who a status went to: phone, BSUID, parent BSUID, username, name. | domain | V2-02A | none | tests/v2/inbound/test_identity.py |
| src/app/v2/inbound/inbox.py | the durable inbox: which events are kept (ADR-10) and thin calls to the four database functions. | domain | V2-02B | none | tests/v2/inbound/test_inbox.py |
| src/app/v2/inbound/normalizer.py | turns one Meta webhook delivery into typed events. Never raises. | domain | V2-02A | none | tests/v2/inbound/test_normalizer.py |
| src/app/v2/pipeline/build.py | wires the adapters to the ports: the only pipeline module allowed to import adapters. | pipeline | V2-02B | none | tests/v2/pipeline/test_build.py |
| src/app/v2/pipeline/entry.py | the one door V1 uses to offer a verified webhook delivery to V2: store it, or step aside. | pipeline | V2-02B | v2_pipeline | tests/v2/pipeline/test_entry.py |
| src/app/v2/ports/db.py | the database port: one call to a database function. | ports | V2-02B | none | tests/v2/ports/test_db.py |
| tooling/conventions/check_conventions.py | the V2 conventions checker: runs every rule and generates the docs. | tooling | V2-00B | none | tests/v2/conventions/test_check_conventions.py |
| tooling/conventions/conv_config.py | constants of the conventions checker: layers, limits, vocabularies. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_docs.py | generates docs/INDEX.md and docs/KNOWLEDGE_MAP.md, checks them, exports Knowledge. | tooling | V2-00B | none | tests/v2/conventions/test_conv_docs.py |
| tooling/conventions/conv_flags.py | feature-flag rules: registry shape, registered use, header Flags (5.7). | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_header.py | checks the mandatory module docstring header (section 2). | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_layers.py | checks that imports respect the layer table (section 3). | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_layout.py | repo layout rules: folders inside the map, SQL migrations paired with rollbacks. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_metrics.py | size and complexity limits: file, function, nesting, params, branches. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_model.py | data types, path classification and source loading for the checker. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_rules_code.py | AST rules: import-time effects, print, except handlers, free-text errors. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_rules_content.py | content rules: TODO ids, secrets, select *, logged text, naming. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/conventions/conv_testkit.py | builds throw-away repo trees so the checker's rules can be tested. | tooling | V2-00B | none | tests/v2/conventions/test_conv_rules.py |
| tooling/installers/build_installer.py | packs a staged file tree into one self-contained install_v2_<nn>.py. | tooling | V2-00B | none | tests/v2/installers/test_build_installer.py |
| tooling/installers/install_v2_00b.py | installs the V2-00B files all-or-nothing (generated by build_installer). | tooling | V2-00B | none | tests/v2/installers/test_build_installer.py |
| tooling/installers/install_v2_01.py | installs the V2-01 files all-or-nothing and prints proof (hand-written, see docs/stages/V2-01.md). | tooling | V2-01 | none | tests/v2/installers/test_install_v2_01.py |
| tooling/installers/install_v2_02a.py | installs the V2-02A files all-or-nothing (generated by build_installer). | tooling | V2-02A | none | tests/v2/installers/test_build_installer.py |
| tooling/installers/install_v2_02b.py | installs the V2-02B files all-or-nothing (generated by build_installer). | tooling | V2-02B | none | tests/v2/installers/test_build_installer.py |
| tooling/installers/installer_template.py | all-or-nothing installer logic reused by every install_v2_<nn>.py. | tooling | V2-00B | none | tests/v2/installers/test_installer_template.py |
| tooling/local-test-support/v2_inbox_model.py | reference model of the inbox SQL functions, a fake database port, and the shared scenarios. | tooling | V2-02B | none | tests/v2/inbound/test_inbox.py |
