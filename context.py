"""
Dynamic Context Builder — Injects booking state into the LLM system prompt.

This is the bridge between the booking state tracker and the LLM.
Instead of hardcoded multilingual responses, we tell the LLM what's happening
and let it generate natural replies in whatever language it's speaking.
"""
from typing import Optional

# ── Native-language reinforcement: "appointment not booked yet" ──────────────
_NOT_BOOKED_NATIVE: dict[str, str] = {
    "hi": "⚠ अपॉइंटमेंट अभी बुक नहीं हुई है। जब तक [Booking Confirmed] न दिखे, तब तक बुक होने की पुष्टि न करें।",
    "bn": "⚠ অ্যাপয়েন্টমেন্ট এখনও বুক হয়নি। [Booking Confirmed] না দেখা পর্যন্ত বলবেন না যে বুক হয়ে গেছে।",
    "ta": "⚠ அப்பாயிண்ட்மெண்ட் இன்னும் புக் ஆகவில்லை. [Booking Confirmed] என்று வரும் வரை புக் ஆகிவிட்டது ಎಂದು சொல்லாதீர்கள்.",
    "te": "⚠ అపాయింట్‌మెంట్ ఇంకా బుక్ కాలేదు. [Booking Confirmed] అని వచ్చే వరకు బుక్ అయిందని చెప్పకండి.",
    "kn": "⚠ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ಇನ್ನೂ ಬುಕ್ ಆಗಿಲ್ಲ. [Booking Confirmed] ಅಂತ ಬರುವವರೆಗೆ ಬುಕ್ ಆಗಿದೆ ಎಂದು ಹೇಳಬೇಡಿ.",
    "ml": "⚠ അപ്പോയിന്റ്മെന്റ് ഇതുവരെ ബുക്ക് ആയിട്ടില്ല. [Booking Confirmed] എന്ന് കാണുന്നത് വരെ ബുക്ക് ആയി എന്ന് പറയരുത്.",
    "mr": "⚠ अपॉइंटमेंट अजून बुक झालेली नाही. [Booking Confirmed] दिसेपर्यंत बुक झाली आहे असे सांगू नका.",
    "gu": "⚠ અપોઈન્ટમેન્ટ હજી બુક નથી થઈ. [Booking Confirmed] ન દેખાય ત્યાં સુધી બુક થઈ ગઈ છે એમ ન કહેશો.",
    "pa": "⚠ ਅਪੌਇੰਟਮੈਂਟ ਅਜੇ ਬੁੱਕ ਨਹੀਂ ਹੋਈ ਹੈ। ਜਦੋਂ ਤੱਕ [Booking Confirmed] ਨਾ ਦਿਖੇ, ਉਦੋਂ ਤੱਕ ਬੁੱਕ ਹੋਣ ਬਾਰੇ ਨਾ ਕਹੋ।",
    "od": "⚠ ଅପଏଣ୍ଟମେଣ୍ଟ ଏବେ ଯାଏଁ ବୁକ୍ ହୋଇନାହିଁ। [Booking Confirmed] ନ ଦେଖିବା ପର୍ଯ୍ୟନ୍ତ ବୁକ୍ ହୋଇଗଲା ବୋଲି କୁହନ୍ତୁ ନାହିଁ।",
}

# ── Native-language MANDATORY next-field instruction ─────────────────────────
_MANDATORY_NATIVE: dict[str, str] = {
    "hi": "⚠ अनिवार्य: अभी सिर्फ '{field}' पूछें। नाम/उम्र/विभाग की जानकारी से पहले समय या स्लॉट न पूछें।",
    "bn": "⚠ বাধ্যতামূলক: এখন শুধু '{field}' জিজ্ঞাসা করুন। নাম/বয়স/বিভাগের তথ্য পাওয়ার আগে সময় বা স্লট জানতে চাইবেন না।",
    "ta": "⚠ கட்டாயம்: இப்போது '{field}' மட்டும் கேளுங்கள். பெயர்/வயது/துறை விவரங்கள் கிடைக்கும் வரை நேரம் கேட்காதீர்கள்.",
    "te": "⚠ తప్పనిసరి: ఇప్పుడు కేవలం '{field}' మాత్రమే అడగండి. పేరు/వయస్సు/విభాగం వివరాలు తెలియకుండా సమయం గురించి అడగకండి.",
    "kn": "⚠ ಕಡ್ಡಾಯ: ಈಗ ಕೇವಲ '{field}' ಮಾತ್ರ ಕೇಳಿ. ಹೆಸರು/ವಯಸ್ಸು/ವಿಭಾಗದ ವಿವರಗಳು ತಿಳಿಯುವ ಮೊದಲು ಸಮಯ ಕೇಳಬೇಡಿ.",
    "ml": "⚠ നിർബന്ധം: ഇപ്പോൾ '{field}' മാത്രം ചോദിക്കുക. പേര്/വയസ്സ്/വിഭാഗം എന്നിവ ലഭിക്കുന്നതിന് മുൻപ് സമയം ചോദിക്കരുത്.",
    "mr": "⚠ अनिवार्य: आता फक्त '{field}' विचारा. नाव/वय/विभाग यांची माहिती मिळेपर्यंत वेळेबद्दल विचारू नका.",
    "gu": "⚠ ફરજિયાત: અત્યારે ફક્ત '{field}' જ પૂછો. નામ/ઉંમર/વિભાગની વિગત મળ્યા પહેલાં સમય ન પૂછશો.",
    "pa": "⚠ ਲਾਜ਼ਮੀ: ਹੁਣੇ ਸਿਰਫ਼ '{field}' ਪੁੱਛੋ। ਨਾਮ/ਉਮਰ/ਵਿਭਾਗ ਦੀ ਜਾਣਕਾਰੀ ਤੋਂ ਪਹਿਲਾਂ ਸਮਾਂ ਨਾ ਪੁੱਛੋ।",
    "od": "⚠ ବାଧ୍ୟତାମୂଳକ: ଏବେ କେବଳ '{field}' ପଚାରନ୍ତୁ। ନାମ/ବୟସ/ବିଭାଗ ସୂଚନା ମିଳିବା ପୂର୍ବରୁ ସମୟ ପଚାରନ୍ତୁ ନାହିଁ。",
}

