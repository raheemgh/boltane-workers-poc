# المرحلة 6 — دليل التشغيل (خطوة بخطوة)

كل المسارات داخل `/workspaces/boltane-workers-poc/files12/cf-worker` (اختصاراً `cf-worker`).
نفّذ خطوة واحدة، ثم أرسل لي مخرجاتها (صورة شاشة تكفي) قبل التالية.

**لا تُعتبر المرحلة مكتملة** حتى تؤكد أنت نتائج الاختبارات الحقيقية أدناه.

---

## ما الذي تغيّر في الشيفرة (ولماذا هذا كل شيء)

- `webhook.py` **لم يتغير**: فرقه عن Render ثلاثة أسطر فقط (استدعاء `get_settings()` بدل `settings`). التحقق: `diff` على الملفين، وفحص AST في `test_stage6_routes.py`. أي أن بنود المهمة 1 و2 و3 (GET التحقق، HMAC، جدولة المهمة الخلفية) منفَّذة أصلاً منذ المرحلة 1.
- `main.py` تغيّر: أُضيف `webhook.router`، وأُزيلت المسارات المؤقتة الأربعة (`debug_settings` و`debug_supabase` و`debug_meta` و`debug_ai`) من الاستيراد والتضمين، فلا توجد `/debug/*` على الـWorker المنشور.
- ملفات `debug_*.py` نفسها **باقية على القرص** عمداً: اختبارات المراحل 2 و3 تحمّلها بالمسار (`test_stage2_supabase_unit.py` و`test_stage2_supabase_real.py` و`test_stage3_meta.py`). حذفها يكسر تلك الاختبارات. نقلها خارج `src/` يؤجَّل للمرحلة 7 (تكييف الاختبارات) ما لم تقرر غير ذلك.

---

## الخطوة 1 — وضع الملفات وتشغيل الاختبار الأوفلاين

الملفات المنزّلة من المتصفح تبقى على جهازك، لا في Codespaces. لذلك صار هناك ملف واحد يضع كل شيء مكانه مهما كانت أسماء الملفات:

1. في VS Code (Codespaces) اسحب الملف `install_stage6.py` وأفلته على مجلد `cf-worker` في الشريط الجانبي (Explorer).
2. في الطرفية:

```
cd /workspaces/boltane-workers-poc/files12/cf-worker
python install_stage6.py
```

المتوقع: ثلاثة أسطر `REPLACED`/`CREATED` (لـ`src/app/main.py` و`test_stage6_routes.py` و`test_stage6_deployed.py`) كل منها بكلمة `compiles`، وفي النهاية `Done.`. إن كنت في مجلد خاطئ يرفض ويخبرك. القديم محفوظ في git (`git diff src/app/main.py`).

3. ثم:

```
source ~/venv-tests/bin/activate
PYTHONPATH=tooling/local-test-support:src python test_stage6_routes.py
```

المتوقع: كل الأسطر `PASS`، والسطر الأخير `23/23 checks passed` (العدد الإجمالي كما ظهر في أول تشغيل حقيقي؛ ومع سطر `SKIPPED` إن لم تكن نسخة Render في `files12/app` يقلّ العدد بفحوص التطابق).
سيظهر أثناء التشغيل سطر تحذير من Starlette عن `httpx` وعدة أسطر `rejected POST /webhook: missing or invalid X-Hub-Signature-256`: كلها متوقعة ولا تعني فشلاً (الأخيرة تسجّلها شيفرة `webhook.py` عند رفض كل توقيع خاطئ يرسله الاختبار عمداً).
اختبار المسارات لم أستطع تشغيله في بيئتي (لا fastapi ولا شبكة)، فهذا أول تشغيل له. أي فشل أرسله كما هو.

ثم اختبار المرحلة 5 (إلزامي بعد أي تغيير):

```
python test_stage5_firewall.py
```

المتوقع: `29/29` عند وجود Render، أو `26/26` بدونه.

## الخطوة 2 — حجم الحزمة قبل النشر

