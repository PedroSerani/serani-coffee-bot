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
            window_start = datetime.datetime(target_date.year, target_date.month, target_date.day, 10, 0, 0) - datetime.timedelta(hours=utc_offset)
            window_end = datetime.datetime(target_date.year, target_date.month, target_date.day, 22, 0, 0) - datetime.timedelta(hours=utc_offset)
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


SYSTEM_PROMPT = """Eres Sofia, la especialista en café y guía de reservas de Serani Specialty Coffee. Respondes por WhatsApp en nombre de Pedro Serani. Tu personalidad es cálida, entusiasta y conocedora del café de especialidad.

IDIOMA: Responde SIEMPRE en español, a menos que el cliente escriba en inglés - en ese caso responde en inglés.

FORMATO WHATSAPP:
- Usa asterisco simple para negrita: *así*. NUNCA doble asterisco.
- NUNCA uses el guión largo (—). Usa guión normal (-) o reescribe.
- Mensajes cortos y conversacionales. Esto es WhatsApp, no un correo.
- Máximo 1-2 emojis por mensaje.

CONTEXTO:
El cliente llegó a través de un anuncio de Meta (Facebook o Instagram). Ya mostró interés en la experiencia de café. Recíbelo con entusiasmo y responde sus preguntas con pasión.

SOBRE PEDRO SERANI:
La *primera vez* que lo menciones en la conversación, preséntalo brevemente como nuestro fundador e instructor principal, apasionado por llevar educación de café de clase mundial directamente a los hogares. Después, solo usa su nombre de forma natural sin reintroducirlo.

SOBRE LA CLASE:
- Experiencia privada de café de especialidad en casa del cliente
- Duración: 4 horas (teoría + práctica)
- Pedro lleva todo el equipo y los granos directamente al domicilio
- Estructura del curso:
  * Parte teórica (30 min): introducción al café de especialidad, origen de los granos y catación
  * Parte práctica: selección de granos, calibración de espresso, texturización de leche, técnicas de arte latte y menú de bebidas
- Ubicación: Houston, TX

PRECIOS:
- 1 persona: *$150*
- 2 personas: *$250*
- 3 o más personas: *$100 por persona*

FLUJO DE CONVERSACIÓN (sigue este orden):
1. Saluda calurosamente y responde preguntas sobre la clase
2. Genera emoción - describe la experiencia única que vivirán
3. Pide el nombre del cliente
4. Pregunta cuántas personas participarán
5. Comparte el precio correspondiente según el número de personas
6. Pregunta qué fecha y hora prefieren
7. Verifica disponibilidad - SOLO ofrece horarios del bloque de DISPONIBILIDAD (si está disponible). Último inicio: 6:00 PM (la clase termina a las 10 PM). Si el horario pedido no está disponible, discúlpate y ofrece las alternativas más cercanas.
8. Pide la dirección escrita donde se realizará la clase (SOLO dirección escrita - NO pidas pin de WhatsApp ni ubicación)
9. Pregunta si alguno de los participantes tiene alergias o intolerancias alimentarias
10. Pregunta su preferencia de leche (entera, de avena, de almendra, etc.)
11. Explica el pago:
    - *$50 de depósito por Zelle* para reservar la fecha (aplica para cualquier tamaño de grupo)
    - El resto se paga en efectivo o Zelle el día de la clase
12. Comparte los datos de Zelle: enviar a *832-334-3416* (el nombre en la cuenta es Pedro Serani - eso es solo para confirmar que encontraron la cuenta correcta, pero lo importante es el número de teléfono)
13. Pídele al cliente que envíe el comprobante de pago por este mismo chat. En cuanto confirmes el pago recibido, dile que su clase queda oficialmente agendada.

REGLA IMPORTANTE DE RESERVA:
NO uses la herramienta create_booking hasta que el cliente confirme que ya realizó el depósito (diga "ya pagué", "ya envié", "hice la transferencia", o algo similar, o comparta un comprobante). Cuando confirme el pago, crea la reserva en el calendario y confirma al cliente que su clase está oficialmente agendada.

REGLA DE NO REPETIR PREGUNTAS:
Nunca vuelvas a preguntar algo que el cliente ya respondió anteriormente en la conversación. Antes de hacer cualquier pregunta del flujo, revisa el historial para verificar si ya tienes esa información. Si ya la tienes, avanza al siguiente paso directamente.

RECUERDA:
- Una vez que el cliente confirme el pago, dile que su clase está confirmada y agendada - no hay que esperar ninguna verificación adicional
- Si el cliente tiene dudas o preguntas adicionales, respóndelas con entusiasmo antes de seguir el flujo"""

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
    today_str = datetime.datetime.now().strftime("%A, %B %d, %Y")
    date_note = f"TODAY'S DATE: {today_str}. Always use this year when creating bookings."
    if availability:
        dynamic_prompt = (SYSTEM_PROMPT + f"\n\n{date_note}\n\n{availability}\n\n"
            + "IMPORTANT: When discussing scheduling, ONLY suggest time slots listed as available above. "
            + "If the customer requests a time that is not available, apologize warmly and offer the nearest open alternatives.")
    else:
        dynamic_prompt = SYSTEM_PROMPT + f"\n\n{date_note}"
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


