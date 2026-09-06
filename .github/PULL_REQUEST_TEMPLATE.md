# Pull Request

## Summary

<!-- What does this PR do? 1-3 sentences. -->

## Motivation

<!-- Why is this change needed? Link to the issue if applicable. -->
<!-- Closes #NNN -->

## Changes

<!-- List the key changes made. -->

- 
- 
- 

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behavior)
- [ ] Documentation update
- [ ] Refactor (no behavior change)

## Token impact

<!-- Does this change affect token consumption? -->
<!-- e.g., "Adds trigger indexing — saves ~3,000 tokens per trigger file read" -->
<!-- or "No token impact (documentation only)" -->

## Testing

<!-- Describe how you tested this change. -->

- [ ] I ran `pytest` and all tests pass
- [ ] I added tests covering the new behavior
- [ ] I tested manually against a real Salesforce DX project

**Test plan:**

1. 
2. 
3. 

## Checklist

- [ ] Code follows the style guide (black + ruff — run `black . && ruff check .`)
- [ ] New public functions/methods have docstrings with Args/Returns
- [ ] No external API calls added (rtk-sf must work fully offline)
- [ ] `--verbose` flag produces useful debug output for the new code
- [ ] CHANGELOG.md entry added under `[Unreleased]`
- [ ] Documentation updated if the feature changes user-facing behavior

## Screenshots (if applicable)

<!-- For UI changes (architecture map), include before/after screenshots. -->
