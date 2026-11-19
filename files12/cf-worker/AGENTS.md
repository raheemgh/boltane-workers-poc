# AGENTS.md: كيف يقرأ أي AI هذا المستودع (`cf-worker/`)

**المشروع:** Boltane Core Engine على Cloudflare Python Workers. **V1 مجمّد ويعمل.** V2 يُبنى بجانبه تحت `src/app/v2/` خلف أعلام كلها `false` افتراضياً، و`false` تعني سلوك V1 حرفياً.

## ابدأ من هنا (بهذا الترتيب)

1. `docs/INDEX.md`: جدول بكل وحدة (الغرض، الطبقة، المرحلة، الأعلام، ملف الاختبار).
2. ترويسة الوحدة المطلوبة (docstring أعلى الملف)، ثم **ملف اختبارها**: هو التوثيق الفعلي لسلوكها.
3. `docs/stages/V2-xx.md` للمرحلة المذكورة في الترويسة (لماذا وكيف وما اختُبر).
4. `docs/adr/` لأي قرار معماري، و`docs/ARCHITECTURE.md` للصورة العامة.
5. القواعد الكاملة: `docs/reference/v2-conventions.md`. أسماء ملفات Knowledge المسطّحة: `docs/KNOWLEDGE_MAP.md`.

إن نقص شيء فقل ما ينقص ومساره الكامل بدل أن تخمّن.

## القواعد التي لا تُكسر (يفرضها `tooling/conventions/check_conventions.py`)

- **لا يُعدَّل ملف موجود قبل أن ترى محتواه الحالي.** تعديل ملف V1 المجمّد يحتاج «سجل تعديل ملف مجمّد» في محضر المرحلة، ونجاح اختبارات الانحدار في الـcommit نفسه.
- **الطبقات:** `core` ثم `ports` ثم `adapters` أو المجالات ثم `jobs` ثم `pipeline`. المجالات لا تستورد بعضها ولا `adapters`. كود V2 لا يستورد V1 إلا عبر `adapters/v1_compat.py`.
- **لا شيء يُنفَّذ عند الاستيراد:** لا ساعة ولا عشوائية ولا `uuid` ولا I/O (سبب رفض النشر 10021).
- **الحدود:** ملف ≤ 400 سطر، دالة ≤ 50، تداخل ≤ 4، معاملات ≤ 6، فروع ≤ 10. الاستثناء الوحيد: `# conv: allow <rule> - <reason> (V2-xx)`.
- **ممنوع:** `print`، و`except:` العاري، و`except Exception` بلا إعادة رفع أو (تسجيل وعدّاد)، وتسجيل نص رسالة العميل، والأسرار داخل الكود، و`select("*")` على `stores`.
- **الأعلام** تُسجَّل في `core/flags.py` بقيمة `False` ووصف ومرحلة مالكة. **الأخطاء** من تصنيف `core/errors.py` برموز ثابتة، لا نص حرّ.
- **قاعدة البيانات:** إضافات فقط. كل ترحيل `sql/migrations/NNN_name.sql` له ترحيل عكسي بالاسم نفسه في `sql/rollbacks/` ويقبل التكرار.
- **اللغة:** الترويسات والتعليقات إنجليزية بسيطة، رسائل العملاء عربية، وثائق المشروع عربية.
- **commit:** `V2-xx: <ما تغيّر>`.
- ما لم تتحقق منه يُكتب صراحةً. لا ادعاء.

## الأوامر (كلها من مجلد `cf-worker/`)

```
cd "$(git rev-parse --show-toplevel)/files12/cf-worker"
python3 tooling/conventions/check_conventions.py               # الحارس: المطلوب exit 0
python3 tooling/conventions/check_conventions.py --write-docs  # يعيد توليد INDEX و KNOWLEDGE_MAP
python3 tooling/conventions/check_conventions.py --rules       # أسماء القواعد
PYTHONPATH=tooling/conventions python3 -m unittest discover -s tests/v2/conventions -v
PYTHONPATH=tooling/installers  python3 -m unittest discover -s tests/v2/installers -v
```

## قبل أن تقول «انتهيت»

الحارس `exit 0` وخلاصته ملصوقة، والمستوى المطلوب لمرحلتك نجح، واختبارات V1 والأعلام كلها `false` خضراء، و`docs/stages/V2-xx.md` كُتب، و`INDEX.md` و`KNOWLEDGE_MAP.md` أُعيد توليدهما. التسليم بمثبّت `tooling/installers/install_v2_<nn>.py` (يُبنى بـ `build_installer.py`).
