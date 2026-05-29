"""
Indian Name Correction Service — sarvam-m
Same prompt as V2, backed by llm.extract_json() instead of Gemini.
"""
import logging
from typing import Tuple, Literal

from llm import extract_json

logger = logging.getLogger(__name__)

ConfidenceLevel = Literal["high", "medium", "low"]

CORRECTION_PROMPT = r"""
You are an expert Indian name correction system. Your task is to fix STT (Speech-to-Text) transcription errors in Indian names.

**Common STT Errors for Indian Names:**
- "Rashto ki Aan" → "Rastogi"
- "Shah ma" → "Sharma"
- "ki Aan" → "Kumar"
- "Patel" → "Patel" (correct)
- "Sing" → "Singh"
- "Gup ta" → "Gupta"
- "Deshmukh" → "Deshmukh" (correct)
- "Priya" → "Priya" (correct)
- "Rahul" → "Rahul" (correct)
- "Ajay" → "Ajay" (correct)

**Common Indian First Names:**
Male: Ajay, Rahul, Amit, Rohan, Arjun, Vikram, Aditya, Karan, Varun, Nikhil, Sanjay, Ravi, Suresh, Rajesh, Arun, Anuj, Akash, Harsh, Manish, Vishal
Female: Priya, Anjali, Sneha, Pooja, Neha, Kavya, Riya, Simran, Divya, Ishita, Ananya, Shreya, Aisha, Meera, Tanvi, Pallavi, Swati, Nikita, Sakshi, Aarti

**Common Indian Surnames (by region):**
North: Sharma, Gupta, Singh, Verma, Kumar, Agarwal, Jain, Chopra, Malhotra, Kapoor, Rastogi, Saxena, Srivastava, Chawla, Bhatia
South: Iyer, Krishnan, Nair, Menon, Rao, Reddy, Naidu, Murthy, Srinivasan, Subramaniam, Pillai, Venkatesh, Ramesh
West: Patel, Shah, Desai, Modi, Mehta, Thakkar, Dave, Trivedi, Joshi, Parikh
East: Banerjee, Chatterjee, Mukherjee, Sen, Das, Roy, Bose, Ghosh, Sarkar, Dutta
Pan-India: Kumar, Singh, Das, Sharma, Verma

**Phonetic Patterns (STT → Correct):**
- "ki Aan" / "ki yan" / "key an" → "Kumar"
- "Rashto ki" / "Rasto gi" / "Rashtogi" → "Rastogi"
- "Shah ma" / "Shar ma" → "Sharma"
- "Gup ta" / "Goop ta" → "Gupta"
- "Ver ma" / "Var ma" → "Verma"
- "Sing" / "Sing h" → "Singh"
- "Pat el" / "Pa tel" → "Patel"
- "Deshmukh" / "Desh muk" → "Deshmukh"
- "Iyer" / "I yer" → "Iyer"
- "Nair" / "Nai r" → "Nair"

**Your Task:**
1. Analyze the transcribed name for STT errors (spaces, phonetic mistakes)
2. Correct it to the most likely valid Indian name
3. If the name is already correct or close to correct, keep it unchanged
4. Return confidence: high (90%+ sure), medium (70-90%), low (<70%)

**Output Format (JSON only):**
{
  "original": "<input name>",
  "corrected": "<corrected name>",
  "confidence": "high|medium|low",
  "reasoning": "<brief 1-sentence explanation>"
}

**Examples:**

Input: "Ajay Rashto ki Aan"
Output: {"original": "Ajay Rashto ki Aan", "corrected": "Ajay Rastogi", "confidence": "high", "reasoning": "Rashto ki Aan is phonetic error for Rastogi surname"}

Input: "Priya Shah ma"
Output: {"original": "Priya Shah ma", "corrected": "Priya Sharma", "confidence": "high", "reasoning": "Shah ma is phonetic error for Sharma surname"}

Input: "Rahul Patel"
Output: {"original": "Rahul Patel", "corrected": "Rahul Patel", "confidence": "high", "reasoning": "Name is already correct"}

Input: "Amit ki Aan"
Output: {"original": "Amit ki Aan", "corrected": "Amit Kumar", "confidence": "high", "reasoning": "ki Aan is phonetic error for Kumar surname"}

Input: "John Smith"
Output: {"original": "John Smith", "corrected": "John Smith", "confidence": "medium", "reasoning": "Not an Indian name, keeping as-is"}

Input: "K"
Output: {"original": "K", "corrected": "K", "confidence": "low", "reasoning": "Insufficient information, single letter could be initial or incomplete name"}

**Important Rules:**
- Do NOT add words that weren't in the input (no "Dr.", "Mr.", etc.)
- Do NOT change correctly spelled names
- If uncertain, prefer returning original with "low" confidence
- Return JSON only, no extra text
"""


async def correct_indian_name(
    transcribed_name: str,
    timeout: float = 2.0,
) -> Tuple[str, ConfidenceLevel, str]:
    """
    Correct STT transcription errors in Indian names using sarvam-m.

    Args:
        transcribed_name: Name from STT (may have errors)
        timeout: Max time to wait (seconds)
    
    Returns:
        Tuple of (corrected_name, confidence_level, reasoning)
    """
    if not transcribed_name or len(transcribed_name.strip()) == 0:
        return transcribed_name, "low", "Empty name"

    if len(transcribed_name.strip()) <= 2:
        return transcribed_name, "low", "Name too short to correct"

    user_prompt = f"Transcribed name: {transcribed_name}\nCorrect name (JSON):"

    try:
        result = await extract_json(
            system_prompt=CORRECTION_PROMPT,
            user_prompt=user_prompt,
            timeout_s=timeout,
        )

        if not result:
            return transcribed_name, "low", "Failed to parse correction"

        corrected = result.get("corrected", transcribed_name).strip()
        confidence = result.get("confidence", "low")
        reasoning = result.get("reasoning", "")

        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        logger.info(
            f"Name correction: '{transcribed_name}' → '{corrected}' "
            f"(confidence: {confidence})"
        )
        return corrected, confidence, reasoning

    except Exception as error:
        logger.error(f"Name correction failed: {error}")
        return transcribed_name, "low", f"Error: {error}"
