"""Deterministic language understanding for English, Telugu and Hindi.

The engine is intentionally transparent: every label comes from a named rule so
the evidence ledger can cite it. An optional Claude adapter can add polish, but
the demo never depends on a model call.
"""

from __future__ import annotations

import re
from typing import Any

TELUGU = range(0x0C00, 0x0C80)
DEVANAGARI = range(0x0900, 0x0980)


def detect_language(text: str, requested: str | None = None) -> str:
    if requested in {"en", "te", "hi"}:
        return requested
    if any(ord(ch) in TELUGU for ch in text):
        return "te"
    if any(ord(ch) in DEVANAGARI for ch in text):
        return "hi"
    return "en"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


STATE_KEYWORDS: dict[str, list[str]] = {
    "Maharashtra": ["maharashtra", "pune", "mumbai", "nagpur", "nashik", "पुणे", "मुंबई", "महाराष्ट्र", "పూణే", "ముంబై"],
    "Telangana": ["telangana", "hyderabad", "warangal", "हैदराबाद", "तेलंगाना", "హైదరాబాద్", "తెలంగాణ"],
    "Karnataka": ["karnataka", "bengaluru", "bangalore", "mysuru", "बेंगलुरु", "बैंगलोर", "कर्नाटक", "బెంగళూరు", "కర్ణాటక"],
    "Central": ["central government", "government of india", "ministry", "passport", "income tax", "railway", "national highway", "defence", "delhi", "केंद्र सरकार", "भारत सरकार", "కేంద్ర ప్రభుత్వం"],
}

# (keyword, weight). Distinctive words weigh more than location words such as
# "ward", which appear in requests about any department.
DEPARTMENT_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "public_works": [
        ("road", 3), ("roads", 3), ("pothole", 3), ("highway", 3), ("bridge", 3),
        ("flyover", 3), ("pavement", 2), ("public works", 3), ("construction", 2),
        ("రోడ్డు", 3), ("రోడ్", 3), ("వంతెన", 3), ("నిర్మాణం", 2),
        ("सड़क", 3), ("पुल", 3), ("निर्माण", 2), ("गड्ढा", 3), ("गड्ढे", 3),
    ],
    "municipal": [
        ("street light", 4), ("streetlight", 4), ("street lights", 4), ("garbage", 3),
        ("drainage", 3), ("sewer", 3), ("municipal corporation", 3), ("municipality", 3),
        ("municipal", 2), ("encroachment", 2), ("footpath", 2), ("ward", 1),
        ("వీధి దీపం", 4), ("వీథి దీపం", 4), ("చెత్త", 3), ("డ్రైనేజీ", 3),
        ("మున్సిపల్", 2), ("వార్డు", 1), ("వార్డ్", 1),
        ("स्ट्रीट लाइट", 4), ("कचरा", 3), ("नाली", 3), ("नगरपालिका", 3),
        ("नगर निगम", 3), ("वार्ड", 1),
    ],
    "home": [
        ("police", 3), ("fir", 3), ("police station", 3), ("crime", 2), ("arrest", 2),
        ("పోలీసు", 3), ("పోలీస్", 3), ("ఎఫ్ఐఆర్", 3),
        ("पुलिस", 3), ("एफआईआर", 3), ("थाना", 3), ("अपराध", 2),
    ],
    "health": [
        ("hospital", 3), ("health", 2), ("medicine", 2), ("ambulance", 3), ("doctor", 2),
        ("clinic", 2), ("vaccination", 2), ("ఆసుపత్రి", 3), ("ఆరోగ్య", 2), ("అంబులెన్స్", 3),
        ("अस्पताल", 3), ("स्वास्थ्य", 2), ("दवा", 2), ("एम्बुलेंस", 3),
    ],
    "water": [
        ("water supply", 3), ("pipeline", 3), ("water pipeline", 3), ("tap", 2),
        ("dam", 2), ("irrigation", 3), ("water", 2),
        ("నీటి సరఫరా", 3), ("నీరు", 2), ("పైప్‌లైన్", 3), ("బావి", 2),
        ("पानी", 2), ("जल", 2), ("पाइपलाइन", 3), ("नल", 2),
    ],
    "transport": [
        ("bus", 2), ("rtc", 3), ("railway", 3), ("train", 2), ("transport", 2),
        ("బస్సు", 2), ("రైలు", 2), ("రవాణా", 2),
        ("बस", 2), ("रेल", 2), ("परिवहन", 2),
    ],
    "revenue": [
        ("land record", 3), ("land", 2), ("survey", 2), ("property", 2), ("patta", 3),
        ("registration", 2), ("భూమి", 2), ("పట్టా", 3), ("జమీన్", 2),
        ("जमीन", 2), ("भूमि", 2), ("पट्टा", 3), ("रजिस्ट्री", 2),
    ],
    "education": [
        ("school", 2), ("college", 2), ("scholarship", 3), ("teacher", 2),
        ("పాఠశాల", 2), ("కళాశాల", 2), ("స్కాలర్‌షిప్", 3), ("ఉపాధ్యాయుడు", 2),
        ("स्कूल", 2), ("कॉलेज", 2), ("छात्रवृत्ति", 3), ("शिक्षक", 2),
    ],
    "environment": [
        ("pollution", 3), ("tree", 2), ("environment", 2), ("కాలుష్యం", 3), ("చెట్టు", 2),
        ("प्रदूषण", 3), ("पेड़", 2), ("पर्यावरण", 2),
    ],
}

