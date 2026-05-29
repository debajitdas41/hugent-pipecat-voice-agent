"""
Simplified field extraction for appointment booking — Sarvam-30B.
Extracts structured fields from natural speech. Used as a silent background
step; the LLM generates all user-facing responses.
"""
import json
import logging

from llm import extract_json

logger = logging.getLogger("extraction")

EXTRACTION_PROMPT = r"""Extract appointment booking fields from user speech. Output ONLY a JSON object — no explanation, no markdown, no extra text.

JSON keys: intent, name, age, department, date, time, confirm

FIELD ISOLATION (CRITICAL): Extract ONLY fields the user explicitly mentions in the CURRENT turn.
DO NOT copy fields from "Current booking state". DO NOT infer from previous turns. DO NOT echo the draft.
ONLY extract what is SAID RIGHT NOW in the "User said" line.
  - User says only their name → ONLY name, everything else null.
  - User says only a number (age) → ONLY age, everything else null.
  - User says only yes/no/haan → ONLY confirm+intent, everything else null.
  - User says date/time only → ONLY date/time, everything else null.
  - If user says "I want appointment at 7 AM" → ONLY time, NOT name/age/department from draft.

intent: "book" | "reschedule" | "cancel" | "none" | "confirm"
  - Use "book" for ALL new appointment requests. This includes:
      • bare single words (any language) → ALWAYS "book":
          EN: "appointment", "booking", "apointment"
          HI: "अपॉइंटमेंट", "बुकिंग", "अपॉइन्टमेंट"
          BN: "অ্যাপয়েন্টমেন্ট", "বুকিং", "অ্যাপয়েন্ট"
          TA: "அப்பாயிண்ட்மெண்ட்", "புக்கிங்"
          TE: "అపాయింట్మెంట్", "బుకింగ్"
          KN: "ಅಪಾಯಿಂಟ್ಮೆಂಟ್", "ಬುಕಿಂಗ್"
          ML: "അപ്പോയിന്റ്മെന്റ്", "ബുക്കിംഗ്"
          MR: "अपॉइंटमेंट", "बुकिंग"
          GU: "એપોઈન્ટમેન્ટ", "બુકિંગ"
          PA: "ਅਪੌਇੰਟਮੈਂਟ", "ਬੁਕਿੰਗ"
          OD: "ଅ୍ଯାପୟିଣ୍ଟମେଣ୍ଟ", "ବୁକିଂ"
      • request phrases (any language):
          HI: "dalna hai", "chahiye", "lena hai", "karni hai", "karwani hai", "book karna hai", "doctor dikhana hai", "doctor se milna hai", "दिखाना है", "मिलना है", "लेनी है", "चाहिए"
          BN: "বুক করতে চাই", "অ্যাপয়েন্ট নিতে চাই", "ডাক্তার দেখাতে চাই"
          TA: "புக் செய்யணும்", "டாக்டர் பார்க்கணும்", "அப்பாயிண்ட்மெண்ட் வேணும்"
          TE: "బుక్ చేయాలి", "డాక్టర్ చూపించాలి", "అపాయింట్మెంట్ కావాలి"
          KN: "ಬುಕ್ ಮಾಡಬೇಕು", "ಡಾಕ್ಟರ್ ತೋರಿಸಬೇಕು", "ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಬೇಕು"
          ML: "ബുക്ക് ചെയ്യണം", "ഡോക്ടറെ കാണണം", "അപ്പോയിന്റ്മെന്റ് വേണം"
          MR: "बुकिंग करायची", "डॉक्टर दाखवायचे", "अपॉइंटमेंट हवी"
          GU: "બુકિંગ કરવી", "ડૉક્ટર ને મળવું", "એપોઈન્ટમેન્ટ જોઈએ"
          PA: "ਬੁਕਿੰਗ ਕਰਨੀ", "ਡਾਕਟਰ ਨੂੰ ਮਿਲਣਾ", "ਅਪੌਇੰਟਮੈਂਟ ਚਾਹੀਦੀ"
          OD: "ବୁକ କରିବା", "ଡାକ୍ତର ଦେଖାଇବା", "ଅ୍ଯାପୟିଣ୍ଟମେଣ୍ଟ ଦରକାର"
      • If user says just ONE word meaning appointment (in any script/language) — they are asking to book. Use "book".
  - Use "book" when user selects or asks about a time slot in an ONGOING booking conversation.
  - Use "confirm" when user says affirmative phrases in any language:
      EN: yes, yeah, yep, yup, okay, ok, sure, correct, right, that's right, it is, exactly, confirm, book it, go ahead, please, proceed, absolutely, definitely, fine, alright, sounds good
      HI: हाँ, हां, जी, जी हाँ, ठीक है, ठीक, बिल्कुल, ज़रूर, सही है, ये है, यह है, वो है, सही है जी, ओके, कर दीजिए, बुक करो, confirm करो, कृपया, चलिए, बिल्कुल ठीक
      BN: হ্যাঁ, হ্যাঁ জি, ঠিক আছে, ঠিক, এটা ঠিক, সেটা, সঠিক, এই, করুন, বুক করুন, confirm করুন, দয়া করে, চলুন, একদম ঠিক
      TA: ஆமாம், ஆம், சரி, சரிதான், அப்படிதான், ஓகே, புக் பண்ணுங்க, confirm பண்ணுங்க, தயவு செய்து, சரிதான்
      TE: అవును, అవునండి, సరే, సరిగ్గా, అలాగే, అదే, ఓకే, బుక్ చెయ్యి, confirm చెయ్యి, దయచేసి, అలాగే
      KN: ಹೌದು, ಹೌದು ಜೀ, ಸರಿ, ಸರಿಯಾಗಿದೆ, ಅದೇ, ಓಕೆ, ಬುಕ್ ಮಾಡಿ, confirm ಮಾಡಿ, ದಯವಿಟ್ಟು, ಸರಿ
      ML: അതേ, ശരി, ശരിയാണ്, അതാണ്, ഓകെ, ബുക്ക് ചെയ്യൂ, confirm ചെയ്യൂ, ദയവായി, ശരിയാണ്
      MR: हो, होय, ठीक आहे, ठीक, बरोबर, तेच, ओके, बुक करा, confirm करा, कृपया, चालेल, बिल्कुल
      GU: હા, હા જી, સારું, બરોબર, એજ, ઓકે, બુક કરો, confirm કરો, કૃપા કરીને, ચાલશે, બિલકુલ
      PA: ਹਾਂ, ਹਾਂ ਜੀ, ਠੀਕ, ਠੀਕ ਹੈ, ਬਿਲਕੁਲ, ਸਹੀ, ਓਕੇ, ਬੁਕ ਕਰੋ, confirm ਕਰੋ, ਕਿਰਪਾ ਕਰਕੇ, ਚੱਲੋ
      OD: ହଁ, ହଁ ଜୀ, ଠିକ, ଠିକ ଅଛି, ସଠିକ, ଓକେ, ବୁକ କର, confirm କର, ଦୟାକରି, ଚାଲିବ, ବିଲକୁଲ
  - Use "reschedule" ONLY if user explicitly says they want to CHANGE a previously BOOKED appointment.
  - Use "cancel" ONLY if user explicitly says "cancel", "nahi chahiye", "band karo", "hata do". NOT for farewells.
  - NEVER use "cancel" for farewell/negative responses: "thank you", "dhanyawad", "shukriya", "bye",
    "nahi thank you", "और नहीं", "कुछ नहीं", "नहीं बस", "बस यही", "कुछ और नहीं", "नहीं थैंक यू",
    "ধন্যবাদ", "நன்றி", "ధన్యవాదాలు" → use "none" for all of these.
  - Use "none" for greetings, clarifications, garbled speech, farewells, or anything else.

name: string or null
  - ONLY extract a proper person name. Reject garbled STT like single syllables, random words.
  - If speech is a fragment like "मेरा नाम है।" WITHOUT a name following, return null.
  - Use recent speech context if the current turn is incomplete (e.g. age follows "उम्र है।").

age: number as string or null
  - MUST be a digit between 1 and 120. Supports Devanagari (२९=29), Bengali (২৯=29), etc.
  - If recent context shows "उम्र है।" and current turn is "२९।", extract age="29".
  - NEVER extract age from time values, prices, or non-age numbers.

department: string or null — preserve original spoken form (e.g. "orthopedic", "ऑर्थोपेडिक").
  - ONLY set if user mentions a body part, symptom, disease, or medical specialty IN THIS TURN.
  - If user says their name, age, a yes/no, a date, or a time → return null for department.
  - Examples: "मेरा नाम राहुल है" → null. "29 साल" → null. "हाँ" → null. "कल 3 बजे" → null.
  - If user says "doctor", "डॉक्टर" without specifying a specialty → return null.
  - Map symptom → department: "tooth pain"→"Dental", "skin rash"→"Dermatology", "knee pain"→"Orthopedic",
    "ear/screen problem"→"ENT", "eye problem"→"Ophthalmology", "stomach/pet pain"→"Gastroenterology".

date: "today" | "tomorrow" | "day after tomorrow" | "Monday" | "DD MMM" | "next week" | null
  - Date means WHEN the patient wants to COME for their appointment.
  - ✅ IMPORTANT: Recognize "day after tomorrow" phrases in ALL languages:
      EN: "day after tomorrow", "overmorrow"
      HI: "परसों", "parso", "parson", "परसो", "day after tom"
      BN: "পরশু", "পরশুদিন"
      TA: "நாளை மறுநாள்"
      TE: "ఎల్లుండి"
      KN: "ನಾಳಿದ್ದು"
      ML: "മറ്റന്നാൾ"
      GU: "પરમ દિવસે"
      MR: "परवा"
      PA: "ਪਰਸੋਂ"
      OD: "ପରଶୁଦିନ"
    If you hear any of these, return "day after tomorrow" (NOT null).
  - Duration phrases describe how long they've had a symptom — they are NOT dates:
    "X दिन से", "X दिनों से", "X दिन से ज़्यादा", "since X days", "for X days",
    "past X days", "last X days", "X दिन हो गए", "कुछ दिनों से", "कई दिनों से" → return null.
  - For "week later", "एक हफ्ते बाद" → "next week". Do NOT return vague phrases.

time: "HH:MM AM/PM" | "morning" | "evening" | "afternoon" | null
  - NEVER extract time from age values.
  - For "सन्ध्या ७", "शाम 7", "7 baje" (without AM/PM) → return "07:00 PM".
  - For "सुबह 10" → "10:00 AM". For "दोपहर 2" → "02:00 PM".
  - VAGUE TIME WORDS: "early", "around early", "early morning", "morning time", "subah" → time: "morning" (NOT a specific hour like "08:00 AM").
    "evening time", "shaam", "evening", "raat", "night time" → time: "evening" (NOT a specific hour).
    "afternoon", "dopahar", "dopaher", "dupahiya" → time: "afternoon" (NOT a specific hour).
    These are PREFERENCES — the system will offer actual slots. Never invent a specific time from a vague word.
  - OFFERED SLOT SELECTION: If user says a number/word clearly selecting from a previously mentioned list,
    extract the EXACT format from that list. See OFFERED SLOT MAPPING section below if provided.

confirm: true | false | null
  - true: Affirmative words/phrases in ANY language:
      EN: yes, yeah, okay, ok, sure, right, correct, that's right, it is, exactly, go ahead, please, proceed, absolutely, definitely, fine, alright, sounds good
      HI: हाँ, हां, जी, ठीक है, ठीक, बिल्कुल, सही है, ये है, यह है, वो है, ओके, कर दीजिए, कृपया, चलिए
      BN: হ্যাঁ, ঠিক আছে, ঠিক, এটা ঠিক, সঠিক, এই, সেটা, করুন, দয়া করে, চলুন
      TA: ஆமாம், ஆம், சரி, சரிதான், அப்படிதான், ஓகே, தயவு செய்து
      TE: అవును, సరే, సరిగ్గా, అలాగే, అదే, ఓకే, దయచేసి
      KN: ಹೌದು, ಸರಿ, ಸರಿಯಾಗಿದೆ, ಅದೇ, ಓಕೆ, ದಯವಿಟ್ಟು
      ML: അതേ, ശരി, ശരിയാണ്, അതാണ്, ഓകെ, ദയവായി
      MR: हो, होय, ठीक, बरोबर, तेच, ओके
      GU: હા, સારું, બરોબર, એજ, ઓકે
      PA: ਹਾਂ, ਠੀਕ, ਬਿਲਕੁਲ, ਸਹੀ, ਓਕੇ
      OD: ହଁ, ଠିକ, ସଠିକ, ଓକେ
  - false: Negative/rejection words:
      EN: no, nope, nah, not right, wrong, change it
      HI: नहीं, नही, गलत है, बदलो, change करो
      BN: না, নাহ, ভুল, বদল করুন
      And similar negatives in all 11 languages
  - null: user mentioned a time/date/name/field but did NOT explicitly confirm or deny

Only extract what user explicitly said or what can be inferred from recent speech + current turn.
Return null for anything genuinely not mentioned.
"""