# ── Native field names (for native MANDATORY instruction substitution) ────────
_FIELD_NAMES_NATIVE: dict[str, dict[str, str]] = {
    "hi": {"name": "नाम", "age": "उम्र", "department": "विभाग", "date": "तारीख", "time": "समय"},
    "bn": {"name": "নাম", "age": "বয়স", "department": "বিভাগ", "date": "তারিখ", "time": "সময়"},
    "ta": {"name": "பெயர்", "age": "வயது", "department": "துறை", "date": "தேதி", "time": "நேரம்"},
    "te": {"name": "పేరు", "age": "వయసు", "department": "విభాగం", "date": "తేదీ", "time": "సమయం"},
    "kn": {"name": "ಹೆಸರು", "age": "ವಯಸ್ಸು", "department": "ವಿಭಾಗ", "date": "ದಿನಾಂಕ", "time": "ಸಮಯ"},
    "ml": {"name": "പേര്", "age": "വയസ്സ്", "department": "വിഭാഗം", "date": "തീയതി", "time": "സമയം"},
    "mr": {"name": "नाव", "age": "वय", "department": "विभाग", "date": "तारीख", "time": "वेळ"},
    "gu": {"name": "નામ", "age": "ઉંमर", "department": "વిभाग", "date": "तारीख", "time": "सময়"},
    "pa": {"name": "ਨਾਮ", "age": "ਉਮਰ", "department": "ਵਿਭਾਗ", "date": "ਤਾਰੀਖ਼", "time": "ਸਮਾਂ"},
    "od": {"name": "ନାମ", "age": "ବୟସ", "department": "ବିଭାଗ", "date": "ତାରିଖ", "time": "ସମୟ"},
}

# ── Readback instruction: explicitly read back all details before confirmation ──
_READBACK_INSTRUCTIONS: dict[str, str] = {
    "en": "BEFORE asking for confirmation, READ BACK every detail clearly: full name, age, department, date (day/month/year), and exact time. Speak naturally.",
    "hi": "कन्फर्मेशन मांगने से पहले, हर जानकारी साफ़ तौर पर दोहराएं: पूरा नाम, उम्र, विभाग, तारीख (दिन/महीना/साल), और सटीक समय। बातचीत की तरह बोलें।",
    "bn": "নিশ্চিত করার আগে, প্রতিটি তথ্য স্পষ্টভাবে পড়ে শোনান: সম্পূর্ণ নাম, বয়স, বিভাগ, তারিখ (দিন/মাস/বছর), এবং সঠিক সময়। স্বাভাবিকভাবে কথা বলুন।",
    "gu": "કન્ફર્મેશન માંગતા પહેલાં, દરેક વિગત સ્પષ્ટ રીતે વાંચો: પૂરું નામ, ઉંમર, વિભાગ, તારીખ (દિવસ/મહિનો/વર્ષ), અને ચોક્કસ સમય. સહજ રીતે બોલો.",
    "kn": "ದೃಢೀಕರಿಸುವ ಮೊದಲು, ಪ್ರತಿಯೊಂದು ವಿವರವನ್ನು ಸ್ಪಷ್ಟವಾಗಿ ಓದಿ: ಪೂರ್ಣ ಹೆಸರು, ವಯಸ್ಸು, ವಿಭಾಗ, ದಿನಾಂಕ (ದಿನ/ತಿಂಗಳು/ವರ್ಷ), ಮತ್ತು ನಿಖರವಾದ ಸಮಯ. ಸಹಜವಾಗಿ ಮಾತನಾಡಿ.",
    "ml": "സ്ഥിരീകരണം ചോദിക്കുന്നതിന് മുമ്പ്, എല്ലാ വിവരങ്ങളും വ്യക്തമായി വായിച്ചു കേൾപ്പിക്കുക: മുഴുവൻ പേര്, വയസ്സ്, വിഭാഗം, തീയതി (ദിവസം/മാസം/വർഷം), കൃത്യമായ സമയം. സ്വാഭാവികമായി സംസാരിക്കുക.",
    "mr": "खात्री करण्यापूर्वी (confirmation), प्रत्येक गोष्ट स्पष्टपणे वाचून दाखवा: पूर्ण नाव, वय, विभाग, तारीख (दिवस/महिना/वर्ष), आणि अचूक वेळ. नैसर्गिकपणे बोला.",
    "od": "ନିଶ୍ଚିତ କରିବା ପୂର୍ବରୁ, ପ୍ରତ୍ୟେକ ବିବରଣୀ ସ୍ପଷ୍ଟ ଭାବରେ ପଢ଼ି ଶୁଣାନ୍ତୁ: ସମ୍ପୂର୍ଣ୍ଣ ନାମ, ବୟସ, ବିଭାଗ, ତାରିଖ (ଦିନ/ମାସ/ବର୍ଷ), ଏବଂ ସଠିକ୍ ସମୟ। ସ୍ୱାଭାବିକ ଭାବରେ କୁହନ୍ତୁ।",
    "pa": "ਪੁਸ਼ਟੀ ਕਰਨ ਤੋਂ ਪਹਿਲਾਂ, ਹਰ ਵੇਰਵੇ ਨੂੰ ਸਾਫ਼ ਤੌਰ 'ਤੇ ਦੁਹਰਾਓ: ਪੂਰਾ ਨਾਮ, ਉਮਰ, ਵਿਭਾਗ, ਤਾਰੀਖ਼ (ਦਿਨ/ਮਹੀਨਾ/ਸਾਲ), ਅਤੇ ਸਹੀ ਸਮਾਂ। ਕੁਦਰਤੀ ਤਰੀਕੇ ਨਾਲ ਗੱਲ ਕਰੋ।",
    "ta": "உறுதிப்படுத்துவதற்கு முன், எல்லா விவரங்களையும் தெளிவாகப் படியுங்கள்: முழு பெயர், வயது, துறை, தேதி (நாள்/மாதம்/வருடம்), மற்றும் சரியான நேரம். இயல்பாகப் பேசுங்கள்.",
    "te": "కన్ఫర్మేషన్ అడిగే ముందు, ప్రతి వివరాన్ని స్పష్టంగా చదివి వినిపించండి: పూర్తి పేరు, వయసు, విభాగం, తేదీ (రోజు/నెల/సంవత్సరం), మరియు ఖచ్చితమైన సమయం. సహజంగా మాట్లాడండి."
}

