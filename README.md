# autoapply

A Chrome extension and a small local server that fill job applications for you from your own profile and resume. It answers the usual questions with fixed rules, writes the open-ended answers and cover letters with an AI model, keeps track of where you applied, and can scan job boards for new openings.

Everything runs on your own computer. Your profile, resume, answers and application history stay in this folder and are never uploaded anywhere except to the AI model that writes the answers (through OpenRouter).

It fills forms on Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling, iCIMS, Oracle Cloud, SuccessFactors and many other company career sites. It never presses Submit for you, except in the optional auto apply queue described below.

## What you need

- macOS, Linux or Windows with Google Chrome (the cover letter PDFs and the auto apply queue use it too)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs Python 3.12 for you)
- An [OpenRouter](https://openrouter.ai/keys) API key. Answers and cover letters are written through it and billed to your key, usually a few cents per application.

## Setup

1. Get the code and install it:

   ```
   git clone https://github.com/pushkaraj-1/autoapply.git
   cd autoapply
   uv sync
   ```

2. Create your own copies of the settings files:

   ```
   cp .env.example .env
   cp profile/profile.example.yaml profile/profile.yaml
   cp profile/content.example.md profile/content.md
   cp profile/cover_letter_sample.example.txt profile/cover_letter_sample.txt
   cp profile/experience_answer_sample.example.txt profile/experience_answer_sample.txt
   ```

3. Fill them in:

   - `.env`: put your OpenRouter key after `OPENROUTER_KEY=`. Add `WORKDAY_EMAIL` and `WORKDAY_PASSWORD` if you want it to sign in to Workday sites for you.
   - `profile/profile.yaml`: your name, contact details, links, work authorization, education, work history, skills and standing answers (salary, relocation, demographics and so on). Set `resume_path` to your resume PDF. Read every line: the tool answers forms exactly as this file says, so anything left from the example would be wrong for you. Use `null` for anything you'd rather be asked about.
   - `profile/content.md`: anything true about you that your resume leaves out (projects, research, awards). The AI only uses facts from your resume and this file.
   - `profile/cover_letter_sample.txt`: a cover letter you like. New letters copy its format and tone.
   - `profile/experience_answer_sample.txt`: an answer you like to a question such as "Describe your experience with X, Y and Z". Similar questions are answered in its shape.

4. Start the server and leave it running while you apply:

   ```
   uv run jobautomate-server
   ```

   It listens only on your own computer, at http://127.0.0.1:8765.

5. Load the extension in Chrome:

   - Open `chrome://extensions` and turn on Developer mode (top right).
   - Click "Load unpacked" and choose the `extension` folder.
   - Pin "Job Autofill" to the toolbar.

## Using it

- Open a job application and click the extension, then "Fill this application". A panel in the corner shows what it filled, what needs your answer, and every answer the AI wrote so you can read it before submitting.
- Answers are saved per job in `data/runs/<job>/answers.yaml`. Edit any answer there and click Fill again to use your version.
- The popup's "Fill forms on open" switch makes it fill application pages as soon as they open.
- The Dashboard shows where you have applied. The Find jobs page scans job boards and scores each job against your profile. Its settings, including search terms and sources, are in `profile/scanner.yaml`, which is created from `profile/scanner.example.yaml` the first time you scan.

## Auto apply queue

The queue can fill and submit simple Greenhouse, Lever and Ashby applications on its own: only fully matched forms with no warnings and no captcha, up to a daily cap. It starts in test mode (`auto_apply.submit: false` in `profile/scanner.yaml`), where it fills but never submits. Watch a few test runs before you turn submitting on.

## Updating

```
git pull
uv sync
```

Then click the reload icon on Job Autofill in `chrome://extensions`, and restart the server.

## Good to know

- Never commit or share `.env`, `profile/` (except the `.example` files) or `data/`. They are ignored by git for that reason.
- The tool never solves captchas or gets around bot checks; it hands those to you.
- On email-verification and sign-in screens (iCIMS, Oracle, SuccessFactors) it fills your email and leaves the Next or Sign In button to you.