RECORDS_SIGNALS = [
    "information", "copy", "copies", "details", "record", "records", "document", "documents",
    "how much", "how many", "what is", "what are", "list of", "expenditure", "spent", "spending",
    "budget", "contract", "agreement", "work order", "voucher", "bill", "bills", "register",
    "minutes", "report", "particulars", "provide", "give me", "tell me", "know", "certified",
    "తెలియజేయండి", "తెలపండి", "వివరాలు", "వివరములు", "నకళ్లు", "ప్రతులు", "ఎంత", "ఎన్ని",
    "ఖర్చు", "బడ్జెట్", "ఒప్పందం", "నివేదిక",
    "बताइए", "बताएं", "जानकारी", "विवरण", "प्रतियां", "प्रतिलिपि", "कितना", "कितने",
    "खर्च", "बजट", "अनुबंध", "रिपोर्ट", "दस्तावेज",
]

ACTION_PATTERNS = [
    r"(?<!\w)(please\s+)?(fix|clean|remove|install|replace|restore|stop|cancel)(?!\w)",
    r"(?<!\w)(please\s+)?repair\s+(this|it|the|my|our)\b",
    r"(?<!\w)(take|initiate)\s+(disciplinary\s+)?action(?!\w)",
    r"action\s+against",
    r"(?<!\w)complaint(?!\w)",
    r"should\s+be\s+(fixed|repaired|removed|cleaned)",
    r"(?<!\w)(చేయండి|చేయవలసిందిగా|తొలగించండి|మరమ్మతు\s+చేయండి|చర్య\s+తీసుకోండి|ఫిర్యాదు)(?!\w)",
    r"(?<!\w)(ठीक\s+करें|ठीक\s+करवाएं|कार्रवाई|हटाएं|शिकायत)(?!\w)",
]

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(the\s+)?(system|previous|above)",
    r"system\s+prompt",
    r"you\s+are\s+now\s+(an?\s+)?(admin|developer|approver)",
    r"developer\s+mode",
    r"jailbreak",
    r"mark\s+(this|it)\s+as\s+(approved|filed)",
    r"without\s+(human\s+)?(review|approval)",
    r"override\s+(the\s+)?(gate|approval|rules)",
    r"act\s+as\s+(an?\s+)?(admin|approver)",
]

PERSONAL_TERMS = [
    "home address", "residential address", "personal address", "phone number", "mobile number",
    "personal phone", "aadhaar", "aadhar", "pan number", "salary of", "date of birth",
    "family details", "personal details", "personal information",
    "ఫోన్ నంబర్", "చిరునామా", "వ్యక్తిగత వివరాలు", "జీతం",
    "फोन नंबर", "निजी पता", "व्यक्तिगत जानकारी", "वेतन",
    # Third-party records as they are phrased in applicants' requests (RTI-Bench).
    "bank account", "account of", "accounts of", "fixed deposit", "fdr", "pension", "salary",
    "insurance policy", "insurance policies", "passport", "telephone", "telephone number",
    "medical record", "patient", "treatment given", "tax return", "income tax return",
    "deceased", "bank statement",
]

PERSON_TERMS = [
    "officer", "employee", "person", "individual", "applicant", "engineer", "clerk",
    "ward officer", "third party",    "official",
    "particular person", "certain person", "a certain", "his son", "her son",
    "husband", "wife", "daughter", "someone", "account holder", "imposter",
    "అధికారి", "ఉద్యోగి", "వ్యక్తి", "अधिकारी", "कर्मचारी", "व्यक्ति",
]

URGENCY_TERMS = [
    "life or liberty", "life and liberty", "danger to life", "threat to life", "loss of life",
    "life-threatening", "life threatening", "risk to life", "emergency", "ambulance", "oxygen",
    "icu", "dying", "death", "medical emergency", "safety of",
    "ప్రాణ", "జీవితం", "మరణం", "ఆసుపత్రి", "అత్యవసర", "ఆక్సిజన్",
    "जान", "जीवन", "मौत", "अस्पताल", "आपातकाल", "ऑक्सीजन",
]

