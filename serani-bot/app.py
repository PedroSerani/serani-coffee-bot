from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import datetime

app = Flask(__name__)
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
conversation_history = {}

# Calendar availability cache - refreshes every 30 minutes
_calendar_cache = {"data": None, "updated_at": None}
CACHE_MINUTES = 30

# Tool definition so Sofia can create bookings
BOOKING_TOOLS = [
    {
        "name": "create_booking",
        "description": (
            "Create a confirmed coffee class booking in Google Calendar. "
            "Call this ONLY when the customer has confirmed ALL of: their name, "
            "the desired date and time, and their address/location for the class. "
            "Do NOT call this until the customer has explicitly said yes or confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer full name"
                },
                "date": {
                    "type": "string",
                    "description": "Booking date in YYYY-MM-DD format"
                },
                "time": {
                    "type": "string",
                    "description": "Start time in HH:MM 24-hour format (e.g. 14:00 for 2pm)"
                },
                "address": {
                    "type": "string",
                    "description": "Location/address where the class will take place"
                },
                "notes": {
                    "type": "string",
                    "description": "Additional info: milk preference, group size, equipment, etc."
                }
            },
            "required": ["customer_name", "date", "time", "address"]
        }
    }
]


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
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(f"Calendar service init error: {e}")
        return None


def get_weekly_availability():
    """Returns human-readable available slots for next 7 days. Cached 30 min."""
    global _calendar_cache
    now_utc = datetime.datetime.utcnow()
    if _calendar_cache["data"] and _calendar_cache["updated_at"]:
        age_minutes = (now_utc - _calendar_cache["updated_at"]).total_seconds() / 60
        if age_minutes < CACHE_MINUTES:
            return _calendar_cache["data"]
    service = get_calendar_service()
    if not service:
        return None
    try:
        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
        utc_offset = int(os.environ.get("HOUSTON_UTC_OFFSET", "-5"))
        lines = ["PEDRO'S AVAILABILITY (Houston time, next 7 days):"]
        for day_offset in range(7):
            target_date = now_utc.date() + datetime.timedelta(days=day_offset)
            window_start = datetime.datetime(target_date.year, target_date.month, target_date.day, 10 - utc_offset, 0, 0)
            window_end = datetime.datetime(target_date.year, target_date.month, target_date.day, 22 - utc_offset, 0, 0)
            body = {
                "timeMin": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeMax": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "items": [{"id": calendar_id}]
            }
            result = service.freebusy().query(body=body).execute()
            busy_periods = result["calendars"][calendar_id]["busy"]
            available_slots = []
            for hour in range(10, 19):
                slot_start_utc = datetime.datetime(target_date.year, target_date.month, target_date.day, hour - utc_offset, 0, 0)
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


