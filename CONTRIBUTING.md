# Contributing

This project explores deterministic agent replay. Its current integration is the bundled offline invoice example. Keep changes focused on that working example and describe the user-visible effect in the pull request.

Use Python 3.11 or newer. From a checkout, run `python -m pip install .` and `python -m unittest discover -s tests -v`. The replay and fork tests need a Docker CLI connected to a Linux engine; the suite skips them otherwise. Add or update a focused test when behavior changes. Keep captures, credentials, and local stores out of commits.

For vulnerabilities, use the private reporting route in [SECURITY.md](SECURITY.md) rather than a public issue.
