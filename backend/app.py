from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from functools import wraps
from datetime import date, datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg import IntegrityError as PsycopgIntegrityError
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - local dev fallback when psycopg is unavailable
    psycopg = None
    PsycopgIntegrityError = Exception
    dict_row = None

BACKEND_DIR = os.path.dirname(__file__)
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
INSTANCE_DIR = os.path.join(PROJECT_DIR, "instance")


app = Flask(
    __name__,
    template_folder=os.path.join(FRONTEND_DIR, "templates"),
    static_folder=os.path.join(FRONTEND_DIR, "static"),
)
app.secret_key = os.environ.get("SECRET_KEY", "freelaunch-dev-secret-2024")

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE = os.path.join(INSTANCE_DIR, "freelance.db")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
SECURITY_QUESTIONS = [
    "What is your favorite color?",
    "What city were you born in?",
    "What was the name of your first pet?",
]


def password_is_strong(password: str) -> bool:
    if len(password) < 8:
        return False
    has_letter = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    return has_letter and has_digit


def get_db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is set.")
        return DBConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return DBConnection(conn)


def translate_sql(sql):
    if DATABASE_URL:
        return sql.replace("?", "%s")
    return sql


class DBConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(translate_sql(sql), params)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def scalar_one(conn, sql, params=(), default=None):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    if hasattr(row, "keys"):
        values = list(row.values())
        return values[0] if values else default
    return row[0]


def format_date(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value[:10]
    return ""


app.jinja_env.filters["format_date"] = format_date


def run_script(conn, script):
    for statement in script.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(translate_sql(statement))


def column_exists(conn, table_name, column_name):
    if not DATABASE_URL:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row[1] == column_name for row in rows)
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    ).fetchone()
    return row is not None


def ensure_column(conn, table_name, column_def):
    column_name = column_def.split()[0]
    if column_exists(conn, table_name, column_name):
        return
    conn.execute(translate_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_def}"))


