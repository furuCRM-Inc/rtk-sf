# Contributing to rtk-sf

Thank you for your interest in contributing to rtk-sf. This project is maintained by furuCRM Inc. and welcomes contributions from the Salesforce developer community.

## Our Values

- **Developer-first**: Every feature must reduce friction, not add it.
- **Zero magic**: Code should be readable and understandable without a PhD.
- **No external API calls**: rtk-sf must work fully offline. No LLM API calls in core code.
- **Token-awareness**: Features are measured by their impact on token consumption.

## Getting Started

### Fork and Clone

```bash
git clone https://github.com/YOUR_USERNAME/rtk-sf.git
cd rtk-sf
```

### Set Up Development Environment

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
.venv\Scripts\activate       # Windows

# Install with dev dependencies
pip install -e ".[dev]"
```

### Verify Your Setup

```bash
pytest
python -m rtk_sf --version
```

## Workflow

### 1. Create a Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

Branch naming conventions:
- `feature/` — new functionality
- `fix/` — bug fixes
- `docs/` — documentation-only changes
- `refactor/` — code restructuring without behavior change

### 2. Write Code

Follow the code style guidelines below. Keep changes focused and atomic.

### 3. Write Tests

All new functionality must include tests. rtk-sf uses `pytest`:

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=rtk_sf --cov-report=term-missing

# Run a specific test file
pytest tests/test_indexer.py -v
```

Tests live in the `tests/` directory and mirror the `rtk_sf/` module structure.

### 4. Open a Pull Request

Push your branch and open a PR against `main`:

```bash
git push origin feature/your-feature-name
```

Fill in the PR template completely. PRs without a test plan will not be merged.

## Code Style

rtk-sf uses **black** for formatting and **ruff** for linting.

```bash
# Format
black rtk_sf/ tests/

# Lint
ruff check rtk_sf/ tests/

# Type check
mypy rtk_sf/
```

Configuration is in `pyproject.toml`. Line length is **100 characters**.

### Style guidelines

- Use `from __future__ import annotations` in all modules (for Python 3.9 compatibility).
- Prefer explicit over implicit — no magic attribute access on `dict` values.
- All public functions and classes must have docstrings.
- Use type hints for all function signatures.
- Avoid global state; use instance attributes.

### Docstring format

```python
def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Search indexed components using FTS5 keyword search.

    Args:
        query: Search query string.
        limit: Maximum number of results to return.

    Returns:
        List of result dicts with keys: name, type, file_path, snippet, score.
    """
```

## Reporting Bugs

Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.yml).

Include:
- Python version (`python --version`)
- rtk-sf version (`rtk-sf --version`)
- Salesforce project structure (anonymized if needed)
- Full error output with `rtk-sf --verbose`

## Requesting Features

Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.yml).

Before submitting:
- Check existing issues and discussions
- Consider the "no external API calls" constraint
- Include a token-impact estimate if relevant

## Adding Support for New Metadata Types

To add support for a new Salesforce metadata type (e.g., Permission Sets):

1. Add a parser function `_parse_<type>_xml(xml: str) -> dict` in `indexer.py`
2. Add a spec builder `_build_<type>_spec(...)` in `indexer.py`
3. Register the new type in `SalesforceIndexer._index_xml()`
4. Add test fixtures in `tests/fixtures/`
5. Add tests in `tests/test_indexer.py`
6. Update `search.py` `list_components()` enum docs

## Commit Messages

Use conventional commits:

```
feat: add permission set indexing
fix: handle empty Apex class files without error
docs: add enterprise ROI examples to roi.md
refactor: extract XML tag parsing to standalone function
test: add test for differential mtime tracking
```

## Questions?

Open a [Discussion](https://github.com/furuCRM/rtk-sf/discussions) or reach out at [dev@furucrm.com](mailto:dev@furucrm.com).

---

Built with love by [furuCRM Inc.](https://www.furucrm.com)
