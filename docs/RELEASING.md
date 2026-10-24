# Releasing

How a version of padspeaker gets from `main` to PyPI. Short on purpose: the work is in the checks, and the checks are
in [`.github/workflows/release.yml`](../.github/workflows/release.yml).

## What a release is

A GitHub release for a `vX.Y.Z` tag. Publishing the release runs the `release` workflow, which builds the package from
that tag, checks it, and uploads it to PyPI. Nothing is uploaded from a laptop and no token is stored anywhere:
PyPI trusts the workflow (trusted publishing, OpenID Connect) and hands it a token that lives for the length of the job.

## Cutting a release

1. Pick the version. It is `__version__` in `padspeaker/__init__.py`; the tag is `v` plus the same string. The
   workflow refuses a tag that does not match.
2. Move the entries you are releasing in [`CHANGELOG.md`](../CHANGELOG.md) under a new heading with today's date.
3. Make sure `main` is green (tests and build) and `python -m pytest` passes locally.
4. Commit, then tag and push: `git tag -a vX.Y.Z -m "padspeaker X.Y.Z" && git push origin main vX.Y.Z`.
5. Create the GitHub release for that tag, with notes. The previous release's notes are a template: what it is, what
   changed, what is verified and what is not, known limitations, links.
6. Publishing the release starts the `release` workflow. Watch it under Actions. When it is green the version is on
   https://pypi.org/project/padspeaker/.

⚠️ **A PyPI version number can never be reused**, even after a delete or a yank. If the upload went wrong, the fix is a
new version, not a re-upload. That is why the workflow checks the tag against the package version and installs the built
wheel in a clean environment before it publishes anything.

## Publishing a tag that was released before the workflow existed

`v0.1.0` was released first. Actions, `release`, **Run workflow**, enter `v0.1.0`. The workflow builds that tag, not
`main`.

## One-time setup (already needed before the first publish)

PyPI cannot trust a workflow for a project that does not exist yet, so the first publish needs a *pending publisher*:

1. On https://pypi.org, sign in, then **Your account, Publishing, Add a new pending publisher**.
2. Fill in: PyPI project name `padspeaker`, owner `eknowledger`, repository `padspeaker`, workflow name `release.yml`,
   environment name `pypi`.
3. In the GitHub repository there must be an environment called `pypi` (Settings, Environments). It exists already.
   Adding yourself as a required reviewer there makes every publish wait for one click, which is worth having if more
   than one person can create releases.

The pending publisher turns into a normal trusted publisher when the first upload succeeds.

## If a release is wrong

- **Before it reached PyPI:** delete the GitHub release and the tag, fix, tag again. Nothing is lost.
- **After:** yank the version on PyPI (it stays installable by exact version but is no longer picked by default), then
  release the next version with the fix. Say what was wrong in that release's notes.
