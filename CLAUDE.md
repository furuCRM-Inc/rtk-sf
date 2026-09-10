# Salesforce Project

## Code Search & Data — Use rtk-sf First (Required)

This project is indexed by **rtk-sf**. Always use the MCP tools before reading raw files or calling sf CLI:

| Task | Tool to call |
|---|---|
| Find a component by name or keyword | `search_codebase(query)` |
| Read a component spec / fields / methods | `query_compressed_spec(component_name)` |
| Blast-radius before editing | `get_relations(component_name)` |
| List all Apex classes / objects / flows | `list_components(type)` |
| Write discovered business logic back | `annotate_component(component_name, key, value)` |
| Read an Apex class before editing (surgical) | `get_class_skeleton(component_name, focus_methods)` |
| Deploy / retrieve / run tests silently | `sf_command(action, target_org, ...)` |
| Get object field list for data creation | `get_object_schema(object_name)` |
| Inspect existing records (sample only) | `soql_query(query, target_org, sample_size)` |
| Read RecordType definitions for an object | `get_record_types(object_name)` |

**Never** do these directly — use the tool instead:
- Read a raw .cls file                    -> use `get_class_skeleton`
- sf sobject describe                      -> use `get_object_schema`
- sf data query                            -> use `soql_query`
- sf project deploy start                  -> use `sf_command(action="deploy")`
- find force-app ... \| xargs cat          -> use `get_record_types(object_name)`
- Any pipeline scan over recordTypes/, fields/, or layouts/ folders -> use `get_record_types` or `get_object_schema`

If search returns no results, re-index with: `python3 -m rtk_sf index`
Do NOT use `npx rtk-sf` — rtk-sf is a Python package, not npm.

