"""S1 TDD: Intent classifier — red/green/refactor.

Acceptance gate: ≥85% correct intent (≥72 of 84 messages hit expected label).
Tracer bullet: classify("hi", "pharmacy") == ("greeting", "low", 1.0)
"""
from app.services.intent_classifier import classify_intent
import pytest


# ── Pharmacy test cases (36 messages, 3 per intent) ──────────────────────────

@pytest.mark.parametrize("msg,expected", [
    # greeting
    ("Hi there",                             "greeting"),
    ("Hello, how are you?",                  "greeting"),
    ("Good morning!",                        "greeting"),
    # small_talk
    ("Thank you so much",                    "small_talk"),
    ("Bye bye, see you!",                    "small_talk"),
    ("Got it, no problem",                   "small_talk"),
    # store_hours
    ("What time do you open?",               "store_hours"),
    ("Are you open on Sundays?",             "store_hours"),
    ("When do you close tonight?",           "store_hours"),
    # store_location
    ("Where are you located?",               "store_location"),
    ("What's your address?",                 "store_location"),
    ("How do I get there?",                  "store_location"),
    # prescription_pickup_status
    ("Is my prescription ready?",            "prescription_pickup_status"),
    ("Can I pick up my refill today?",       "prescription_pickup_status"),
    ("What's the status of my order?",       "prescription_pickup_status"),
    # product_availability
    ("Do you have ibuprofen in stock?",      "product_availability"),
    ("Do you sell blood pressure monitors?", "product_availability"),
    ("Is vitamin C available?",              "product_availability"),
    # medication_dosage_question
    ("How much ibuprofen should I take?",    "medication_dosage_question"),
    ("What's the dosage for a child?",       "medication_dosage_question"),
    ("How many tablets should I take daily?","medication_dosage_question"),
    # drug_interaction_question
    ("Can I take aspirin with warfarin?",    "drug_interaction_question"),
    ("Is it safe to mix these two meds?",    "drug_interaction_question"),
    ("Can I take ibuprofen along with my blood pressure medication?",
                                             "drug_interaction_question"),
    # allergy_concern
    ("I'm allergic to penicillin",           "allergy_concern"),
    ("I have a severe nut allergy",          "allergy_concern"),
    ("I'm sensitive to sulfa drugs",         "allergy_concern"),
    # side_effect_report
    ("I've been feeling dizzy after taking my medication",
                                             "side_effect_report"),
    ("I'm having a reaction after taking aspirin",
                                             "side_effect_report"),
    ("I feel nauseous since I started this medicine",
                                             "side_effect_report"),
    # medical_emergency
    ("I cannot breathe!",                    "medical_emergency"),
    ("Someone overdosed on pills",           "medical_emergency"),
    ("Severe pain in my chest, help me!",    "medical_emergency"),
    # default (catch-all)
    ("I'd like to speak to someone",         "default"),
    ("Can you help me with something?",      "default"),
    ("I have a general question",            "default"),
])
def test_pharmacy_intents(msg, expected):
    intent, difficulty, confidence = classify_intent(msg, "pharmacy")
    assert intent == expected, f"msg={msg!r}: got {intent!r}, expected {expected!r}"


# ── Agriculture test cases (48 messages, 3 per intent) ───────────────────────

