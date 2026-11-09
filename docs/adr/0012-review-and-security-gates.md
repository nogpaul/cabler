# ADR-0012: Review and security gates

Status: accepted · Date: 2026-11-09

## Context
The project parses files from many public publishers, builds SQL, writes to a database and will
later run an agent that proposes actions. A wrong result here is a wrong cable route, and a
security flaw is a flaw in everything downstream. Until now the only gate was one continuous
integration workflow (lint, types, tests). No single tool finds every kind of defect, and the
repository is public.

## Decision
- **Pull request for every change**, enforced by a ruleset on `main`.
- **Layers, each aimed at a different kind of defect:**
  - ruff (with the `S` security rules), mypy strict and tests: correctness and obvious unsafe code;
  - CodeQL for Python and for the workflow files: data-flow security flaws;
  - `pip-audit` on the pinned lock file: known flaws in libraries;
  - gitleaks over the full history, plus GitHub secret scanning with push protection: leaked secrets;
  - a human-directed AI review of every diff and of every release (`/code-review`,
    `/security-review` for sensitive areas): logic and design problems that rule-based tools miss.
- **After merge:** release-to-release review before production, weekly scheduled scans, weekly
  Dependabot pull requests.
- **Supply chain:** workflow actions are pinned to full commit SHAs; workflows default to read-only
  permissions and do not persist credentials.
- **Not adopted yet:** container image scanning (Trivy) and a hosted AI review action. The first
  needs a built image to scan and is added with the first release image; the second needs an API
  key stored as a repository secret and is added when a pull-request volume justifies it. The
  local review gives the same review without a key.

## Consequences
- A change takes longer to merge, by minutes of automated checks plus the review.
- Several tools overlap on purpose; a finding that two tools report is evidence, not noise.
- The dependency audit covers only what is in `requirements/dev.lock`; Dependabot alerts cover
  what GitHub can read from `pyproject.toml`.
- Scanners do not find logic flaws such as a review step that can be skipped; those are the
  reason the diff review exists.
