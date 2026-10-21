"""Deterministic language understanding for English, Telugu, Hindi, Marathi, Kannada and Tamil.

The engine is intentionally transparent: every label comes from a named rule so
the evidence ledger can cite it. An optional Claude adapter can add polish, but
the demo never depends on a model call.

The Marathi, Kannada and Tamil keyword lists are first-pass and have not been
reviewed by native speakers. They are deliberately small: a wrong keyword shows up
as a wrong department, which the routing confidence and confirmation step expose.
"""

from __future__ import annotations

import re
from typing import Any

TELUGU = range(0x0C00, 0x0C80)
DEVANAGARI = range(0x0900, 0x0980)
KANNADA = range(0x0C80, 0x0D00)
TAMIL = range(0x0B80, 0x0C00)

# Marathi shares Devanagari with Hindi, so the script alone cannot separate them.
# Deliberately grammatical, not proper nouns: "पुणे" appears in Hindi requests too.
MARATHI_MARKERS = (
    "आहे", "मला", "माहिती", "किती", "झाला", "झाले", "मध्ये", "साठी",
    "करा", "द्या", "नमस्कार", "रस्ता", "पाणी", "शाळा", "मिळकत",
)


def detect_language(text: str, requested: str | None = None) -> str:
    if requested in {"en", "te", "hi", "mr", "kn", "ta"}:
        return requested
    if any(ord(ch) in KANNADA for ch in text):
        return "kn"
    if any(ord(ch) in TAMIL for ch in text):
        return "ta"
    if any(ord(ch) in TELUGU for ch in text):
        return "te"
    if any(ord(ch) in DEVANAGARI for ch in text):
        return "mr" if any(marker in text for marker in MARATHI_MARKERS) else "hi"
    return "en"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# Every state and union territory is listed so a request outside the three pilot
# states is still routed to a jurisdiction and confirmed, rather than dropped.
STATE_KEYWORDS: dict[str, list[str]] = {
    "Andhra Pradesh": ["andhra pradesh", "amaravati", "visakhapatnam", "vijayawada", "ఆంధ్రప్రదేశ్"],
    "Arunachal Pradesh": ["arunachal pradesh", "itanagar"],
    "Assam": ["assam", "dispur", "guwahati"],
    "Bihar": ["bihar", "patna"],
    "Chhattisgarh": ["chhattisgarh", "raipur"],
    "Goa": ["goa", "panaji"],
    "Gujarat": ["gujarat", "ahmedabad", "gandhinagar", "surat"],
    "Haryana": ["haryana", "gurugram", "faridabad"],
    "Himachal Pradesh": ["himachal pradesh", "shimla"],
    "Jharkhand": ["jharkhand", "ranchi"],
    "Karnataka": [
        "karnataka", "bengaluru", "bangalore", "mysuru",
        "बेंगलुरु", "बैंगलोर", "कर्नाटक", "బెంగళూరు", "కర్ణాటక",
        "ಬೆಂಗಳೂರು", "ಕರ್ನಾಟಕ", "ಮೈಸೂರು", "பெங்களூரு", "கர்நாடகா",
    ],
    "Kerala": ["kerala", "thiruvananthapuram", "kochi", "kozhikode"],
    "Madhya Pradesh": ["madhya pradesh", "bhopal", "indore"],
    "Maharashtra": [
        "maharashtra", "pune", "mumbai", "nagpur", "nashik",
        "पुणे", "पुण्य", "मुंबई", "महाराष्ट्र", "పూణే", "ముంబై",
        "ಪುಣೆ", "ಮುಂಬೈ", "ಮಹಾರಾಷ್ಟ್ರ", "புனே", "மும்பை", "மகாராஷ்டிரா",
    ],
    "Manipur": ["manipur", "imphal"],
    "Meghalaya": ["meghalaya", "shillong"],
    "Mizoram": ["mizoram", "aizawl"],
    "Nagaland": ["nagaland", "kohima"],
    "Odisha": ["odisha", "bhubaneswar", "cuttack"],
    "Punjab": ["punjab", "amritsar", "ludhiana"],
    "Rajasthan": ["rajasthan", "jaipur", "jodhpur"],
    "Sikkim": ["sikkim", "gangtok"],
    "Tamil Nadu": ["tamil nadu", "chennai", "madurai", "coimbatore", "தமிழ்நாடு", "சென்னை", "மதுரை"],
    "Telangana": [
        "telangana", "hyderabad", "warangal",
        "हैदराबाद", "तेलंगाना", "హైదరాబాద్", "తెలంగాణ",
        "ಹೈದರಾಬಾದ್", "ತೆಲಂಗಾಣ", "ஹைதராபாத்", "தெலங்கானா",
    ],
    "Tripura": ["tripura", "agartala"],
    "Uttar Pradesh": ["uttar pradesh", "lucknow", "kanpur", "varanasi", "noida"],
    "Uttarakhand": ["uttarakhand", "dehradun"],
    "West Bengal": ["west bengal", "kolkata", "howrah"],
    "Andaman and Nicobar Islands": ["andaman", "nicobar", "port blair"],
    "Chandigarh": ["chandigarh"],
    "Dadra and Nagar Haveli and Daman and Diu": ["dadra", "nagar haveli", "daman", "diu"],
    "Delhi": ["delhi", "new delhi"],
    "Jammu and Kashmir": ["jammu", "kashmir", "srinagar"],
    "Ladakh": ["ladakh", "leh"],
    "Lakshadweep": ["lakshadweep", "kavaratti"],
    "Puducherry": ["puducherry", "pondicherry"],
    "Central": [
        "central government", "government of india", "ministry", "passport", "income tax",
        "railway", "national highway", "defence",
        "केंद्र सरकार", "भारत सरकार", "కేంద్ర ప్రభుత్వం",
        "केंद्रीय सरकार", "ಕೇಂದ್ರ ಸರ್ಕಾರ", "மத்திய அரசு",
    ],
}

