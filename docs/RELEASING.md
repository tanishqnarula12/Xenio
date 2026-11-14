# Releasing XENIO

## Versions

XENIO follows [Semantic Versioning](https://semver.org/). The version lives in `app/__init__.py` (`__version__`).

- **MAJOR** when something you rely on stops working the way it did: a setting removed or renamed, data that needs
  migrating, a permission that changes meaning.
- **MINOR** for new features that change nothing existing.
- **PATCH** for fixes.

Until 1.0, a minor release may also change behaviour; the changelog says when.

## Making a release

1. `main` is green in CI.
2. Run the full checks locally on a Mac ([DEVELOPMENT.md](DEVELOPMENT.md)), and one real War Room job on real models
   in a scratch folder: it must end *Done and verified*.
3. In [CHANGELOG.md](../CHANGELOG.md), rename *Unreleased* to the new version and date, and add a fresh *Unreleased*.
4. Set `__version__` in `app/__init__.py`.
5. Commit (`Release 0.2.0`), tag (`git tag -a v0.2.0 -m "XENIO 0.2.0"`), push the commit and the tag.
6. On GitHub, create the release from the tag and paste that version's changelog section.

## Checking an installation

On a clean Mac, follow [INSTALLATION.md](INSTALLATION.md) exactly, then:

```bash
.venv/bin/python -m app.main --health    # every part named, nothing missing that you need
.venv/bin/python -m pytest -q             # the suite passes
```

## Upgrading

```bash
git pull
.venv/bin/pip install -r requirements.lock
(cd ui && npm ci && npm run build)
.venv/bin/python -m app.main --install-guard    # updates the Hermes plugin
```

Then restart XENIO (Quit, and start it again). XENIO's data in `~/.local/share/xenio` is kept.

## Rolling back

```bash
git checkout v0.1.0      # the version you want
.venv/bin/pip install -r requirements.lock
(cd ui && npm ci && npm run build)
.venv/bin/python -m app.main --install-guard
```

Before upgrading, a copy of `~/.local/share/xenio` lets you roll the data back too: newer versions may add tables or
fields that an older one ignores.
