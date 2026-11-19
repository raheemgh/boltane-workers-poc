# البنية المعمارية لـ Core Engine V2

**الحالة:** هيكل. تضع V2-01 التفاصيل (سجلات القرارات في `docs/adr/` وتخطيط الوحدات الكامل). ما يلي مأخوذ من `docs/reference/v2-conventions.md` ويفرضه الحارس.

## الفكرة

V1 (`src/app/routers`, `src/app/services`, `main.py`) **مجمّد ويعمل**. V2 يعيش في `src/app/v2/` ويُختار بعلم عام وقائمة `phone_number_id` مسموحة. مع كل الأعلام `false` يكون السلوك مطابقاً لـ V1 حرفياً. نقطة الاتصال الوحيدة: `v2.pipeline`.

## الطبقات وما يحق لكل طبقة استيراده

| الطبقة | المجلدات | تستورد | ممنوع عليها |
|---|---|---|---|
| `core` | `core/` | المكتبة القياسية فقط | أي شيء آخر |
| `ports` | `ports/` | `core` | `adapters` ومكتبات خارجية |
| `adapters` | `adapters/` | `core` و`ports`، ومكتبات خارجية **داخل الدوال** فقط | `domain` و`jobs` و`pipeline` |
| `domain` | `inbound/ gate/ response/ llm/ outbound/ ledger/ memory/ store_data/ handoff/ alerts/ integrations/` | `core` و`ports` ومجلدها هو | `adapters` ومجال آخر |
| `jobs` | `jobs/` | `core` و`ports` والمجالات | `adapters` مباشرة (تُحقَن) |
| `pipeline` | `pipeline/` | الطبقات السابقة | `adapters` إلا في `build.py` وحده |

كود V2 لا يستورد وحدات V1 إلا عبر `adapters/v1_compat.py`. وكود V1 لا يستورد من V2 إلا `app.v2.pipeline`.

## تدفّق الرسالة (يُفصَّل في V2-08)

`inbound` (تطبيع الحمولة) ثم `gate` (الجلسة والسقف والحد) ثم `response` (الصياغة) ثم `llm` ثم `outbound` (الإرسال) ثم `ledger` (التسجيل بعد الإرسال).

## أين أجد ماذا

| أريد | أذهب إلى |
|---|---|
| قائمة الوحدات وأغراضها | `docs/INDEX.md` |
| لماذا بُنيت مرحلة بهذا الشكل | `docs/stages/V2-xx.md` |
| قرار معماري | `docs/adr/` |
| قاعدة كود أو تسمية | `docs/reference/v2-conventions.md` |
| اسم ملف Knowledge ومساره | `docs/KNOWLEDGE_MAP.md` |
