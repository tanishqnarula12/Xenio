# Contributing to XENIO

Thank you for wanting to help. XENIO acts on people's computers, so correctness and safety come before features.

## Before you start

- Set up a development environment: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
- For anything bigger than a small fix, open an issue first and describe the problem you want to solve.
- Security problems: do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Making a change

1. Branch from `main`.
2. Make the change, with tests. A bug fix comes with a test that fails without it.
3. Run the checks: `ruff check .`, `python -m pytest -q`, and in `ui/`: `npm run typecheck && npm test`.
4. Update the docs the change affects. If you add a setting, add it to `.env.example`.
5. Open a pull request that says what changed, why, and how you verified it. Say plainly what you could not verify.

## The rules

- **Safety first.** Every tool call that touches the computer or leaves it goes through `app/safety/policy.py`. Do not
  add a way around it. Code XENIO did not write runs in the sandbox, and if the sandbox is unavailable it is refused.
- **Never weaken a test or a security check** to make something pass.
- **No secrets in the code, the tests, or the history.** Use obviously fake values in tests. CI scans every commit.
- **Do not claim what is not verified.** Docs and pull requests separate what was tested from what was not.
- **Keep it local.** XENIO listens on this Mac only. A change that opens a port to the network needs a strong reason.

## Required checks

Pull requests into `main` must pass CI: Python lint and tests, the interface's type check, tests and build, and the
security job. Once branch protection is switched on for `main` (a repository setting, tracked in [the roadmap](docs/ROADMAP.md)), a pull request with failing checks cannot be merged.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
