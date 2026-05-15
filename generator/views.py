import json
import random
import requests
import ast
import math

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

from .models import Subject, Topic, Question


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Model ek baar mein max itne questions reliably deta hai
MAX_PER_CALL = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_openrouter(prompt: str) -> list:
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert MCQ generator. Return ONLY valid JSON arrays. Never add explanation or markdown."
                },
                {
                    "role": "user",
                    "content": prompt
                }
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

    # Markdown fences remove karo
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()

    questions = json.loads(raw)

    if not isinstance(questions, list):
        raise ValueError("Expected a JSON array.")

    return questions


def _build_prompt(topic: str, num: int, offset: int = 0) -> str:
    """
    offset se model ko pata chalta hai ki yeh questions already generated ke
    aage se start karne hain — duplicate avoid hote hain.
    """
    start = offset + 1
    end = offset + num
    return f"""Generate EXACTLY {num} multiple-choice questions on: "{topic}".
These are questions number {start} to {end} in a series.
Make sure these are DIFFERENT from any previous questions on this topic.

STRICT RULES:
- EXACTLY {num} questions — not less, not more.
- Each question: exactly 4 options (A, B, C, D).
- Mark the correct answer clearly.
- Return ONLY a raw JSON array. No markdown, no explanation.

[
  {{
    "question": "...",
    "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
    "answer": "A. ..."
  }}
]"""


def _validate(raw_list: list) -> list:
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


def _remove_duplicates(questions: list) -> list:
    seen = set()
    unique = []
    for q in questions:
        key = q["question"].strip().lower()[:80]
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def _generate_questions(topic: str, total: int) -> list:
    """
    ✅ Smart split:
    - Total ko MAX_PER_CALL chunks mein todo
    - Har chunk alag API call — silently background mein
    - Sab merge karo — user ko exact count milega
    """
    all_questions = []
    remaining = total
    offset = 0

    while remaining > 0:
        # Is call mein kitne maangne hain
        ask = min(remaining, MAX_PER_CALL)

        # Thoda extra maango taaki valid ke baad bhi enough ho
        ask_with_buffer = min(ask + 3, ask + remaining)

        prompt = _build_prompt(topic, ask_with_buffer, offset)

        try:
            raw = _call_openrouter(prompt)
            valid = _validate(raw)
            # Sirf utne lo jitne chahiye
            all_questions.extend(valid[:ask])
        except Exception:
            # Ek call fail ho toh dusri try karo
            try:
                raw = _call_openrouter(_build_prompt(topic, ask, offset))
                valid = _validate(raw)
                all_questions.extend(valid[:ask])
            except Exception:
                pass

        offset += ask
        remaining -= ask

    # Duplicates hatao
    all_questions = _remove_duplicates(all_questions)

    return all_questions[:total]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
@login_required
def ai_generate(request):
    questions = []
    error = None
    requested_num = 5

    if request.method == "POST":
        topic = request.POST.get("topic", "").strip()
        try:
            requested_num = max(1, min(int(request.POST.get("num", 5)), 50))
        except (TypeError, ValueError):
            requested_num = 5

        if not topic:
            error = "Please enter a topic."
        else:
            try:
                questions = _generate_questions(topic, requested_num)

                if len(questions) < requested_num:
                    error = (
                        f"Note: {requested_num} mein se {len(questions)} questions mile. "
                        f"Topic thoda aur specific karo ya dobara try karo."
                    )

            except requests.exceptions.Timeout:
                error = "AI service timed out. Thodi der baad try karo."
            except requests.exceptions.RequestException as e:
                error = f"Network error: {e}"
            except (json.JSONDecodeError, ValueError) as e:
                error = f"AI response parse nahi hua. Dobara try karo."
            except Exception as e:
                error = f"Unexpected error: {e}"

    return render(request, "ai_generate.html", {
        "questions": questions,
        "error": error,
        "requested_num": requested_num,
    })


@require_POST
def download_pdf(request):
    raw = request.POST.get("questions_data", "[]")

    try:
        questions = ast.literal_eval(raw)
        if not isinstance(questions, list):
            raise ValueError("Expected a list.")
    except Exception:
        questions = []

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="question_paper.pdf"'

    doc = SimpleDocTemplate(
        response,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", parent=styles["Heading1"], fontSize=16, spaceAfter=12
    )
    question_style = ParagraphStyle(
        "Question", parent=styles["Normal"], fontSize=11, spaceAfter=4,
        leading=16, fontName="Helvetica-Bold"
    )
    option_style = ParagraphStyle(
        "Option", parent=styles["Normal"], fontSize=10, leftIndent=20,
        leading=14, spaceAfter=2
    )
    answer_style = ParagraphStyle(
        "Answer", parent=styles["Normal"], fontSize=10, leftIndent=20,
        leading=14, textColor="#2e7d32", fontName="Helvetica-Oblique"
    )

    story = [
        Paragraph("AI-Generated Question Paper", title_style),
        Spacer(1, 0.3 * cm),
    ]

    for i, q in enumerate(questions, 1):
        question_text = q.get("question", "")
        options = q.get("options", [])
        answer = q.get("answer", "")

        story.append(Paragraph(f"Q{i}. {question_text}", question_style))
        for opt in options:
            story.append(Paragraph(f"• {opt}", option_style))
        if answer:
            story.append(Paragraph(f"✓ Answer: {answer}", answer_style))
        story.append(Spacer(1, 0.4 * cm))

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
        questions = random.sample(
            list(all_questions), min(num, len(all_questions))
        )
    topics = Topic.objects.all()
    return render(request, "generate.html", {"topics": topics, "questions": questions})