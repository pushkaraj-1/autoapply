"""Loads the applicant's facts, resume text and extra material."""

from functools import cache
from pathlib import Path

import yaml
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / "profile"


def profile_file(name: str) -> Path:
    """A file in profile/. Each one starts as a copy of its .example template."""
    path = PROFILE_DIR / name
    if not path.exists():
        example = path.with_name(f"{path.stem}.example{path.suffix}")
        raise FileNotFoundError(f"profile/{name} is missing. Copy profile/{example.name} to profile/{name} and fill it in with your own details (see README.md).")
    return path


@cache
def load_profile() -> dict:
    return yaml.safe_load(profile_file("profile.yaml").read_text())


def resume_path() -> Path:
    path = Path(load_profile()["resume_path"]).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Resume not found at {path}")
    return path


@cache
def resume_text() -> str:
    return "\n".join(page.extract_text() for page in PdfReader(resume_path()).pages)


@cache
def content_text() -> str:
    return profile_file("content.md").read_text()


def cover_letter_sample() -> str:
    return profile_file("cover_letter_sample.txt").read_text()


def experience_answer_sample() -> str:
    return profile_file("experience_answer_sample.txt").read_text().strip()