صفحة حدود Cloudflare الرسمية (محدّثة 12 أكتوبر 2026): حجم الـWorker **64 MiB قبل الضغط** على الخطتين ولا حد للحجم المضغوط، ووقت بدء التشغيل (startup) **ثانية واحدة**، والـCPU لكل طلب **10 ms** على المجانية.
قياسك: 19177 KiB قبل تعديل 2ب و10486 KiB بعده، وكلاهما تحت الحد بكثير. الحد الذي ظهر لي سابقاً (3 MB مضغوطة) كان قديماً، فتعديل 2ب لم يكن لازماً لأجل الحجم، لكنه يبقى لأنه يزيل نحو 8.5 MB من حزم لا نستعملها ويقلّل عمل البدء.

```
uv run pywrangler deploy --dry-run
```

المتوقع: سطر مثل `Total Upload: ... / gzip: ...`. أرسل لي الرقم. (استخدام `--dry-run` مع pywrangler رأيته في مشاريع أخرى ولم أجرّبه هنا؛ إن رفضه أخبرني بالرسالة.)

## الخطوة 2ب — تقليص الحزمة: استبدال `supabase` بـ`postgrest` وحدها

السبب (مقاس عندك): `cryptography` وتوابعها نحو 8 MB، وتأتي فقط عبر `supabase_auth` ← `pyjwt[crypto]`، أي جزء المصادقة الذي لا نستعمله. `postgrest` وحدها لا تطلبها.

1. ارفع `install_stage6b.py` إلى مجلد `cf-worker` (سحب وإفلات)، ثم:

```
cd /workspaces/boltane-workers-poc/files12/cf-worker
python install_stage6b.py
```

المتوقع: `REPLACED` لـ`supabase_client.py` و`PATCHED` لـ`pyproject.toml` ولـ`test_stage2_supabase_unit.py`. المثبّت **كل شيء أو لا شيء**: إن اختلف ملفك عن نسخة Knowledge يرفض ولا يكتب شيئاً.

2. اختبارات أوفلاين (بنفس البيئة):

```
source ~/venv-tests/bin/activate
PYTHONPATH=tooling/local-test-support:src python test_stage1_settings.py
PYTHONPATH=tooling/local-test-support:src python test_stage2_supabase_unit.py
python test_stage5_firewall.py
```

المتوقع: كل أسطر PASS في الثلاثة، والمرحلة 5 تنتهي بـ`29/29` (أو `26/26` بدون Render).

3. فحص التكافؤ في المهلة (سطر واحد):

```
python -c "from supabase.lib.client_options import AsyncClientOptions as O; from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT as D; print(O().postgrest_client_timeout, D)"
```

المتوقع: قيمتان متطابقتان. إن اختلفتا أو فشل الأمر أرسل المخرجات قبل المتابعة.

4. حزمة نظيفة وقياس جديد:

```
rm -rf python_modules
uv run pywrangler deploy --dry-run
```

المتوقع: `Total Upload` أقل بكثير من 65536 KiB (64 MiB). وقد قسته أنت: 10486 KiB.

## الخطوة 2ج — محاكي Meta (بلا Meta، بلا شبكة، بلا تكلفة)

يعيد تشغيل نحو 54 حالة واقعية (نصوص، أنواع غير نصية، إشعارات حالة، أشكال شاذة، تكرار وتزامن، أعطال Meta وOpenRouter وSupabase) عبر الخط الحقيقي، ويسجّل ما كان سيصل للعميل. التفاصيل في `stage6-meta-simulation-plan.md`.

1. ارفع `install_stage6c.py` إلى مجلد `cf-worker` (سحب وإفلات)، ثم:

```
cd /workspaces/boltane-workers-poc/files12/cf-worker
python install_stage6c.py
source ~/venv-tests/bin/activate
python test_stage6_meta_sim.py
```

المتوقع: سطر لكل حالة بالتصنيف `PASS` أو `GAP` أو `FAIL`، ثم ملخص وقائمة «KNOWN GAPS».
ثم اختبار التخريب (يكسر المحرك عمداً بـ14 طريقة، ويجب أن يفشل الاختبار في كل مرة): `python test_stage6_meta_sim.py --sabotage` والمتوقع `14/14 deliberate breakages detected`. الحالات `GAP` ليست أعطالاً في الهجرة بل سلوك حالي معروف (نفسه على Render). أما `FAIL` فيعني أن توقعي من قراءة الشيفرة خاطئ: أرسله كما هو.
يكتب أيضاً `stage6_meta_sim_report.json` (لا يُرفع إلى git).

## الخطوة 3 — مشروع Supabase للاختبار وصف المتجر

