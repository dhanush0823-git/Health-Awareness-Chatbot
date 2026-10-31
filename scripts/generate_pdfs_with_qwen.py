from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data_sources"
PDF_DIR = ROOT / "knowledge_base"
CHECKPOINT_DIR = ROOT / "generation_cache"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
MAX_RETRIES = 4

TOPICS = {
    "Health_FAQ": [
        "Hydration and dehydration prevention", "Understanding persistent fever",
        "Reducing respiratory infection spread", "Sleep and whole-person health",
        "Recognising urgent warning signs", "Everyday prevention habits",
        "Tracking symptoms responsibly", "When to seek professional advice",
        "Health information literacy", "Building a practical personal wellness plan",
    ],
    "Vaccination": [
        "How vaccination works", "Why vaccination matters", "Understanding vaccine schedules",
        "Common side effects and safety", "Preparing for a vaccination visit",
        "Vaccination across life stages", "Community immunization and outbreaks",
        "Vaccine myths and evidence", "Keeping accurate immunization records",
        "Making an informed vaccination decision",
    ],
    "Outbreak": [
        "What an infectious disease outbreak means", "How infections spread",
        "Community prevention measures", "What to do during a local outbreak",
        "Recognising symptoms and seeking care", "Protecting higher-risk people",
        "Testing, isolation and public-health guidance", "Outbreak communication and misinformation",
        "Healthcare and community coordination", "After an outbreak: review and preparedness",
    ],
    "General_Medical_Questions": [
        "Understanding symptoms without self-diagnosing", "Preparing for a medical appointment",
        "Medicines: safe-use principles", "Understanding common health measurements",
        "Nutrition and hydration fundamentals", "Physical activity and recovery",
        "Mental wellbeing and stress", "Preventive healthcare and screening",
        "Allergies and adverse reactions", "When symptoms require urgent attention",
    ],
}

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="DocTitle", parent=styles["Heading1"], fontSize=17, leading=21, textColor=colors.HexColor("#123047"), spaceAfter=8))
styles.add(ParagraphStyle(name="SubTitle", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#647789"), alignment=1, spaceAfter=5))
styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontSize=10, leading=12, textColor=colors.HexColor("#087E78"), spaceBefore=6, spaceAfter=3))
styles.add(ParagraphStyle(name="BodySmall", parent=styles["BodyText"], fontSize=8, leading=10.5, textColor=colors.HexColor("#263746"), spaceAfter=4))
styles.add(ParagraphStyle(name="Callout", parent=styles["BodyText"], fontSize=7.5, leading=9, textColor=colors.HexColor("#704A0A"), backColor=colors.HexColor("#FFF4D8"), borderPadding=6, spaceBefore=5, spaceAfter=5))

def page_footer(canvas, doc):
    canvas.saveState()
    w, _ = A4
    canvas.setStrokeColor(colors.HexColor("#DCE6EC"))
    canvas.line(17*mm, 14*mm, w-17*mm, 14*mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#6B7F8E"))
    canvas.drawString(17*mm, 9*mm, "AURA HEALTH · QWEN-STRUCTURED EDUCATIONAL CONTENT")
    canvas.drawRightString(w-17*mm, 9*mm, f"Page {doc.page}")
    canvas.restoreState()

def read_seed(name: str) -> list[dict]:
    filename = {
        "Health_FAQ": "health_faq.csv",
        "Vaccination": "vaccination.csv",
        "Outbreak": "outbreak.csv",
    }.get(name)
    if not filename:
        return []
    with (SOURCE_DIR / filename).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def extract_json(raw: str) -> dict:
    """Parse JSON from Ollama output and tolerate surrounding markdown/text."""
    raw = raw.strip().lstrip("\ufeff")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise json.JSONDecodeError("No JSON object found in Qwen output", raw, 0)
    # Find the end of the outer object while respecting quoted strings.
    depth, in_string, escaped = 0, False, False
    for pos in range(start, len(raw)):
        char = raw[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start:pos + 1])
    raise json.JSONDecodeError(
        "Qwen output ended before the JSON object was complete",
        raw,
        len(raw)
    )

