import email
import imaplib
import os
import time

import requests

# Configuration from Environment Variables
IMAP_SERVER = "imap.hostinger.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SIGNAL_API_URL = os.getenv("SIGNAL_API_URL", "http://signal-api:8080/v2/send")
SIGNAL_SENDER = os.getenv("SIGNAL_SENDER")
SIGNAL_GROUP_ID = os.getenv("SIGNAL_GROUP_ID")
TIMEOUT = 180  # maybe 3 min is okay to avoid spam check
LLM_PROMPT = (
    "You are a summary bot. Summarize the following email in ONE short sentence "
    "suitable for a Signal message. Ignore headers and signatures.\n\n"
    "Email Content: {email_body}"
)


def get_llm_summary(email_body):
    """
    Sends email text to the Ollama container and returns a 1-sentence summary.
    """
    ollama_url = (
        "http://"
        + os.getenv("OLLAMA_HOST", "ollama")
        + ":"
        + os.getenv("OLLAMA_PORT", "11434")
        + "/api/generate"
    )

    payload = {
        "model": os.getenv("OLLAMA_MODEL", "llama3.2-1b"),
        "prompt": LLM_PROMPT.format(
            email_body=email_body[:3000]
        ),  # Limit input to 3000 chars to save time
        "stream": False,
        "options": {
            "temperature": 0.3,  # Keep it focused/deterministic
            "num_predict": 200,  # Limit output length to save time
        },
    }

    try:
        response = requests.post(ollama_url, json=payload, timeout=30)
        response.raise_for_status()
        print(
            "DEBUG: LLM inference time: ", response.elapsed.total_seconds(), "seconds"
        )
        return response.json().get("response", "").strip()
    except Exception as e:
        print(f"⚠️ Error generating summary: {e}")
        return None


def listen_for_emails():
    print(f"🚀 Starting bridge for {EMAIL_USER}...")

    while True:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)

            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")

            # Search for unread emails
            status, messages = mail.search(None, "UNSEEN")

            for num in messages[0].split():
                _, data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])

                subject = msg.get("Subject", "No Subject")
                sender = msg.get("From", "Unknown Sender")

                print(f"📩 New email detected: {subject}")

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(
                                "utf-8", errors="ignore"
                            )
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                # summary = get_llm_summary(body) if len(body) > 100 else body
                summary = (
                    body if len(body) < 500 else body[:500] + "...\nMessaggio Troncato"
                )

                message = (
                    f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
                )
                if summary is not None:
                    message = message + f"\n\n**Summary:** {summary}"

                payload = {
                    "message": message,
                    "number": SIGNAL_SENDER,
                    "recipients": [SIGNAL_GROUP_ID],
                    "text_mode": "styled",
                }

                response = requests.post(SIGNAL_API_URL, json=payload)
                if response.status_code == 201:
                    print("✅ Successfully forwarded to Signal.")
                else:
                    print(f"❌ Failed to send: {response.text}")
            else:
                print("There is not any new mail")

            mail.logout()
        except Exception as e:
            print(f"⚠️ Error: {e}")

        time.sleep(TIMEOUT)


if __name__ == "__main__":
    listen_for_emails()
