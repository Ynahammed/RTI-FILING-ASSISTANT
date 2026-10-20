"""Citizen-facing chat copy in English, Telugu and Hindi.

Plain language only: no section numbers, no jargon. The citizen sees what will
happen next; the reasoning trail carries the legal citations.
"""

from __future__ import annotations

TEMPLATES: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "I can help you ask a public authority for records. Tell me what you need to know and where.",
        "te": "ప్రభుత్వ కార్యాలయం నుండి సమాచారం అడగడంలో నేను సహాయం చేస్తాను. మీకు ఏమి కావాలో, ఎక్కడ ఉందో చెప్పండి.",
        "hi": "मैं किसी सार्वजनिक संस्था से जानकारी मांगने में मदद कर सकता हूं। बताइए आपको क्या चाहिए और कहां।",
    },
    "ask_information_needed": {
        "en": "What records do you need? For example, the amount spent on a road repair, or a copy of a contract.",
        "te": "మీకు ఏ రికార్డులు కావాలి? ఉదాహరణకు, రోడ్డు మరమ్మతుకు ఎంత ఖర్చు చేశారు, లేదా ఒప్పందం కాపీ.",
        "hi": "आपको कौन से रिकॉर्ड चाहिए? जैसे सड़क मरम्मत पर कितना खर्च हुआ, या अनुबंध की प्रति।",
    },
    "ask_state": {
        "en": "Which state is this request about? I will not guess, because the wrong office wastes your fee.",
        "te": "ఈ అభ్యర్థన ఏ రాష్ట్రానికి సంబంధించినది? ఊహించను—తప్పు కార్యాలయం మీ రుసుమును వృథా చేస్తుంది.",
        "hi": "यह अनुरोध किस राज्य से जुड़ा है? मैं अनुमान नहीं लगाऊंगा, क्योंकि गलत कार्यालय आपकी फीस बर्बाद करता है।",
    },
    "ask_department": {
        "en": "Which department handles this? For example, public works for roads, or the municipal corporation for street lights.",
        "te": "ఇది ఏ శాఖ పరిధిలో ఉంది? ఉదాహరణకు రోడ్లకు పీడబ్ల్యూడీ, వీధి దీపాలకు మున్సిపల్ కార్పొరేషన్.",
        "hi": "यह किस विभाग का काम है? जैसे सड़कों के लिए लोक निर्माण विभाग, स्ट्रीट लाइट के लिए नगर निगम।",
    },
    "grievance": {
        "en": "You are asking for action, not records. That belongs in the grievance portal, so I will not file an RTI. Here is the link: {grievance_url}",
        "te": "మీరు చర్య తీసుకోమని అడుగుతున్నారు, రికార్డులు కాదు. అది ఫిర్యాదు పోర్టల్‌కు చెందుతుంది, కాబట్టి ఆర్‌టీఐ దాఖలు చేయను. లింక్: {grievance_url}",
        "hi": "आप कार्रवाई चाहते हैं, रिकॉर्ड नहीं। यह शिकायत पोर्टल का विषय है, इसलिए मैं आरटीआई दाखिल नहीं करूंगा। लिंक: {grievance_url}",
    },
    "mixed": {
        "en": "This has two parts. I will prepare the records request, and you can send the repair complaint through the grievance portal: {grievance_url}",
        "te": "ఇందులో రెండు భాగాలు ఉన్నాయి. రికార్డుల అభ్యర్థనను నేను సిద్ధం చేస్తాను; మరమ్మతు ఫిర్యాదును ఫిర్యాదు పోర్టల్ ద్వారా పంపవచ్చు: {grievance_url}",
        "hi": "इसके दो हिस्से हैं। मैं रिकॉर्ड अनुरोध तैयार करूंगा, और मरम्मत की शिकायत आप शिकायत पोर्टल पर भेज सकते हैं: {grievance_url}",
    },
    "exempt": {
        "en": "This asks for another person's private information. I have stopped it for human review and will not file it.",
        "te": "ఇది మరొక వ్యక్తి యొక్క వ్యక్తిగత సమాచారాన్ని అడుగుతోంది. మానవ సమీక్ష కోసం ఆపివేశాను; దాఖలు చేయను.",
        "hi": "यह किसी दूसरे व्यक्ति की निजी जानकारी मांगता है। मैंने इसे मानवीय समीक्षा के लिए रोक दिया है और दाखिल नहीं करूंगा।",
    },
    "uncertain": {
        "en": "I am not confident enough to file yet. Missing: {missing}. Could you add that?",
        "te": "ఇంకా దాఖలు చేయడానికి నాకు సరిపడా నమ్మకం లేదు. లేనివి: {missing}. దయచేసి చెప్పండి.",
        "hi": "मैं अभी दाखिल करने के लिए पर्याप्त आश्वस्त नहीं हूं। कमी: {missing}। कृपया बताइए।",
    },
    "approval": {
        "en": "Please review the draft, the office it will go to, and the fee of Rs {fee}. Approve and I will file it; decline and nothing is sent.",
        "te": "దయచేసి డ్రాఫ్ట్, దాని కార్యాలయం, మరియు రూ. {fee} రుసుమును పరిశీలించండి. ఆమోదిస్తే దాఖలు చేస్తాను; తిరస్కరిస్తే ఏమీ పంపబడదు.",
        "hi": "कृपया ड्राफ्ट, कार्यालय और रु. {fee} शुल्क देखें। स्वीकृति दें तो मैं दाखिल करूंगा; अस्वीकार करें तो कुछ नहीं भेजा जाएगा।",
    },
    "filed": {
        "en": "Filed. Registration number {reg_no}, response due {deadline}. I re-opened the portal myself to check these values.",
        "te": "దాఖలు అయింది. నమోదు సంఖ్య {reg_no}, సమాధానం {deadline} లోపు. ఈ విలువలను నేను స్వయంగా పోర్టల్ తెరిచి నిర్ధారించాను.",
        "hi": "दाखिल हो गया। पंजीकरण संख्या {reg_no}, जवाब {deadline} तक। ये मान मैंने खुद पोर्टल खोलकर जांचे।",
    },
    "filed_unverified": {
        "en": "The payment went through but the portal's confirmation page did not open. I recorded this as pending and will not pay again. Registration number {reg_no}.",
        "te": "చెల్లింపు జరిగింది కానీ పోర్టల్ నిర్ధారణ పేజీ తెరవలేదు. దీన్ని పెండింగ్‌గా నమోదు చేశాను, మళ్లీ చెల్లించను. నమోదు సంఖ్య {reg_no}.",
        "hi": "भुगतान हो गया लेकिन पोर्टल का पुष्टिकरण पेज नहीं खुला। इसे लंबित दर्ज किया, दोबारा भुगतान नहीं करूंगा। पंजीकरण संख्या {reg_no}।",
    },
    "awaiting_captcha": {
        "en": "The portal is waiting for a human to complete its verification step. This is the manual handoff; I will continue after the demo operator completes it.",
        "te": "పోర్టల్ మానవ వెరిఫికేషన్ కోసం వేచి ఉంది. ఇది మాన్యువల్ హ్యాండాఫ్; డెమో ఆపరేటర్ పూర్తి చేసిన తర్వాత కొనసాగిస్తాను.",
        "hi": "पोर्टल मानव सत्यापन की प्रतीक्षा कर रहा है। यह मैनुअल हैंडऑफ है; डेमो ऑपरेटर के पूरा करने पर मैं आगे बढ़ूंगा।",
    },
    "declined": {
        "en": "Understood. Nothing was filed and no fee was paid.",
        "te": "అర్థమైంది. ఏమీ దాఖలు కాలేదు, రుసుము చెల్లించబడలేదు.",
        "hi": "समझ गया। कुछ दाखिल नहीं हुआ और कोई शुल्क नहीं दिया गया।",
    },
    "appeal": {
        "en": "The response deadline has passed without a proper reply. I drafted a first appeal for your approval.",
        "te": "సమాధాన గడువు ముగిసింది, సరైన సమాధానం రాలేదు. మీ ఆమోదం కోసం మొదటి అప్పీల్ డ్రాఫ్ట్ చేశాను.",
        "hi": "जवाब की समय सीमा बिना उचित उत्तर के बीत गई। आपकी स्वीकृति के लिए पहली अपील का ड्राफ्ट तैयार किया है।",
    },
    "injection": {
        "en": "I ignored an instruction embedded in your message and logged it as data. It cannot change the approval gate.",
        "te": "మీ సందేశంలో దాచిన సూచనను నేను పట్టించుకోలేదు, దాన్ని డేటాగా నమోదు చేశాను. అది ఆమోద నియమాన్ని మార్చలేదు.",
        "hi": "आपके संदेश में छिपे निर्देश को मैंने अनदेखा किया और डेटा के रूप में दर्ज किया। वह स्वीकृति नियम नहीं बदल सकता।",
    },
    "closed": {
        "en": "This request is closed.",
        "te": "ఈ అభ్యర్థన మూసివేయబడింది.",
        "hi": "यह अनुरोध बंद है।",
    },
}


def t(key: str, language: str, **kwargs: object) -> str:
    table = TEMPLATES.get(key, TEMPLATES["greeting"])
    template = table.get(language, table["en"])
    return template.format(**kwargs) if kwargs else template
