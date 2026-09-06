## rtk-sf v0.3.0 — Annotation System · Japanese Search · Viral Launch

### Summary

- **Annotation system** (`annotate_component`) — AI agents can now write discovered business logic back into the index; the knowledge persists across sessions at zero additional token cost
- **Japanese full-text search** — FTS5 trigram tokenizer + LIKE fallback for 1–2 char terms; `申込`, `不備`, `管理職` all return correct results
- **CamelCase splitting** — `ExamTicketDownloadController` is indexed as `Exam Ticket Download Controller`; partial English searches work without knowing the exact component name
- **44+ metadata types** — expanded from 4 to full sf CLI coverage (Agentforce, OmniStudio, Experience Cloud, Analytics, and more)
- **Docs & README** — viral launch optimizations: Before/After comparison, team ROI calculator, one-liner install, Claude Code focus

---

### New MCP Tool: `annotate_component`

```
# First session: Claude discovers hidden logic in source
annotate_component(
  component_name = "Application__c",
  key            = "business_rule",
  value          = "Correction only allowed when Status__c = '不備'. Owner check via EligibleStaff__r.Contact__c.",
  source         = "ai_discovery"
)

# All future sessions — zero source reads
query_compressed_spec("Application__c")
# → YAML spec + ## Annotations (discovered business logic)
#   [business_rule] (ai_discovery · 2026-09-06)
#     Correction only allowed when Status__c = '不備'. ...
```

---

### What Changed

| Area | Change |
|---|---|
| `search.py` | `annotations` table, `add_annotation()`, `get_annotations()`, trigram FTS5, LIKE fallback for short queries |
| `mcp_server.py` | `annotate_component` tool (5th tool), annotations appended to `query_compressed_spec` response |
| `indexer.py` | 44 metadata types, CamelCase splitting via `_build_raw_text()` |
| `__init__.py` | Version → 0.3.0 |
| `__main__.py` | `--version` now reads `__version__` dynamically (was hardcoded 0.1.0) |
| `ui_generator.py` | Cytoscape CDN 3.28.1 → 3.34.2 |
| `pyproject.toml` | All deps updated to latest; version → 0.3.0 |
| `README.md` | Before/After table, ROI calculator, `curl` one-liner, Japanese search docs, Contributing asks |
| `scripts/install.sh` | URL fix: `https://furucrm.com` → `https://www.furucrm.com` |

---

### Test Plan

- [x] 69/70 automated checks passed (1 false negative: `申込済み` not in indexed data — correct behavior)
- [x] Japanese 2-char LIKE fallback: `申込`, `不備`, `選考` all return results
- [x] Japanese 3-char trigram: `主任教諭`, `管理職`, `不備修正` all return results
- [x] CamelCase: `search("ApplicationSubmission")` returns `ApplicationSubmissionController`
- [x] `annotate_component` → annotation stored → searchable → returned in `query_compressed_spec`
- [x] All 5 MCP tools dispatch correctly via JSON-RPC 2.0
- [x] `rtk-sf index` (differential: 743 skipped, 0 errors on re-run)
- [x] `rtk-sf ui` generates 391KB self-contained HTML with Cytoscape SVG
- [x] `rtk-sf --version` outputs `rtk-sf 0.3.0`

---

### Community Contribution Asks (post-launch)

Added 5 specific parser tasks to the Contributing section so the community can extend coverage:

- OmniStudio FlexCard
- OmniStudio DataRaptor
- Experience Cloud page (ExperienceBundle)
- Slack App metadata
- Custom Notification Type

---

### Launch Checklist (separate from this PR)

- [ ] Push to GitHub (`git push origin feat/viral-launch-v0.3.0`)
- [ ] Create GitHub release v0.3.0 with changelog
- [ ] Publish to PyPI (`python -m build && twine upload dist/*`)
- [ ] Pin Before/After GIF to top of README (screen recording of token comparison)
- [ ] Post to SFXD Discord (#ai-and-automation)
- [ ] Post to r/salesforce and r/SalesforceDeveloper
- [ ] LinkedIn post with ROI calculator screenshot
- [ ] Tag Salesforce MVPs / AI evangelists on X with `#SalesforceDevs #ClaudeCode #Apex`