# (keyword, weight). Distinctive words weigh more than location words such as
# "ward", which appear in requests about any department.
DEPARTMENT_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "public_works": [
        ("road", 3), ("roads", 3), ("pothole", 3), ("highway", 3), ("bridge", 3),
        ("flyover", 3), ("pavement", 2), ("public works", 3), ("construction", 2),
        ("రోడ్డు", 3), ("రోడ్", 3), ("వంతెన", 3), ("నిర్మాణం", 2),
        ("सड़क", 3), ("पुल", 3), ("निर्माण", 2), ("गड्ढा", 3), ("गड्ढे", 3),
        ("रस्ता", 3), ("पूल", 3), ("बांधकाम", 2), ("खड्डा", 3),
        ("ರಸ್ತೆ", 3), ("ಸೇತುವೆ", 3), ("ಗುಂಡಿ", 3), ("ನಿರ್ಮಾಣ", 2),
        ("சாலை", 3), ("பாலம்", 3), ("குழி", 3), ("கட்டுமானம்", 2),
    ],
    "municipal": [
        ("street light", 4), ("streetlight", 4), ("street lights", 4), ("garbage", 3),
        ("drainage", 3), ("sewer", 3), ("municipal corporation", 3), ("municipality", 3),
        ("municipal", 2), ("encroachment", 2), ("footpath", 2), ("ward", 1),
        ("వీధి దీపం", 4), ("వీథి దీపం", 4), ("చెత్త", 3), ("డ్రైనేజీ", 3),
        ("మున్సిపల్", 2), ("వార్డు", 1), ("వార్డ్", 1),
        ("स्ट्रीट लाइट", 4), ("कचरा", 3), ("नाली", 3), ("नगरपालिका", 3),
        ("नगर निगम", 3), ("वार्ड", 1), ("महानगरपालिका", 3), ("रस्त्यावरील दिवे", 4),
        ("ಬೀದಿ ದೀಪ", 4), ("ಕಸ", 3), ("ಚರಂಡಿ", 3), ("ನಗರಪಾಲಿಕೆ", 3), ("ವಾರ್ಡ್", 1),
        ("தெரு விளக்கு", 4), ("குப்பை", 3), ("வடிகால்", 3), ("மாநகராட்சி", 3), ("வார்டு", 1),
    ],
    "home": [
        ("police", 3), ("fir", 3), ("police station", 3), ("crime", 2), ("arrest", 2),
        ("పోలీసు", 3), ("పోలీస్", 3), ("ఎఫ్ఐఆర్", 3),
        ("पुलिस", 3), ("एफआईआर", 3), ("थाना", 3), ("अपराध", 2), ("पोलीस", 3), ("गुन्हा", 2),
        ("ಪೊಲೀಸ್", 3), ("ಎಫ್ಐಆರ್", 3), ("ಅಪರಾಧ", 2),
        ("காவல்துறை", 3), ("போலீஸ்", 3), ("எஃப்ஐஆர்", 3), ("குற்றம்", 2),
    ],
    "health": [
        ("hospital", 3), ("health", 2), ("medicine", 2), ("ambulance", 3), ("doctor", 2),
        ("clinic", 2), ("vaccination", 2), ("ఆసుపత్రి", 3), ("ఆరోగ్య", 2), ("అంబులెన్స్", 3),
        ("अस्पताल", 3), ("स्वास्थ्य", 2), ("दवा", 2), ("एम्बुलेंस", 3),
        ("रुग्णालय", 3), ("आरोग्य", 2), ("रुग्णवाहिका", 3),
        ("ಆಸ್ಪತ್ರೆ", 3), ("ಆರೋಗ್ಯ", 2), ("ಆಂಬ್ಯುಲೆನ್ಸ್", 3),
        ("மருத்துவமனை", 3), ("சுகாதாரம்", 2), ("ஆம்புலன்ஸ்", 3),
    ],
    "water": [
        ("water supply", 3), ("pipeline", 3), ("water pipeline", 3), ("tap", 2),
        ("dam", 2), ("irrigation", 3), ("water", 2),
        ("నీటి సరఫరా", 3), ("నీరు", 2), ("పైప్‌లైన్", 3), ("బావి", 2),
        ("पानी", 2), ("जल", 2), ("पाइपलाइन", 3), ("नल", 2),
        ("पाणी", 2), ("पाणीपुरवठा", 3), ("नळ", 2),
        ("ನೀರು", 2), ("ನೀರಿನ ಸರಬರಾಜು", 3), ("ಪೈಪ್ಲೈನ್", 3),
        ("தண்ணீர்", 2), ("குடிநீர்", 2), ("குழாய்", 3),
    ],
    "transport": [
        ("bus", 2), ("rtc", 3), ("railway", 3), ("train", 2), ("transport", 2),
        ("బస్సు", 2), ("రైలు", 2), ("రవాణా", 2),
        ("बस", 2), ("रेल", 2), ("परिवहन", 2), ("रेल्वे", 3),
        ("ಬಸ್", 2), ("ರೈಲು", 2), ("ಸಾರಿಗೆ", 2),
        ("பேருந்து", 2), ("ரயில்", 2), ("போக்குவரத்து", 2),
    ],
    "revenue": [
        ("land record", 3), ("land", 2), ("survey", 2), ("property", 2), ("patta", 3),
        ("registration", 2), ("భూమి", 2), ("పట్టా", 3), ("జమీన్", 2),
        ("जमीन", 2), ("भूमि", 2), ("पट्टा", 3), ("रजिस्ट्री", 2), ("मिळकत", 2),
        ("ಭೂಮಿ", 2), ("ಪಟ್ಟಾ", 3),
        ("நிலம்", 2), ("பட்டா", 3),
    ],
    "education": [
        ("school", 2), ("college", 2), ("scholarship", 3), ("teacher", 2),
        ("పాఠశాల", 2), ("కళాశాల", 2), ("స్కాలర్‌షిప్", 3), ("ఉపాధ్యాయుడు", 2),
        ("स्कूल", 2), ("कॉलेज", 2), ("छात्रवृत्ति", 3), ("शिक्षक", 2),
        ("शाळा", 2), ("महाविद्यालय", 2), ("शिष्यवृत्ती", 3),
        ("ಶಾಲೆ", 2), ("ಕಾಲೇಜು", 2), ("ವಿದ್ಯಾರ್ಥಿವೇತನ", 3),
        ("பள்ளி", 2), ("கல்லூரி", 2), ("கல்வி உதவித்தொகை", 3),
    ],
    "environment": [
        ("pollution", 3), ("tree", 2), ("environment", 2), ("కాలుష్యం", 3), ("చెట్టు", 2),
        ("प्रदूषण", 3), ("पेड़", 2), ("पर्यावरण", 2), ("झाड", 2),
        ("ಮಾಲಿನ್ಯ", 3), ("ಮರ", 2),
        ("மாசு", 3), ("மரம்", 2),
    ],
}

