# Using Ademly

Ademly finds software jobs that match a description and prepares a tailored, fact-checked resume for each one. It runs locally on your machine.

**Current status:** early development. Resume intake and job search work today. Resume tailoring and PDF output are not built yet. See [PLAN.md](PLAN.md) for the roadmap.

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

Approval applies to that exact content. If you later edit your resume, run `ademly init` and `ademly init --approve` again. The job search refuses to run until the current version is approved.

### 4. Set up your search

```bash
cp search.example.yaml data/search.yaml
```

Edit `data/search.yaml` to describe the roles, cities, and work authorization you want. Its comments explain each setting. The main ones:

- `description` and `preferences`: the kind of work, companies, and mission you want, in your own words. The ranker weighs preferences in the order listed.
- `min_salary` and `target_salary`: jobs posting a top of range below the minimum are rejected. Jobs without posted pay aren't penalized.
- `max_years_required`: jobs requiring more years than this are rejected, so senior titles can stay in without flooding the results.
- `excluded_companies` and `excluded_industries`: listed companies are never checked. When the ranker finds a company's main business is in an excluded industry, that company is excluded too.

You can change settings at any time. Jobs selected under the old settings are ranked again, and jobs the free filters rejected are rechecked.

### 5. Find jobs

```bash
ademly run --stage discover
```

Each run:

1. Runs the web searches in your settings to find companies with job boards on Greenhouse, Lever, or Ashby. A search isn't repeated within 14 days.
2. Checks every known company's job board, starting with those in `companies.yaml`.
3. Drops postings in code, at no cost: by title, by location (including remote roles that name another country, such as "Remote - Mexico"), by age, and by any "N+ years of experience" figure above `max_years_required`.
4. Uses Claude to score up to 40 remaining postings against your resume. The rest wait for the next run. Change the limit with `--max-rank`.
5. Selects up to 20 jobs scoring at least `min_score`, with at most 5 from one company and each role only once.

A run checking about 100 companies and ranking 40 postings cost about $0.90 in testing, plus Anthropic's per-search fee for web searches. Each run prints its estimated cost. Add `--no-web-search` to check only known companies.

### 6. Check the ranking against your judgment

```bash
ademly label export
```

This writes 30 ranked jobs to `data/ranking_labels.csv`, without their scores. Mark each one `good` or `bad` in the `label` column, then run `ademly label score`. Aim for at least 80% agreement. If it's lower, the disagreements it lists show what to adjust in your search description.

## Company history

Ademly remembers every company it has checked, so it doesn't waste runs on companies that don't fit. A company is paused automatically when:

| Situation | Paused for |
|---|---|
| None of its open roles pass your filters | 14 days |
| 4 or more of its eligible roles were ranked and none reached `min_score` | 60 days |
| You skipped 3 or more of its jobs and applied to none | 60 days |

Only eligible roles count toward the second rule. A role ruled out on location, work authorization, years, pay, or industry says nothing about whether the company fits you.

After a pause ends, the company is checked again, and the rules are re-applied to its full history. A pause also ends early when the settings it was based on change: the first rule depends on your filter settings, and the second on your ranking settings and resume. Pauses from the third rule last regardless, because they come from your own choices.

- `ademly companies` lists each company with its history: jobs seen, passed filters, ranked, best score, selected, applied, and skipped. Add `--reasons` to see why companies are paused, or `--all` to include those with no jobs yet.
- `ademly companies exclude <name> --reason "..."` stops checking a company permanently and removes its selected jobs. `ademly companies include <name>` undoes this, or ends a pause early.
- `ademly searches` lists past web searches and how many new companies each found.

To exclude a company for every user of a shared setup, add it to `companies.yaml` with `exclude: true`, as the file does for job aggregators.

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
| `ademly jobs` | Lists selected and later jobs. Add `--state selected` to filter, or `--details` to see the ranker's summary, posted pay, years asked for, mission match, how product-oriented the role is, and gaps |
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
