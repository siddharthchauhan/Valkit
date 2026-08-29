# Continuous integration

`github-workflow.yml` is the GitHub Actions workflow for this project. It is
here rather than at `.github/workflows/ci.yml` because the GitHub App used to
push this branch does not hold the `workflows` permission, and a push that
creates or updates a file under `.github/workflows/` is rejected outright.

To enable it:

```bash
mkdir -p .github/workflows
git mv ci/github-workflow.yml .github/workflows/ci.yml
git commit -m "Enable CI"
```

That commit has to come from an account or app with the `workflows` permission.

## What it does

Three jobs, each checking something the others do not.

**test** runs the full suite on Python 3.11 and 3.12 with every optional extra
installed.

**core-only** installs the package with no extras and imports every core module,
then runs the CLI. The core is meant to depend on PyYAML and Jinja2 and nothing
else; a lazy import that quietly became eager would pass the `test` job, where
everything is installed, and fail for a customer who installed only the core.
This is the job that catches it.

**demo** runs `examples/demo.py` end to end and asserts the package was actually
produced, including that the credibility report reaches step 7. A broken
demonstration is worse than a broken test, because it is the first thing anyone
runs.