RECORDS_SIGNALS = [
    "information", "copy", "copies", "details", "record", "records", "document", "documents",
    "how much", "how many", "what is", "what are", "list of", "expenditure", "spent", "spending",
    "budget", "contract", "agreement", "work order", "voucher", "bill", "bills", "register",
    "minutes", "report", "particulars", "provide", "give me", "give the", "share the",
    "send me", "list with", "names of", "tell me", "know", "certified",
    "తెలియజేయండి", "తెలపండి", "వివరాలు", "వివరములు", "నకళ్లు", "ప్రతులు", "ఎంత", "ఎన్ని",
    "ఖర్చు", "బడ్జెట్", "ఒప్పందం", "నివేదిక",
    "बताइए", "बताएं", "जानकारी", "विवरण", "प्रतियां", "प्रतिलिपि", "कितना", "कितने",
    "खर्च", "बजट", "अनुबंध", "रिपोर्ट", "दस्तावेज",
    "माहिती", "तपशील", "प्रत", "किती", "खर्च", "बजेट", "करार", "अहवाल",
    "द्या", "दस्तऐवज", "कागदपत्रे",
    "ಮಾಹಿತಿ", "ವಿವರ", "ಪ್ರತಿ", "ಎಷ್ಟು", "ಖರ್ಚು", "ಬಜೆಟ್", "ಒಪ್ಪಂದ", "ವರದಿ",
    "ನೀಡಿ", "ದಾಖಲೆ", "ದಾಖಲೆಗಳು",
    "தகவல்", "விவரம்", "நகல்", "எவ்வளவு", "செலவு", "பட்ஜெட்", "ஒப்பந்தம்", "அறிக்கை",
    "தாருங்கள்", "வழங்குங்கள்", "ஆவணங்கள்", "ஆவணம்",
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
    r"(?<!\w)(दुरुस्त\s+करा|हटवा|तक्रार|कृपया\s+करा)(?!\w)",
    r"(?<!\w)(ಸರಿಪಡಿಸಿ|ತೆಗೆದುಹಾಕಿ|ದೂರು)(?!\w)",
    r"(?<!\w)(சரிசெய்|அகற்று|புகார்)(?!\w)",
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

# ---------------------------------------------------------------------------
# Personal-data screening (Section 8(1)(j))
# ---------------------------------------------------------------------------
# The screen fires only when a *record* (a private fact about someone) meets a
# *subject* (the someone it is about). Either side alone is far too common:
# "address the grievance" is not a record, and "number of doctors on duty" is
# not personal. The conjunction is what keeps the rule precise enough to block
# a filing rather than merely warn.
#
# Both lists are matched on word boundaries with simple English plurals, so
# "beneficiaries" and "defaulters" hit without listing every inflection. That
# matters more than it sounds: as substring tests, the old lists were both too
# loose ("man" inside "informants") and too narrow ("beneficiary" missed
# "beneficiaries"), which is how colloquial requests slipped through.
#
# Vocabulary is the honest part of this rule: it encodes the phrasings the
# project has actually seen, and it will always lag a phrasing it has not.
# `scripts/bench_citizen_dev.py` exists to keep that gap visible.
PERSONAL_RECORDS = [
    # Where someone lives or how to reach them.
    "home address", "residential address", "house address", "permanent address",
    "residence address", "personal address", "residence of", "where does",
    "phone number", "mobile number", "contact number", "phone no", "mobile no",
    "personal phone", "call detail", "call record", "cdr", "whatsapp", "chat between",
    # Identity papers and identifiers.
    "aadhaar", "aadhar", "pan number", "pan card", "passport", "voter id",
    "caste certificate", "caste validity", "birth certificate of", "disability certificate",
    "disability record", "income certificate", "biometric",
    "photograph", "photo of", "signature", "fingerprint",
    # Health and treatment.
    "medical record", "medical report", "medical certificate", "discharge summary", "hiv",
    "psychiatric", "mental health record", "prescription", "test report", "blood test",
    "x-ray", "fitness report", "fitness certificate", "treatment given", "patient history",
    "diagnosis", "diagnoses",
    # Money, debt and tax.
    "salary of", "salary slip", "salary drawn", "salary account", "bank account",
    "account of", "accounts of", "account number", "passbook", "ifsc", "fixed deposit",
    "fdr", "pension amount", "pension drawn", "loan amount", "repayment",
    "income tax return", "tax return",
    "insurance policy", "insurance policies", "bank statement", "credit card",
    "outstanding amount", "defaulter",
    # Family, status and personal circumstances.
    "family details", "family particulars", "personal details", "personal information",
    "marital status", "spouse", "date of birth",
    # Employment and conduct files.
    "personnel file", "service record", "appraisal", "apar", "confidential report",
    "biometric attendance", "punishment record", "disciplinary record",
    # Location, movement and third-party property.
    "cctv", "footage", "property details", "ownership document",
    "school record", "report card", "marksheet", "marks", "roll number",
    # Asking who someone is, when the someone is identifiable.
    "names of", "name of the", "name and address", "name and contact",
    "identity of", "list of names",
    # Legacy entries kept for the appellate phrasings they were tuned on.
    "telephone number",
    # Indic terms; first-pass vocabulary, same caveat as the department lists.
    "ఫోన్ నంబర్", "చిరునామా", "వ్యక్తిగత వివరాలు", "జీతం",
    "फोन नंबर", "मोबाइल", "निजी पता", "व्यक्तिगत जानकारी", "वेतन",
    "पत्ता", "वैयक्तिक माहिती", "पगार",
    "ಫೋನ್ ಸಂಖ್ಯೆ", "ವಿಳಾಸ", "ವೈಯಕ್ತಿಕ ಮಾಹಿತಿ", "ಸಂಬಳ",
    "தொலைபேசி எண்", "முகவரி", "தனிப்பட்ட தகவல்", "சம்பளம்",
]

PERSONAL_SUBJECTS = [
    # Public servants, named as individuals.
    "officer", "employee", "official", "engineer", "clerk", "ward officer",
    "pio", "public information officer", "inspector", "supervisor", "nurse",
    "doctor", "teacher", "peon", "sweeper", "guard", "watchman", "driver",
    "commissioner", "collector", "mayor", "sarpanch", "mla", "minister",
    "secretary", "director", "head clerk", "chief engineer",
    # Members of the public the record is about.
    "person", "individual", "applicant", "third party", "patient", "resident",
    "occupant", "tenant", "landlord", "owner", "shopkeeper", "contractor",
    "farmer", "student", "child", "children", "beneficiary", "candidate",
    "businessman", "businesswoman", "defaulter", "informant", "complainant",
    "visitor", "worker", "labourer", "pensioner", "widow", "victim", "witness",
    "accused", "deceased", "parent", "guardian", "spouse", "minor", "woman",
    "women", "girl", "boy", "account holder", "imposter", "member of staff",
    "staff", "dealer", "vendor", "accountant", "cashier", "builder",
    # Relatives, as applicants phrase them.
    "husband", "wife", "son", "daughter", "brother", "sister", "mother", "father",
    "relative", "someone", "particular person", "certain person", "a certain",
    "his son", "her son",
    # Indic terms.
    "అధికారి", "ఉద్యోగి", "వ్యక్తి", "अधिकारी", "अधिकाऱ्या", "कर्मचारी", "कर्मचाऱ्या", "व्यक्ति",
    "व्यक्ती", "ಅಧಿಕಾರಿ", "ಉದ್ಯೋಗಿ", "ವ್ಯಕ್ತಿ", "அதிகாரி", "ஊழியர்", "நபர்",
]

# Asking who someone is is not by itself a personal-data request: the names of
# officers, contractors and engineers who acted in an official capacity are
# disclosable. Identity-only records therefore need a subject who is a private
# person, while a substantive private fact (address, health, money) is enough on
# its own — the codebook's "designation is public, home address is not" line.
IDENTITY_ONLY_RECORDS = ["names of", "name of the", "list of names", "identity of"]

# Subjects who act for a public body. A record about them is personal only when
# it is a substantive private fact rather than their identity or their duties.
PUBLIC_ROLE_SUBJECTS = [
    "officer", "official", "employee", "engineer", "clerk", "ward officer", "pio",
    "public information officer", "inspector", "supervisor", "nurse", "doctor",
    "teacher", "peon", "sweeper", "guard", "watchman", "driver", "commissioner",
    "collector", "mayor", "sarpanch", "mla", "minister", "secretary", "director",
    "head clerk", "chief engineer", "contractor", "member of staff",
]

# A record plus a named or honorific reference ("the home address of Dr Kulkarni")
# is personal even when no generic subject noun appears.
PERSONAL_NAME_PATTERNS = [
    r"\b(?:dr|mr|mrs|ms|shri|smt|sri)\.?\s+[a-z]",
    r"\bnamed\b",
    r"\bparticular (?:person|individual)\b",
]


def _personal_regex(terms: list[str]) -> re.Pattern[str]:
    """One alternation for the list: word-bounded English, plain match for Indic.

    Word boundaries do not work after a Devanagari matra: `\\b` needs a word
    character on one side, and a combining vowel sign is not one, so `\\bपत्ता\\b`
    never matches "पत्ता ". Indic terms are therefore matched as substrings — they
    are long enough that a false positive is unlikely — while English terms keep
    their boundaries and simple plurals.
    """
    ascii_patterns: set[str] = set()
    other_forms: set[str] = set()
    for term in terms:
        if term.isascii():
            # Every word may be plural, so "name and address" also catches
            # "names and addresses".
            ascii_patterns.add(r"\s+".join(_word_pattern(word) for word in term.split()))
        else:
            other_forms.add(term)
    parts: list[str] = []
    if ascii_patterns:
        ordered = sorted(ascii_patterns, key=len, reverse=True)
        parts.append(r"\b(?:" + "|".join(ordered) + r")\b")
    if other_forms:
        ordered = sorted(other_forms, key=len, reverse=True)
        parts.append("(?:" + "|".join(re.escape(t) for t in ordered) + ")")
    return re.compile("|".join(parts))


def _word_pattern(word: str) -> str:
    """One English word, tolerating its regular plural."""
    if word.endswith("s"):
        # "address" only needs "-es"; adding "-s" too would be noise.
        return re.escape(word) + r"(?:es)?"
    if word.endswith("y"):
        return re.escape(word[:-1]) + r"(?:y|ies)"
    return re.escape(word) + r"(?:s|es)?"


PERSONAL_RECORD_RE = _personal_regex(PERSONAL_RECORDS)
PERSONAL_SUBJECT_RE = _personal_regex(PERSONAL_SUBJECTS)
IDENTITY_RECORD_RE = _personal_regex(IDENTITY_ONLY_RECORDS)
PUBLIC_ROLE_RE = _personal_regex(PUBLIC_ROLE_SUBJECTS)
# Kept as the documented names of the two sides of the screen.
PERSONAL_TERMS = PERSONAL_RECORDS
PERSON_TERMS = PERSONAL_SUBJECTS

URGENCY_TERMS = [
    "life or liberty", "life and liberty", "danger to life", "threat to life", "loss of life",
    "life-threatening", "life threatening", "risk to life", "emergency", "ambulance", "oxygen",
    "icu", "dying", "death", "medical emergency", "safety of",
    "ప్రాణ", "జీవితం", "మరణం", "ఆసుపత్రి", "అత్యవసర", "ఆక్సిజన్",
    "जान", "जीवन", "मौत", "अस्पताल", "आपातकाल", "ऑक्सीजन",
    "ಜೀವ", "ಸಾವು", "ಆಸ್ಪತ್ರೆ", "ತುರ್ತು", "ಆಕ್ಸಿಜನ್",
    "உயிர்", "இறப்பு", "மருத்துவமனை", "அவசரம்", "ஆக்சிஜன்",
]

OPINION_PATTERNS = [
    r"(?<!\w)why(?!\w)", r"who\s+is\s+responsible", r"(?<!\w)explain(?!\w)",
    r"reason\s+for", r"how\s+could", r"should\s+have",
    r"ఎందుకు", r"बताइए\s+क्यों", r"(?<!\w)क्यों(?!\w)", r"जिम्मेदार",
    r"(?<!\w)ಏಕೆ(?!\w)", r"(?<!\w)ஏன்(?!\w)",
]

BPL_PATTERN = re.compile(
    r"(?<!\w)(bpl|below poverty line|antyodaya|बीपीएल|अंत्योदय|బీపీఎల్|అంత్యోదయ|ಬಿಪಿಎಲ್|பிபிஎல்)(?!\w)",
    re.I,
)
NAME_PATTERNS = [
    r"my name is ([A-Za-z][A-Za-z .'-]{2,40})",
    r"నా పేరు ([^\s,.]{2,40})",
    r"मेरा नाम ([^\s,.]{2,40})",
    r"माझे नाव ([^\s,.]{2,40})",
    r"ನನ್ನ ಹೆಸರು ([^\s,.]{2,40})",
    r"என் பெயர் ([^\s,.]{2,40})",
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
    # Longer, more specific matches win over short forms.
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

    if state == "Central" or any(
        k in lowered
        for k in ("railway", "train", "national highway", "passport", "ministry", "income tax")
    ):
        level = "central"
    else:
        level = "state"
    if level == "central":
        state = "Central"

    records = any(_contains(lowered, s) for s in RECORDS_SIGNALS)
    action = any(re.search(p, lowered) for p in ACTION_PATTERNS)
    personal = has_personal_data(text)
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


def personal_data_signals(text: str) -> dict[str, list[str] | bool]:
    """The evidence behind a personal-data verdict, for audits and debugging.

    `records` are the private facts the request asks for, `subjects` the people it
    asks about, and `named_reference` is true when the request names someone
    ("Dr Kulkarni") without using a generic noun. The rule fires when a record
    meets a subject or a named reference.
    """
    lowered = normalise(text)
    records = {m.group(0) for m in PERSONAL_RECORD_RE.finditer(lowered)}
    subjects = {m.group(0) for m in PERSONAL_SUBJECT_RE.finditer(lowered)}
    identity = {m.group(0) for m in IDENTITY_RECORD_RE.finditer(lowered)}
    public = {m.group(0) for m in PUBLIC_ROLE_RE.finditer(lowered)}
    named = any(re.search(pattern, lowered) for pattern in PERSONAL_NAME_PATTERNS)
    return {
        "records": sorted(records),
        "subjects": sorted(subjects),
        "private_subjects": sorted(subjects - public),
        "identity_only": bool(records) and records <= identity,
        "named_reference": named,
    }


def has_personal_data(text: str) -> bool:
    signals = personal_data_signals(text)
    if not signals["records"]:
        return False
    # A record with no person in the sentence is the applicant's own document or an
    # aggregate figure, not a third party's information.
    if not signals["subjects"] and not signals["named_reference"]:
        return False
    if signals["private_subjects"] or signals["named_reference"]:
        return True
    # An identity-only request about a public servant is a request about a post,
    # not about a person; a substantive private fact is not.
    return not signals["identity_only"]


def has_urgency(text: str) -> bool:
    lowered = normalise(text)
    return any(s in lowered for s in URGENCY_TERMS)


def has_opinion(text: str) -> bool:
    lowered = normalise(text)
    return any(re.search(p, lowered) for p in OPINION_PATTERNS)