def init_db():
    if not DATABASE_URL:
        os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = get_db()
    if DATABASE_URL:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('client', 'freelancer')),
            bio TEXT DEFAULT '',
            skills TEXT DEFAULT '',
            avatar_letter TEXT DEFAULT 'U',
            phone TEXT DEFAULT '',
            security_question TEXT DEFAULT '',
            security_answer_hash TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            budget_min REAL NOT NULL,
            budget_max REAL NOT NULL,
            skills_required TEXT DEFAULT '',
            status TEXT DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'closed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id SERIAL PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            freelancer_id INTEGER NOT NULL REFERENCES users(id),
            cover_letter TEXT NOT NULL,
            bid_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, freelancer_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL REFERENCES users(id),
            receiver_id INTEGER NOT NULL REFERENCES users(id),
            job_id INTEGER,
            content TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            reviewer_id INTEGER NOT NULL,
            reviewee_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, reviewer_id)
        );

        CREATE TABLE IF NOT EXISTS job_insights (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
            payload TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    else:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('client', 'freelancer')),
            bio TEXT DEFAULT '',
            skills TEXT DEFAULT '',
            avatar_letter TEXT DEFAULT 'U',
            phone TEXT DEFAULT '',
            security_question TEXT DEFAULT '',
            security_answer_hash TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            budget_min REAL NOT NULL,
            budget_max REAL NOT NULL,
            skills_required TEXT DEFAULT '',
            status TEXT DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'closed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(client_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            freelancer_id INTEGER NOT NULL,
            cover_letter TEXT NOT NULL,
            bid_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(freelancer_id) REFERENCES users(id),
            UNIQUE(job_id, freelancer_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            job_id INTEGER,
            content TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(sender_id) REFERENCES users(id),
            FOREIGN KEY(receiver_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            reviewee_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            UNIQUE(job_id, reviewer_id)
        );

        CREATE TABLE IF NOT EXISTS job_insights (
            job_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        """
    run_script(conn, schema)
    ensure_column(conn, "users", "phone TEXT DEFAULT ''")
    ensure_column(conn, "users", "security_question TEXT DEFAULT ''")
    ensure_column(conn, "users", "security_answer_hash TEXT DEFAULT ''")

    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        seed_data(conn)

    conn.commit()
    conn.close()


def seed_data(c):
    c.execute(
        "INSERT INTO users (name, email, password, role, bio, skills, avatar_letter, phone, security_question, security_answer_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "Alice Chen",
            "alice@demo.com",
            generate_password_hash("demo123"),
            "client",
            "Product manager building SaaS tools",
            "project management,product",
            "A",
            "",
            SECURITY_QUESTIONS[0],
            generate_password_hash("Green"),
        ),
    )
    c.execute(
        "INSERT INTO users (name, email, password, role, bio, skills, avatar_letter, phone, security_question, security_answer_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "Bob Sharma",
            "bob@demo.com",
            generate_password_hash("demo123"),
            "freelancer",
            "Full-stack developer with 6 years experience",
            "Python,React,Node.js,PostgreSQL",
            "B",
            "",
            SECURITY_QUESTIONS[1],
            generate_password_hash("Mumbai"),
        ),
    )
    c.execute(
        "INSERT INTO users (name, email, password, role, bio, skills, avatar_letter, phone, security_question, security_answer_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "Priya Nair",
            "priya@demo.com",
            generate_password_hash("demo123"),
            "freelancer",
            "UI/UX designer and Figma expert",
            "Figma,CSS,Illustrator,User Research",
            "P",
            "",
            SECURITY_QUESTIONS[2],
            generate_password_hash("Misty"),
        ),
    )
    c.execute(
        "INSERT INTO users (name, email, password, role, bio, skills, avatar_letter, phone, security_question, security_answer_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "David Wu",
            "david@demo.com",
            generate_password_hash("demo123"),
            "client",
            "Startup founder looking for tech talent",
            "",
            "D",
            "",
            SECURITY_QUESTIONS[0],
            generate_password_hash("Blue"),
        ),
    )

    jobs = [
        (
            1,
            "Build a REST API for mobile app",
            "We need a Python/FastAPI developer to build our backend REST API with JWT auth, CRUD endpoints, and PostgreSQL integration.",
            "Backend Development",
            800,
            1500,
            "Python,FastAPI,PostgreSQL",
            "open",
        ),
        (
            1,
            "React dashboard for analytics",
            "Looking for a React developer to build an interactive analytics dashboard with charts, filters, and real-time data.",
            "Frontend Development",
            600,
            1200,
            "React,TypeScript,Chart.js",
            "open",
        ),
        (
            4,
            "Logo and brand identity design",
            "Need a professional logo and complete brand kit for our fintech startup including color palette, typography, and usage guidelines.",
            "Design",
            300,
            700,
            "Illustrator,Branding,Figma",
            "open",
        ),
        (
            4,
            "WordPress website redesign",
            "Redesign our existing WordPress site with a modern look, mobile responsive, and improved UX.",
            "Web Design",
            400,
            900,
            "WordPress,CSS,PHP",
            "open",
        ),
        (
            1,
            "Python data scraper",
            "Build a robust web scraper using Python and Scrapy to collect product data from e-commerce sites with proxy rotation.",
            "Data & Analytics",
            200,
            500,
            "Python,Scrapy,BeautifulSoup",
            "open",
        ),
        (
            4,
            "Mobile app UI design",
            "Create complete UI designs for our iOS and Android app with 15 screens and prototypes in Figma.",
            "Design",
            500,
            1000,
            "Figma,Mobile UI,Prototyping",
            "open",
        ),
    ]
    for job in jobs:
        c.execute(
            """
            INSERT INTO jobs (client_id, title, description, category, budget_min, budget_max, skills_required, status)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            job,
        )

    c.execute(
        "INSERT INTO proposals (job_id, freelancer_id, cover_letter, bid_amount, status) VALUES (?,?,?,?,?)",
        (
            1,
            2,
            "I have 4 years of FastAPI experience and have built similar APIs for fintech companies. I can deliver in 2 weeks.",
            1200,
            "pending",
        ),
    )
    c.execute(
        "INSERT INTO proposals (job_id, freelancer_id, cover_letter, bid_amount, status) VALUES (?,?,?,?,?)",
        (
            3,
            3,
            "I specialize in brand identity for startups. My portfolio includes 20+ logo projects. Let's chat!",
            550,
            "pending",
        ),
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def split_list(text):
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def build_job_prompt(job):
    skills = ", ".join(split_list(job["skills_required"])) or "No explicit skills listed"
    return f"""
You are a career and curriculum strategist for a freelance marketplace.
Analyze the job below and return ONLY valid JSON with these keys:
- summary: short job summary in plain English
- match_score: number from 1 to 10
- must_have_skills: array of 5 or fewer strings
- recommended_courses: array of objects with keys title, provider, reason
- custom_topic: one practical custom learning topic tailored to this job
- interview_questions: array of 3 short screening questions
- portfolio_project: one custom project idea a freelancer could build to prove skill

Job title: {job['title']}
Category: {job['category']}
Budget: {job['budget_min']} to {job['budget_max']}
Required skills: {skills}
Description: {job['description']}
""".strip()


def fallback_job_analysis(job):
    category = (job["category"] or "").lower()
    skills = split_list(job["skills_required"])
    description = (job["description"] or "").lower()

    catalog = [
        {
            "match": ["backend", "api", "python", "fastapi", "django", "flask", "postgres"],
            "courses": [
                {"title": "FastAPI Crash Course", "provider": "Udemy", "reason": "Great for shipping production APIs quickly."},
                {"title": "PostgreSQL Essential Training", "provider": "LinkedIn Learning", "reason": "Covers schema design and query fundamentals."},
                {"title": "Python for Backend Development", "provider": "Coursera", "reason": "Builds strong API and service foundations."},
            ],
            "topic": "Designing secure REST APIs with authentication and database integration",
        },
        {
            "match": ["frontend", "react", "ui", "dashboard", "typescript", "chart"],
            "courses": [
                {"title": "React: The Complete Guide", "provider": "Udemy", "reason": "Useful for component-driven product work."},
                {"title": "TypeScript Essentials", "provider": "Pluralsight", "reason": "Helps maintain clean, scalable frontend code."},
                {"title": "Data Visualization in React", "provider": "LinkedIn Learning", "reason": "Good match for charts and analytics dashboards."},
            ],
            "topic": "Building responsive analytics dashboards with reusable chart components",
        },
        {
            "match": ["design", "figma", "branding", "illustrator", "ux", "ui"],
            "courses": [
                {"title": "Figma UI/UX Design Essentials", "provider": "Coursera", "reason": "Matches the design workflow in the brief."},
                {"title": "Brand Identity Design", "provider": "Skillshare", "reason": "Helpful for logo, typography, and brand systems."},
                {"title": "User Experience Research", "provider": "LinkedIn Learning", "reason": "Improves discovery and validation skills."},
            ],
            "topic": "Creating a complete brand system and responsive design handoff",
        },
        {
            "match": ["data", "scraper", "scrapy", "beautifulsoup", "analytics"],
            "courses": [
                {"title": "Web Scraping with Python", "provider": "Udemy", "reason": "Directly relevant to data extraction workflows."},
                {"title": "Python Data Analysis", "provider": "Coursera", "reason": "Helps clean and structure scraped data."},
                {"title": "SQL for Data Analysis", "provider": "DataCamp", "reason": "Useful for storing and querying data at scale."},
            ],
            "topic": "Robust scraping pipelines with proxy rotation and data cleaning",
        },
        {
            "match": ["wordpress", "web design", "php", "css"],
            "courses": [
                {"title": "WordPress Theme Development", "provider": "LinkedIn Learning", "reason": "Strong fit for redesign and customization work."},
                {"title": "Modern CSS Layouts", "provider": "Frontend Masters", "reason": "Improves responsive layout implementation."},
                {"title": "PHP for Beginners", "provider": "Udemy", "reason": "Useful when extending WordPress functionality."},
            ],
            "topic": "Building a responsive WordPress redesign with custom templates",
        },
    ]

    selected = None
    haystack = " ".join([category, " ".join(skills), description])
    for item in catalog:
        if any(token in haystack for token in item["match"]):
            selected = item
            break
    if not selected:
        selected = {
            "courses": [
                {"title": "Freelancing Fundamentals", "provider": "LinkedIn Learning", "reason": "Useful for discovery, scoping, and delivery."},
                {"title": "Project Management Basics", "provider": "Coursera", "reason": "Helps with estimation and client communication."},
                {"title": "Portfolio Project Design", "provider": "Skillshare", "reason": "Good for packaging your work professionally."},
            ],
            "topic": f"Building a project portfolio for {job['title'].lower()}",
        }

    summary = f"This job is a {job['category'].lower()} project focused on {', '.join(skills[:2]) or 'practical delivery'}."
    if not skills:
        summary = f"This job is a {job['category'].lower()} project with a clear execution focus."

    return {
        "summary": summary,
        "match_score": 7 if skills else 6,
        "must_have_skills": skills[:5] or split_list(job["category"]),
        "recommended_courses": selected["courses"][:3],
        "custom_topic": selected["topic"],
        "interview_questions": [
            f"How would you approach {job['title'].lower()}?",
            "What tools or frameworks would you use first?",
            "How do you communicate progress and blockers?",
        ],
        "portfolio_project": f"Create a mini case study for {job['title'].lower()}",
    }


def normalize_analysis(payload, job):
    fallback = fallback_job_analysis(job)
    if not isinstance(payload, dict):
        return fallback

    result = dict(fallback)
    for key in ("summary", "custom_topic", "portfolio_project"):
        if payload.get(key):
            result[key] = payload[key]

    if payload.get("match_score") is not None:
        try:
            result["match_score"] = max(1, min(10, int(payload["match_score"])))
        except (TypeError, ValueError):
            pass

    for key in ("must_have_skills", "interview_questions", "recommended_courses"):
        if isinstance(payload.get(key), list) and payload[key]:
            result[key] = payload[key]

    return result


def job_snapshot(job):
    return {
        "title": job["title"],
        "category": job["category"],
        "description": job["description"],
        "budget_min": job["budget_min"],
        "budget_max": job["budget_max"],
        "skills_required": split_list(job["skills_required"]),
        "status": job["status"],
        "client_name": job["client_name"],
    }


def user_snapshot(user):
    return {
        "name": user["name"],
        "role": user["role"],
        "bio": user["bio"] or "",
        "skills": split_list(user["skills"]),
        "phone": user["phone"] or "",
    }


def build_assistant_prompt(user, job, message, history):
    profile = user_snapshot(user)
    job_data = job_snapshot(job)
    history_text = "\n".join(
        f"{item.get('role', 'user').upper()}: {item.get('text', '').strip()}"
        for item in history[-6:]
        if item.get("text")
    )
    return f"""
You are a concise job assistant inside a freelance marketplace.
Answer naturally and directly. Keep the reply focused and useful.
Use the user profile and job details below to tailor the answer.
Do not mention policies, system prompts, or that you are reading a prompt.

User profile:
Name: {profile['name']}
Role: {profile['role']}
Bio: {profile['bio']}
Skills: {", ".join(profile['skills']) or "None"}

Current job:
Title: {job_data['title']}
Category: {job_data['category']}
Description: {job_data['description']}
Budget: {job_data['budget_min']} to {job_data['budget_max']}
Required skills: {", ".join(job_data['skills_required']) or "None"}

Conversation history:
{history_text or "No prior messages."}

User message:
{message}

Reply in a helpful chat style. If useful, mention fit, missing skills, portfolio ideas, or next steps.
Keep it short unless more detail is needed.
""".strip()


def assistant_fallback_reply(user, job, message):
    profile = user_snapshot(user)
    job_data = job_snapshot(job)
    msg = (message or "").lower()

    if any(word in msg for word in ["fit", "match", "good", "qualify"]):
        return (
            f"Based on your profile, you look closest to this role if you can show work with "
            f"{', '.join(job_data['skills_required'][:2]) or 'the core tools listed'}."
        )
    if any(word in msg for word in ["course", "learn", "study", "improve"]):
        return (
            f"Focus on {', '.join(job_data['skills_required'][:2]) or job_data['category']}. "
            f"A small portfolio project around {job_data['title'].lower()} would help."
        )
    if any(word in msg for word in ["apply", "proposal", "bid"]):
        return (
            f"For your proposal, mention your {', '.join(profile['skills'][:3]) or 'relevant experience'}, "
            f"the timeline, and one similar project."
        )
    return (
        f"This job is centered on {job_data['category'].lower()}. "
        f"Given your profile, highlight {', '.join(profile['skills'][:2]) or 'relevant work'} and keep the proposal specific."
    )


def call_gemini_chat(user, job, message, history):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    prompt = build_assistant_prompt(user, job, message, history)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 500,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            raise RuntimeError("Gemini returned an empty response.")
        return text
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini request failed.")


def parse_json_payload(text):
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def call_gemini_analysis(job):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    prompt = build_job_prompt(job)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        raise RuntimeError("Gemini request failed.")

    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        parsed = parse_json_payload(text)
        if not parsed:
            raise RuntimeError("Gemini returned an empty response.")
        return parsed
    except (IndexError, KeyError, TypeError):
        raise RuntimeError("Gemini request failed.")


def store_job_analysis(conn, job_id, analysis):
    conn.execute(
        """
        INSERT INTO job_insights (job_id, payload, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(job_id) DO UPDATE SET
            payload=excluded.payload,
            updated_at=CURRENT_TIMESTAMP
        """,
        (job_id, json.dumps(analysis)),
    )
    conn.commit()


def get_job_analysis(conn, job, refresh=False):
    row = conn.execute("SELECT payload FROM job_insights WHERE job_id=?", (job["id"],)).fetchone()
    if row and not refresh:
        try:
            return normalize_analysis(json.loads(row["payload"]), job)
        except json.JSONDecodeError:
            pass

    analysis = call_gemini_analysis(job)
    if not analysis:
        analysis = fallback_job_analysis(job)
    else:
        analysis = normalize_analysis(analysis, job)

    store_job_analysis(conn, job["id"], analysis)
    return analysis


@app.route("/")
def index():
    conn = get_db()
    jobs = conn.execute(
        """
        SELECT j.*, u.name AS client_name, u.avatar_letter,
               (SELECT COUNT(*) FROM proposals WHERE job_id = j.id) AS proposal_count
        FROM jobs j
        JOIN users u ON j.client_id = u.id
        WHERE j.status = 'open'
        ORDER BY j.created_at DESC
        LIMIT 6
        """
    ).fetchall()
    stats = {
        "jobs": scalar_one(conn, "SELECT COUNT(*) AS count FROM jobs WHERE status='open'", default=0),
        "freelancers": scalar_one(conn, "SELECT COUNT(*) AS count FROM users WHERE role='freelancer'", default=0),
        "clients": scalar_one(conn, "SELECT COUNT(*) AS count FROM users WHERE role='client'", default=0),
    }
    conn.close()
    return render_template("index.html", jobs=jobs, stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        role = request.form["role"]
        security_question = request.form.get("security_question", "").strip()
        security_answer = request.form.get("security_answer", "").strip()
        conn = get_db()
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            conn.close()
            flash("Email already registered.", "error")
            return render_template("register.html")
        if not password_is_strong(password):
            conn.close()
            flash("Use at least 8 characters with letters and numbers.", "error")
            return render_template("register.html")
        if not security_question or security_question not in SECURITY_QUESTIONS:
            conn.close()
            flash("Choose a security question.", "error")
            return render_template("register.html")
        if not security_answer:
            conn.close()
            flash("Add an answer for the security question.", "error")
            return render_template("register.html")
        conn.execute(
            "INSERT INTO users (name, email, password, role, avatar_letter, security_question, security_answer_hash) VALUES (?,?,?,?,?,?,?)",
            (
                name,
                email,
                generate_password_hash(password),
                role,
                name[0].upper(),
                security_question,
                generate_password_hash(security_answer) if security_answer else "",
            ),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_role"] = user["role"]
        session["avatar_letter"] = user["avatar_letter"]
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["user_role"] = user["role"]
            session["avatar_letter"] = user["avatar_letter"]
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    conn = get_db()
    user = None
    step = "email"
    email = ""

    if request.method == "POST":
        step = request.form.get("step", "email")
        email = request.form.get("email", "").strip().lower()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if step == "email":
            if not user:
                flash("No account found for that email.", "error")
            elif not user["security_question"]:
                flash("This account does not have a security question set.", "error")
            else:
                step = "reset"
        elif step == "reset":
            answer = request.form.get("security_answer", "").strip()
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not user:
                flash("No account found for that email.", "error")
                step = "email"
            elif not user["security_question"]:
                flash("This account does not have a security question set.", "error")
                step = "email"
            elif not user["security_answer_hash"] or not check_password_hash(user["security_answer_hash"], answer):
                flash("Security answer is incorrect.", "error")
                step = "reset"
            elif not new_password or new_password != confirm_password:
                flash("Passwords do not match.", "error")
                step = "reset"
            elif not password_is_strong(new_password):
                flash("Use at least 8 characters with letters and numbers.", "error")
                step = "reset"
            else:
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(new_password), user["id"]),
                )
                conn.commit()
                conn.close()
                flash("Password updated. Please sign in.", "success")
                return redirect(url_for("login"))

    conn.close()
    return render_template(
        "forgot_password.html",
        user=user,
        step=step,
        email=email,
        security_questions=SECURITY_QUESTIONS,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    uid = session["user_id"]
    role = session["user_role"]
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    data = {}
    if role == "client":
        data["my_jobs"] = conn.execute(
            """
            SELECT j.*, (SELECT COUNT(*) FROM proposals WHERE job_id = j.id) AS proposal_count
            FROM jobs j
            WHERE client_id = ?
            ORDER BY created_at DESC
            """,
            (uid,),
        ).fetchall()
        data["total_proposals"] = sum(job["proposal_count"] for job in data["my_jobs"])
    else:
        data["my_proposals"] = conn.execute(
            """
            SELECT p.*, j.title, j.budget_min, j.budget_max, j.category, u.name AS client_name
            FROM proposals p
            JOIN jobs j ON p.job_id = j.id
            JOIN users u ON j.client_id = u.id
            WHERE p.freelancer_id = ?
            ORDER BY p.created_at DESC
            """,
            (uid,),
        ).fetchall()
        data["open_jobs"] = scalar_one(conn, "SELECT COUNT(*) AS count FROM jobs WHERE status='open'", default=0)
    data["unread_messages"] = scalar_one(
        conn,
        "SELECT COUNT(*) AS count FROM messages WHERE receiver_id=? AND is_read=0",
        (uid,),
        default=0,
    )
    conn.close()
    return render_template("dashboard.html", data=data, role=role, user=user)


@app.route("/jobs")
def jobs():
    category = request.args.get("category", "")
    search = request.args.get("q", "")
    budget_max = request.args.get("budget", "")
    conn = get_db()
    query = """
        SELECT j.*, u.name AS client_name, u.avatar_letter,
               (SELECT COUNT(*) FROM proposals WHERE job_id = j.id) AS proposal_count
        FROM jobs j
        JOIN users u ON j.client_id = u.id
        WHERE j.status = 'open'
    """
    params = []
    if category:
        query += " AND j.category = ?"
        params.append(category)
    if search:
        query += " AND (j.title LIKE ? OR j.description LIKE ?)"
        params.extend([f"%{search}%"] * 2)
    if budget_max:
        query += " AND j.budget_min <= ?"
        params.append(float(budget_max))
    query += " ORDER BY j.created_at DESC"
    jobs = conn.execute(query, params).fetchall()
    categories = conn.execute("SELECT DISTINCT category FROM jobs ORDER BY category").fetchall()
    conn.close()
    return render_template(
        "jobs.html",
        jobs=jobs,
        categories=categories,
        selected_category=category,
        search=search,
        budget_max=budget_max,
    )


@app.route("/jobs/new", methods=["GET", "POST"])
@login_required
def new_job():
    if session["user_role"] != "client":
        flash("Only clients can post jobs.", "error")
        return redirect(url_for("jobs"))
    if request.method == "POST":
        conn = get_db()
        conn.execute(
            """
            INSERT INTO jobs (client_id, title, description, category, budget_min, budget_max, skills_required)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                session["user_id"],
                request.form["title"],
                request.form["description"],
                request.form["category"],
                float(request.form["budget_min"]),
                float(request.form["budget_max"]),
                request.form["skills_required"],
            ),
        )
        conn.commit()
        conn.close()
        flash("Job posted successfully!", "success")
        return redirect(url_for("dashboard"))
    return render_template("new_job.html")


@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    conn = get_db()
    job = conn.execute(
        """
        SELECT j.*, u.name AS client_name, u.avatar_letter, u.bio AS client_bio
        FROM jobs j
        JOIN users u ON j.client_id = u.id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if not job:
        conn.close()
        return "Job not found", 404

    proposals = []
    user_proposal = None
    if "user_id" in session:
        if session["user_role"] == "client" and job["client_id"] == session["user_id"]:
            proposals = conn.execute(
                """
                SELECT p.*, u.name, u.avatar_letter, u.skills, u.bio
                FROM proposals p
                JOIN users u ON p.freelancer_id = u.id
                WHERE job_id = ?
                ORDER BY p.created_at DESC
                """,
                (job_id,),
            ).fetchall()
        else:
            user_proposal = conn.execute(
                "SELECT * FROM proposals WHERE job_id = ? AND freelancer_id = ?",
                (job_id, session["user_id"]),
            ).fetchone()
    conn.close()
    return render_template(
        "job_detail.html",
        job=job,
        proposals=proposals,
        user_proposal=user_proposal,
    )


@app.route("/api/jobs/<int:job_id>/analysis")
def job_analysis_api(job_id):
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return jsonify({"error": "Job not found"}), 404
    analysis = get_job_analysis(conn, job, refresh=request.args.get("refresh") == "1")
    conn.close()
    return jsonify(analysis)


@app.route("/api/jobs/<int:job_id>/assistant", methods=["POST"])
@login_required
def job_assistant_api(job_id):
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    if not message:
        return jsonify({"error": "Message is required"}), 400

    conn = get_db()
    job = conn.execute(
        """
        SELECT j.*, u.name AS client_name, u.avatar_letter, u.bio AS client_bio
        FROM jobs j
        JOIN users u ON j.client_id = u.id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    if not job or not user:
        return jsonify({"error": "Not found"}), 404

    reply = call_gemini_chat(user, job, message, history if isinstance(history, list) else [])
    return jsonify({"reply": reply})


@app.route("/jobs/<int:job_id>/proposal", methods=["POST"])
@login_required
def submit_proposal(job_id):
    if session["user_role"] != "freelancer":
        flash("Only freelancers can submit proposals.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO proposals (job_id, freelancer_id, cover_letter, bid_amount) VALUES (?,?,?,?)",
            (
                job_id,
                session["user_id"],
                request.form["cover_letter"],
                float(request.form["bid_amount"]),
            ),
        )
        conn.commit()
        flash("Proposal submitted!", "success")
    except (PsycopgIntegrityError, sqlite3.IntegrityError):
        flash("You already submitted a proposal for this job.", "error")
    conn.close()
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/proposals/<int:proposal_id>/<action>")
@login_required
def update_proposal(proposal_id, action):
    if action not in ("accepted", "rejected"):
        return "Invalid", 400
    conn = get_db()
    proposal = conn.execute(
        """
        SELECT p.*, j.client_id
        FROM proposals p
        JOIN jobs j ON p.job_id = j.id
        WHERE p.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if proposal and proposal["client_id"] == session["user_id"]:
        conn.execute("UPDATE proposals SET status=? WHERE id=?", (action, proposal_id))
        if action == "accepted":
            conn.execute("UPDATE jobs SET status='in_progress' WHERE id=?", (proposal["job_id"],))
        conn.commit()
        flash(f"Proposal {action}.", "success")
    conn.close()
    return redirect(url_for("job_detail", job_id=proposal["job_id"]))


@app.route("/freelancers")
def freelancers():
    search = request.args.get("q", "")
    conn = get_db()
    query = """
        SELECT u.*, COALESCE(AVG(r.rating), 0) AS avg_rating,
               COUNT(DISTINCT r.id) AS review_count,
               COUNT(DISTINCT p.id) AS proposal_count
        FROM users u
        LEFT JOIN reviews r ON r.reviewee_id = u.id
        LEFT JOIN proposals p ON p.freelancer_id = u.id AND p.status = 'accepted'
        WHERE u.role = 'freelancer'
    """
    params = []
    if search:
        query += " AND (u.name LIKE ? OR u.skills LIKE ?)"
        params.extend([f"%{search}%"] * 2)
    query += " GROUP BY u.id ORDER BY avg_rating DESC"
    freelancers = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("freelancers.html", freelancers=freelancers, search=search)


@app.route("/profile/<int:user_id>")
def profile(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return "Not found", 404
    reviews = conn.execute(
        """
        SELECT r.*, u.name AS reviewer_name, u.avatar_letter, j.title AS job_title
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        JOIN jobs j ON r.job_id = j.id
        WHERE r.reviewee_id = ?
        ORDER BY r.created_at DESC
        """,
        (user_id,),
    ).fetchall()
    avg_rating = scalar_one(conn, "SELECT AVG(rating) AS value FROM reviews WHERE reviewee_id=?", (user_id,), default=0) or 0
    reviews_count = scalar_one(conn, "SELECT COUNT(*) AS count FROM reviews WHERE reviewee_id=?", (user_id,), default=0)
    if user["role"] == "freelancer":
        jobs_done = conn.execute(
            """
            SELECT j.title
            FROM proposals p
            JOIN jobs j ON p.job_id = j.id
            WHERE p.freelancer_id = ? AND p.status = 'accepted'
            ORDER BY p.created_at DESC
            LIMIT 6
            """,
            (user_id,),
        ).fetchall()
        completed_count = scalar_one(
            conn,
            "SELECT COUNT(*) AS count FROM proposals WHERE freelancer_id = ? AND status = 'accepted'",
            (user_id,),
            default=0,
        )
        recent_items = conn.execute(
            """
            SELECT j.title, p.status, p.bid_amount, p.created_at
            FROM proposals p
            JOIN jobs j ON p.job_id = j.id
            WHERE p.freelancer_id = ?
            ORDER BY p.created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()
    else:
        jobs_done = conn.execute(
            "SELECT title, status, created_at FROM jobs WHERE client_id=? ORDER BY created_at DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        completed_count = scalar_one(
            conn,
            "SELECT COUNT(*) AS count FROM jobs WHERE client_id = ?",
            (user_id,),
            default=0,
        )
        recent_items = conn.execute(
            """
            SELECT title, status, budget_min, budget_max, created_at
            FROM jobs
            WHERE client_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()
    conn.close()
    return render_template(
        "profile.html",
        user=user,
        reviews=reviews,
        avg_rating=avg_rating,
        reviews_count=reviews_count,
        jobs_done=jobs_done,
        completed_count=completed_count,
        recent_items=recent_items,
    )


@app.route("/messages")
@login_required
def messages():
    conn = get_db()
    uid = session["user_id"]
    contacts = conn.execute(
        """
        SELECT DISTINCT
            CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END AS other_id,
            u.name, u.avatar_letter, u.role,
            MAX(m.created_at) AS last_msg_time,
            SUM(CASE WHEN m.receiver_id = ? AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread
        FROM messages m
        JOIN users u ON u.id = CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END
        WHERE sender_id = ? OR receiver_id = ?
        GROUP BY other_id
        ORDER BY last_msg_time DESC
        """,
        (uid, uid, uid, uid, uid),
    ).fetchall()
    conn.close()
    return render_template("messages.html", contacts=contacts)


@app.route("/messages/<int:other_id>", methods=["GET", "POST"])
@login_required
def chat(other_id):
    conn = get_db()
    uid = session["user_id"]
    other = conn.execute("SELECT * FROM users WHERE id=?", (other_id,)).fetchone()
    if not other:
        conn.close()
        return "User not found", 404
    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            conn.execute(
                "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
                (uid, other_id, content),
            )
            conn.execute("UPDATE messages SET is_read=1 WHERE sender_id=? AND receiver_id=?", (other_id, uid))
            conn.commit()
    else:
        conn.execute("UPDATE messages SET is_read=1 WHERE sender_id=? AND receiver_id=?", (other_id, uid))
        conn.commit()
    msgs = conn.execute(
        """
        SELECT m.*, u.name AS sender_name, u.avatar_letter
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
        ORDER BY m.created_at ASC
        """,
        (uid, other_id, other_id, uid),
    ).fetchall()
    conn.close()
    return render_template("chat.html", other=other, msgs=msgs)


@app.route("/api/messages/<int:other_id>/new")
@login_required
def new_messages(other_id):
    after = request.args.get("after", "")
    conn = get_db()
    uid = session["user_id"]
    msgs = conn.execute(
        """
        SELECT m.*, u.name AS sender_name, u.avatar_letter
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
          AND m.created_at > ?
        ORDER BY m.created_at ASC
        """,
        (uid, other_id, other_id, uid, after or "1970-01-01"),
    ).fetchall()
    conn.execute("UPDATE messages SET is_read=1 WHERE sender_id=? AND receiver_id=?", (other_id, uid))
    conn.commit()
    conn.close()
    return jsonify([dict(msg) for msg in msgs])


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    conn = get_db()
    uid = session["user_id"]
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form.get("phone", "").strip()
        bio = request.form.get("bio", "").strip()
        skills = request.form.get("skills", "").strip()
        security_question = request.form.get("security_question", "").strip()
        security_answer = request.form.get("security_answer", "").strip()
        password = request.form.get("password", "").strip()

        if conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, uid)).fetchone():
            flash("Email already in use.", "error")
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return render_template("settings.html", user=user)
        if security_question and security_question not in SECURITY_QUESTIONS:
            flash("Choose a valid security question.", "error")
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return render_template("settings.html", user=user)
        if security_question and not security_answer:
            flash("Add an answer for the security question.", "error")
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return render_template("settings.html", user=user)
        if password and not password_is_strong(password):
            flash("Use at least 8 characters with letters and numbers.", "error")
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return render_template("settings.html", user=user)

        fields = ["name=?", "email=?", "phone=?", "bio=?", "skills=?", "security_question=?", "avatar_letter=?"]
        params = [name, email, phone, bio, skills, security_question, name[0].upper()]
        if security_answer:
            fields.append("security_answer_hash=?")
            params.append(generate_password_hash(security_answer))
        if password:
            fields.append("password=?")
            params.append(generate_password_hash(password))

        params.append(uid)
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
        conn.execute(
            "UPDATE users SET avatar_letter=? WHERE id=?",
            (name[0].upper(), uid),
        )
        conn.commit()
        session["user_name"] = name
        session["avatar_letter"] = name[0].upper()
        flash("Settings saved.", "success")
        conn.close()
        return redirect(url_for("settings"))
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return render_template("settings.html", user=user)


init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
