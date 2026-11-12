# Boltane V2.0 — سجل البحث والمصادر (تحقّقتُ منه بتاريخ 2026-11-04)

كيف تقرأ الوسوم: **[رسمي]** وثائق الجهة نفسها · **[وسيط]** شريك/مزوّد يلخّص وثائق رسمية · **[تسويقي]** صفحات منتجات منافسين (ادعاءات غير مدقّقة) · **[أكاديمي]** ورقة بحثية.
كل سياسة أو سعر هنا قابل للتغيّر. راجع الرابط الرسمي قبل أن نبني شيئاً عليه. ما لم أستطع تأكيده مكتوب صراحةً في آخر الملف.

---

## أ) Meta / واتساب

| # | ما وجدته | الوسم | المصدر |
|---|---|---|---|
| A1 | **BSUID والأسماء المستعارة:** دعم معرّف المستخدم الخاص بالنشاط (BSUID) مطلوب من كل الشركاء والأعمال المتكاملة مباشرة. المعرّفات بدأت تظهر في الـwebhooks من أوائل أبريل 2026 والإرسال إليها مدعوم من يوليو 2026. حين يتبنّى المستخدم اسماً مستعاراً قد لا يصل رقمه في الـwebhook (إلا بشروط)، وقد يغيب `from` و`wa_id` ويبقى `user_id`. | رسمي | https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/ |
| A1ب | طرح الأسماء المستعارة تدريجي: حجز أسماء الأعمال من أواخر يونيو 2026 وأول موجة دول في 7 يوليو 2026، والطرح مستمر خلال 2026. | وسيط (مدونة مزوّد) | ابحث: "WhatsApp usernames rollout" (لم أثبّت رابطاً واحداً) |
| A2 | **سياسة الذكاء الاصطناعي:** منذ 15 يناير 2026 تمنع شروط Meta مزوّدي الذكاء الاصطناعي من توزيع مساعدين عامّي الأغراض عبر المنصة. بوتات خدمة العملاء لنشاط تجاري محدد تبقى مسموحة. | وسيط/صحافة | المرجع الأصلي: نص WhatsApp Business Solution Terms (لم أثبّت رابطاً واحداً) |
| A3 | **التسعير:** التسعير بالرسالة منذ 1 يوليو 2025. ومنذ **1 أكتوبر 2026** تُحاسَب رسائل الخدمة (ردود البوت الحرة داخل 24 ساعة) بعد أول 1000 مجانية في الشهر لكل رقم نشاط، بسعر الـUtility. وتقارير الشركاء تقول إن الحساب بلا وسيلة دفع لدى Meta يتوقف تسليمه بعد الحد المجاني. أمثلة أسعار (مصادر وسيطة): السعودية Utility/Service حوالي 0.0107$ وتسويقي حوالي 0.0576$؛ الإمارات Utility/Service حوالي 0.0157$ وتسويقي حوالي 0.0576$. | رسمي عبر وسيط (Twilio) + وسطاء | https://help.twilio.com/articles/53100480177819 · https://respond.io/blog/whatsapp-pricing-change-2026 · https://www.flowcall.co/blog/whatsapp-business-api-pricing · الصفحة الرسمية: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing |
| A3ب | تقرير وسيط واحد يقول إن التسويق عبر Cloud API العادي ينتهي في 13 أكتوبر 2027 لصالح Marketing Messages API. لم أؤكده من Meta. | وسيط — غير مؤكد | https://sendseven.com/en/blog/whatsapp-business-news-meta-updates |
| A4 | **سجل التغييرات الرسمي:** مؤشر الكتابة (أبريل 2025) · إعادة محاولة الـwebhook حتى 7 أيام · معرّفات الوسائط صالحة 7 أيام فقط بعد 9 أكتوبر 2025 مع `url` داخل webhook الوسائط · حدود الرسائل صارت على مستوى محفظة الأعمال (أكتوبر 2025) · حدود لعدد الرسائل التسويقية لكل مستخدم · تفضيلات المستخدم لإيقاف التسويق (خطأ 131050) · حالة `played` للرسائل الصوتية · Marketing Messages API صارت GA (فبراير 2026) · إمكانية تجاوز عنوان الـwebhook لكل WABA. | رسمي | https://developers.facebook.com/documentation/business-messaging/whatsapp/changelog/ |
| A5 | **الموافقة المسبقة (opt-in):** على النشاط الحصول على موافقة قبل مراسلة الناس على واتساب. يجوز جمعها خارج واتساب إن كانت نظامية. الجودة والحظر والإبلاغ تؤدي إلى تقييد الإرسال. | رسمي | https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in |
| A6 | **Coexistence:** رقم واحد يعمل على تطبيق Business وعلى Cloud API معاً. الرسائل التي يكتبها المالك من التطبيق تصل للنظام عبر webhook اسمه `smb_message_echoes`. الدعم صار عالمياً (آخر دولتين أُضيفتا في أبريل 2026). يتطلب التسجيل عبر Embedded Signup من مزوّد معتمد. قيود: بعض ميزات التطبيق تتعطل، والسرعة القصوى تنخفض، وWhatsApp Pay غير مدعوم. | وسيط + سجل Meta | https://chakrahq.com/article/whatsapp-coexistence-live-eu-uk-europe-whatsapp-business-for-api-live/ · https://whautomate.com/whatsapp-coexistence |
| A7 | **أن تصبح Tech Provider:** توثيق الأعمال (اسم، عنوان، هاتف، موقع، مستندات إن لزم) ثم مراجعة التطبيق بفيديوهات والحصول على صلاحيتين متقدمتين (`whatsapp_business_messaging` و`whatsapp_business_management`). يمكن الانضمام عبر Solution Partner قائم. على كل عميل مسجَّل إضافة وسيلة دفع. | رسمي | https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers |
| A8 | **Click-to-WhatsApp والإسناد:** أول رسالة بعد الإعلان تحمل كائن `referral` فيه `ctwa_clid`. يمكن إرسال أحداث (مثل Purchase) إلى Conversions API for Business Messaging. نافذة الإسناد 7 أيام من النقرة. تعمل مع واجهة واتساب الرسمية فقط. | وسيط/وثائق مزوّدين | https://docs.zernio.com/platforms/whatsapp/ctwa · https://stape.io/news/whatsapp-conversion-api |
| A9 | **إنستغرام/ماسنجر:** نافذة 24 ساعة. وسم Human Agent يمدّها 7 أيام لكنه للبشر لا للأتمتة. الرد الخاص على تعليق: رسالة واحدة فقط لكل تعليق خلال 7 أيام، وحتى 750 رداً خاصاً في الساعة لكل حساب. يحتاج صلاحيات `instagram_business_manage_messages` و`..._manage_comments` وAdvanced Access (مراجعة تطبيق). موجة تشدّد في أواخر 2025 ضد إساءة أتمتة التعليقات. | رسمي + وسطاء | https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/private-replies · https://creatorflow.so/blog/instagram-automation-features/ |

