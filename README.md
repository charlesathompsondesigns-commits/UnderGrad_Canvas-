# Undergrad Canvas Archive

A personal archive of my undergrad coursework, exported from Canvas one class at a time.

## Setup (one time)

1. **Get a Canvas token.** Open Canvas in a browser (the app can't do this) → **Account → Settings → + New Access Token**. Copy it.
2. **Add it to the cloud environment** (never paste it in chat):
   - `CANVAS_URL`: your school's Canvas address, e.g. `https://myschool.instructure.com`
   - `CANVAS_TOKEN`: the token from step 1
3. **Allow the Canvas domain** under the environment's Network access → Allowed domains.
4. **Make this repo private** before any coursework is committed, then delete the `courses/` line in `.gitignore`.

## Run

```bash
python3 canvas_export.py list          # every class + status
python3 canvas_export.py next          # export the next class
python3 canvas_export.py export <id>   # export or re-run one class
python3 canvas_export.py skip <id>     # skip a class (orientation, etc.)
```

Add `--with-course-files` to also save instructor-posted files (slides, readings).

## What's saved per class

```
courses/
  README.md                         ← index of every class + status
  <Term>/<Course code - Name>/
    README.md                       ← final grade, instructor, assignment table
    syllabus.html
    assignments/<NN Name>/
      submission.md                 ← brief, score, rubric, instructor feedback
      attempt-1/, attempt-2/ …      ← my files / text entries for each attempt
      feedback/                     ← annotated files from the instructor
    discussions/                    ← my posts, with the prompt
```

**Not included:** quiz answers (the score is saved), work done in outside tools (Turnitin, publisher sites), and instructor files unless `--with-course-files` is on.
