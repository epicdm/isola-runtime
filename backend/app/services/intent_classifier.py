"""Intent classifier — rules-first regex matcher for LCR routing.

Pattern dict stores the matching logic; routing policy (tier, cost cap,
capability_required) lives in the intent_routing DB table.
Pattern edits = code change. Policy edits = DB row.
"""
from __future__ import annotations
import re

# ── Pattern dict: vertical → intent → compiled regex (or None for default) ──
# 'default' entry has no pattern — it is returned when nothing else matches.
_RAW_PATTERNS: dict[str, dict[str, str | None]] = {
    "pharmacy": {
        "medical_emergency": (
            r"\b(cannot breathe|can.t breathe|overdose|overdosed|emergency|"
            r"severe pain|accident|call 911|dying|collapsed)\b"
        ),
        "side_effect_report": (
            r"\b(side effect|feeling (unwell|sick|dizzy|nauseous|strange)|"
            r"reaction after (taking|using)|since (i started|taking)|adverse)\b"
        ),
        "allergy_concern": (
            r"\b(allerg|allergic|allergy|intolerant|sensitive to|bad reaction to)\b"
        ),
        "drug_interaction_question": (
            r"\b(interact|interaction|mix(ing)?|combine|take.*(with|together|along)|"
            r"can i take.*and|safe to take.*with)\b"
        ),
        "medication_dosage_question": (
            r"\b(how much|dosage|dose\b|mg\b|milligram|tablet|how many.*(take|pills?)|"
            r"how often (do|should)|times? (a day|per day|daily))\b"
        ),
        "prescription_pickup_status": (
            r"\b(refill|prescription|pickup|pick up|collect|ready|order status|status)\b"
        ),
        "product_availability": (
            r"\b(do you (have|sell|carry|stock)|in stock|available|"
            r"can i (buy|get|find)|do you (carry|stock))\b"
        ),
        "store_hours": (
            r"\b(hours?|what time|when (do you |are you )?(open|close)|"
            r"closing time|opening time|open on (sundays?|saturdays?|weekends?)|"
            r"are you open)\b"
        ),
        "store_location": (
            r"\b(where (are you|is the|can i find)|address|location|"
            r"directions?|how (do i|to) (get|find)|situated|located)\b"
        ),
        "small_talk": (
            r"\b(thank(s| you)|goodbye|bye( bye)?|see you|"
            r"have a (good|nice|great)|you.re welcome|no problem|got it)\b"
        ),
        "greeting": (
            r"\b(hi\b|hello|hey\b|good (morning|afternoon|evening|day)|howdy|sup\b)\b"
        ),
        "default": None,
    },
    "agriculture": {
        "pesticide_exposure_emergency": (
            r"\b(pesticide.*(skin|eyes?|mouth|swallow|inhaled?)|"
            r"herbicide.*(skin|eyes?|mouth|swallow)|"
            r"chemical.*(burn|splash|spill)|(swallow|inhaled?).*(chemical|pesticide|spray|herbicide)|"
            r"poisoned|burning (sensation|eyes))\b"
        ),
        "livestock_disease_outbreak_risk": (
            r"\b(sudden(ly)? (dead|died|death)|(died|dead).*(sudden(ly)?|overnight)|"
            r"found dead|dead overnight|"
            r"blisters? (on|around) (their )?(mouths?|feet|snout)|"
            r"neurological (symptoms?|signs?)|"
            r"(several|many|multiple|lots of) (animals?|goats?|cows?|cattle|sheep).*(dead|sick|dying)|"
            r"outbreak|notifiable)\b"
        ),
        "human_animal_disease_exposure": (
            r"\b(touched?.*(sick|dead|infected) animal|got blood (on me|on my)|"
            r"zoonotic|disease from (animal|livestock|goat|cow)|"
            r"handled?.*(without gloves?|dead animal)|worried (about|i (got|have)))\b"
        ),
        "pesticide_dosage_reference": (
            r"\b(glyphosate|herbicide|fungicide|"
            r"how much.*(spray|pesticide|herbicide|chemical)|"
            r"dosage.*(acre|hectare|plant)|"
            r"pesticide.*(dosage|rate|amount|how much))\b"
        ),
        "pest_identification_with_photo": (
            r"\b(photo|picture|image|attached|sending (you|a)|look at this|"
            r"see (this|my|what)|i.m sending)\b"
        ),
        "pest_identification_text": (
            r"\b(bugs?|pest|insect|worm|caterpillar|"
            r"(leaves?|plants?).*(yellow|brown|curl|spot|hole|dying)|"
            r"yellow(ing)? leaves?|"
            r"disease (on|in) (my |the )?(plant|crop|leaf)|"
            r"eating (my |the )?(plants?|crop|leaf|leaves))\b"
        ),
        "livestock_disease_routine": (
            r"\b(goats?|cows?|cattle|sheep|chickens?|pigs?|poultry|"
            r"(animal|goats?|cows?|chickens?|poultry).*(not eating|limping|coughing|sick|lame|swollen|lethargic)|"
            r"(not eating|limping|coughing|lethargic).*(goat|cow|animal|chicken|poultry))\b"
        ),
        "grant_program_lookup": (
            r"\b(grants?|program|subsidy|assistance|EC\$|eligible|"
            r"apply (for|to)|funding|financial (help|support)|bursary)\b"
        ),
        "market_price_lookup": (
            r"\b(price (of|for)|how much (is|are).*(selling|cost)|"
            r"market price|current price|this week.*(price|cost)|"
            r"selling (price|for)|what.*(going for|worth))\b"
        ),
        "weather_planting_calendar": (
            r"\b(when (to|should i) plant|plant(ing)? (time|season|calendar)|"
            r"best time (to plant|for planting)|"
            r"weather.*(plant|grow|crop)|rain(fall)?.*(plant|grow)|"
            r"should i plant (now|this month))\b"
        ),
        "specialist_referral": (
            r"\b(vet(erinarian)?|agronomist|extension officer|specialist|"
            r"who (to call|should i (see|contact|call))|soil test(ing)?|"
            r"recommend (a |an )?(vet|expert|specialist))\b"
        ),
        "office_hours": (
            r"\b(office hours?|when (is|are) (the|your)( extension)? office|"
            r"extension office.*(open|available|hours?)|"
            r"(open|available|in) (on weekends?|saturdays?|sundays?)|"
            r"when can i (visit|come|call))\b"
        ),
        "office_location": (
            r"\b(where is (the|your) (office|extension)|"
            r"address (of|for) (the|your)|location of (the|your)|"
            r"how (do i|to) (find|get to) (your |the )?office)\b"
        ),
        "small_talk": (
            r"\b(thank(s| you)|goodbye|bye( bye)?|"
            r"ok(ay)?|no problem|you.re welcome|"
            r"have a (good|nice|great)|understood)\b"
        ),
        "greeting": (
            r"\b(hi\b|hello|hey\b|good (morning|afternoon|evening|day)|"
            r"bonjour|bonsoir|howdy)\b"
        ),
        "default": None,
    },
}

