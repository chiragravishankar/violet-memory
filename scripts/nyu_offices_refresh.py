"""Rebuild config/nyu_offices.json from official nyu.edu pages (web search restricted to nyu.edu).

    .venv/bin/python scripts/nyu_offices_refresh.py

Every contact detail must come from a cited nyu.edu page; anything not found stays empty. Review the
file afterwards; it's plain JSON you can edit.
"""

import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")
import llm  # noqa: E402

OFFICES = [
    ("Office of Global Services (OGS)", "visas, F-1/J-1 status, I-20, CPT, OPT, travel signatures, international students"),
    ("Office of the University Registrar", "registration, adding and dropping classes, holds, transcripts, enrollment verification, academic calendar"),
    ("Office of the Bursar", "tuition bills, payments, refunds, payment plans, financial holds"),
    ("Office of Financial Aid", "financial aid, scholarships, loans, FAFSA, work-study"),
    ("NYU Student Health Center", "medical appointments, immunizations, insurance waivers, health"),
    ("NYU Wellness Exchange", "mental health support, counseling, crisis support, 24/7 hotline"),
    ("Henry and Lucy Moses Center for Student Accessibility", "disability accommodations, accessibility"),
    ("Office of Residential Life and Housing Services", "housing, residence halls, housing assignments, housing contracts"),
    ("NYU Dining", "meal plans, dining halls, Campus Cash, dining dollars"),
    ("NYU IT Service Desk", "NetID, passwords, email, Wi-Fi, software, ServiceLink tickets, tech help"),
    ("NYU Card Center", "NYU ID card, replacing a lost ID, card access"),
    ("NYU Printing Services", "printing on campus, print stations, NYU print, printing at Tandon and Dibner"),
    ("Wasserman Center for Career Development", "careers, internships, jobs, Handshake, resume reviews, career fairs"),
    ("NYU Libraries (Ask a Librarian)", "library help, research, borrowing, study spaces"),
    ("NYU Tandon Office of Graduate Academics", "Tandon graduate advising, Tandon academic policies, Tandon registration questions"),
    ("NYU Entrepreneurial Institute / Leslie eLab", "startups, entrepreneurship programs, Leslie eLab"),
    ("NYU Campus Safety", "campus safety, emergencies, lost and found, escorts"),
    ("NYU Transportation Services", "campus shuttles, NYU transit, shuttle routes"),
]

TASK = """Find the official NYU contact for: {name} (handles: {topics}).
Use only nyu.edu pages. Return ONLY JSON:
{{"name": "...", "url": "main official page", "email": "... or null", "phone": "... or null",
"location": "street address or building, or null", "hours": "... or null",
"sources": ["nyu.edu URLs where each detail appears"]}}
Only include a detail if it appears on a cited nyu.edu page. Never guess an email or phone."""


async def one(name, topics):
    r = await llm.client().responses.create(
        model="gpt-5.4", input=TASK.format(name=name, topics=topics),
        tools=[{"type": "web_search", "filters": {"allowed_domains": ["nyu.edu"]}}])
    m = re.search(r"\{.*\}", r.output_text, re.S)
    data = json.loads(m.group(0)) if m else {"name": name}
    data["topics"] = [t.strip() for t in topics.split(",")]
    data["checked"] = date.today().isoformat()
    return data


async def main():
    out = await asyncio.gather(*(one(n, t) for n, t in OFFICES), return_exceptions=True)
    offices = [o for o in out if isinstance(o, dict)]
    for (n, _), o in zip(OFFICES, out):
        if not isinstance(o, dict):
            print("failed:", n, type(o).__name__)
    path = Path("config/nyu_offices.json")
    path.write_text(json.dumps({"_about": "Official NYU offices Violet points students to. Built from nyu.edu pages "
                                          "by scripts/nyu_offices_refresh.py; every detail has a source. Edit freely.",
                                "offices": offices}, indent=1))
    for o in offices:
        print(f"{o.get('name')}: {o.get('email')} | {o.get('phone')} | {o.get('url')}")


asyncio.run(main())
