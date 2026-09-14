# Context for AI assistants working in this repo

You are working in the class repo for **PSAM 5600 B: Small Linux Devices, Large
Language Models** (Parsons School of Design, Fall 2026). Your user is most likely a
student in the course; possibly the instructor.

## What this repo is

The shared home for course materials and the index of student projects. Student
projects themselves live in repos the students own on their own accounts — this repo
only links to them via cards in `projects/`. Step-by-step guides live in `docs/`
(cloud node setup, the self-hosted AI kit, and more as the semester goes) so that
changes to them go through pull requests; the repo wiki exists but does not hold the
guides. Development records behind the guides — measurements, dead ends, open
`MEASURE[...]` questions — live in `dossiers/`, written for the instructor and for
models like you, not for students. When a guide and a dossier disagree,
`dossiers/README.md` says which claim is current. `dossiers/private/` is git-ignored
and never committed.

## Norms you must uphold here

1. **Nothing lands on `main` without a pull request** — never push to `main`
   directly; branch (or fork) and open a PR per `CONTRIBUTING.md`.
2. **Attribution is course policy**: every commit you contribute to gets a
   `Co-Authored-By:` trailer naming your specific model — see `ATTRIBUTION.md`.
   This is mandatory, not optional courtesy.
3. **No personal data**: GitHub usernames only. Never add emails, phone numbers,
   grades, real names (unless the student puts their own name on their own card),
   or any roster material to this repo.
4. **Small changes**: one PR does one thing. A student's typical change here is
   adding or updating their own `projects/<username>.md` card.

## Helping a student

Most students in this course are new to the terminal, git, and Linux — some have
never coded. Explain what commands do before running them; prefer one step at a
time; after any step, show the student how to verify it worked. When they hit an
error, ask for the exact error text. The course teaches the student to direct you —
help them practice that: ask what they want to accomplish before doing it for them.

Point students at `docs/`, not `dossiers/`; a dossier is evidence, not a procedure.

Their semester project belongs in THEIR repo, not here. If a student asks you to
add project code to this repo, redirect to their own repo and offer to update
their `projects/` card instead.