@pytest.mark.parametrize("msg,expected", [
    # greeting
    ("Hi",                                   "greeting"),
    ("Good morning",                         "greeting"),
    ("Hello there, I need help",             "greeting"),
    # small_talk
    ("Thanks so much for your help",         "small_talk"),
    ("Goodbye, have a great day",            "small_talk"),
    ("Okay, I understand",                   "small_talk"),
    # office_hours
    ("What are your office hours?",          "office_hours"),
    ("When is the extension office open?",   "office_hours"),
    ("Are you available on weekends?",       "office_hours"),
    # office_location
    ("Where is your office located?",        "office_location"),
    ("What's the address of the extension office?",
                                             "office_location"),
    ("How do I find your office?",           "office_location"),
    # market_price_lookup
    ("What's the current price of tomatoes?","market_price_lookup"),
    ("How much is cassava selling for this week?",
                                             "market_price_lookup"),
    ("What's the market price for plantains?",
                                             "market_price_lookup"),
    # grant_program_lookup
    ("Are there any grants available for farmers?",
                                             "grant_program_lookup"),
    ("How do I apply for agricultural assistance?",
                                             "grant_program_lookup"),
    ("Am I eligible for the EC$ subsidy program?",
                                             "grant_program_lookup"),
    # weather_planting_calendar
    ("When should I plant sweet potatoes?",  "weather_planting_calendar"),
    ("What's the best time to plant tomatoes this season?",
                                             "weather_planting_calendar"),
    ("Should I plant now given the rainfall?",
                                             "weather_planting_calendar"),
    # pest_identification_text
    ("My tomato leaves are turning yellow and curling",
                                             "pest_identification_text"),
    ("There are bugs eating my plants",      "pest_identification_text"),
    ("My crops have yellow leaves and holes in them",
                                             "pest_identification_text"),
    # pest_identification_with_photo
    ("I'm sending you a photo of my plant",  "pest_identification_with_photo"),
    ("Can you look at this picture?",        "pest_identification_with_photo"),
    ("I've attached an image of the problem","pest_identification_with_photo"),
    # pesticide_dosage_reference
    ("How much glyphosate should I use per acre?",
                                             "pesticide_dosage_reference"),
    ("What's the correct dosage for this herbicide?",
                                             "pesticide_dosage_reference"),
    ("What's the pesticide dosage for my crop?",
                                             "pesticide_dosage_reference"),
    # livestock_disease_routine
    ("My goat hasn't been eating for 2 days","livestock_disease_routine"),
    ("My cow is limping and coughing",       "livestock_disease_routine"),
    ("My chickens seem sick and lethargic",  "livestock_disease_routine"),
    # livestock_disease_outbreak_risk
    ("Three of my goats died suddenly overnight",
                                             "livestock_disease_outbreak_risk"),
    ("My animals have blisters on their mouths",
                                             "livestock_disease_outbreak_risk"),
    ("Several animals are showing neurological symptoms",
                                             "livestock_disease_outbreak_risk"),
    # pesticide_exposure_emergency
    ("I got pesticide on my skin",           "pesticide_exposure_emergency"),
    ("My child swallowed some herbicide",    "pesticide_exposure_emergency"),
    ("Pesticide splashed in my eyes",        "pesticide_exposure_emergency"),
    # human_animal_disease_exposure
    ("I touched a sick animal and got blood on me",
                                             "human_animal_disease_exposure"),
    ("I'm worried about getting a disease from my livestock",
                                             "human_animal_disease_exposure"),
    ("I handled a dead animal without gloves",
                                             "human_animal_disease_exposure"),
    # specialist_referral
    ("Can you recommend a vet?",             "specialist_referral"),
    ("I need to speak to an agronomist",     "specialist_referral"),
    ("Who should I contact for soil testing?",
                                             "specialist_referral"),
    # default (catch-all)
    ("I need some help please",              "default"),
    ("I have a question about farming",      "default"),
    ("Can you assist me with something general?",
                                             "default"),
])
def test_agriculture_intents(msg, expected):
    intent, difficulty, confidence = classify_intent(msg, "agriculture")
    assert intent == expected, f"msg={msg!r}: got {intent!r}, expected {expected!r}"


# ── Tracer bullet (explicit per PM S1 spec) ───────────────────────────────────

def test_tracer_bullet():
    """S1 tracer: classify('hi', 'pharmacy') == ('greeting', 'low', 1.0)"""
    result = classify_intent("hi", "pharmacy")
    assert result == ("greeting", "low", 1.0), f"Tracer failed: {result}"


# ── Difficulty sanity checks ──────────────────────────────────────────────────

@pytest.mark.parametrize("msg,vertical,expected_difficulty", [
    ("Hi",                                        "pharmacy",     "low"),
    ("How much ibuprofen?",                        "pharmacy",     "critical"),
    ("I cannot breathe",                           "pharmacy",     "critical"),
    ("Do you have aspirin?",                       "pharmacy",     "medium"),
    ("Good morning",                               "agriculture",  "low"),
    ("My goat died suddenly",                      "agriculture",  "livestock_disease_outbreak_risk"),  # intent check
    ("Pesticide in my eyes",                       "agriculture",  "critical"),
    ("When should I plant tomatoes?",              "agriculture",  "medium"),
])
def test_difficulty_values(msg, vertical, expected_difficulty):
    intent, difficulty, _ = classify_intent(msg, vertical)
    if expected_difficulty in ("low", "medium", "high", "critical"):
        assert difficulty == expected_difficulty, (
            f"msg={msg!r} vertical={vertical}: difficulty={difficulty!r}, "
            f"expected={expected_difficulty!r}"
        )
    else:
        # intent check variant
        assert intent == expected_difficulty, (
            f"msg={msg!r}: intent={intent!r}, expected={expected_difficulty!r}"
        )


# ── Unknown vertical graceful fallback ───────────────────────────────────────

def test_unknown_vertical():
    intent, difficulty, confidence = classify_intent("hello", "retail")
    assert intent == "default"
    assert confidence == 0.5
