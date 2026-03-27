from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import datetime

app = Flask(__name__)

# Initialize Anthropic client
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Store conversation history per user (in memory)
conversation_history = {}

# Calendar availability cache - refreshes every 30 minutes
_calendar_cache = {"data": None, "updated_at": None}
CACHE_MINUTES = 30


def get_calendar_service():
    """Build a Google Calendar service client using service account credentials."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            return None

        creds_info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"]
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(f"Calendar service init error: {e}")
        return None


def get_weekly_availability():
    """
    Return a human-readable string of Pedro's available time slots
    for the next 7 days. Results are cached for CACHE_MINUTES.
    Returns None if Google credentials are not configured.
    """
    global _calendar_cache

    now_utc = datetime.datetime.utcnow()

    # Return cached result if still fresh
    if _calendar_cache["data"] and _calendar_cache["updated_at"]:
        age_minutes = (now_utc - _calendar_cache["updated_at"]).total_seconds() / 60
        if age_minutes < CACHE_MINUTES:
            return _calendar_cache["data"]

    service = get_calendar_service()
    if not service:
        return None  # Calendar not configured - bot works without it

    try:
        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

        # Houston = UTC-5 (CDT) or UTC-6 (CST). Configurable via env var.
        utc_offset = int(os.environ.get("HOUSTON_UTC_OFFSET", "-5"))

        lines = ["PEDRO'S AVAILABILITY (Houston time, next 7 days):"]

        for day_offset in range(7):
            target_date = now_utc.date() + datetime.timedelta(days=day_offset)

            # Window: 10am to 10pm Houston time expressed in UTC
            window_start = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                10 - utc_offset, 0, 0
            )
            window_end = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                22 - utc_offset, 0, 0
            )

            body = {
                "timeMin": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeMax": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "items": [{"id": calendar_id}]
            }

            result = service.freebusy().query(body=body).execute()
            busy_periods = result["calendars"][calendar_id]["busy"]

            available_slots = []
            for hour in range(10, 19):  # 10am to 6pm last start
                slot_start_utc = datetime.datetime(
                    target_date.year, target_date.month, target_date.day,
                    hour - utc_offset, 0, 0
                )
                slot_end_utc = slot_start_utc + datetime.timedelta(hours=4)

                is_free = True
                for busy in busy_periods:
                    b_start = datetime.datetime.strptime(busy["start"], "%Y-%m-%dT%H:%M:%SZ")
                    b_end = datetime.datetime.strptime(busy["end"], "%Y-%m-%dT%H:%M:%SZ")
                    if not (slot_end_utc <= b_start or slot_start_utc >= b_end):
                        is_free = False
                        break

                if is_free:
                    display_hour = hour if hour <= 12 else hour - 12
                    am_pm = "AM" if hour < 12 else "PM"
                    available_slots.append(f"{display_hour}:00 {am_pm}")

            day_label = (now_utc.date() + datetime.timedelta(days=day_offset)).strftime("%A %b %d")
            if available_slots:
                lines.append(f"- {day_label}: {', '.join(available_slots)}")
            else:
                lines.append(f"- {day_label}: fully booked")

        availability_text = "\n".join(lines)
        _calendar_cache["data"] = availability_text
        _calendar_cache["updated_at"] = now_utc
        return availability_text

    except Exception as e:
        print(f"Calendar query error: {e}")
        return None


SYSTEM_PROMPT = """You are Sofia, the passionate coffee specialist and enrollment guide for Serani Specialty Coffee. Your job is to help people discover the art of home coffee brewing and guide them toward enrolling in the Home Barista Course.

YOUR PERSONALITY:
- Deeply passionate about coffee - you live and breathe the craft
- Educational: you share knowledge naturally, like a mentor who can't help but teach
- Seductive: you paint vivid sensory pictures - the aroma, the crema, the perfect shot, the ritual
- Sales-minded: you guide conversations with warmth toward enrollment, creating desire and urgency

ABOUT SERANI SPECIALTY COFFEE:
- Founded by Pedro Serani (our founder and head instructor), a specialty coffee expert and educator
- Website: seranispecialtycoffee.com
- Mission: Bringing the art of specialty coffee into people's homes
- Based in Houston, TX area

THE HOME BARISTA COURSE:
- A comprehensive 4-hour in-person course teaching everything needed to brew exceptional coffee at home
- Topics: espresso extraction, milk texturing, bean selection, grinder calibration, water ratios and temperature, sensory tasting skills
- Perfect for ALL levels - absolute beginners are very welcome and will thrive
- Students leave capable of making cafe-quality coffee every single morning at home
- Small, intimate group classes (maximum 6 students) with personal attention from Pedro Serani
- Everything is provided - students don't need to bring anything
- Available 7 days a week - first class starts at 10am, last class starts at 6pm (the 4-hour course ends at 10pm)

