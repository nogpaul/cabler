# Code review and security gates

How a change reaches `main`, and how a release reaches production. The decision and its
reasons are in ADR-0012.

## Before a change is merged

Every change, including a one-line fix, is a pull request from a branch. Nothing is pushed to
`main` directly.

| Gate | What it checks | Where it runs |
|---|---|---|
| `ci / quality` | lint (ruff, including its security rules), formatting, types (mypy strict), unit tests with a coverage floor | GitHub Actions |
| `ci / integration` | migrations and the pipeline against a real PostGIS database | GitHub Actions |
| `codeql / analyze (python)` | data-flow security analysis of the code (injection, unsafe parsing, path handling) | GitHub Actions |
| `codeql / analyze (actions)` | the workflow files themselves (untrusted input, excess permissions) | GitHub Actions |
| `security / dependencies` | every pinned library against the public vulnerability databases (`pip-audit`) | GitHub Actions |
| `security / secrets` | the whole git history for passwords, tokens and keys (`gitleaks`) | GitHub Actions |
| Secret scanning with push protection | a secret in a push is refused before it reaches the repository | GitHub |
| Review of the diff | logic, edge cases, data-safety, naming, tests that would fail if the change were wrong | before the pull request is opened |

The review of the diff is done with `/code-review` on the branch. Findings are fixed, or answered
in the pull request with the reason they are not defects. A change that touches parsing of
downloaded data, SQL, file paths, credentials or workflow files also gets `/security-review`.

A pull request is merged only when every gate above is green.

## After a change is merged

- **Tagged release.** Before a tag is promoted to production, the diff from the previous tag is
  reviewed once more as a whole (`/code-review` on `previous-tag..new-tag`), because changes that
  were each fine can interact.
- **Scheduled scans.** CodeQL, the dependency audit and the secret scan run weekly on `main`
  without any change, so a newly published flaw in an unchanged library, or a new query, is
  found within a week.
- **Alerts.** Dependabot opens weekly update pull requests (Python, workflow actions, the database
  image). They go through the same gates. A vulnerability alert on a library in use is handled
  before any new feature work.
- **Reports from others** arrive privately (see `SECURITY.md`).

## Repository settings that enforce this

- A ruleset on `main` requires a pull request, requires the six checks above to pass, blocks
  force-push and deletion, and requires branches to be up to date before merging.
- Workflow actions are pinned to a full commit SHA with the version in a comment; Dependabot
  moves them forward.
- Workflows request read-only permissions unless a job needs more.