# ── Validation rules for collected fields ─────────────────────────────────────
_VALIDATION_RULES: dict[str, str] = {
    "en": "Validate inputs: Age should be 1-120, Department should match clinic offerings, Date must be today or future (not past), Name should be a real name.",
    "hi": "जानकारी जाँचें: उम्र 1-120 के बीच हो, विभाग क्लिनिक की लिस्ट से मेल खाए, तारीख आज या भविष्य की हो (पुरानी नहीं), और नाम सही होना चाहिए।",
    "bn": "তথ্য যাচাই করুন: বয়স 1-120 এর মধ্যে হওয়া উচিত, বিভাগটি ক্লিনিকের তালিকার সাথে মিলতে হবে, তারিখ আজ বা ভবিষ্যতের হওয়া চাই (অতীত নয়), এবং নাম সঠিক হতে হবে।",
    "gu": "માહિતી તપાસો: ઉંમર 1-120 ની વચ્ચે હોવી જોઈએ, વિભાગ ક્લિનિકની યાદી સાથે મેળ ખાતો હોવો જોઈએ, તારીખ આજ કે ભવિષ્યની હોવી જોઈએ (ભૂતકાળની નહીં), અને નામ સાચું હોવું જોઈએ.",
    "kn": "ಮಾಹಿತಿ ಪರಿಶೀಲಿಸಿ: ವಯಸ್ಸು 1-120 ರ ನಡುವೆ ಇರಬೇಕು, ವಿಭಾಗವು ಕ್ಲಿನಿಕ್‌ನಲ್ಲಿ ಲಭ್ಯವಿರಬೇಕು, ದಿನಾಂಕ ಇಂದು ಅಥವಾ ಭವಿಷ್ಯದ್ದಾಗಿರಬೇಕು (ಹಳೆಯದಲ್ಲ), ಮತ್ತು ಹೆಸರು ಸರಿಯಾಗಿರಬೇಕು.",
    "ml": "വിവരങ്ങൾ പരിശോധിക്കുക: വയസ്സ് 1-120 നും ഇടയിലായിരിക്കണം, വിഭാഗം ക്ലിനിക്കിൽ ലഭ്യമായിരിക്കണം, തീയതി ഇന്നോ ഭാവിയിലോ ആയിരിക്കണം (കഴിഞ്ഞ കാലമാകരുത്), പേര് ശരിയായിരിക്കണം.",
    "mr": "माहिती तपासा: वय 1-120 च्या दरम्यान असावे, विभाग क्लिनिकच्या यादीशी जुळणारा असावा, तारीख आज किंवा भविष्यातील असावी (भूतकाळ नको), आणि नाव योग्य असावे.",
    "od": "ତଥ୍ୟ ଯାଞ୍ଚ କରନ୍ତୁ: ବୟସ 1-120 ମଧ୍ୟରେ ହେବା ଉଚିତ, ବିଭାଗ କ୍ଲିନିକରେ ଉପଲବ୍ଧ ଥିବା ଆବଶ୍ୟକ, ତାରିଖ ଆଜି କିମ୍ବା ଭବିଷ୍ୟତର ହେବା ଉଚିତ (ଅତୀତ ନୁହେଁ), ଏବଂ ନାମ ସଠିକ୍ ହେବା ଉଚିତ।",
    "pa": "ਜਾਣਕਾਰੀ ਦੀ ਜਾਂਚ ਕਰੋ: ਉਮਰ 1-120 ਦੇ ਵਿਚਕਾਰ ਹੋਵੇ, ਵਿਭਾਗ ਕਲੀਨਿਕ ਦੀ ਸੂਚੀ ਨਾਲ ਮੇਲ ਖਾਂਦਾ ਹੋਵੇ, ਤਾਰੀਖ਼ ਅੱਜ ਜਾਂ ਭਵਿੱਖ ਦੀ ਹੋਵੇ (ਪੁਰਾਣੀ ਨਹੀਂ), ਅਤੇ ਨਾਮ ਸਹੀ ਹੋਣਾ ਚਾਹੀਦਾ ਹੈ।",
    "ta": "விவரங்களைச் சரிபார்க்கவும்: வயது 1-120 இருக்க வேண்டும், துறை கிளினிக்கில் உள்ளதாக இருக்க வேண்டும், தேதி இன்று அல்லது எதிர்காலமாக இருக்க வேண்டும் (கடந்த காலம் அல்ல), பெயர் சரியாக இருக்க வேண்டும்.",
    "te": "వివరాలను ధృవీకరించండి: వయసు 1-120 మధ్య ఉండాలి, విభాగం క్లినిక్ జాబితాలో ఉండాలి, తేదీ నేడు లేదా భవిష్యత్తు అయి ఉండాలి (గతం కాదు), మరియు పేరు సరైనదిగా ఉండాలి."
}


