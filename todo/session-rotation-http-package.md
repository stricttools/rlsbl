# Add session rotation support to the HTTP package

rate_limiting.py in shopkeep imports curl_cffi directly (with a ruff lint exemption) because it needs session rotation — creating/destroying Sessions to reset TLS fingerprints as an anti-bot strategy. The HTTP package's fetch() uses long-lived cached sessions, which is fundamentally incompatible.

To remove the exemption, the HTTP package would need a batch-oriented API that manages session lifecycle, rotation, delays, and retry classification internally.
