# Using Ademly

Ademly finds software jobs that match a description and prepares a tailored, fact-checked resume for each one. It runs locally on your machine.

**Current status:** early development. Resume intake, the job log, and the job board connectors work today. Job search, resume tailoring, and PDF output are not built yet, so there is no `ademly run` command. See [PLAN.md](PLAN.md) for the roadmap.

## What you need

- Python 3.11 or newer
- A `master_resume.md` file containing your full work history, in the format described below
- An [Anthropic API key](https://console.anthropic.com/), which the job search and tailoring stages will need. Calls are billed to your account. Intake doesn't use it.

## Setup

```bash
git clone <this-repo-url>
cd ademly
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Install with `-e` as shown, because Ademly finds your files relative to the repo folder.

To add your API key, copy the example file, then replace the placeholder in `.env` with your key:

```bash
cp .env.example .env
```

## Getting started

### 1. Add your master resume

Create `master_resume.md` in the repo folder. List everything you might want on a resume, because Ademly will only ever use claims found in this file.

### 2. Run the init command

```bash
ademly init
```

This parses your resume and writes `data/master_resume.review.md`, which lists every claim Ademly extracted. If a line can't be understood, the command stops and shows that line, so nothing is silently lost. Fix the line and run it again.

### 3. Review, then approve

Read the review file carefully. Every accuracy check treats this content as the truth, so errors here carry into every resume. When it's correct, run:

```bash
ademly init --approve
```

Approval applies to that exact content. If you later edit your resume, run `ademly init` and `ademly init --approve` again.

## Master resume format

The parser is deliberately strict. This example shows every supported element:

```markdown
# Sam Example

sam@example.com | (555)-123-4567 | [linkedin.com/in/sam](https://linkedin.com/in/sam) | Toronto, ON

---

## Technical Skills

- **Programming Languages:** Java, Python
- **Tools:** Docker, Git
- **Certifications:** Cloud Basics

---

## Education

### Example University — Vancouver, BC
*May 2024*

Bachelor of Science, Computer Science

---

## Work Experience

### Acme — Toronto, ON
**Software Developer** | *Aug 2024 – Present*
*Java, Docker*

#### Payments Platform
*Oct 2024 – Present*

- Improved throughput by up to 40% using Java
- Mentored new hires by:
  - Reviewing their code
  - Leading weekly 1-1s

---

## Internships

### Beta Labs — Remote
**Software Intern** | *May 2023 – Aug 2023*
*Python*

- Built a reporting API in Python

---

## Projects

### Widget Tracker — Inventory App
*Jan 2022 – Apr 2022*

- Built an inventory tracker using Python

---

## Awards and Recognitions

- **Example Hackathon** 1st place (40 teams) — *Apr 2022*

---

## Related Experience

### Robotics Club, Example University
*Sep 2020 – May 2022*

#### Team Lead
- Led a team of 9 people
```

Rules to follow:

- Section names must match the example exactly. You can leave out sections you don't need.
- Use an em dash with spaces (` — `) between an organization and its location, and before an award's date.
- Write dates as `Mon YYYY`, and ranges with an en dash and spaces, such as `Aug 2024 – Present`.
- The italic line of technologies below a job title is optional, as are `####` sub-headings.
- A nested bullet needs a parent ending in `:`. Each nested bullet becomes its own complete claim.

## Optional: resume policy

Create `data/resume_policy.yaml` to control what Ademly may claim without editing your resume. Every field is optional.

```yaml
# Exact text changes. Each must match, or init stops.
replacements:
  - find: "Secret Client Inc"
    replace: "Major Retail Company"

# Fails init if any of these remain, such as a confidential client's name.
forbidden_terms:
  - Secret Client

# Whole-word spelling fixes.
spelling:
  SpringBoot: Spring Boot

# Any rewrite of a matching claim must keep this wording.
required_terms:
  - contains: "by up to 40%"
    terms: ["up to 40%"]

# Skills missing from your skills section. Each must appear in your experience.
additional_skills:
  - React
```

Run `ademly init` again after changing this file, then approve the result.

## Other commands

| Command | What it does |
|---|---|
| `ademly status` | Shows how many jobs are in each state |
| `ademly mark <job-id> applied` | Records that you applied to a job. Use `skipped` to pass on it, and `--notes "..."` to add a note |
| `ademly check-ats` | Confirms the Greenhouse, Lever, and Ashby job board APIs are reachable |

## Keeping your data private

`master_resume.md`, `.env`, and the `data/` folder are listed in `.gitignore` because they contain personal details or secrets. Keep them out of commits, especially if you fork this repo publicly.

## Running the tests

```bash
pytest
```

The tests run without an API key.
