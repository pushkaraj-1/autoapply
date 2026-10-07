"""apply <job url>: preview the answers and cover letter for a job without filling anything."""

import argparse

from jobautomate.prepare import prepare


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview the answers for a job. The Chrome extension does the filling.")
    parser.add_argument("url")
    args = parser.parse_args()

    job = prepare(args.url)
    print(f"{job.title} at {job.company} ({job.location})\n")
    for warning in job.warnings:
        print(f"WARNING: {warning}\n")
    for f in job.fields:
        mark = "*" if f.required else " "
        value = f.answer.value if f.answer.value not in (None, []) else "(left empty)"
        note = f"  [{f.answer.note}]" if f.answer.note else ""
        print(f"{mark} {' '.join(f.label.split())[:70]:<70} -> {value}  ({f.answer.source}){note}")
    if job.letter_text:
        print(f"\nCover letter ({job.run_dir / 'cover_letter.txt'}):\n\n{job.letter_text}")


if __name__ == "__main__":
    main()