async def extract_booking_info(
    user_text: str,
    current_draft: dict | None = None,
    clinic_departments: list[str] | None = None,
    recent_utterances: list[str] | None = None,
    offered_times: list[str] | None = None,
) -> dict:
    """
    Extract booking fields from user's natural language input.
    Returns dict with: intent, name, age, department, date, time, confirm, language
    """
    draft_text = json.dumps(current_draft or {}, ensure_ascii=False)

    # Start with the base extraction prompt (always needed)
    system_prompt = EXTRACTION_PROMPT

    # Include recent speech fragments so the extraction LLM can piece together
    # barge-in splits like "उम्र है।" + "२९।" into a single age extraction.
    recent_ctx = ""
    if recent_utterances:
        fragments = " | ".join(u.strip() for u in recent_utterances if u.strip())
        if fragments:
            recent_ctx = f"Recent speech (prior turns, may be fragments): {fragments}\n"

    # Add offered slot mapping so user selections like "Six" resolve to "06:00 PM"
    if offered_times:
        lines = []
        for slot in offered_times:
            # Build number word hints for each slot
            hour_part = slot.split(":")[0].lstrip("0") or "0"
            period = slot.split(" ")[1] if " " in slot else ""
            lines.append(f'  "{slot}" — user may say: "{hour_part}", "{hour_part} {period}", or spoken-word form (e.g. "six", "five thirty")')
        system_prompt += (
            "\n\nOFFERED SLOT MAPPING (CRITICAL — takes priority over vague inference):\n"
            "The available slots offered to the user are:\n"
            + "\n".join(lines)
            + "\nIf the user's word clearly selects one of the above slots, return that slot's EXACT string as \"time\". "
            "Examples: if offered=[\"05:00 PM\", \"05:30 PM\", \"06:00 PM\"] and user says \"Six\" or \"6 PM\" → time=\"06:00 PM\"; "
            "\"five thirty\" or \"5:30\" → time=\"05:30 PM\"; \"five\" or \"5 PM\" → time=\"05:00 PM\"."
        )

    # Add clinic-specific departments regardless of whether there are recent utterances
    if clinic_departments:
        dept_list = ", ".join(clinic_departments)
        system_prompt += (
            f"\n\nAVAILABLE DEPARTMENTS at this clinic: {dept_list}\n"
            f"When user mentions a symptom or body part (e.g. 'tooth pain', 'skin rash', 'knee pain', 'ear problem'), "
            f"extract 'department' as the closest matching department from the above list.\n"
            f"If the symptom implies a specialty NOT in the list (e.g. stomach pain → Gastroenterology), "
            f"still extract that specialty name — the system will inform the patient and suggest available options."
        )

    user_prompt = (
        f"Current booking state: {draft_text}\n"
        f"{recent_ctx}"
        f"User said: \"{user_text}\"\n"
        f"Extract JSON:"
    )

    try:
        result = await extract_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_s=8.0,   # reasoning model is slow; this runs in background so it doesn't affect TTFA
        )
        if not result:
            logger.warning(f"Extraction returned empty for: '{user_text}'")
            return {"intent": "none"}

        # Validate age: reject anything that isn't a plausible number (1-120).
        # Trust LLM to extract ages from spoken words (e.g. "twenty nine", "उन्तीस").
        # The range check below catches true hallucinations.
        age = result.get("age")

        if age is not None:
            try:
                # Normalise regional digits to ASCII before parsing:
                # Devanagari ०-९, Bengali ০-৯, Tamil ௦-௯, etc.
                age_str = str(age).strip().split()[0]
                age_str = age_str.translate(str.maketrans(
                    "०१२३४५६७८९"   # Devanagari
                    "০১২৩৪৫৬৭৮৯"   # Bengali
                    "੦੧੨੩੪੫੬੭੮੯"   # Gurmukhi
                    "૦૧૨૩૪૫૬૭૮૯"   # Gujarati
                    "೦೧೨೩೪೫೬೭೮೯"   # Kannada
                    "൦൧൨൩൪൫൬൭൮൯"   # Malayalam
                    "௦௧௨௩௪௫௬௭௮௯"   # Tamil
                    "౦౧౨౩౪౫౬౭౮౯",  # Telugu
                    "0123456789" * 8
                ))
                age_num = int(age_str)
                if not (1 <= age_num <= 120):
                    raise ValueError
                result["age"] = str(age_num)  # normalise to plain ASCII digits
            except (ValueError, AttributeError):
                logger.warning(f"Rejecting non-numeric age: '{age}'")
                result["age"] = None

        logger.info(f"Extracted: {result}")
        return result

    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return {"intent": "none"}