أولاً تحقق أن المشروع **غير موقوف**: Supabase توقف مشاريع الخطة المجانية ذات النشاط القليل خلال 7 أيام، ويمكن استعادتها من اللوحة (Dashboard ← المنظمة ← المشروع الموقوف ← Restore project). المشروع الموقوف يُفشل كل طلب من الـWorker، فيبدو كأن المهمة الخلفية سقطت وهي لم تبدأ أصلاً. بعد الاستعادة افتح المشروع مرة واحدة وتأكد أن جدولي `stores` و`conversation_windows` موجودان بالأعمدة الحالية (آخر `stores_table.sql`).

قرارك: استعمل المشروع الأساسي بصف متجر مؤقت واحد (`stage6-test-pn`) يُحذف بعد الانتهاء. نفّذ في SQL Editor:

```sql
insert into stores (phone_number_id, store_name, system_prompt, access_token, package, is_trial, is_api_free)
values ('stage6-test-pn', 'stage6-test',
        'You are TGF Test, the assistant for TGF Test. You provide information about the menu, including quick sandwiches like beef and chicken burgers, grilled sandwiches, skewers, and side dishes. You assist customers with their orders and inquiries in a professional manner.',
        'stage6-fake-token-phase-a', 'pro', false, true);
```

(في المرحلة أ التوكن مزيّف عمداً: إرسال واتساب سيفشل بـ401 ويُسجَّل ولا يُرمى، وهذا سلوك `meta_client` الموثّق. أما الوصول إلى `add_conversation_tokens` فيثبت أن الخط عمل كله حتى ما قبل الإرسال.)
بعد الانتهاء: `delete from stores where phone_number_id = 'stage6-test-pn';` (النوافذ تُحذف تلقائياً).

## الخطوة 4 — النشر والأسرار

المصادقة: مفتاح API من لوحة Cloudflare (My Profile ← API Tokens ← قالب «Edit Cloudflare Workers»)، يُحفظ في جلسة الطرفية فقط ولا يُرسل لأحد:

```
export CLOUDFLARE_API_TOKEN='ضع-المفتاح-هنا'
npx wrangler whoami
```

ثم من مجلد `cf-worker`:

```
uv run pywrangler deploy
```

المتوقع: يطبع رابطاً بصيغة `https://boltane-core-engine.<نطاقك>.workers.dev`. (اسم الـWorker من `wrangler.jsonc`؛ النطاق الفرعي لا أعرفه.)
ثم الأسرار الخمسة (يطلب كل أمر القيمة، والقيم نفسها التي ستضعها في `.dev.vars`):

```
npx wrangler secret put META_VERIFY_TOKEN
npx wrangler secret put META_APP_SECRET
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_KEY
npx wrangler secret put OPENROUTER_DEFAULT_KEY
```

`SUPABASE_URL/KEY` = المشروع الأساسي (قرارك). في المرحلة أ يصلح أي نص لـ`META_VERIFY_TOKEN` و`META_APP_SECRET` بشرط أن يطابق ما في `.dev.vars` محلياً.

## الخطوة 5 — فحوص بلا Meta على الـWorker المنشور

في `.dev.vars` (محلياً، لا يُرفع): `META_VERIFY_TOKEN` و`META_APP_SECRET` و`SUPABASE_URL` و`SUPABASE_KEY` بقيم الأسرار نفسها، و`META_TEST_PHONE_NUMBER_ID=stage6-test-pn` و`META_TEST_RECIPIENT=15551230001`.

```
python test_stage6_deployed.py check --url https://<رابط-الـWorker>
```

المتوقع: 12 فحصاً `PASS`، منها رفض 4 أنواع من التواقيع الخاطئة بـ403 بنص رسالتنا نحن، وكون مسارات `/debug/*` تُرجع 404.

## الخطوة 6 — اختبار المهمة الخلفية (جوهر المرحلة)

نافذة ثانية للسجلات: `npx wrangler tail`. ثم:

```
python test_stage6_deployed.py deliver --url https://<رابط-الـWorker> --count 10 --gap 5
python test_stage6_deployed.py deliver --url https://<رابط-الـWorker> --count 5 --gap 90
python test_stage6_deployed.py deliver --url https://<رابط-الـWorker> --count 3 --long
```