## ب) منصات المتاجر

| # | ما وجدته | الوسم | المصدر |
|---|---|---|---|
| B1 | **سلة:** أحداث ويبهوك تشمل `abandoned.cart` و`abandoned.cart.updated` و`abandoned.cart.status.changed` و`abandoned.cart.purchased` وأحداث الطلبات والعملاء. الـwebhook يدعم توقيعاً أو رمزاً. التسجيل عبر بوابة الشركاء (تسجيل مطوّر + تحقق هوية). تطبيقات خاصة بمتجر واحد ممكنة بموافقة صاحب المتجر. OAuth 2.0. | رسمي | https://docs.salla.dev/421119m0 · https://docs.salla.dev/1726838m0 · https://docs.salla.dev/playbook/building/dev-preparation · https://help.salla.sa/en/article/create-and-publish-private-apps/ddivmwvwi575q35dx8o8a2wx |
| B2 | **زد:** أحداث `abandoned_cart.created` و`abandoned_cart.completed`، وواجهات لعرض السلات المتروكة وإرسال تذكير يدوي (أي أن لدى زد ميزة تذكير أصلية). OAuth 2.0. | رسمي | https://docs.zid.sa/webhooks.md · https://docs.zid.sa/project-613905/webhook-events-abandoned-cart · https://docs.zid.sa/oauth |
| B3 | **شوبيفاي:** لا يوجد حدث "متروكة". السلة المتروكة حالة تُستنتج من `checkouts/create` و`checkouts/update` وغياب `orders/create`. بيانات العميل (هاتف/بريد) "بيانات عملاء محمية" تحتاج موافقة وشروط حماية. الويبهوكات قد تصل مكررة وغير مرتبة. | رسمي + مطوّر | https://shopify.dev/docs/api/admin-rest/latest/resources/abandoned-checkouts · https://dev.to/andras_palinkas/there-is-no-abandoned-event-a-developers-map-of-shopifys-abandoned-checkout-api-and-webhooks-25fh |
| B4 | **ووكومرس:** لم أتحقق في هذه الجلسة. معلومتي أنه لا يوجد حدث سلة متروكة أصلي وأن الحل إضافة أو تتبع من جهتنا. | غير مؤكد | — |

