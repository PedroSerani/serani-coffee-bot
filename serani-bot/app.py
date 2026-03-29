from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import datetime

app = Flask(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
conversation_history = {}

GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")


def get_calendar_service():
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None


def crear_evento_google_calendar(nombre, telefono, fecha_hora_inicio, num_personas, tipo_curso, direccion, alergia, tipo_leche):
    service = get_calendar_service()
    if not service or not GOOGLE_CALENDAR_ID:
        return {"success": False, "error": "Calendar not configured"}
    try:
        start_dt = datetime.datetime.fromisoformat(fecha_hora_inicio)
        end_dt = start_dt + datetime.timedelta(hours=2)
        description = (
            "Reserva Serani Specialty Coffee\n\n"
            + "Nombre: " + nombre + "\n"
            + "Telefono: " + telefono + "\n"
            + "Personas: " + str(num_personas) + "\n"
            + "Curso: " + tipo_curso + "\n"
            + "Direccion: " + direccion + "\n"
            + "Alergia o intolerancia: " + alergia + "\n"
            + "Tipo de leche: " + tipo_leche
        )
        event = {
            "summary": "Clase Barista " + nombre + " (" + str(num_personas) + "p)",
            "location": direccion,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        }
        result = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return {"success": True, "event_id": result.get("id")}
    except Exception as e:
        print(f"Calendar event error: {e}")
        return {"success": False, "error": str(e)}


BOOKING_TOOL = [
    {
        "name": "crear_reserva",
        "description": (
            "Creates the reservation in Google Calendar. "
            "Call this ONLY after the client has confirmed all their details. "
            "Required: nombre, fecha_hora_inicio (ISO 8601), num_personas, tipo_curso, direccion, alergia, tipo_leche."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Full name of the client"},
                "telefono": {"type": "string", "description": "Client phone number"},
                "fecha_hora_inicio": {
                    "type": "string",
                    "description": "Start date and time in ISO 8601 format, e.g. 2025-04-15T10:00:00"
                },
                "num_personas": {"type": "integer", "description": "Number of people attending"},
                "tipo_curso": {"type": "string", "description": "Course type"},
                "direccion": {"type": "string", "description": "Client home address where the class will take place"},
                "alergia": {"type": "string", "description": "Allergies or intolerances. Use Ninguna if none."},
                "tipo_leche": {"type": "string", "description": "Preferred milk type"},
            },
            "required": ["nombre", "fecha_hora_inicio", "num_personas", "tipo_curso", "direccion", "alergia", "tipo_leche"],
        },
    }
]


def build_system_prompt():
    now = datetime.datetime.now().strftime("%A %B %d %Y %I:%M %p")
    return (
        "You are Sofia, the booking assistant and coffee specialist for Serani Specialty Coffee, "
        "founded by Pedro Serani (seranispecialtycoffee.com).\n\n"
        "Today is " + now + ". Use this to calculate exact dates when clients say things like next Saturday or this weekend.\n\n"
        "YOUR PERSONALITY\n"
        "Warm, enthusiastic about coffee, naturally educational. You handle everything from first contact to confirmed booking. "
        "You never pass clients to anyone else.\n\n"
        "THE HOME BARISTA COURSE\n"
        "Pedro comes to the client's home and teaches espresso extraction, milk texturing, bean selection, "
        "grinder calibration, water ratios, and sensory tasting. Perfect for all levels. Intimate class with personal "
        "attention from Pedro. The only course currently available is the Home Barista course.\n\n"
        "BOOKING FLOW\n"
        "When a client wants to book, collect the following naturally, one or two pieces at a time:\n\n"
        "1. Full name\n"
        "2. Preferred date and time\n"
        "3. Number of people attending\n"
        "4. Home address where the class will take place\n"
        "5. Any allergies or food intolerances (if none, use Ninguna)\n"
        "6. Preferred milk type (whole, oat, almond, skim, lactose-free, etc.)\n\n"
        "Once you have ALL the above, summarize it for the client and ask them to confirm.\n"
        "After they confirm, immediately call the crear_reserva tool.\n"
        "If the tool returns success, tell the client their spot is confirmed and you look forward to seeing them.\n"
        "If the tool returns an error, tell the client the reservation is noted and the team will send a final confirmation shortly.\n\n"
        "STRICT RULES\n"
        "Keep every message to 2 or 3 sentences max. WhatsApp style only. Short and punchy.\n"
        "Use coffee or sparkle emojis occasionally to feel warm and human.\n"
        "Always end with a question or a clear next step.\n"
        "Respond in the language the client uses. Switch between Spanish and English fluidly.\n"
        "NEVER say you will transfer, connect, or refer the client to Pedro or anyone else. You handle everything.\n"
        "NEVER mention you are an AI. If asked, say you are Sofia from the Serani team.\n"
        "NEVER use hyphens or em dashes in your responses to clients. Use commas or line breaks instead."
    )


def process_message(session_key, phone, user_message):
    if session_key not in conversation_history:
        conversation_history[session_key] = []

    conversation_history[session_key].append({"role": "user", "content": user_message})

    if len(conversation_history[session_key]) > 20:
        conversation_history[session_key] = conversation_history[session_key][-20:]

    system = build_system_prompt()

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=BOOKING_TOOL,
            messages=conversation_history[session_key]
        )

        if response.stop_reason == "tool_use":
            tool_block = next((b for b in response.content if b.type == "tool_use"), None)

            conversation_history[session_key].append({
                "role": "assistant",
                "content": response.content
            })

            if tool_block and tool_block.name == "crear_reserva":
                inp = tool_block.input
                result = crear_evento_google_calendar(
                    nombre=inp.get("nombre", ""),
                    telefono=inp.get("telefono", phone),
                    fecha_hora_inicio=inp.get("fecha_hora_inicio", ""),
                    num_personas=inp.get("num_personas", 1),
                    tipo_curso=inp.get("tipo_curso", "Home Barista"),
                    direccion=inp.get("direccion", ""),
                    alergia=inp.get("alergia", "Ninguna"),
                    tipo_leche=inp.get("tipo_leche", ""),
                )
                tool_result = json.dumps(result)
            else:
                tool_result = json.dumps({"error": "Unknown tool"})

            conversation_history[session_key].append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_block.id, "content": tool_result}]
            })

            final = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=system,
                tools=BOOKING_TOOL,
                messages=conversation_history[session_key]
            )
            reply = next((b.text for b in final.content if hasattr(b, "text")), "")
            conversation_history[session_key].append({"role": "assistant", "content": reply})

        else:
            reply = next((b.text for b in response.content if hasattr(b, "text")), "")
            conversation_history[session_key].append({"role": "assistant", "content": reply})

    except Exception as e:
        reply = "Uy, algo fallo de mi lado! Puedes repetir eso?"
        print(f"Error in process_message: {e}")

    return reply


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")

    if not incoming_msg:
        return str(MessagingResponse())

    reply = process_message(session_key=sender, phone=sender, user_message=incoming_msg)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/manychat", methods=["POST"])
def manychat():
    data = request.get_json(force=True) or {}
    incoming_msg = data.get("message", "").strip()
    contact_id   = data.get("contact_id", "").strip()
    phone        = data.get("phone", "")

    if not incoming_msg or not contact_id:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": ""}]}}), 200

    session_key = f"{contact_id}_{phone}" if phone else contact_id
    reply = process_message(session_key=session_key, phone=phone, user_message=incoming_msg)

    return jsonify({
        "version": "v2",
        "content": {
            "messages": [{"type": "text", "text": reply}]
        }
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return "Serani Specialty Coffee Bot Sofia is online!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
