# ADR-0028: The data dictionary lives in the database

Status: accepted · Date: 2026-11-21

## Context
The database holds 29 tables, 13 views and 15 functions in five schemas, with 569 columns. Before
this decision 33 of the columns had a description; what a column holds, in which unit and with which
values had to be read from the migration that created it, or from the code that fills it. The data
phase ends with the question whether every layer is understood well enough to build on, and the
routing phases need units (metres, kilowatts, square metres) to be certain. Three places for the
descriptions were considered:

- **A hand-written document.** Rejected. It drifts from the schema with the first migration that
  forgets it, and nothing notices.
- **Descriptions in the Python code.** Rejected. The tables are read by SQL, by QGIS and by ArcGIS
  too; the description belongs where every client can see it.
- **`COMMENT ON` in the database, and a document generated from it.** Chosen. The description sits
  next to what it describes, every client shows it, and the document cannot drift.

## Decision
- **Every schema, table, view, column and function has a comment.** Migration 0023 documents every
  object that existed; every later migration documents what it adds.
- **A test enforces it.** `cabler.dictionary.missing` names everything without a description, and
  an integration test fails when the list is not empty. A second test fails when
  `docs/data-dictionary.md` differs from what the database gives, so a forgotten `make dictionary`
  is caught in CI.
- **`make dictionary` writes the document** from the system catalogue, read as the read-only role.
  It refuses to write while anything is undocumented; `--check` only compares.
- **A view column passes a description on.** A column of a view that only passes a column of a
  table on takes its description, followed through views of views. When the view reads two columns
  of that name whose descriptions differ (a join on a shared key), nothing is guessed: the view
  column needs a comment of its own. Computed columns (coordinates in degrees, suggestions) always
  need their own.
- **What a description says.** What the value is, its unit, the values it can take, and when it is
  NULL. Codes of a publisher are named with the publisher's field name (AGS, GEN, Flur), so that a
  value can be traced back to the delivery.

## Consequences
- The dictionary is checked against the real data once: every listed value set (statuses, reasons,
  codes) was compared with the values in the sandbox, and four descriptions were corrected before
  the release (a district key of 5 digits, stages without a snapshot, real rule names, the reasons
  of the wind core).
- A description can still be wrong, as any comment can; the test proves that it exists, not that it
  is true. A description that names values (`passed or quarantined`) is kept close to the CHECK
  constraint that enforces them, in the same migration.
- The document is long (about 900 lines). It is reference material, generated, and not reviewed line
  by line in a pull request; the migration that changes a comment is.
