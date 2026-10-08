# SILICON Telegram bot

Telegramdagi SILICON o‘quv markazi uchun alohida bot. Asl sayt fayliga tegmaydi. Bot va sayt hozircha bir-biridan mustaqil: bot ma’lumotlarini o‘zining `silicon.sqlite3` faylida saqlaydi.

## Talablar

- Python 3.10 yoki yangiroq
- BotFather orqali olingan Telegram bot tokeni
- Admin bo‘ladigan Telegram akkaunt(lar)ining raqamli ID si

Bot Python standart kutubxonalaridan foydalanadi; qo‘shimcha paket o‘rnatish shart emas.

## Ishga tushirish — Windows PowerShell

1. Telegramda [@BotFather](https://t.me/BotFather) orqali bot yarating va token oling. Tokenni chat, repozitoriy yoki `.env.example` faylida saqlamang hamda boshqalarga yubormang.
2. Telegram ID raqamingizni [@userinfobot](https://t.me/userinfobot) orqali aniqlang.
3. PowerShell oynasida `silicon_bot` papkasiga o‘ting, muhit o‘zgaruvchilarini kiriting va botni ishga tushiring:

```powershell
$env:TELEGRAM_BOT_TOKEN = "BotFather bergan maxfiy token"
$env:TELEGRAM_ADMIN_IDS = "123456789"
python .\bot.py
```

Bir nechta admin ID raqamlarini vergul bilan ajrating:

```powershell
$env:TELEGRAM_ADMIN_IDS = "123456789,987654321"
```

Bot ishga tushganda shu papkadagi `.env` faylini avtomatik o‘qiydi. `.env.example` dan nusxa olib `.env` yarating va unga BotFather tokeni hamda admin ID ni kiriting. `.env` Git’dan chiqarilgan. PowerShell’da oldindan o‘rnatilgan muhit o‘zgaruvchilari `.env` dagi qiymatlardan ustun turadi. Botni papka ichida `python .\bot.py` orqali ishga tushirgach, Telegramda unga `/start` yuboring.

## Rollar va tasdiqlash

- `TELEGRAM_ADMIN_IDS` ro‘yxatidagi ID admin bo‘ladi. Boshqa akkauntlar Admin rejimiga kira olmaydi.
- O‘quvchi va ustoz ism, telefon, kurs yoki yo‘nalishini yuborib ariza qoldiradi.
- Ariza admin tasdiqlamaguncha akkauntga kirish berilmaydi.
- Ustoz faqat o‘ziga biriktirilgan guruhlarda davomat belgilaydi. To‘lov kiritish, guruhlarni boshqarish va o‘quvchi statusini o‘zgartirish admin huquqida qoladi.

## Admin uchun eslatma

Ma’lumotlar bot ishlayotgan kompyuterda SQLite fayli sifatida saqlanadi. Zaxira nusxa olish uchun botni to‘xtating va `silicon.sqlite3` faylini nusxalang. CSV hisobotida shaxsiy ma’lumotlar bo‘lishi mumkin — uni himoyalang.

Bot ishlayotgan kompyuter internetga ulangan bo‘lishi kerak. Bitta token bilan polling orqali faqat bitta bot nusxasini ishlating.

## Tekshirish

```powershell
python -m unittest discover -s .\tests -v
python -m py_compile .\bot.py
```

Moliyaviy ko‘rsatkichlar botga kiritilgan ma’lumotlardan hisoblanadi. Haqiqiy to‘lov qabuli, avvalgi saytdagi akkauntlar bilan sinxronlash va to‘lov shlyuzi bu demo versiyaga ulanmagan.
