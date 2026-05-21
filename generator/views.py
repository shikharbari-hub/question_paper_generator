import json
import random
import requests
import ast
import os
import urllib.request

from django.conf import settings
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .models import Subject, Topic, Question


MAX_PER_CALL = 15
TEACHER_PASSWORD = "teacher123"

# ✅ Hindi font — download and register karo
HINDI_FONT_PATH = "/tmp/NotoSansDevanagari.ttf"
HINDI_FONT_URL = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf"
HINDI_FONT_REGISTERED = False


def _ensure_hindi_font():
    """Hindi font download aur register karo agar nahi hai toh"""
    global HINDI_FONT_REGISTERED
    if HINDI_FONT_REGISTERED:
        return True
    try:
        if not os.path.exists(HINDI_FONT_PATH):
            urllib.request.urlretrieve(HINDI_FONT_URL, HINDI_FONT_PATH)
        pdfmetrics.registerFont(TTFont('NotoDevanagari', HINDI_FONT_PATH))
        HINDI_FONT_REGISTERED = True
        return True
    except Exception as e:
        print(f"Hindi font error: {e}")
        return False


def _call_openrouter(prompt: str) -> list:
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://question-paper-generator-n7gm.onrender.com",
            "X-Title": "QuestionAI",
        },
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert question paper generator. Return ONLY valid JSON arrays. Never add explanation or markdown."
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 8000,
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    if "choices" not in data:
        raise ValueError(f"Unexpected API response: {data}")

    raw = data["choices"][0]["message"]["content"].strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()

    questions = json.loads(raw)
    if not isinstance(questions, list):
        raise ValueError("Expected a JSON array.")
    return questions


def _build_mcq_prompt(topic: str, num: int, difficulty: str, language: str, offset: int = 0) -> str:
    start = offset + 1
    diff_map = {
        "easy":   "All questions EASY level — basic concepts only.",
        "medium": "All questions MEDIUM level — moderate difficulty.",
        "hard":   "All questions HARD level — advanced concepts.",
        "mixed":  "Mix of Easy, Medium and Hard questions.",
    }
    diff_text = diff_map.get(difficulty, diff_map["mixed"])
    lang_text = "Generate ALL questions and options in HINDI language using Devanagari script." if language == "hindi" else "Generate in ENGLISH."

    return f"""Generate EXACTLY {num} MCQ questions on: "{topic}". Starting from Q{start}.
DIFFICULTY: {diff_text}
LANGUAGE: {lang_text}
RULES: EXACTLY {num} questions, 4 options each (A,B,C,D), mark correct answer, ONLY JSON array.
[{{"question":"...","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A. ..."}}]"""


def _build_subjective_prompt(topic: str, num: int, difficulty: str, language: str, offset: int = 0) -> str:
    start = offset + 1
    diff_map = {
        "easy":   "EASY level questions — basic understanding.",
        "medium": "MEDIUM level questions — moderate depth.",
        "hard":   "HARD level questions — deep analysis required.",
        "mixed":  "Mix of Easy, Medium and Hard questions.",
    }
    diff_text = diff_map.get(difficulty, diff_map["mixed"])
    lang_text = "Generate ALL questions and answers in HINDI language using Devanagari script." if language == "hindi" else "Generate in ENGLISH."

    return f"""Generate EXACTLY {num} subjective questions on: "{topic}". Starting from Q{start}.
DIFFICULTY: {diff_text}
LANGUAGE: {lang_text}
RULES: EXACTLY {num} questions, each with detailed answer, ONLY JSON array.
[{{"question":"Explain ...?","answer":"Detailed answer here..."}}]"""


def _validate_mcq(raw_list: list) -> list:
    valid = []
    for q in raw_list:
        if (
            isinstance(q, dict)
            and isinstance(q.get("question"), str) and q["question"].strip()
            and isinstance(q.get("options"), list) and len(q["options"]) == 4
            and isinstance(q.get("answer"), str) and q["answer"].strip()
        ):
            valid.append(q)
    return valid


