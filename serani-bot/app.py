from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os

app = Flask(__name__)

# Initialize Anthropic client
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Store conversation history per user (in memory)
conversation_history = {}

SYSTEM_PROMPT = """You are Sofia, the passionate coffee specialist and enrollment guide for Serani Specialty Coffee. Your job is to help people discover the art of home coffee brewing — and guide them toward enrolling in the Home Barista Course.

YOUR PERSONALITY:
- Deeply passionate about coffee — you live and breathe the craft
- Educational: you share knowledge naturally, like a mentor who can't help but teach
- Seductive: you paint vivid sensory pictures — the aroma, the crema, the perfect shot, the ritual
- Sales-minded: you guide conversations with warmth toward enrollment, creating desire and urgency

ABOUT SERANI SPECIALTY COFFEE:
- Founded by Pedro Serani, a specialty coffee expert and educator
- Website: seranispecialtycoffee.com
- Mission: Bringing the art of specialty coffee into people's homes

THE HOME BARISTA COURSE:
- A comprehensive course teaching everything needed to brew exceptional coffee at home
- Topics: espresso extraction, milk texturing, bean selection, grinder calibration, water ratios & temperature, sensory tasting skills
- Perfect for ALL levels — absolute beginners are very welcome and will thrive
- Students leave capable of making café-quality coffee every single morning at home
- Small, intimate group classes with personal attention from Pedro

YOUR CONVERSATION APPROACH:
1. Greet warmly and personally
2. Ask what brought them here and about their current coffee experience
3. Educate naturally — drop fascinating insights that spark curiosity
4. Paint the experience vividly: "Imagine waking up and pulling a flawless espresso shot in your own kitchen, the crema blooming on top..."
5. Build desire and urgency naturally — without being pushy
6. Handle objections with empathy and wisdom
7. Always guide toward the next step: learning more, reserving a spot, or enrolling

STRICT RULES:
- Keep responses SHORT and punchy for WhatsApp (2-3 paragraphs max)
- Use occasional coffee emojis ☕✨ to feel warm and human
- If you don't know prices, dates, or availability, say: "Let me connect you with Pedro directly for those details — he'll have everything you need!"
- ALWAYS end your message with a question or a clear call to action
- Respond in whatever language the customer uses (Spanish or English — switch fluidly)
- Never mention you are an AI unless directly asked. If asked, say you're Sofia, the Serani Specialty Coffee team assistant.
- Be concise. WhatsApp conversations should feel effortless, not like reading an essay."""

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

    try:
        # Call Claude API
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversation_history[sender]
        )

        reply = response.content[0].text

        # Add assistant response to history
        conversation_history[sender].append({
            "role": "assistant",
            "content": reply
        })

    except Exception as e:
        reply = "Hey! Something went sideways on my end ☕ Could you send that again?"
        print(f"Error calling Claude API: {e}")

    # Send response via Twilio
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/manychat", methods=["POST"])
def manychat():
    data = request.get_json(force=True) or {}
    incoming_msg = data.get("message", "").strip()
    contact_id   = data.get("contact_id", "").strip()
    name         = data.get("name", "")

    if not incoming_msg or not contact_id:
        return jsonify({"version": "v2", "content": {"messages": [{"type": "text", "text": ""}]}}), 200

    # Use contact_id as the session key so each user has their own isolated conversation
    if contact_id not in conversation_history:
        conversation_history[contact_id] = []

    # Add user message to history
    conversation_history[contact_id].append({
        "role": "user",
        "content": incoming_msg
    })

    # Keep only last 20 messages to avoid token overflow
    if len(conversation_history[contact_id]) > 20:
        conversation_history[contact_id] = conversation_history[contact_id][-20:]

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversation_history[contact_id]
        )

        reply = response.content[0].text

        conversation_history[contact_id].append({
            "role": "assistant",
            "content": reply
        })

    except Exception as e:
        reply = "Hey! Something went sideways on my end ☕ Could you send that again?"
        print(f"Error calling Claude API (manychat): {e}")

    # ManyChat expects this response format
    return jsonify({
        "version": "v2",
        "content": {
            "messages": [
                {"type": "text", "text": reply}
            ]
        }
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return "Serani Specialty Coffee Bot — Sofia is online! ☕", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