def build_booking_context(
    draft: dict,
    missing_fields: list[str],
    slot_info: Optional[dict] = None,
    booking_result: Optional[dict] = None,
    existing_appointment: Optional[dict] = None,
    name_to_confirm: Optional[str] = None,
    age_to_confirm: Optional[str] = None,
    original_name: Optional[str] = None,
    awaiting_confirmation: bool = False,
    awaiting_reschedule_confirm: bool = False,
    time_clarification: Optional[str] = None,
    slot_options: Optional[list[str]] = None,
    department_unavailable: Optional[str] = None,
    clinic_departments: Optional[list[str]] = None,
    user_wants_change: bool = False,
    validation_failed: bool = False,
    lang: str = "en",
) -> str:
    """
    Build a context block to inject after the base system prompt.

    The LLM sees this context and naturally incorporates it into
    its conversational response — no templated replies needed.

    Returns empty string if no booking is in progress.
    """
    parts: list[str] = []

    # ✅ PHASE 3: Validation failed - missing fields detected at confirmation
    if validation_failed and missing_fields:
        _missing_native = {
            "hi": {"name": "नाम", "age": "उम्र", "department": "विभाग", "date": "तारीख", "time": "समय"},
            "bn": {"name": "নাম", "age": "বয়স", "department": "বিভাগ", "date": "তারিখ", "time": "সময়"},
            "ta": {"name": "பெயர்", "age": "வயது", "department": "துறை", "date": "தேதி", "time": "நேரம்"},
            "te": {"name": "పేరు", "age": "వయసు", "department": "విభాగం", "date": "తేదీ", "time": "సమయం"},
            "kn": {"name": "ಹೆಸರು", "age": "ವಯಸ್ಸು", "department": "ವಿಭಾಗ", "date": "ದಿನಾಂಕ", "time": "ಸಮಯ"},
            "ml": {"name": "പേര്", "age": "വയസ്സ്", "department": "വിഭാഗം", "date": "തീയതി", "time": "സമയം"},
            "mr": {"name": "नाव", "age": "वय", "department": "विभाग", "date": "तारीख", "time": "वेळ"},
            "gu": {"name": "નામ", "age": "ઉંમર", "department": "વિભાગ", "date": "તારીખ", "time": "સમય"},
            "pa": {"name": "ਨਾਮ", "age": "ਉਮਰ", "department": "ਵਿਭਾਗ", "date": "ਤਾਰੀਖ਼", "time": "ਸਮਾਂ"},
            "od": {"name": "ନାମ", "age": "ବୟସ", "department": "ବିଭାଗ", "date": "ତାରିଖ", "time": "ସମୟ"},
            "en": {"name": "name", "age": "age", "department": "department", "date": "date", "time": "time"},
        }
        field_names = _missing_native.get(lang, _missing_native["en"])
        missing_native = [field_names.get(f, f) for f in missing_fields]
        missing_str = ", ".join(missing_native)
        
        parts.append(
            f"[Validation Failed]\n"
            f"CRITICAL: Attempted to confirm booking but these fields are missing: {missing_str}\n"
            f"Politely apologise (e.g. 'I'm sorry, I still need a few details') and ask for the FIRST missing field only: {missing_native[0]}.\n"
            f"Do NOT ask for multiple fields at once. Just ask for: {missing_native[0]}"
        )
        return "\n\n".join(parts)

    # ── Department not available ──
    if department_unavailable:
        if clinic_departments:
            dept_list = ", ".join(clinic_departments)
            parts.append(
                f"[Department Not Available]\n"
                f"The patient's symptom maps to '{department_unavailable}' but this clinic does NOT offer that specialty.\n"
                f"This clinic's available departments: {dept_list}\n"
                f"Apologise briefly and tell them EXACTLY which departments are available.\n"
                f"Ask which one is relevant to their problem — or help them pick the closest match.\n"
                f"For example, stomach issues might go to General; ear/nose/throat → ENT; skin → Skin/Dermatology."
            )
        else:
            parts.append(
                f"[Department Not Available]\n"
                f"The patient asked for '{department_unavailable}' department, but this clinic does not offer that service.\n"
                f"Politely inform them we don't have that department and ask if they have a different concern or problem."
            )
        return "\n\n".join(parts)

    # ── Existing appointment (reschedule prompt) ──
    if existing_appointment and awaiting_reschedule_confirm:
        apt_date = existing_appointment.get("date", "unknown")
        apt_time = existing_appointment.get("time", "unknown")
        parts.append(
            f"[Existing Appointment]\n"
            f"Patient already has an appointment on {apt_date} at {apt_time}.\n"
            f"Ask if they want to reschedule it or book a new one.\n"
            f"Wait for their answer before proceeding."
        )
        return "\n\n".join(parts)

    # ── Name confirmation ──
    if name_to_confirm:
        if original_name and original_name != name_to_confirm:
            parts.append(
                f"[Name Confirmation]\n"
                f"You heard the patient say '{original_name}' and understood it as '{name_to_confirm}'.\n"
                f"Naturally confirm: is their name {name_to_confirm}? If wrong, ask them to repeat."
            )
        else:
            parts.append(
                f"[Name Confirmation]\n"
                f"You heard the patient's name as '{name_to_confirm}'.\n"
                f"Naturally confirm: is their name {name_to_confirm}?"
            )
        return "\n\n".join(parts)

    # ── Age confirmation ──
    if age_to_confirm:
        parts.append(
            f"[Age Confirmation]\n"
            f"You heard the patient's age as {age_to_confirm}.\n"
            f"Naturally confirm: is their age {age_to_confirm}?"
        )
        return "\n\n".join(parts)

    # ── Time clarification (AM/PM ambiguity) ──
    if time_clarification:
        parts.append(
            f"[Time Clarification]\n"
            f"Patient said {time_clarification} but it's ambiguous — morning or evening?\n"
            f"Ask naturally whether they mean morning or evening."
        )
        return "\n\n".join(parts)

    # ── Slot selection (show available times) ──
    if slot_options:
        options_str = ", ".join(slot_options)
        date = draft.get("date", "")
        name = draft.get("name", "")
        age = draft.get("age", "")
        dept = draft.get("department", "")
        preferred_period = slot_info.get("preferred_period", "") if isinstance(slot_info, dict) else ""
        period_hint = f" (filtered to {preferred_period} slots as requested)" if preferred_period else ""
        parts.append(
            f"[Available Slots]\n"
            f"[Booking Status: name={name}, age={age}, dept={dept}, date={date} — Waiting for: TIME only. NOT BOOKED.]\n"
            f"⚠️ CRITICAL: When the patient selects a time, say ONLY a brief acknowledgment like 'Perfect!' and STOP.\n"
            f"Do NOT say the appointment is booked, confirmed, or scheduled.\n"
            f"Do NOT summarize all details. The system will handle confirmation next.\n\n"
            f"We have slots available on {date}{period_hint} at: {options_str}.\n"
            f"Ask the patient which time works best for them. "
            f"Be natural and friendly. Present the options naturally in conversation."
        )
        return "\n\n".join(parts)

    # ── Booking confirmation ──
    if awaiting_confirmation and not missing_fields:
        name = draft.get("name", "")
        age = draft.get("age", "")
        dept = draft.get("department", "")
        date = draft.get("date", "")
        time = draft.get("time", "")
        
        # ✅ CRITICAL: Double-check all required fields are present
        if not all([name, age, dept, date, time]):
            missing_now = [f for f in ["name", "age", "department", "date", "time"] 
                          if not draft.get(f)]
            parts.append(
                f"⚠️ VALIDATION FAILED: Cannot confirm booking. Missing fields: {', '.join(missing_now)}\n"
                f"NEVER say the appointment is booked or confirmed.\n"
                f"Ask for the missing fields: {', '.join(missing_now)}"
            )
            return "\n\n".join(parts)
        
        _confirm_instructions = {
            "en": "Ask: 'Would you like me to confirm this appointment?'",
            "hi": "पूछें: 'क्या मैं आपका appointment confirm कर दूँ?'",
            "bn": "জিজ্ঞেস করুন: 'আমি কি আপনার appointment confirm করে দেব?'",
            "gu": "પૂછો: 'શું હું તમારું appointment confirm કરી દઉં?'",
            "kn": "ಕೇಳಿ: 'ನಾನು ನಿಮ್ಮ appointment confirm ಮಾಡಿಕೊಡಲಾ?'",
            "ml": "ചോദിക്കുക: 'ഞാൻ നിങ്ങളുടെ appointment confirm ചെയ്ത് തരട്ടേ?'",
            "mr": "विचारा: 'मी तुमचं appointment confirm करून देऊ का?'",
            "od": "ପଚାରନ୍ତୁ: 'ମୁଁ ଆପଣଙ୍କ appointment confirm କରିଦେବି କି?'",
            "pa": "ਪੁੱਛੋ: 'ਕੀ ਮੈਂ ਤੁਹਾਡਾ appointment confirm ਕਰ ਦਿਆਂ?'",
            "ta": "கேளுங்கள்: 'நான் உங்கள் appointment confirm பண்ணட்டுமா?'",
            "te": "అడగండి: 'నేను మీ appointment confirm చేయాలా?'",
        }
        _ask_confirm = _confirm_instructions.get(lang, _confirm_instructions["en"])
        _readback = _READBACK_INSTRUCTIONS.get(lang, _READBACK_INSTRUCTIONS["en"])
        _validation = _VALIDATION_RULES.get(lang, _VALIDATION_RULES["en"])
        
        parts.append(
            f"[Confirm Appointment — ASK FOR PERMISSION]\n"
            f"⚠️⚠️ USE THESE EXACT VALUES — DO NOT substitute any value from conversation history:\n"
            f"- Name: {name}\n"
            f"- Age: {age}\n"
            f"- Department: {dept}\n"
            f"- Date: {date}\n"
            f"- Time: {time}  ← THIS IS THE FINAL CONFIRMED TIME. DO NOT say any other time.\n\n"
            f"⚠ {_validation}\n\n"
            f"⚠⚠⚠ CRITICAL: The appointment is NOT booked yet. DO NOT BOOK IT NOW.\n"
            f"YOUR ONLY JOB: Read back these exact details and ask for confirmation.\n\n"
            f"📋 {_readback}\n\n"
            f"{_ask_confirm}\n\n"
            f"WAIT for their yes/no answer.\n"
            f"DO NOT say: 'I'll book it', 'give me time', 'let me book', 'booking now', or similar.\n"
            f"DO NOT say the appointment IS booked, confirmed, or scheduled.\n\n"
            f"Only AFTER they say YES, the system will book it and show [Booking Confirmed]."
        )
        return "\n\n".join(parts)

    # ── Booking result ──
    if booking_result:
        if booking_result.get("success"):
            name = draft.get("name", "")
            age = draft.get("age", "")
            dept = draft.get("department", "")
            date = draft.get("date", "")
            time = draft.get("time", "")
            
            _success_examples = {
                "en": "Perfect! Your appointment is confirmed for {date} at {time}. Please arrive 15 minutes early for registration. We wish you good health!",
                "hi": "बहुत बढ़िया! आपका appointment {date} को {time} बजे confirm हो गया है। registration के लिए 15 मिनट पहले आ जाएं। आपको जल्द स्वास्थ्य लाभ की शुभकामनाएं!",
                "bn": "দারুণ! আপনার appointment {date} তে {time} এ confirm হয়েছে। registration-এর জন্য ১৫ মিনিট আগে আসুন। আপনার সুস্বাস্থ্য কামনা করি!",
                "gu": "સરસ! તમારું appointment {date} ના {time} વાગ્યે confirm થઈ ગયું. registration માટે ૧૫ મિનિટ પહેલાં આવો. સ્વસ્થ રહો!",
                "kn": "ಚೆನ್ನಾಗಿದೆ! ನಿಮ್ಮ appointment {date} ರಂದು {time}ಕ್ಕೆ confirm ಆಗಿದೆ. registration ಗಾಗಿ ೧೫ ನಿಮಿಷ ಮುಂಚೆ ಬನ್ನಿ. ಆರೋಗ್ಯವಾಗಿರಿ!",
                "ml": "വളരെ നല്ലത്! {date} ന് {time}ക്ക് appointment confirm ആയി. registration-നായി 15 മിനിറ്റ് മുൻപേ വരൂ. ആരോഗ്യം ആശംസിക്കുന്നു!",
                "mr": "छान! {date} रोजी {time} वाजता तुमचं appointment confirm झालं. registration साठी १५ मिनिटे आधी या. लवकर बरे व्हा!",
                "od": "ବହୁତ ଭଲ! {date} ରେ {time} ସମୟରେ appointment confirm ହୋଇଛି। registration ପାଇଁ ୧୫ ମିନିଟ ଆଗରୁ ଆସନ୍ତୁ। ସୁସ୍ଥ ରୁହନ୍ତୁ!",
                "pa": "ਬਹੁਤ ਵਧੀਆ! {date} ਨੂੰ {time} ਵਜੇ appointment confirm ਹੋ ਗਿਆ। registration ਲਈ ੧੫ ਮਿੰਟ ਪਹਿਲਾਂ ਆਓ। ਤੁਹਾਡੀ ਸਿਹਤ ਜਲਦੀ ਠੀਕ ਹੋਵੇ!",
                "ta": "சரி! {date} அன்று {time}க்கு appointment confirm ஆகிவிட்டது. registration-க்கு 15 நிமிடம் முன்பே வாருங்கள். நல்வாழ்த்துகள்!",
                "te": "బాగుంది! {date} న {time}కి appointment confirm అయింది. registration కోసం 15 నిమిషాలు ముందే రండి. మీ ఆరోగ్యం మెరుగవుతుందని ఆశిస్తున్నాం!",
            }
            _example = _success_examples.get(lang, _success_examples["en"]).format(date=date, time=time)
            
            parts.append(
                f"[Booking Confirmed — GENERATE NATURAL RESPONSE]\n"
                f"✅ Appointment successfully booked:\n"
                f"- Patient: {name}, Age: {age}\n"
                f"- Department: {dept}\n"
                f"- Date: {date}, Time: {time}\n\n"
                f"⚠️ DO NOT echo this marker. DO NOT say '[Booking Confirmed]'.\n"
                f"Generate a warm, natural congratulatory message in your own words.\n\n"
                f"Example: \"{_example}\"\n\n"
                f"REQUIRED: You MUST include — 'Please arrive 15 minutes before your appointment time for registration.' "
                f"(Say this naturally, not as a robotic instruction.)\n"
                f"Also: congratulate them, confirm the date/time, wish them good health. "
                f"Vary the wording each time. Be human and conversational, NOT robotic."
            )
            return "\n\n".join(parts)
        elif booking_result.get("slot_full"):
            available = booking_result.get("available_times", [])
            nearest = booking_result.get("nearest_time")
            if nearest:
                parts.append(
                    f"[Slot Unavailable]\n"
                    f"The requested time is full. Nearest available: {nearest}.\n"
                    f"Suggest this time naturally. Ask if that works."
                )
            elif available:
                options = ", ".join(available[:5])
                parts.append(
                    f"[Slot Unavailable]\n"
                    f"The requested time is full. Available times: {options}.\n"
                    f"Suggest these naturally. Ask which one works."
                )
            else:
                parts.append(
                    f"[No Slots]\n"
                    f"No slots available on the requested date.\n"
                    f"Ask if they'd like to try a different date."
                )
            return "\n\n".join(parts)
        elif booking_result.get("error"):
            parts.append(
                f"[Booking Error]\n"
                f"Something went wrong while booking. Apologize naturally and "
                f"ask if they'd like to try again."
            )
            return "\n\n".join(parts)

    # ── Active booking: show collected fields + what's missing ──
    collected = {k: v for k, v in draft.items() if v}
    if not collected and not missing_fields:
        return ""  # No booking in progress

    # ── User rejected the confirmation — ask what to change ──
    if user_wants_change:
        name = draft.get("name", "")
        age = draft.get("age", "")
        dept = draft.get("department", "")
        date = draft.get("date", "")
        time = draft.get("time", "")
        parts.append(
            f"[Change Request]\n"
            f"The patient said NO to the confirmation. Current details:\n"
            f"- Name: {name}, Age: {age}, Department: {dept}, Date: {date}, Time: {time}\n\n"
            f"⚠️ RESPOND ONLY with: 'Sure, which detail would you like to change — name, age, department, date, or time?'\n"
            f"DO NOT say 'let me check', 'let me see', or mention specific available time slots.\n"
            f"DO NOT offer time options. Just ask WHICH FIELD they want to change and wait for their answer."
        )
        return "\n\n".join(parts)

    # Use a strong header so the LLM never responds with a greeting instead of
    # continuing the booking — the [ACTIVE BOOKING] tag signals that we are
    # mid-conversation and the user is already registered as a patient.
    _not_booked_en = (
        "⚠ The appointment has NOT been booked yet. "
        "Do NOT say it is booked, confirmed, or scheduled until you see [Booking Confirmed]."
    )
    _not_booked_native = _NOT_BOOKED_NATIVE.get(lang, "")
    _not_booked_line = (
        f"{_not_booked_en}\n{_not_booked_native}" if _not_booked_native else _not_booked_en
    )
    
    # Explicit prohibition against "wait while I book" phrases in multiple languages
    _never_say_wait = {
        "en": "Never say: 'give me time to book', 'let me book it', 'I'll book it now', or 'wait while I book'.",
        "hi": "कभी भी ऐसा न कहें: 'एक सेकंड रुकिए', 'थोड़ा रुकिए', 'एक पल', 'मुझे बुक करने के लिए थोड़ा समय दीजिए', 'मैं इसे अभी book करती हूँ', 'रुकिए, मैं book कर देती हूँ।'",
        "bn": "কখনোই বলবেন না: 'এক সেকেন্ড', 'একটু অপেক্ষা করুন', 'আমাকে book করার জন্য একটু সময় দিন', 'আমি এখনই book করছি', 'অপেক্ষা করুন, আমি book করে দিচ্ছি।'",
        "gu": "ક્યારેય આવું ન કહો: 'મને book કરવા માટે થોડો સમય આપો', 'હું હમણાં book કરું છું', 'જરા રાહ જુઓ, હું book કરી દઉં।'",
        "kn": "ಎಂದಿಗೂ ಹೀಗೆ ಹೇಳಬೇಡಿ: 'book ಮಾಡಲು ನನಗೆ ಸ್ವಲ್ಪ ಸಮಯ ನೀಡಿ', 'ನಾನು ಈಗಲೇ book ಮಾಡುತ್ತೇನೆ', 'ಸ್ವಲ್ಪ ಕಾಯಿರಿ, ನಾನು book ಮಾಡಿಕೊಡುತ್ತೇನೆ.'",
        "ml": "ഒരിക്കലും ഇങ്ങനെ പറയരുത്: 'book ചെയ്യാൻ എനിക്ക് കുറച്ച് സമയം തരൂ', 'ഞാൻ ഇപ്പോൾ തന്നെ book ചെ���്യാം', 'ഒന്ന് കാത്തിരിക്കൂ, ഞാൻ book ചെയ്ത് തരാം.'",
        "mr": "कधीही असे म्हणू नका: 'मला book करण्यासाठी थोडा वेळ द्या', 'मी आत्ताच book करते', 'थोडं थांबा, मी book करून देते.'",
        "od": "କେବେବି ଏଭଳି କୁହନ୍ତୁ ନାହିଁ: 'ମୋତେ book କରିବା ପାଇଁ କିଛି ସମୟ ଦିଅନ୍ତୁ', 'ମୁଁ ଏବେଇ book କରୁଛି', 'ଅଳ୍ପ ସମୟ ଅପେକ୍ଷା କରନ୍ତୁ, ମୁଁ book କରିଦେଉଛି।'",
        "pa": "ਕਦੇ ਵੀ ਇਸ ਤਰ੍ਹਾਂ ਨਾ ਕਹੋ: 'ਮੈਨੂੰ book ਕਰਨ ਲਈ ਥੋੜ੍ਹਾ ਸਮਾਂ ਦਿਓ', 'ਮੈਂ ਹੁਣੇ ਹੀ book ਕਰਦੀ ਹਾਂ', 'ਥੋੜ੍ਹਾ ਇੰਤਜ਼ਾਰ ਕਰੋ, ਮੈਂ book ਕਰ ਦਿੰਦੀ ਹਾਂ।'",
        "ta": "ஒருபோதும் இப்படி சொல்லாதீர்கள்: 'book பண்ண எனக்கு கொஞ்சம் நேரம் கொடுங்க', 'நான் இப்போவே book பண்றேன்', 'கொஞ்சம் wait பண்ணுங்க, நான் book பண்ணிடுறேன்.'",
        "te": "ఎప్పుడూ ఇలా చెప్పకండి: 'book చేయడానికి నాకు కొంచెం సమయం ఇవ్వండి', 'నేను ఇప్పుడే book చేస్తాను', 'కొద్దిసేపు wait చేయండి, నేను book చేసి పెడతాను.'",
    }
    _wait_prohibition = _never_say_wait.get(lang, _never_say_wait["en"])
    
    # ✅ NEW: Explicit prohibition against making up field values
    _never_make_up_values = {
        "en": "⚠️ CRITICAL: NEVER make up or assume field values (name/age/department/date/time) that the patient hasn't explicitly provided. If a field is empty, ASK for it — do NOT guess or invent.",
        "hi": "⚠️ अनिवार्य: कभी भी ऐसी जानकारी न बनाएं जो मरीज ने नहीं दी है (नाम/उम्र/विभाग/तारीख/समय)। अगर कोई field खाली है, तो पूछें — अनुमान न लगाएं।",
        "bn": "⚠️ বাধ্যতামূলক: কখনোই এমন তথ্য তৈরি করবেন না যা রোগী স্পষ্টভাবে দেননি (নাম/বয়স/বিভাগ/তারিখ/সময়)। যদি কোনো field খালি থাকে, তাহলে জিজ্ঞাসা করুন — অনুমান করবেন না।",
        "gu": "⚠️ ફરજિયાત: ક્યારેય એવી વિગતો બનાવશો નહીં જે દર્દીએ સ્પષ્ટપણે આપી નથી (નામ/ઉંમર/વિભાગ/તારીખ/સમય)। જો કોઈ field ખાલી હોય, તો પૂછો — અનુમાન ન કરો।",
        "kn": "⚠️ ಕಡ್ಡಾಯ: ರೋಗಿ ಸ್ಪಷ್ಟವಾಗಿ ನೀಡದ ವಿವರಗಳನ್ನು (ಹೆಸರು/ವಯಸ್ಸು/ವಿಭಾಗ/ದಿನಾಂಕ/ಸಮಯ) ಎಂದಿಗೂ ರಚಿಸಬೇಡಿ। ಯಾವುದಾದರೂ field ಖಾಲಿ ಇದ್ದರೆ, ಕೇಳಿ — ಊಹಿಸಬೇಡಿ।",
        "ml": "⚠️ നിർബന്ധം: രോഗി വ്യക്തമായി നൽകാത്ത വിവരങ്ങൾ (പേര്/വയസ്സ്/വിഭാഗം/തീയതി/സമയം) ഒരിക്കലും സൃഷ്ടിക്കരുത്. ഏതെങ്കിലും field ശൂന്യമാണെങ്കിൽ, ചോദിക്കുക — അനുമാനിക്കരുത്।",
        "mr": "⚠️ अनिवार्य: रुग्णाने स्पष्टपणे न दिलेली माहिती (नाव/वय/विभाग/तारीख/वेळ) कधीही तयार करू नका। जर कोणतेही field रिक्त असेल, तर विचारा — अंदाज लावू नका।",
        "od": "⚠️ ବାଧ୍ୟତାମୂଳକ: ରୋଗୀ ସ୍ପଷ୍ଟଭାବେ ନ ଦେଇଥିବା ସୂଚନା (ନାମ/ବୟସ/ବିଭାଗ/ତାରିଖ/ସମୟ) କେବେବି ତିଆରି କରନ୍ତୁ ନାହିଁ। ଯଦି କୌଣସି field ଖାଲି ଥାଏ, ତେବେ ପଚାରନ୍ତୁ — ଅନୁମାନ କରନ୍ତୁ ନାହିଁ।",
        "pa": "⚠️ ਲਾਜ਼ਮੀ: ਮਰੀਜ਼ ਦੁਆਰਾ ਸਪਸ਼ਟ ਤੌਰ ਤੇ ਨਾ ਦਿੱਤੀ ਗਈ ਜਾਣਕਾਰੀ (ਨਾਮ/ਉਮਰ/ਵਿਭਾਗ/ਤਾਰੀਖ਼/ਸਮਾਂ) ਕਦੇ ਵੀ ਨਾ ਬਣਾਓ। ਜੇ ਕੋਈ field ਖਾਲੀ ਹੈ, ਤਾਂ ਪੁੱਛੋ — ਅੰਦਾਜ਼ਾ ਨਾ ਲਾਓ।",
        "ta": "⚠️ கட்டாயம்: நோயாளி தெளிவாகக் கொடுக்காத விவரங்களை (பெயர்/வயது/துறை/தேதி/நேரம்) ஒருபோதும் உருவாக்காதீர்கள். ஏதேனும் field காலியாக இருந்தால், கேளுங்கள் — யூகிக்காதீர்கள்.",
        "te": "⚠️ తప్పనిసరి: రోగి స్పష్టంగా ఇవ్వని వివరాలను (పేరు/వయస్సు/విభాగం/తేదీ/సమయం) ఎప్పుడూ సృష్టించవద్దు। ఏదైనా field ఖాళీగా ఉంటే, అడగండి — అంచనా వేయవద్దు।",
    }
    _no_makeup_line = _never_make_up_values.get(lang, _never_make_up_values["en"])
    
    parts.append(
        "[ACTIVE BOOKING IN PROGRESS]\n"
        "An appointment booking conversation is in progress. "
        "Do NOT greet the patient again or say 'welcome'. "
        "Continue naturally from where the conversation left off.\n"
        + _not_booked_line + "\n\n"
        + f"⚠ {_wait_prohibition}\n\n"
        + f"{_no_makeup_line}\n\n"
        + "You can ONLY say the appointment is booked AFTER you see [Booking Confirmed] marker.\n\n"
        + "⚠️ STALL PHRASES BANNED: NEVER say 'let me check', 'one moment', 'एक सेकंड', 'रुकिए', "
        + "'थोड़ा रुकिए', 'एक पल', 'checking availability', 'এক সেকেন্ড', 'একটু অপেক্ষা', "
        + "or any phrase implying you are pausing or searching. Ask the next field IMMEDIATELY and DIRECTLY.\n\n"
        + "⚠️ PHONE NUMBER: The patient is calling FROM their phone. You already have their number. "
        + "NEVER ask for phone number. Only ask for: name, age, department, date, time."
    )

    if collected:
        field_strs = [f"{k}={v}" for k, v in collected.items()]
        parts.append(
            f"✅ Already collected — DO NOT ask for these again: {', '.join(field_strs)}\n"
            f"These fields are CONFIRMED. Never re-ask, re-confirm, or mention them as missing."
        )

    if missing_fields:
        next_field = missing_fields[0]
        
        # MANDATORY instruction: force LLM to ask for the EXACT next field, nothing else
        _mandatory_en = f"⚠️⚠️⚠️ MANDATORY: Ask ONLY for {next_field}. DO NOT ask for any other field. DO NOT skip ahead."
        _mandatory_native_template = _MANDATORY_NATIVE.get(lang, "")
        if _mandatory_native_template:
            _native_field_name = _FIELD_NAMES_NATIVE.get(lang, {}).get(next_field, next_field)
            _mandatory_native = _mandatory_native_template.format(field=_native_field_name)
        else:
            _mandatory_native = ""
        _mandatory_line = f"{_mandatory_en}\n{_mandatory_native}" if _mandatory_native else _mandatory_en

        # Build department hint with available departments if known
        _dept_slot_ban = (
            "⚠️ CRITICAL — NO TIME SLOTS YET: The patient may have mentioned a date or time, "
            "but you CANNOT offer time slots without knowing the department. "
            "DO NOT offer any time slots. DO NOT ask about date or time. "
            "Your ONLY job right now: ask which department/problem they need help with."
        )

        # ── Dynamic date/time hints: reference already-collected counterpart ──
        _already_time = draft.get("time", "")
        _already_date = draft.get("date", "")
        if _already_time:
            _date_hint = (
                f"Time '{_already_time}' is already noted. Ask ONLY for the date now — "
                f"connect it naturally, e.g. '{_already_time} — तो कौन सी तारीख को आना है?' "
                f"⚠️ CRITICAL: Even if the patient said 'okay', 'yes', 'हाँ', 'हाँ जी', 'ठीक है', "
                f"'बिल्कुल', or ANY affirmative WITHOUT giving a date — "
                f"DO NOT reply with only 'okay'/'बिल्कुल'/'ठीक है'. "
                f"Your response MUST include the date question. Ask IMMEDIATELY, no preamble."
            )
        else:
            _date_hint = (
                "You still need the DATE. If user has NOT given date yet: ask 'किस तारीख को आना है?' IMMEDIATELY.\n"
                "⚠️ IF USER PROVIDES DATE IN THIS MESSAGE: Do NOT just say 'ठीक है'/'शुक्रिया'/'बिल्कुल' and stop. "
                "IMMEDIATELY ask for the TIME too — in the SAME response. "
                "Example: 'कल? बढ़िया — किस समय आना है?' — acknowledge in 2-3 words, then ask for time directly.\n"
                "⚠️ IF USER GIVES TIME INSTEAD OF DATE (e.g., 'साढ़े पांच बजे', '5:30 PM', etc.): "
                "acknowledge the time briefly and IMMEDIATELY ask for date. "
                "Example: 'साढ़े पांच बजे के लिए ठीक है! — तो किस तारीख को आना है?' DO NOT just say 'शुक्रिया' and stop.\n"
                "Even if the patient said 'okay' or 'yes' without giving date, your response MUST include the date question."
            )
        if _already_date:
            _time_hint = (
                f"Date '{_already_date}' is already confirmed. Ask ONLY for the time now — "
                f"e.g. 'कौन से समय पर आना है?' Ask IMMEDIATELY, no preamble. "
                f"⚠️ Even if patient said 'okay'/'हाँ'/'ठीक है' without giving time — "
                f"DO NOT reply with only an acknowledgment. Ask for time DIRECTLY in your response. "
                f"DO NOT re-ask for the date."
            )
        else:
            _time_hint = (
                "You still need the TIME. Ask: 'What time works best for you?' — ONLY this. "
                "Ask IMMEDIATELY — even if the patient said 'okay' or 'yes', "
                "your response MUST include the time question."
            )

        if next_field == "department" and clinic_departments:
            dept_list = ", ".join(clinic_departments)
            dept_hint = (
                f"You still need the department. "
                f"This clinic offers: {dept_list}. "
                f"Ask what problem/symptom the patient has and suggest the closest available department.\n"
                + _dept_slot_ban
            )
            field_hints = {
                "name": "You still need the patient's name. Ask naturally. DO NOT ask for phone number — you already have it from caller ID.",
                "age": "You still need the patient's age. Ask casually (just the number).",
                "department": dept_hint,
                "date": _date_hint,
                "time": _time_hint,
            }
        else:
            field_hints = {
                "name": "You still need the patient's name. Ask naturally. DO NOT ask for phone number — you already have it from caller ID.",
                "age": "You still need the patient's age. Ask casually (just the number).",
                "department": (
                    "You still need to know what problem they're facing or which department. Ask what's troubling them.\n"
                    + _dept_slot_ban
                ),
                "date": _date_hint,
                "time": _time_hint,
            }

        hint = field_hints.get(next_field, f"You still need: {next_field}")
        parts.append(_mandatory_line)
        parts.append(hint)
        # Build closing rules block
        _closing = (
            "Ask for ONE field at a time, in this exact order. NEVER skip fields.\n\n"
            f"⚠️ AFFIRMATIVE WITHOUT FIELD: If the patient says 'yes', 'okay', 'हाँ', 'हाँ जी', "
            f"'ठीक है', 'बिल्कुल', 'sure', or any short affirmative WITHOUT providing {next_field} — "
            f"DO NOT reply with just an acknowledgment. Your response MUST ask for {next_field} directly.\n\n"
        )
        if len(missing_fields) > 1:
            next_next = missing_fields[1]
            _closing += (
                f"⚠️ FIELD TRANSITION: If the patient provides {next_field} in THIS message — "
                f"DO NOT just say 'ठीक है'/'शुक्रिया'/'बिल्कुल' and stop. "
                f"IMMEDIATELY ask for {next_next} in the SAME response. "
                f"Acknowledge in 2-3 words MAX, then ask for {next_next} directly. "
                f"Example flow: '[2-word ack] — [question for {next_next}]?'\n\n"
            )
        _closing += (
            f"⚠️ OUT-OF-ORDER FIELD: If the patient provides ANY field that is NOT {next_field} — "
            f"briefly acknowledge it (2-3 words MAX) and IMMEDIATELY ask for {next_field}. "
            f"Your response MUST end with the question for {next_field}. "
            f"NEVER say 'शुक्रिया'/'बिल्कुल'/'ठीक है' and stop without asking for {next_field}.\n\n"
            "⚠️ NEVER offer to book the appointment until you see [Confirm Appointment — ASK FOR PERMISSION].\n"
            "You MUST collect ALL fields first, then WAIT for the system to show [Confirm Appointment]."
        )
        parts.append(_closing)

    # ── Slot availability hint (if date is known) ──
    if slot_info:
        available_times = slot_info.get("available_times", [])
        if available_times:
            sample = ", ".join(available_times[:6])
            parts.append(f"Available times on {draft.get('date', 'that date')}: {sample}")
            if len(available_times) > 6:
                parts.append(f"({len(available_times)} total slots available)")

    return "\n".join(parts)