## ج) حدود المنصات والتكاليف

| # | ما وجدته | الوسم | المصدر |
|---|---|---|---|
| C1 | **Cloudflare (الخطة المجانية):** 100 ألف طلب/يوم للحساب كله · 10ms CPU لكل استدعاء · 50 استدعاءً فرعياً لكل طلب · `waitUntil` حتى 30 ثانية · Cron Triggers مدعومة (وفي بايثون عبر `scheduled`). | رسمي | https://developers.cloudflare.com/workers/platform/limits |
| C2 | **Queues على الخطة المجانية** منذ 4 فبراير 2026: 10 آلاف عملية/يوم، والاحتفاظ 24 ساعة. النشر إلى الطابور من بايثون ممكن؛ **لم أتأكد من وجود معالج استهلاك (consumer) في بايثون**. | رسمي | https://developers.cloudflare.com/changelog/post/2026-02-04-queues-free-plan/ |
| C3 | **Smart Placement** متاح على كل الخطط، ويقرّب الـWorker من الخدمات الخلفية لتقليل زمن الدورات المتتابعة (مثل قاعدة البيانات). | رسمي | https://developers.cloudflare.com/workers/configuration/placement/ |
| C4 | **Workers AI:** 10 آلاف neuron/يوم مجاناً. Whisper large-v3-turbo حوالي 0.0005$ للدقيقة (حوالي 214 دقيقة/يوم ضمن المجاني). تضمين bge-m3 (يدعم العربية) رخيص جداً. Vectorize المجاني محدود (حوالي 5 ملايين بُعد مخزّن). | رسمي | https://developers.cloudflare.com/workers-ai/platform/pricing/ |
| C5 | **OpenRouter:** يمكن فرض ترتيب المزوّدين بـ`provider.sort` (السعر/السرعة/زمن الاستجابة) ويختصره اللاحق `:nitro`. DeepSeek V4 Flash يخدمه نحو 15 مزوّداً بتباين كبير (زمن استجابة من أقل من ثانية إلى أكثر من 10 ثوانٍ، وسرعة 14–89 توكن/ث، وتوفّر 80–99.8% وقت الفحص). يوجد نموذج أحدث `deepseek-v4.1-flash` بأرقام أفضل على الصفحة (يُقاس لا يُفترض). | رسمي | https://openrouter.ai/docs/features/provider-routing · https://openrouter.ai/models/deepseek/deepseek-v4-flash · https://openrouter.ai/deepseek/deepseek-v4.1-flash |
| C6 | **Supabase المجاني:** مشروعان · 500MB قاعدة · 5GB egress · **إيقاف تلقائي بعد 7 أيام خمول** · **بلا نسخ احتياطية**. خطة Pro بـ25$/شهر تضيف نسخاً يومية (7 أيام) وتمنع الإيقاف. | وسطاء متعددون | https://www.nocode.mba/articles/supabase-pricing · https://designrevision.com/blog/supabase-pricing.md |
| C7 | **Google OAuth:** نطاقات غير حساسة (مثل `drive.file`) لا تحتاج مراجعة. النطاقات الحساسة (معظم التقويم) تحتاج مراجعة تطبيق. النطاقات المقيّدة (Drive الواسع، Gmail) تحتاج مراجعة + تقييم أمني سنوي (CASA). التطبيق غير الموثّق: سقف 100 مستخدم، تحذير مخيف، وانتهاء الرموز بعد 7 أيام. (نطاق Sheets: حساس حسب علمي، تأكد من لوحة Google Cloud.) | وسيط (Nango) | https://nango.dev/docs/integrations/google-security-review |

## د) امتثال وسياسات

| # | ما وجدته | الوسم | المصدر |
|---|---|---|---|
| D1 | **حجب التقييمات (review gating):** سياسة Google تمنع تثبيط التقييمات السلبية أو استدعاء الإيجابية انتقائياً. قاعدة FTC للتقييمات (سارية منذ 21 أكتوبر 2024، أمريكية) تعالج التقييمات المزيفة وتقييد/قمع السلبية؛ والاستدعاء الانتقائي قد يخالف قانون FTC العام بحسب الاستعمال. | وسيط/قانوني عام | https://www.shiftflow.app/playbook/what-is-review-gating · https://synpost.synup.com/what-is-review-gating/ |
| D2 | **حماية البيانات الشخصية السعودية (PDPL):** نافذة بالكامل منذ 14 سبتمبر 2024، وتسري أيضاً على جهات خارج المملكة تعالج بيانات أفراد داخلها. نقل البيانات للخارج له مسارات نظامية (قرار كفاية قيد الإعداد، بنود تعاقدية قياسية، قواعد ملزمة، تقييم مخاطر). الإمارات: المرسوم بقانون 45/2021. هذه ليست استشارة قانونية. | وسطاء قانونيون | https://cms.law/en/are/legal-updates/one-year-anniversary-saudi-personal-data-protection-law · https://www.kslaw.com/news-and-insights/international-personal-data-transfers-under-saudi-arabias-data-protection-law |

