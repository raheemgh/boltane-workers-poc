# خريطة الملفات (مختصرة)

للتفاصيل الآلية: `docs/INDEX.md` (الوحدات) و`docs/KNOWLEDGE_MAP.md` (أسماء Knowledge المسطّحة). القواعد: `AGENTS.md`.

## V1 (مجمّد، يعمل على Cloudflare)

| المسار تحت `cf-worker/` | دوره |
|---|---|
| `src/entry.py` | نقطة دخول الـWorker (تعيد تصدير `Default`) |
| `src/app/main.py` | تطبيق FastAPI والصنف `Default` |
| `src/app/config.py` | `get_settings()`: الأسرار من `workers.env` |
| `src/app/worker_ctx.py` | تمرير `ctx.waitUntil` إلى المسارات |
| `src/app/routers/webhook.py` | استقبال Meta ومعالجة الرسالة (`_process_message`) |
| `src/app/routers/health.py` | `/health` بلا أي استدعاء خارجي |
| `src/app/services/ai_client.py` | استدعاء OpenRouter وسلسلة النماذج |
| `src/app/services/supabase_client.py` | PostgREST غير المتزامن (المتاجر والنوافذ) |
| `src/app/services/pii_masking.py` | تقنيع أرقام الهواتف قبل OpenRouter |
| `src/app/services/token_limits.py` | حدود الرموز وتقدير الطول |
| `src/app/services/meta_client.py` | إرسال الرد عبر Graph API |
| `wrangler.jsonc` و`pyproject.toml` | الإعداد والتبعيات |
| `stores_table.sql` | المخطط المرجعي لـV1 |

## V2 (جديد، خلف أعلام `false`)

`src/app/v2/` فيه `core` (نقي) و`ports` و`adapters` و`jobs` و`pipeline` والمجالات (`inbound` `gate` `response` `llm` `outbound` `ledger` `memory` `store_data` `handoff` `alerts` `integrations`). الطبقات وما يحق لكل طبقة استيراده: `docs/reference/v2-conventions.md` القسم 3.

حالياً في `core`: `errors.py` (تصنيف الأخطاء) و`flags.py` (الأعلام) و`log_schema.py` (سطر السجل) و`store_config.py` (فحص إعدادات المتجر).

## بقية المستودع

| المسار | محتواه |
|---|---|
| `sql/migrations` و`sql/rollbacks` | ترحيلات مرقّمة وعكسها (`001_core_schema.sql`) |
| `contracts/` | عقود JSON مشتركة مع Ava واللوحة والبوت الإداري |
| `tests/v2/<area>/` | اختبار لكل وحدة؛ المستوى في ترويسة الملف |
| `tooling/conventions` | الحارس `check_conventions.py` |
| `tooling/installers` | مثبّتات المراحل `install_v2_<nn>.py` |
| `tooling/local-test-support` | أدوات الاختبار المحلي (المحاكيات) |
| `docs/adr` و`docs/stages` و`docs/runbooks` و`docs/reference` | القرارات ومحاضر المراحل ودلائل التشغيل والمراجع |

اختبارات V1 والملفات المسطحة القديمة (`test_*.py` و`stage*`) في جذر `cf-worker/` وتبقى كما هي.
