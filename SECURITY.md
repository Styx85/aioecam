# Security

aioecam controls a physical appliance that heats water and dispenses hot
liquids, and it handles LAN keys and cloud credentials. Please report
vulnerabilities privately through GitHub's "Report a vulnerability" (Security
tab) rather than in a public issue.

## Design rules

- LAN keys, tokens and passwords are never logged, never part of exceptions and
  never written to disk by the library; callers own their storage.
- The LAN callback server only accepts requests from the IP addresses of
  machines that were added to it.
- Every session message is authenticated (HMAC-SHA256) before it is acted on.
- The machine's firmware is old and unpatched. Keep it on an isolated network,
  allow only `machine -> integration:<lan port>` and `integration -> machine:80`,
  and block its internet access if you do not need the cloud.