# Compile at import time (not per-call)
INTENT_PATTERNS: dict[str, dict[str, re.Pattern | None]] = {
    vertical: {
        intent: re.compile(pattern, re.IGNORECASE) if pattern else None
        for intent, pattern in intents.items()
    }
    for vertical, intents in _RAW_PATTERNS.items()
}

# Difficulty lookup — mirrors intent_routing rows from Phase 1 Step 9.
# Used to avoid a DB round-trip on the hot classification path.
INTENT_DIFFICULTY: dict[str, dict[str, str]] = {
    "pharmacy": {
        "greeting":                    "low",
        "small_talk":                  "low",
        "store_hours":                 "low",
        "store_location":              "low",
        "prescription_pickup_status":  "medium",
        "product_availability":        "medium",
        "medication_dosage_question":  "critical",
        "drug_interaction_question":   "critical",
        "allergy_concern":             "critical",
        "side_effect_report":          "critical",
        "medical_emergency":           "critical",
        "default":                     "medium",
    },
    "agriculture": {
        "greeting":                      "low",
        "small_talk":                    "low",
        "office_hours":                  "low",
        "office_location":               "low",
        "market_price_lookup":           "medium",
        "grant_program_lookup":          "medium",
        "weather_planting_calendar":     "medium",
        "pest_identification_text":      "high",
        "pest_identification_with_photo": "high",
        "pesticide_dosage_reference":    "high",
        "livestock_disease_routine":     "medium",
        "livestock_disease_outbreak_risk": "critical",
        "pesticide_exposure_emergency":  "critical",
        "human_animal_disease_exposure": "critical",
        "specialist_referral":           "medium",
        "default":                       "medium",
    },
}


def classify_intent(message: str, vertical: str) -> tuple[str, str, float]:
    """Classify a message against a vertical's intent patterns.

    Returns (intent_pattern, difficulty, confidence):
      - intent_pattern: label matching an intent_routing.intent_pattern value
      - difficulty: low | medium | high | critical (from INTENT_DIFFICULTY)
      - confidence: 1.0 for rule match, 0.5 for default fallback
    """
    patterns = INTENT_PATTERNS.get(vertical)
    if not patterns:
        return "default", "medium", 0.5

    msg = message.strip()
    for intent, compiled in patterns.items():
        if intent == "default":
            continue
        if compiled and compiled.search(msg):
            difficulty = INTENT_DIFFICULTY.get(vertical, {}).get(intent, "medium")
            return intent, difficulty, 1.0

    difficulty = INTENT_DIFFICULTY.get(vertical, {}).get("default", "medium")
    return "default", difficulty, 0.5
