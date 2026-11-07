# Security policy

AxLab contains public experiment code, not production AXIOM secrets. Do not include `.env` files, signing keys, HMAC release keys, audit private keys, API tokens, customer datasets, or internal deployment configuration.

Known design boundary: the file-based fleet switch is **not a security boundary against arbitrary malicious code running as the same OS user with access to release-key material**. Run untrusted extensions in separately permissioned environments. A signed release is not a substitute for OS isolation.

Please report suspected vulnerabilities privately to hello@orivael.dev rather than opening a public issue containing exploit details.