def _validate_subjective(raw_list: list) -> list:
    valid = []
    for q in raw_list:
        if (
            isinstance(q, dict)
            and isinstance(q.get("question"), str) and q["question"].strip()
            and isinstance(q.get("answer"), str) and q["answer"].strip()
        ):
            valid.append(q)
    return valid


def _remove_duplicates(questions: list) -> list:
    seen = set()
    unique = []
    for q in questions:
        key = q["question"].strip().lower()[:80]
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def _generate_questions(topic: str, total: int, q_type: str, difficulty: str, language: str) -> list:
    all_questions = []
    remaining = total
    offset = 0

    while remaining > 0:
        ask = min(remaining, MAX_PER_CALL)
        try:
            if q_type == "subjective":
                prompt = _build_subjective_prompt(topic, ask, difficulty, language, offset)
                raw = _call_openrouter(prompt)
                valid = _validate_subjective(raw)
            elif q_type == "mixed":
                mcq_ask = max(1, ask // 2)
                sub_ask = ask - mcq_ask
                mcq_raw = _call_openrouter(_build_mcq_prompt(topic, mcq_ask, difficulty, language, offset))
                mcq_valid = _validate_mcq(mcq_raw)
                sub_raw = _call_openrouter(_build_subjective_prompt(topic, sub_ask, difficulty, language, offset + mcq_ask))
                sub_valid = _validate_subjective(sub_raw)
                valid = mcq_valid[:mcq_ask] + sub_valid[:sub_ask]
            else:
                prompt = _build_mcq_prompt(topic, ask, difficulty, language, offset)
                raw = _call_openrouter(prompt)
                valid = _validate_mcq(raw)

            all_questions.extend(valid[:ask])
        except Exception:
            pass

        offset += ask
        remaining -= ask

    return _remove_duplicates(all_questions)[:total]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def ai_generate(request):
    questions = []
    error = None
    requested_num = 5
    topic = ""
    difficulty = "mixed"
    language = "english"
    q_type = "mcq"

    if request.method == "POST":
        topic = request.POST.get("topic", "").strip()
        difficulty = request.POST.get("difficulty", "mixed")
        language = request.POST.get("language", "english")
        q_type = request.POST.get("q_type", "mcq")

        try:
            requested_num = max(1, min(int(request.POST.get("num", 5)), 50))
        except (TypeError, ValueError):
            requested_num = 5

        if not topic:
            error = "Please enter a topic."
        else:
            try:
                questions = _generate_questions(topic, requested_num, q_type, difficulty, language)
                if len(questions) < requested_num:
                    error = f"Note: {requested_num} mein se {len(questions)} questions mile."
            except requests.exceptions.Timeout:
                error = "AI service timed out. Dobara try karo."
            except Exception as e:
                error = f"Error: {e}"

    return render(request, "ai_generate.html", {
        "questions": questions,
        "error": error,
        "requested_num": requested_num,
        "topic": topic,
        "difficulty": difficulty,
        "language": language,
        "q_type": q_type,
    })


@require_POST
def download_pdf(request):
    raw = request.POST.get("questions_data", "[]")
    pdf_type = request.POST.get("pdf_type", "student")
    coaching_name = request.POST.get("coaching_name", "").strip()
    topic = request.POST.get("topic", "").strip()
    q_type = request.POST.get("q_type", "mcq")
    language = request.POST.get("language", "english")

    # ✅ Teacher password check
    if pdf_type == "teacher":
        entered_password = request.POST.get("teacher_password", "")
        if entered_password != TEACHER_PASSWORD:
            return HttpResponse(
                "<h2 style='font-family:sans-serif;color:red;padding:2rem'>❌ Wrong password!</h2>"
                "<a href='javascript:history.back()' style='font-family:sans-serif;padding:1rem;display:block'>← Wapis jao</a>"
            )

    try:
        questions = ast.literal_eval(raw)
        if not isinstance(questions, list):
            raise ValueError
    except Exception:
        questions = []

    # ✅ Hindi font load karo agar Hindi language hai
    use_hindi_font = (language == "hindi") and _ensure_hindi_font()
    hindi_font = 'NotoDevanagari' if use_hindi_font else 'Helvetica'
    hindi_font_bold = 'NotoDevanagari' if use_hindi_font else 'Helvetica-Bold'

    fname = "student_paper.pdf" if pdf_type == "student" else "teacher_answerkey.pdf"
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{fname}"'

    doc = SimpleDocTemplate(
        response, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2.5*cm, bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()

    coaching_style = ParagraphStyle("Coaching", parent=styles["Normal"],
        fontSize=20, fontName="Helvetica-Bold", alignment=1,
        spaceAfter=4, textColor=colors.HexColor("#2563ff"))

    title_style = ParagraphStyle("Title", parent=styles["Heading1"],
        fontSize=14, spaceAfter=8, alignment=1)

    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"],
        fontSize=11, alignment=1, spaceAfter=8,
        textColor=colors.HexColor("#5a5f72"))

    # ✅ Hindi font use karo question aur options mein
    question_style = ParagraphStyle("Question", parent=styles["Normal"],
        fontSize=11, spaceAfter=4, leading=18,
        fontName=hindi_font_bold)

    option_style = ParagraphStyle("Option", parent=styles["Normal"],
        fontSize=10, leftIndent=20, leading=16, spaceAfter=2,
        fontName=hindi_font)

    answer_style = ParagraphStyle("Answer", parent=styles["Normal"],
        fontSize=10, leftIndent=20, leading=16,
        textColor=colors.HexColor("#1a7a4a"),
        fontName=hindi_font_bold)

    subjective_answer_style = ParagraphStyle("SubjAnswer", parent=styles["Normal"],
        fontSize=10, leftIndent=20, leading=18, spaceAfter=4,
        textColor=colors.HexColor("#1a7a4a"),
        fontName=hindi_font)

    story = []

    # ✅ Coaching naam header
    if coaching_name:
        story.append(Paragraph(coaching_name, coaching_style))
        story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#2563ff")))
        story.append(Spacer(1, 0.3*cm))

    if pdf_type == "student":
        story.append(Paragraph("Question Paper", title_style))
    else:
        story.append(Paragraph("Answer Key — Teacher Copy", title_style))

    if topic:
        story.append(Paragraph(f"Topic: {topic}", subtitle_style))

    story.append(Spacer(1, 0.3*cm))

    for i, q in enumerate(questions, 1):
        question_text = q.get("question", "")
        options = q.get("options", [])
        answer = q.get("answer", "")

        story.append(Paragraph(f"Q{i}. {question_text}", question_style))

        if options:
            for opt in options:
                if pdf_type == "teacher" and opt == answer:
                    story.append(Paragraph(f"✓ {opt}", answer_style))
                else:
                    story.append(Paragraph(f"    {opt}", option_style))
            if pdf_type == "student":
                story.append(Paragraph("Answer: _______", option_style))
        else:
            if pdf_type == "student":
                story.append(Paragraph("Answer:", option_style))
                for _ in range(3):
                    story.append(Paragraph("_" * 55, option_style))
            else:
                story.append(Paragraph(f"Answer: {answer}", subjective_answer_style))

        story.append(Spacer(1, 0.4*cm))

    doc.build(story)
    return response


def signup(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("login")
    else:
        form = UserCreationForm()
    return render(request, "signup.html", {"form": form})


@login_required
def generate_paper(request):
    questions = []
    if request.method == "POST":
        topic_id = request.POST.get("topic")
        num = request.POST.get("num_questions")
        difficulty = request.POST.get("difficulty")
        all_questions = Question.objects.filter(topic_id=topic_id)
        if difficulty:
            all_questions = all_questions.filter(difficulty=difficulty)
        num = int(num) if num else 5
        questions = random.sample(list(all_questions), min(num, len(all_questions)))
    topics = Topic.objects.all()
    return render(request, "generate.html", {"topics": topics, "questions": questions})