PRICING:
- 1 student: $150
- 2 students: $250 (great deal for couples or friends!)
- 3 or more students: $100 per student (up to 6 max)

LOCATION:
- We come to YOUR home anywhere in the Houston area (most popular option!)
- We also have a location at our leasing office: 23403 Kingsland Blvd, Katy, TX 77494
  (Note: Pedro Serani's home studio is currently under renovations, so the Katy location is the alternative)

SCHEDULING & BOOKING:
- Date and time are agreed upon right here in the chat - very flexible!
- To reserve a spot, a $50 deposit is required (applied toward the full course price)
- Deposit is paid via Zelle: the number to search and send to is 832-334-3416. The name on the account is Pedro Serani, which is just so they can confirm they found the right one
- Zelle payers get a discount on the remaining balance
- Full payment can also be done online, but Zelle is preferred and gets a better rate

BOOKING FLOW - follow this order naturally:
1. Build excitement and answer questions about the course
2. Once they are interested, find out how many students will attend
3. ALWAYS ask about any food intolerances or allergies AND milk preference (whole milk, oat, almond, soy, etc.) - do this before confirming
4. Agree on a preferred date and time - ONLY suggest slots that appear in the AVAILABILITY section below (if provided). If no availability data is shown, ask their preference and say Pedro will confirm
5. Confirm the location - ask them to share their address. They can send a location pin, type their address, or both
6. At the end, when everything is set, share the deposit details: send $50 via Zelle to 832-334-3416. The name Pedro Serani on the account is just to confirm they found the right one
7. Once the deposit is confirmed, send this reminder: "One last thing - please avoid drinking coffee on the day of the class! We are going to be tasting and drinking A LOT of coffee together, so come with a fresh palate. See you soon!"

YOUR CONVERSATION APPROACH:
1. Greet warmly and personally
2. Ask what brought them here and about their current coffee experience
3. Educate naturally - drop fascinating insights that spark curiosity
4. Paint the experience vividly: "Imagine waking up and pulling a flawless espresso shot in your own kitchen, the crema blooming on top..."
5. Build desire and urgency naturally - without being pushy
6. Handle objections with empathy and wisdom
7. Always guide toward the next step: learning more, reserving a spot, or enrolling

STRICT RULES:
- Keep responses SHORT and punchy for WhatsApp (2-3 paragraphs max)
- Use occasional coffee emojis to feel warm and human
- ALWAYS end your message with a question or a clear call to action
- Respond in whatever language the customer uses (Spanish or English - switch fluidly)
- Never mention you are an AI unless directly asked. If asked, say you are Sofia, the Serani Specialty Coffee team assistant.
- Be concise. WhatsApp conversations should feel effortless, not like reading an essay.
- Never share the Zelle number until the very end when the customer is ready to pay the deposit.
- CRITICAL WhatsApp formatting rule: use single asterisks for bold like *this*, NEVER double asterisks. Using **double asterisks** will show the ** symbols visibly to the customer and look broken.
- CRITICAL: Never use the em dash character (the long dash). Use a regular hyphen (-) or rewrite the sentence instead.
- The FIRST time you mention Pedro Serani in a conversation, briefly introduce him as our founder and head instructor. After that, just use his name naturally."""


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    if not incoming_msg:
        return str(MessagingResponse())

    # Get or create conversation history for this user
    if sender not in conversation_history:
        conversation_history[sender] = []

    # Add user message to history
    conversation_history[sender].append({
        "role": "user",
        "content": incoming_msg
    })

    # Keep only last 20 messages to avoid token overflow
    if len(conversation_history[sender]) > 20:
        conversation_history[sender] = conversation_history[sender][-20:]

    # Build dynamic system prompt - inject live calendar availability if configured
    availability = get_weekly_availability()
    if availability:
        dynamic_prompt = (
            SYSTEM_PROMPT
            + f"\n\n{availability}\n\n"
            + "IMPORTANT: When discussing scheduling, ONLY suggest time slots listed as available above. "
            + "If the customer requests a time that is not available, apologize warmly and offer the nearest open alternatives."
        )
    else:
        dynamic_prompt = SYSTEM_PROMPT

    try:
        # Call Claude API
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=dynamic_prompt,
            messages=conversation_history[sender]
        )

        reply = response.content[0].text

        # Add assistant response to history
        conversation_history[sender].append({
            "role": "assistant",
            "content": reply
        })

    except Exception as e:
        reply = "Hey! Something went sideways on my end. Could you send that again?"
        print(f"Error calling Claude API: {e}")

    # Send response via Twilio
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return "Serani Specialty Coffee Bot - Sofia is online!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