def validate_page(data: dict, dataset: str, topic: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Qwen response is not a JSON object.")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("Qwen response is missing a title.")
    if not isinstance(data.get("sections"), list) or len(data["sections"]) < 3:
        raise ValueError("Qwen response needs at least three sections.")
    clean_sections = []
    for section in data["sections"]:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading", "")).strip()
        content = str(section.get("content", "")).strip()
        if heading and content:
            clean_sections.append({"heading": heading, "content": content})
    if len(clean_sections) < 3:
        raise ValueError("Qwen response contains fewer than three usable sections.")
    takeaways = data.get("key_takeaways", [])
    if not isinstance(takeaways, list):
        takeaways = []
    return {
        "title": data["title"].strip(),
        "learning_objective": str(data.get("learning_objective", "")).strip(),
        "sections": clean_sections[:5],
        "key_takeaways": [str(x).strip() for x in takeaways[:5] if str(x).strip()],
    }

def call_qwen(dataset: str, topic: str, seed_rows: list[dict], page_number: int) -> dict:
    seed_text = json.dumps(seed_rows, ensure_ascii=False)
    prompt = f"""
Write one detailed but concise general-health education page.
Dataset: {dataset}
Page: {page_number}/10
Topic: {topic}
Source records: {seed_text}

Return ONLY one valid JSON object with:
{{
 "title": "page title",
 "learning_objective": "2 short sentences",
 "sections": [
   {{"heading":"Core concepts","content":"paragraph"}},
   {{"heading":"Practical guidance","content":"paragraph"}},
   {{"heading":"What to observe or ask","content":"paragraph"}},
   {{"heading":"When to seek help","content":"paragraph"}}
 ],
 "key_takeaways": ["item 1","item 2","item 3"]
}}

IMPORTANT:
- Keep the complete response around 220-280 words. Do not exceed this.
- Finish every JSON string and close every bracket. Never stop mid-sentence.
- Use the CSV records as factual anchors when present; do not invent facts or claim unsupported source details.
- General_Medical_Questions is an additional educational topic, not a supplied CSV dataset.
- Do not invent statistics, citations, drug doses, diagnoses or individualized treatment plans.
- Advice varies by person and location. Recommend qualified clinicians for personal decisions.
- Mention emergency care for severe or life-threatening symptoms.
- Output valid JSON only, without markdown fences.
"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You create careful health education material. "
                                "Return a complete, syntactically valid JSON object only. "
                                "Be concise and always close all strings and braces."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 1800,
                        "num_ctx": 4096,
                    },
                },
                timeout=300,
            )
            response.raise_for_status()
            raw = response.json().get("message", {}).get("content", "")
            data = validate_page(extract_json(raw), dataset, topic)
            return data
        except (json.JSONDecodeError, ValueError, requests.RequestException) as exc:
            last_error = exc
            print(f"  Attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(
        f"Qwen could not generate valid content for {dataset} page {page_number} "
        f"({topic}) after {MAX_RETRIES} attempts. Last error: {last_error}"
    )

def safe_paragraph(text: str) -> str:
    return escape(str(text)).replace("\n", "<br/>")

def create_pdf(dataset: str, pages: list[dict]):
    output = PDF_DIR / f"{dataset}.pdf"
    doc = SimpleDocTemplate(
        str(output), pagesize=A4, rightMargin=16*mm, leftMargin=16*mm,
        topMargin=12*mm, bottomMargin=18*mm
    )
    story = []
    for index, page in enumerate(pages, start=1):
        if index == 1:
            story.append(Paragraph("AURA HEALTH KNOWLEDGE BASE", styles["SubTitle"]))
            story.append(Paragraph(dataset.replace("_", " "), styles["DocTitle"]))
            story.append(Paragraph("Qwen 2.5 (1.5B) structured educational content · 10 pages", styles["SubTitle"]))
        story.append(Paragraph(f"{index:02d}. {safe_paragraph(page['title'])}", styles["DocTitle"]))
        story.append(Paragraph("<b>Learning objective:</b> " + safe_paragraph(page.get("learning_objective", "")), styles["BodySmall"]))
        for section in page.get("sections", []):
            story.append(Paragraph(safe_paragraph(section.get("heading", "Key information")), styles["Section"]))
            story.append(Paragraph(safe_paragraph(section.get("content", "")), styles["BodySmall"]))
        takeaways = page.get("key_takeaways", [])
        if takeaways:
            story.append(Paragraph("Key takeaways", styles["Section"]))
            story.append(Paragraph(" • ".join(safe_paragraph(x) for x in takeaways), styles["BodySmall"]))
        story.append(Paragraph(
            "Medical safety: General education only, not diagnosis or individualized treatment. "
            "For severe breathing difficulty, chest pain, stroke signs, severe bleeding, "
            "loss of consciousness or severe allergic reaction, seek emergency help immediately.",
            styles["Callout"]
        ))
        if index < 10:
            story.append(PageBreak())
    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    count = len(PdfReader(str(output)).pages)
    if count != 10:
        raise RuntimeError(
            f"{output.name} rendered as {count} pages, not 10. "
            "The generated page text overflowed. Reduce page text and run again."
        )
    print(f"Created {output} ({count} pages)")

def main():
    PDF_DIR.mkdir(exist_ok=True)
    CHECKPOINT_DIR.mkdir(exist_ok=True)
    for dataset, topics in TOPICS.items():
        seeds = read_seed(dataset)
        checkpoint_file = CHECKPOINT_DIR / f"{dataset}.json"
        if checkpoint_file.exists():
            try:
                generated = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                if not isinstance(generated, list):
                    generated = []
            except (json.JSONDecodeError, OSError):
                generated = []
        else:
            generated = []

        # Keep valid completed pages and resume at the first missing page.
        generated = generated[:len(topics)]
        for page_no, topic in enumerate(topics, start=1):
            if page_no <= len(generated):
                print(f"Using saved page {page_no}/10: {dataset}")
                continue
            print(f"Generating {dataset}: page {page_no}/10 — {topic}")
            page = call_qwen(dataset, topic, seeds, page_no)
            generated.append(page)
            checkpoint_file.write_text(
                json.dumps(generated, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        create_pdf(dataset, generated)

if __name__ == "__main__":
    main()
