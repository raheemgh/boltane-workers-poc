# ADR-3: قاعدة البيانات: إضافات فقط، أسماء بلا بادئة، RLS

**الحالة:** معتمد (V2-01)

**السياق:** V1 وAva ولوحة التحكم والبوت الإداري تقرأ جدول `stores` الحي.

**القرار:** الترحيلات `sql/migrations/NNN_name.sql` إضافية وتقبل التكرار، ولكل منها عكسي بالاسم نفسه في `sql/rollbacks/`. جداول V2 بلا بادئة (`wa_customers` و`inbound_events` و`chat_sessions` و`message_log` و`usage_events` و`admin_alerts`). كل الأزمنة `timestamptz`. `store_id` في كل جدول (الاستثناء: `admin_alerts` يقبل null لتنبيهات المنصة). RLS مفعّل بلا سياسات: مفتاح الخدمة فقط. أعمدة `stores` الجديدة: `features` و`timezone` و`business_hours` و`default_language` و`billing_anchor` و`owner_phone`، إما اختيارية أو بقيمة افتراضية، فلا يتأثر V1. القيود فقط على المجموعات المستقرة؛ ما سينمو (`kind` و`content_type`) نص حر يُفحص في الكود.

**النتائج:** الحذف الوحيد المسموح هو في ملف الترحيل العكسي. أرقام الهواتف أرقام فقط بلا `+` (مثل `wa_id` من Meta).
