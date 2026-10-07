# Development and CI
tests/
test/
__tests__/
.github/
scripts/
docs/
e2e/

# Private paths: the upload-private-paths check refuses a release whose
# package carries any of them
{{npm.privatePathIgnore}}

# Python artifacts (for multi-target projects)
__pycache__/
*.pyc
*.py
*.toml
.venv/

# Build/cache
coverage/
.nyc_output/

# Editor/OS
.DS_Store
*.log

# Selfdoc
selfdoc.json