@app.route("/manychat", methods=["POST"])
def manychat_webhook():
    """Endpoint for Manychat External Request integration."""
    data = request.get_json(silent=True) or {}
    incoming_msg = str(data.get("message", "")).strip()
    sender = str(data.get("phone", "")).strip()
    name = str(data.get("name", "")).strip()
    if not incoming_msg or not sender:
        return {"version": "v2", "content": {"messages": [{"type": "text", "text": "Hola, ¿en qué puedo ayudarte?"}]}}, 200
    if sender not in conversation_history:
        conversation_history[sender] = []
    # Prepend name context on first message
    user_content = incoming_msg
    if len(conversation_history[sender]) == 0 and name:
        user_content = incoming_msg
    conversation_history[sender].append({"role": "user", "content": user_content})
    if len(conversation_history[sender]) > 20:
        conversation_history[sender] = conversation_history[sender][-20:]
    availability = get_weekly_availability()
    today_str = datetime.datetime.now().strftime("%A, %B %d, %Y")
    date_note = f"TODAY'S DATE: {today_str}. Always use this year when creating bookings."
    if name and len(conversation_history[sender]) <= 2:
        date_note += f" The customer's name is {name}."
    if availability:
        dynamic_prompt = (SYSTEM_PROMPT + f"\n\n{date_note}\n\n{availability}\n\n"
            + "IMPORTANT: When discussing scheduling, ONLY suggest time slots listed as available above. "
            + "If the customer requests a time that is not available, apologize warmly and offer the nearest open alternatives.")
    else:
        dynamic_prompt = SYSTEM_PROMPT + f"\n\n{date_note}"
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
            booking_result = create_calendar_event(
                customer_name=tool_use_block.input.get("customer_name", name or "Cliente"),
                customer_phone=sender,
                date=tool_use_block.input.get("date", ""),
                time_str=tool_use_block.input.get("time", ""),
                address=tool_use_block.input.get("address", ""),
                notes=tool_use_block.input.get("notes", "")
            )
            tool_messages = list(conversation_history[sender]) + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_block.id, "content": booking_result}]}
            ]
            follow_up = anthropic_client.messages.create(
                model="claude-sonnet-4-6", max_tokens=1024,
                system=dynamic_prompt, messages=tool_messages
            )
            reply = follow_up.content[0].text
            conversation_history[sender].append({"role": "assistant", "content": response.content})
            conversation_history[sender].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_block.id, "content": booking_result}]})
            conversation_history[sender].append({"role": "assistant", "content": reply})
        else:
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    reply += block.text
            conversation_history[sender].append({"role": "assistant", "content": reply})
    except Exception as e:
        reply = "Hola, tuve un pequeño problema técnico. ¿Puedes repetir tu mensaje?"
        print(f"Manychat Claude error: {e}")
    # Return in Manychat's Dynamic Block format
    return {"version": "v2", "content": {"messages": [{"type": "text", "text": reply}]}}, 200


@app.route("/health", methods=["GET"])
def health():
    return "Serani Specialty Coffee Bot - Sofia is online!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
