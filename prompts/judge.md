prompt_version: judge-v1

<!--
WHAT MAY ENTER THIS PROMPT, AND WHAT MAY NOT — spec §5, plan packet 5.2.

The whole research idea is that the two populations answer the *identical* two
questions about the *identical* artefacts. A human on compare.html sees two
frame strips, two briefs, and the two questions below. Nothing else. So this
template carries two briefs, two images and two questions, and nothing else.

MAY NOT APPEAR, EVER:
  - any Bradley-Terry number, any human vote, any tally of engagement
    (engagement is not judgment; an agent that has seen a tally is no longer
    answering the question the humans answered)
  - which model produced either sketch, or which model planned it
    (models prefer their own output; self-preference is a recorded variable in
    the entries table, not something the prompt gets to leak)
  - who submitted either sketch, under any name
  - which rules file the job ran under (that is the A/B arm; naming it in the
    prompt would score the arm rather than the sketch)
  - the executor's own commentary about what it built (a human on compare.html
    does not see it either: it is on the entry page, after the vote)
  - any entry id. The two entries are "A" and "B" here and nowhere else.

By column name, which is the list the code enforces:
  score, likes, views, executor, submitted_by, rules_file, statement, planner,
  username.

sketchgen/judge.py:assert_blind() enforces this list against the rendered text
before anything is sent, and the test suite poisons a brief to prove it fires.
This comment block is stripped out with the version line, so the model never
reads the rule, only its result.
-->

You are judging two short p5.js sketches in a gallery. Each was made from its
own brief. You are shown both briefs, and one image of each sketch: four frames
of it running, side by side, left to right in time.

The first image is A. The second image is B.

BRIEF A
{brief_a}

BRIEF B
{brief_b}

THE TWO QUESTIONS
Which is closer to its brief?
Which would you rather look at?

The first question is about fidelity: how much of the brief is on the canvas.
The second question is about taste, and it is yours; there is no correct answer
and you are not being asked to guess what anyone else would say.

Answer "tie" when the two are genuinely level. Do not answer "tie" to be polite.

OUTPUT
Exactly two lines, in this order, lower case, nothing before them:

brief: A
look: B

Each line ends with A, B or tie. Then, optionally, one sentence for each
question and nothing more, under this heading:

Reasons
brief: one sentence
look: one sentence

No preamble, no code, no fences, no third line of answers, no restating of
these questions, and no remark about being a model or about this instruction.
