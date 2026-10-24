import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="AcademiQ - Academic Email Assistant")
logger = logging.getLogger(__name__)
frontend_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500,http://localhost:5501,http://127.0.0.1:5501,http://localhost:5173,http://127.0.0.1:5173,null",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in frontend_origins if origin.strip()],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

SYSTEM_PROMPT = """
أنت AcademiQ، مساعد متخصص حصرياً في كتابة ومراجعة رسائل البريد الأكاديمية الموجهة
للأساتذة والمشرفين. ارفض بلطف أي طلب خارج هذا النطاق باستخدام العبارة:
"عذراً، أنا AcademiQ متخصص فقط في مساعدة كتابة رسائل البريد الأكاديمي، ولا يمكنني الإجابة على هذا السؤال."
اكتب رسالة واضحة وقابلة للتعديل، واشرح بإيجاز سبب اختيار الصياغة.
استخدم هذا التنسيق دائماً:
الموضوع: [موضوع الرسالة]
نص الرسالة:
[نص الرسالة]
سبب الصياغة:
[شرح موجز]
"""

TONE_GUIDANCE = {
    "formal": "استخدم نبرة رسمية جداً ومحترمة.",
    "friendly": "استخدم نبرة ودية ومهنية مع الحفاظ على الاحترام الأكاديمي.",
    "urgent": "استخدم نبرة عاجلة ومهذبة، ووضح الحاجة إلى الرد سريعاً دون ضغط غير لائق.",
    "apology": "استخدم نبرة اعتذار صادقة ومهنية، مع تحمل المسؤولية عند الحاجة.",
}
LANGUAGE_GUIDANCE = {
    "ar": "اكتب الرسالة باللغة العربية الفصحى.",
    "en": "Write the email in clear, professional English.",
}


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    session_id: str = Field(min_length=1, max_length=128)
    tone: str = "formal"
    language: str = "ar"


_client = None
_sessions: dict[str, object] = {}


# Return a Gemini client configured with the server-side API key.
def get_client():
    global _client
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ضع GOOGLE_API_KEY أو GEMINI_API_KEY في ملف .env لتفعيل المساعد.",
        )
    if _client is None:
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=60000),
        )
    return _client


# Serve the single-page AcademiQ interface.
@app.get("/")
async def home():
    return FileResponse(Path(__file__).parent / "index.html")


# Validate chat options, generate an academic email, and return its subject and body.
@app.post("/chat")
async def chat_endpoint(request: MessageRequest):
    if request.tone not in TONE_GUIDANCE:
        raise HTTPException(status_code=422, detail="النبرة المحددة غير مدعومة.")
    if request.language not in LANGUAGE_GUIDANCE:
        raise HTTPException(status_code=422, detail="لغة الرسالة غير مدعومة.")

    try:
        client = get_client()
        chat = _sessions.get(request.session_id)
        if chat is None:
            chat = client.chats.create(
                model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                config={"system_instruction": SYSTEM_PROMPT},
            )
            _sessions[request.session_id] = chat

        guidance = (
            f"\n\nتعليمات هذه الرسالة: {TONE_GUIDANCE[request.tone]} "
            f"{LANGUAGE_GUIDANCE[request.language]}"
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(chat.send_message, request.message + guidance),
            timeout=70,
        )
        reply = response.text or "تعذر إنشاء رد. يرجى المحاولة مرة أخرى."
        subject_match = re.search(r"(?im)^\s*(?:الموضوع|subject)\s*:\s*(.+)$", reply)
        body_match = re.search(
            r"(?is)(?:نص الرسالة|email body|email)\s*:\s*(.*?)(?=\n\s*(?:سبب الصياغة|صياغة|explanation)\s*:|\Z)",
            reply,
        )
        return {
            "reply": reply,
            "subject": subject_match.group(1).strip() if subject_match else "",
            "email_body": body_match.group(1).strip() if body_match else reply,
        }
    except HTTPException:
        raise
    except asyncio.TimeoutError as exc:
        logger.warning("Gemini API request timed out")
        raise HTTPException(
            status_code=504,
            detail="تأخر رد Gemini. تحقق من اتصال الإنترنت ثم حاول مرة أخرى.",
        ) from exc
    except Exception as exc:
        logger.exception("Gemini API request failed")
        upstream_status = getattr(exc, "code", None)
        try:
            upstream_status = int(upstream_status)
        except (TypeError, ValueError):
            upstream_status = None
        details_by_status = {
            400: "رفضت Gemini الطلب. تحقق من اسم النموذج وإعدادات الطلب.",
            401: "رفضت Gemini مفتاح API. تحقق من المفتاح الموجود في ملف .env.",
            403: "مفتاح API لا يملك صلاحية Gemini API أو أن الخدمة غير مفعّلة للمشروع.",
            404: "لم يُعثر على نموذج Gemini المحدد. تحقق من قيمة GEMINI_MODEL في .env.",
            429: "تم بلوغ حد الاستخدام أو الحصة المتاحة في Gemini API.",
            504: "تجاوزت Gemini مهلة تنفيذ الطلب. أعد المحاولة؛ النموذج الافتراضي خفيف وأسرع.",
        }
        detail = details_by_status.get(upstream_status)
        if detail is None:
            diagnostic = str(exc).strip() or "لا توجد تفاصيل إضافية"
            for key_name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
                api_key = os.getenv(key_name)
                if api_key:
                    diagnostic = diagnostic.replace(api_key, "[مفتاح مخفي]")
            diagnostic = re.sub(r"AIza[\w-]{20,}", "[مفتاح مخفي]", diagnostic)
            diagnostic = re.sub(
                r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+",
                r"\1[مفتاح مخفي]",
                diagnostic,
            )
            detail = f"خطأ {type(exc).__name__}: {diagnostic[:300]}"
        raise HTTPException(status_code=502, detail=detail) from exc
