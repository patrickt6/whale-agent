# Releasing

A release goes out in this order:

1. PyPI (`whale-agent`). Everything else installs from it.
2. npm (`whale-agent-cli`). Its launcher runs the PyPI package, so it breaks if PyPI is not there yet.
3. The site `/install` script: run `deploy-site/sync-install.sh`, then deploy the site.

## PyPI

Releases go out through `.github/workflows/release.yml`. The workflow builds the sdist
and wheel when you push a tag that starts with `v`, then uploads them with PyPI Trusted
Publishing. There is no API token to create, store or rotate.

References (opened 2026-09-17):
https://docs.pypi.org/trusted-publishers/adding-a-publisher/ and
https://docs.pypi.org/trusted-publishers/using-a-publisher/

### Before the first release

These must be true first. `docs/LAUNCH_AUDIT.md` has the evidence for each.

- `whale digest --demo` works from an installed wheel. It now reads
  `whale_agent/demo_data/form4_purchase.xml`, which ships inside the wheel.
- A `LICENSE` file is tracked on the branch you release from.
- The code that ships from the public repo `patrickt6/whale-agent` has no personal
  defaults (`src/whale_agent/config.py:90` and `:112` on `wa9a-cli`).

### One-time setup (Patrick)

1. Create a PyPI account at https://pypi.org/account/register/ and turn on 2FA.
2. On PyPI, open Your account, then Publishing, and add a pending publisher for GitHub
   with these exact values:

   | Field | Value |
   |---|---|
   | PyPI project name | `whale-agent` |
   | Owner | `patrickt6` |
   | Repository name | `whale-agent` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name. Whoever uploads first gets it, so do
   the first release soon after this step. `whale-agent` returned 404 from
   `https://pypi.org/pypi/whale-agent/json` on 2026-09-17, which means it was free then.
3. On GitHub, in `patrickt6/whale-agent`, go to Settings, then Environments, and create
   an environment called `pypi`. Add yourself as a required reviewer, so a tag alone
   cannot publish without your click.
4. Optional dry run: repeat steps 1 and 2 on https://test.pypi.org with a copy of the
   workflow that sets `repository-url: https://test.pypi.org/legacy/` on the publish
   step, plus an environment called `testpypi`.

### Every release

1. Set `version` in `pyproject.toml`, for example `0.1.1`, and commit.
2. Check the build locally in a scratch venv:

   ```bash
   python -m pip install build twine
   python -m build && python -m twine check dist/*
   python -m venv /tmp/wa && /tmp/wa/bin/pip install dist/*.whl
   cd /tmp && /tmp/wa/bin/whale --help && /tmp/wa/bin/whale digest --demo
   ```

3. Tag and push. The tag must match the version exactly, or the build job stops:

   ```bash
   git tag v0.1.1 && git push origin v0.1.1
   ```

4. Approve the `pypi` environment deployment in the Actions tab.
5. Check `https://pypi.org/project/whale-agent/`, then run `pipx install whale-agent` on a
   clean machine.

You cannot re-upload a version PyPI already has. If a release is broken, yank it on
PyPI and ship the next patch version.

## npm

The npm package lives in `npm/`. It is a small launcher that runs the PyPI package
through uv or pipx, so publish it only after PyPI has the matching version.

The name `whale-agent` was already taken on npm when checked on 2026-09-17
(`npm view whale-agent` showed version 1.0.1, an unrelated project). The package is
therefore called `whale-agent-cli`, and `whale-agent-cli` returned 404 then.

### First publish (from your machine)

1. Create an account at https://www.npmjs.com/signup and turn on 2FA.
2. Log in and publish:

   ```bash
   cd npm
   npm login
   npm pack --dry-run          # check that only bin/whale.js and README.md ship
   npm publish --access public
   ```

3. Check it: `npx whale-agent-cli --help` on a machine with uv.

### Later releases, with provenance

`npm publish --provenance` only works from a cloud CI runner such as GitHub Actions. It
does not work from a laptop. The job needs `permissions: id-token: write`, npm 9.5.0 or
newer, and a `repository` field in `package.json` that matches the GitHub repo. The
field is already set to `patrickt6/whale-agent` with `directory: npm`.

```bash
cd npm
npm version 0.1.1 --no-git-tag-version
npm publish --provenance --access public
```

Reference (opened 2026-09-17): https://docs.npmjs.com/generating-provenance-statements

## Site installer

The site serves `install.sh` at `/install`. After PyPI and npm are live:

```bash
deploy-site/sync-install.sh     # copies ../install.sh into the worker bundle
```

Then deploy the site. Check that
`curl -fsSL https://whale-agent.com/install | sh` installs
the new version in a fresh `HOME`.

The installer installs uv from https://astral.sh/uv/install.sh when it is missing. uv's
docs give that script and say it puts uv in `~/.local/bin` (opened 2026-09-17:
https://docs.astral.sh/uv/getting-started/installation/).
