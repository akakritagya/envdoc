# Security Policy

## Supported Versions

envdoc is not published to PyPI — every install path pulls directly from a git
ref (see [Installation](README.md#installation)). Only the latest commit on
`main` is supported; there are no maintained release branches.

## Reporting a Vulnerability

Please do **not** open a public issue for security vulnerabilities.

Instead, use GitHub's private reporting:

1. Go to the [Security tab](https://github.com/akakritagya/envdoc/security)
   of this repository.
2. Click **Report a vulnerability** under "Advisories".

If you're unable to use GitHub's private reporting, email
rameshneupane.ai@gmail.com with details.

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal repository or command line is ideal, since
  envdoc's whole surface is reading files from a target repo)
- Any relevant environment details (OS, Python version)

We'll acknowledge reports as promptly as possible and follow up once the issue
is understood or resolved. Please allow time for a fix before any public
disclosure.