def create_calendar_event(customer_name, customer_phone, date, time_str, address, notes=""):
    """Creates a 4-hour coffee class event in Pedro's Google Calendar."""
    service = get_calendar_service()
    if not service:
        return "Calendar not configured - booking noted but not added to calendar."
    try:
        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
        utc_offset = int(os.environ.get("HOUSTON_UTC_OFFSET", "-5"))
        year, month, day = map(int, date.split("-"))
        hour, minute = map(int, time_str.split(":"))
        # Convert Houston local time to UTC
        start_utc = datetime.datetime(year, month, day, hour, minute) - datetime.timedelta(hours=utc_offset)
        end_utc = start_utc + datetime.timedelta(hours=4)
        description_parts = [
            f"Customer: {customer_name}",
            f"WhatsApp: {customer_phone}",
            f"Location: {address}",
        ]
        if notes:
            description_parts.append(f"Notes: {notes}")
        event = {
            "summary": f"Coffee Class - {customer_name}",
            "location": address,
            "description": "\n".join(description_parts),
            "start": {"dateTime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
            "end": {"dateTime": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"}
        }
        result = service.events().insert(calendarId=calendar_id, body=event).execute()
        # Invalidate availability cache so next check reflects the new booking
        _calendar_cache["data"] = None
        _calendar_cache["updated_at"] = None
        print(f"Calendar event created: {result.get('id')}")
        return f"Booking successfully added to Pedro's calendar. Event ID: {result.get('id', 'ok')}"
    except Exception as e:
        print(f"Calendar event creation error: {e}")
        return f"Booking noted but calendar error occurred: {str(e)}"


SYSTEM_PROMPT = """You are Sofia, the passionate coffee specialist and enrollment guide for Serani Specialty Coffee. You answer via WhatsApp on behalf of Pedro Serani. Your personality is warm, knowledgeable, and enthusiastic about specialty coffee.

CRITICAL WhatsApp formatting rule: use single asterisks for bold like *this*, NEVER double asterisks like **this**.
CRITICAL: Never use the em dash character (--). Use a regular hyphen (-) or rewrite the sentence.

ABOUT THE BUSINESS:
Serani Specialty Coffee offers private in-home specialty coffee experiences led by Pedro. The 4-hour class covers brewing techniques, bean origins, tasting, and more. Classes are conducted at the customer's home in Houston.

ABOUT PEDRO:
The FIRST time you mention Pedro Serani in a conversation, briefly introduce him as our founder and head instructor with a passion for bringing world-class coffee education directly to people's homes. After the first mention, just use his name naturally without re-introducing him.

PRICING AND PAYMENT:
- Price: $250 for the full 4-hour private class
- Payment via Zelle: send to *832-334-3416* (the name on the account is Pedro Serani - that's just so they can confirm they found the right account, but the phone number is what matters)

BOOKING FLOW - follow these steps in order:
1. Warmly engage and answer any questions about the class
2. Ask for the customer's name
3. Ask for their preferred date and time
4. Check availability - ONLY suggest slots from the AVAILABILITY section below (if provided). Last available booking is 6:00 PM (class ends at 10 PM). If requested time is unavailable, apologize warmly and offer nearest open alternatives.
5. Ask for their address or location pin (accept either a typed address, a WhatsApp location pin, or both)
6. Ask about milk preferences (whole, oat, almond, etc.) and group size
7. Share pricing ($250) and Zelle payment details
8. Once the customer confirms everything (name, date, time, address) and is ready to book - use the create_booking tool to lock in their spot on Pedro's calendar, then send them a warm confirmation message

IMPORTANT BOOKING RULE: Only use the create_booking tool after the customer has explicitly confirmed their date, time, and address. After you call create_booking, send a warm confirmation message to the customer with all the booking details."""

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    if not incoming_msg:
        return str(MessagingResponse())
    if sender not in conversation_history:
        conversation_history[sender] = []
    conversation_history[sender].append({"role": "user", "content": incoming_msg})
    if len(conversation_history[sender]) > 20:
        conversation_history[sender] = conversation_history[sender][-20:]
    # Inject live calendar availability into system prompt
    availability = get_weekly_availability()
    if availability:
        dynamic_prompt = (SYSTEM_PROMPT + f"\n\n{availability}\n\n"
            + "IMPORTANT: When discussing scheduling, ONLY suggest time slots listed as available above. "
            + "If the customer requests a time that is not available, apologize warmly and offer the nearest open alternatives.")
    else:
        dynamic_prompt = SYSTEM_PROMPT
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=dynamic_prompt,
            messages=conversation_history[sender],
            tools=BOOKING_TOOLS
        )
        reply = ""
        tool_use_block = None
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use" and block.name == "create_booking":
                tool_use_block = block
                break
        if tool_use_block:
            # Execute the calendar booking
            booking_result = create_calendar_event(
                customer_name=tool_use_block.input.get("customer_name", "Customer"),
                customer_phone=sender,
                date=tool_use_block.input.get("date", ""),
                time_str=tool_use_block.input.get("time", ""),
                address=tool_use_block.input.get("address", ""),
                notes=tool_use_block.input.get("notes", "")
            )
            # Feed tool result back to get Sofia's confirmation message
            tool_messages = list(conversation_history[sender]) + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_block.id, "content": booking_result}
                ]}
            ]
            follow_up = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=dynamic_prompt,
                messages=tool_messages
            )
            reply = follow_up.content[0].text
            # Store full tool exchange in history
            conversation_history[sender].append({"role": "assistant", "content": response.content})
            conversation_history[sender].append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_block.id, "content": booking_result}
            ]})
            conversation_history[sender].append({"role": "assistant", "content": reply})
        else:
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    reply += block.text
            conversation_history[sender].append({"role": "assistant", "content": reply})
    except Exception as e:
        reply = "Hey! Something went sideways on my end. Could you send that again?"
        print(f"Error calling Claude API: {e}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return "Serani Specialty Coffee Bot - Sofia is online!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