OPINION_PATTERNS = [
    r"(?<!\w)why(?!\w)", r"who\s+is\s+responsible", r"(?<!\w)explain(?!\w)",
    r"reason\s+for", r"how\s+could", r"should\s+have",
    r"ఎందుకు", r"बताइए\s+क्यों", r"(?<!\w)क्यों(?!\w)", r"जिम्मेदार",
]

BPL_PATTERN = re.compile(r"(?<!\w)(bpl|below poverty line|antyodaya|बीपीएल|अंत्योदय|బీపీఎల్|అంత్యోదయ)(?!\w)", re.I)
NAME_PATTERNS = [
    r"my name is ([A-Za-z][A-Za-z .'-]{2,40})",
    r"నా పేరు ([^\s,.]{2,40})",
    r"मेरा नाम ([^\s,.]{2,40})",
]


def _contains(text: str, needle: str) -> bool:
    return needle in text


def extract(text: str) -> dict[str, Any]:
    lowered = normalise(text)
    clean = strip_injections(text)

    # State / level
    state = None
    state_hits: list[str] = []
    for name, keywords in STATE_KEYWORDS.items():
        for kw in keywords:
            if _contains(lowered, kw):
                state = name if state is None else state
                state_hits.append(kw)
    # Longer, more specific matches win over "delhi" short forms.
    if state_hits:
        for name, keywords in STATE_KEYWORDS.items():
            for kw in keywords:
                if len(kw) > 5 and _contains(lowered, kw):
                    state = name
                    break

    # Department with weighted scoring.
    scores: dict[str, float] = {}
    dept_hits: list[str] = []
    for dept, keywords in DEPARTMENT_KEYWORDS.items():
        for kw, weight in keywords:
            if _contains(lowered, kw):
                scores[dept] = scores.get(dept, 0) + weight
                dept_hits.append(kw)
    department = max(scores, key=scores.get) if scores else None
    dept_score = scores.get(department, 0) if department else 0

    if state == "Central" or any(k in lowered for k in ("railway", "train", "national highway", "passport", "ministry", "income tax")):
        level = "central"
    else:
        level = "state"
    if level == "central":
        state = "Central"

    records = any(_contains(lowered, s) for s in RECORDS_SIGNALS)
    action = any(re.search(p, lowered) for p in ACTION_PATTERNS)
    personal = any(_contains(lowered, s) for s in PERSONAL_TERMS) and any(
        _contains(lowered, s) for s in PERSON_TERMS
    )
    urgency = any(_contains(lowered, s) for s in URGENCY_TERMS)
    opinion = any(re.search(p, lowered) for p in OPINION_PATTERNS)
    bpl = bool(BPL_PATTERN.search(lowered))
    injection = bool(injection_spans(text))

    applicant = "Synthetic Applicant"
    for pattern in NAME_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            applicant = m.group(1).strip()
            break

    return {
        "state": state,
        "level": level,
        "department": department,
        "department_score": dept_score,
        "state_hits": state_hits,
        "department_hits": sorted(set(dept_hits)),
        "records_signal": records,
        "action_signal": action,
        "personal_data": personal,
        "urgency": urgency,
        "opinion": opinion,
        "bpl": bpl,
        "injection": injection,
        "applicant_name": applicant,
        "language": detect_language(text),
        "clean_text": clean,
        "information_needed": clean.strip(),
    }


def injection_spans(text: str) -> list[str]:
    spans = []
    for pattern in INJECTION_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            spans.append(match.group(0))
    return spans


def strip_injections(text: str) -> str:
    """Remove instruction-like sentences so they never reach a draft."""
    sentences = re.split(r"(?<=[.!?।])\s+", text)
    kept = []
    for sentence in sentences:
        if injection_spans(sentence):
            continue
        kept.append(sentence)
    return " ".join(kept).strip() or text.strip()


def grievance_signal(text: str) -> bool:
    lowered = normalise(text)
    return any(re.search(p, lowered) for p in ACTION_PATTERNS)


def has_records_signal(text: str) -> bool:
    lowered = normalise(text)
    return any(s in lowered for s in RECORDS_SIGNALS)


def has_personal_data(text: str) -> bool:
    lowered = normalise(text)
    return any(s in lowered for s in PERSONAL_TERMS) and any(s in lowered for s in PERSON_TERMS)


def has_urgency(text: str) -> bool:
    lowered = normalise(text)
    return any(s in lowered for s in URGENCY_TERMS)


def has_opinion(text: str) -> bool:
    lowered = normalise(text)
    return any(re.search(p, lowered) for p in OPINION_PATTERNS)