## هـ) السوق والنماذج

| # | ما وجدته | الوسم | المصدر |
|---|---|---|---|
| E1 | **منافسون في الخليج للمتاجر:** LetsBot (تطبيقات رسمية لسلة وزد، سلة متروكة، بوت بلهجات، رسائل جماعية، دردشة متعددة الوكلاء، تطبيق جوال، تجربة 14 يوماً بلا بطاقة) · Thikaa (عربي أولاً، تطبيق جوال لكل القنوات، مزامنة كتالوج سلة، فهم رسائل صوتية، تعليق إلى رسالة خاصة، يبدأ من 10$/شهر) · ChatMaxima (سلة وزد، من 19$/شهر) · Unifonic (مؤسسي، روابط سلة وزد وشوبيفاي ووكومرس) · ZOE (لهجات، رسائل صوتية، صور مطابقة لمنتجات المتجر، سلة متروكة على أربع منصات). عالمياً: respond.io من 79$ وWATI من 49$/99$. | تسويقي | https://letsbot.net/en/comparison.html · https://thikaa.com/compare/thikaa-vs-respond-io · https://chatmaxima.com/integrations/zid-whatsapp-integration/ · https://www.unifonic.com/en/resources/top-whatsapp-solutions-for-e-commerce-why-unifonic-leads-in-mena |
| E2 | **نماذج تسعير الوكيل الذكي 2026:** لكل حل (Intercom حوالي 0.99$، Zendesk حوالي 1.2–1.5$) · لكل محادثة (HubSpot حوالي 0.5$، Salesforce حوالي 2$) · أرصدة (Crisp: رصيد ينتهي شهرياً) · ثابت شامل. مشكلة "الحل": تعريفه رخو ("حل مفترض" إن غادر العميل). | وسيط/تحليلي | https://www.dragapp.com/blog/state-of-ai-support-pricing/ · https://aissist.io/industries/ai-agent-pricing-benchmark-2026 |
| E3 | **لهجات النماذج:** لا نموذج واحد يتفوق على كل اللهجات؛ أداء النماذج على اللهجات أضعف من الفصحى؛ في الشامي تتصدر نماذج عربية متخصصة بحسب مصدر ثانوي. الحاجة: اختبار على لهجتك أنت. | أكاديمي + ثانوي | https://arxiv.org/html/2409.11404v3 |
| E4 | **التعرّف على الكلام العربي:** في معيار متعدد اللهجات أفضل نظام وصل إلى نحو 35% خطأ كلمات، وWhisper-large-v3 نحو 49.5%. أي أن فهم الرسائل الصوتية سيخطئ، ويلزم تأكيد التفاصيل الحرجة مع العميل. | أكاديمي | https://arxiv.org/pdf/2609.35564 |

---

## ما لم أستطع تأكيده (لا تعتمد عليه قبل التحقق)
1. تفاصيل تحديث 15 سبتمبر 2026 المذكور في صفحة BSUID الرسمية، ووضع بلدك/عملائك من حيث طرح الأسماء المستعارة.
2. هل توجد في بايثون Workers دالة استهلاك طوابير (`queue`)؟ (النشر مؤكد، الاستهلاك لا).
3. هل نداءات الربط بين Workers (Service Bindings) تُحسب ضمن حد 50 استدعاءً فرعياً؟
4. زمن CPU الفعلي لخط الرسالة في بايثون مقابل حد 10ms (لم يظهر خطأ في 18 تسليماً، لكن لا قياس).
5. حقول حدث `abandoned.cart` في سلة (هل يتضمن رقم الجوال دائماً؟).
6. نموذج الربط الحالي لأرقام العملاء مع Meta (من يملك التطبيق وسرّه) — سؤال لك، لا بحث.
7. موعد نهاية التسويق عبر Cloud API العادي (13 أكتوبر 2027) — ادعاء وسيط واحد.
8. ادعاءات المنافسين التسويقية (نسب استرجاع السلة، "كل اللهجات") — لا أعتمدها أرقاماً.
