"""PDF report generation for reviews.

Public surface is the one function `render_review_pdf` — pure input/output,
takes plain dicts and returns bytes. That keeps testing simple (feed
handcrafted dicts, assert the returned buffer is a valid PDF) and keeps
the FastAPI endpoint thin (fetch data → render → StreamingResponse).
"""

from app.reports.pdf import render_review_pdf

__all__ = ["render_review_pdf"]