الأول: 10 تسليمات متتالية. الثاني: فواصل 90 ثانية لملاقاة بدايات باردة. الثالث: الرسالة الطويلة (أبطأ مسار للنموذج؛ سبق أن بلغ خط الأنابيب 108 ثوانٍ).
لكل تسليم يطبع: كود HTTP وزمن الرد (المطلوب أقل من 3 ثوانٍ تقريباً)، وبعد كم ثانية **من الرد** بدأ الخط وانتهى (من Supabase).

| الحكم | معناه |
|---|---|
| `COMPLETE` | وصل الخط إلى `add_conversation_tokens` بعد إرسال الرد = المهمة الخلفية لم تُسقَط |
| `STARTED_NOT_FINISHED` | بدأ ولم يكتمل: إما أُلغيت المهمة، أو ردّ `GRACEFUL` (توكنات 0 — موثّق من المرحلة 4)، أو بلغ المتجر سقفه. الفيصل: `wrangler tail` ونص رد واتساب |
| `NOT_STARTED` | لم يبدأ أصلاً: سقط بعد الرد أو فشل مبكر (مثلاً المتجر غير موجود) |

**قاعدة التوقف (من المهمة):** أي تسليم ليس `COMPLETE` ولا تفسره حالة `GRACEFUL` الموثّقة ← توقف وأبلغ، ولا يُعدّ ذلك «تقطعاً مقبولاً».

الحدود المعروفة (من صفحة Cloudflare الرسمية): ما بعد الرد عبر `waitUntil` يعمل **حتى 30 ثانية فقط** بعد إرسال الرد، بينما خط الأنابيب قد يطول (حتى 108 ثوانٍ في قياس المرحلة 4). السكربت يطبع ملاحظة إن اكتمل خط بعد أكثر من 30 ثانية (دليل أن الإلغاء لم يحدث)، وإن توقف خط بعد 30 ثانية من بدايته فهذا **متسق مع** الإلغاء لكنه ليس إثباتاً له. إن ظهر إسقاط، فالخيارات (طوابير Cloudflare أو Workflows أو انتظار الخط داخل الطلب) قرار تصميم تأخذه أنت قبل أي شيء.

نتائج كل تشغيل تُكتب في `cf-worker/stage6_deployed_results.json` (لا تُرفع إلى git).

## الخطوة 7 — المرحلة ب (تحتاج Meta فعلياً) — مؤجلة: لا يتوفر وصول Meta الآن، وتحل محلها محاكاة Meta (انظر stage6-meta-simulation-plan.md)

شروط لا أملكها ولا يمكن تجاوزها بالشيفرة:
1. `META_APP_SECRET` و`META_VERIFY_TOKEN` الحقيقيان من تطبيق Meta، ووصول إلى لوحة Meta لإدخال رابط الـWorker واختبار التحقق (GET) من داخلها.
2. رقم اختبار: `phone_number_id` و`access_token` صالحان، ومستلم مسموح في قائمة الاختبار.
3. نافذة 24 ساعة: يجب أن يكون المستلم قد راسل رقم الاختبار خلال آخر 24 ساعة، وإلا رفضت Meta الرد الحر.

عندها: تحديث صف المتجر في مشروع الاختبار (`phone_number_id` و`access_token` الحقيقيان) و`.dev.vars`، وأعد الخطوة 6 (10 تسليمات على الأقل)، ثم **عدّ ردود واتساب على الهاتف**: السكربت يبيّن العدد المتوقع، لكن الهاتف وحده يثبت التسليم.
ولا تغيّر رابط Webhook الإنتاجي الذي يخدمه Render، وفق قيد المهمة.

## ما لم يُفحص في هذه المرحلة (لا تدّعِ عكسه)

- تزامن رسالتين متزامنتين لمتجر واحد (تحديث العدّاد غير ذرّي).
- سلوك الـWorker تحت حمل حقيقي أو من حدود الذاكرة.
- هل تظهر رسائل `logger.info` في `wrangler tail`: مستوى التسجيل الافتراضي في بايثون WARNING، ولم أتحقق من إعداد Workers. لذلك يعتمد السكربت على علامات Supabase لا على السجلات.
- `logger.info("received webhook payload: %s", payload)` يسجّل الحمولة كاملة (أرقام العملاء ونصوصهم) إن ظهر INFO. موجود في Render أيضاً؛ موثّق لا معدَّل.
