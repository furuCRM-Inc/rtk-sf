"""
Pins the documentation engine in rtk_sf/docgen/.

Builds a miniature Salesforce + Redux project, indexes it with the real indexer,
then asserts on what the extractor derives and what the generators write. The
point is the derived facts — `@AuraEnabled`, DML target resolution, access mode,
LWC→Apex wiring, per-construct Apex attribution — since none of that is in the
YAML spec index and the documents are only as honest as the extraction.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rtk_sf.docgen import DOC_TYPES, GENERATORS, export_documentation
from rtk_sf.docgen.extract import build_model

# ---------------------------------------------------------------------------
# Fixture project
# ---------------------------------------------------------------------------

_FILES = {
    "force-app/main/default/classes/OrderController.cls": '''
        /**
         * @description Order entry points for the checkout console.
         */
        public with sharing class OrderController {
            /**
             * @description Submits an order and closes its opportunity.
             */
            @AuraEnabled
            public static Order__c submitOrder(Id orderId) {
                try {
                    Order__c order = [SELECT Id, Status__c FROM Order__c WHERE Id = :orderId WITH SECURITY_ENFORCED LIMIT 1];
                    order.Status__c = 'Submitted';
                    update as user order;
                    AuditService.log(orderId);
                    return order;
                } catch (DmlException e) {
                    throw new AuraHandledException(e.getMessage());
                }
            }

            @AuraEnabled(cacheable=true)
            public static List<Order__c> listOrders() {
                return [SELECT Id, Name FROM Order__c WITH USER_MODE];
            }

            @AuraEnabled
            public static void purge(Id orderId) {
                List<Order__c> doomed = new List<Order__c>();
                delete doomed;
            }
        }
        ''',
    "force-app/main/default/classes/AuditService.cls": '''
        public class AuditService {
            public static void log(Id recordId) {
                Audit__c entry = new Audit__c(Record__c = recordId);
                insert entry;
            }
        }
        ''',
    # DML shapes that a real project turned up: prose in comments, inline
    # construction, collection literals, and a variable of unknown type.
    "force-app/main/default/classes/DmlShapes.cls": '''
        public class DmlShapes {
            public static void shapes(SObject mystery) {
                // Get data for insert the ContactRole before continuing
                String note = 'remember to update the Invoice__c record';
                insert new Order__c(Status__c = 'Draft');
                Database.insert(new List<Contact>{ new Contact() }, true, AccessLevel.SYSTEM_MODE);
                Database.update(new Map<Id, Account>());
                update mystery;
            }
        }
        ''',
    "force-app/main/default/classes/LegacyOrderApi.cls": '''
        public class LegacyOrderApi {
            @AuraEnabled
            public static void submitOrder(Id orderId) {
                Order__c stale = new Order__c(Id = orderId);
                update stale;
            }
        }
        ''',
    "force-app/main/default/triggers/OrderTrigger.trigger": '''
        trigger OrderTrigger on Order__c (before update, after update) {
            OrderTriggerHandler.run(Trigger.new);
        }
        ''',
    "force-app/main/default/classes/OrderTriggerHandler.cls": '''
        public class OrderTriggerHandler {
            public static void run(List<Order__c> records) {
                update records;
            }
        }
        ''',
    "force-app/main/default/lwc/orderConsole/orderConsole.js": '''
        import { LightningElement, api, wire } from 'lwc';
        import submitOrder from '@salesforce/apex/OrderController.submitOrder';
        import listOrders from '@salesforce/apex/OrderController.listOrders';
        import STATUS_FIELD from '@salesforce/schema/Order__c.Status__c';
        import { ShowToastEvent } from 'lightning/platformShowToastEvent';
        import { store } from 'c/orderStore';
        import { submitOrderThunk } from 'c/orderSlice';

        export default class OrderConsole extends LightningElement {
            @api recordId;
            @wire(listOrders) orders;

            handleSubmit() {
                store.dispatch(submitOrderThunk({ orderId: this.recordId }));
                this.dispatchEvent(new CustomEvent('ordersubmitted'));
                this.dispatchEvent(new ShowToastEvent({ title: 'Submitted' }));
            }
        }
        ''',
    "force-app/main/default/lwc/orderConsole/orderConsole.html": '''
        <template>
            <lightning-button onclick={handleSubmit}></lightning-button>
        </template>
        ''',
    "force-app/main/default/lwc/orderConsole/orderConsole.js-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
            <isExposed>true</isExposed>
            <targets>
                <target>lightning__RecordPage</target>
            </targets>
        </LightningComponentBundle>
        ''',
    "force-app/main/default/lwc/orderSlice/orderSlice.js": '''
        import { createSlice, createAsyncThunk } from '@reduxjs/toolkit';
        import submitOrder from '@salesforce/apex/OrderController.submitOrder';
        import listOrders from '@salesforce/apex/OrderController.listOrders';

        export const submitOrderThunk = createAsyncThunk('order/submit', async (payload) => {
            return await submitOrder(payload);
        });

        const orderSlice = createSlice({
            name: 'order',
            initialState: { records: [] },
            reducers: {
                setOrders(state, action) { state.records = action.payload; },
                reset(state) { state.records = []; }
            }
        });
        ''',
    "force-app/main/default/lwc/orderStore/api.js": '''
        import { createApi } from '@reduxjs/toolkit/query';
        import listOrders from '@salesforce/apex/OrderController.listOrders';

        export const orderApi = createApi({
            reducerPath: 'orderApi',
            endpoints: (builder) => ({
                getOrders: builder.query({ queryFn: listOrders }),
                touchOrder: builder.mutation({ queryFn: () => null })
            })
        });
        ''',
    "force-app/main/default/objects/Order__c/Order__c.object-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
            <label>Order</label>
            <pluralLabel>Orders</pluralLabel>
        </CustomObject>
        ''',
    "force-app/main/default/objects/Order__c/fields/Status__c.field-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
            <fullName>Status__c</fullName>
            <label>Status</label>
            <type>Picklist</type>
            <required>true</required>
            <valueSet>
                <valueSetDefinition>
                    <value><fullName>Draft</fullName></value>
                    <value><fullName>Submitted</fullName></value>
                </valueSetDefinition>
            </valueSet>
        </CustomField>
        ''',
    "force-app/main/default/objects/Order__c/fields/Opportunity__c.field-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
            <fullName>Opportunity__c</fullName>
            <label>Opportunity</label>
            <type>Lookup</type>
            <referenceTo>Opportunity</referenceTo>
        </CustomField>
        ''',
    "force-app/main/default/objects/Order__c/fields/Total__c.field-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
            <fullName>Total__c</fullName>
            <label>Total</label>
            <type>Currency</type>
            <formula>Opportunity__r.Amount * 1.1</formula>
        </CustomField>
        ''',
    "force-app/main/default/objects/Invoice__c/Invoice__c.object-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
            <label>Invoice</label>
        </CustomObject>
        ''',
    "force-app/main/default/objects/Invoice__c/fields/Status__c.field-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
            <fullName>Status__c</fullName>
            <label>Invoice Status</label>
            <type>Picklist</type>
            <valueSet>
                <valueSetDefinition>
                    <value><fullName>Unpaid</fullName></value>
                </valueSetDefinition>
            </valueSet>
        </CustomField>
        ''',
    "force-app/main/default/objects/Order__c/validationRules/Status_Required.validationRule-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata">
            <fullName>Status_Required</fullName>
            <active>true</active>
            <errorConditionFormula>ISBLANK(TEXT(Status__c))</errorConditionFormula>
            <errorMessage>Status is required before submitting an order.</errorMessage>
        </ValidationRule>
        ''',
    "force-app/main/default/permissionsets/Order_Manager.permissionset-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
            <label>Order Manager</label>
            <objectPermissions>
                <object>Order__c</object>
                <allowRead>true</allowRead>
                <allowCreate>true</allowCreate>
                <allowEdit>true</allowEdit>
                <allowDelete>false</allowDelete>
            </objectPermissions>
        </PermissionSet>
        ''',
    "force-app/main/default/flows/Order_Followup.flow-meta.xml": '''
        <?xml version="1.0" encoding="UTF-8"?>
        <Flow xmlns="http://soap.sforce.com/2006/04/metadata">
            <label>Order Followup</label>
            <processType>AutoLaunchedFlow</processType>
            <status>Active</status>
            <recordCreates><name>Create_Task</name></recordCreates>
        </Flow>
        ''',
}


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    """An indexed miniature project, built once for the whole module."""
    root = tmp_path_factory.mktemp("docgen_project")
    for rel, body in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")

    from rtk_sf.indexer import SalesforceIndexer

    SalesforceIndexer(root).index_project()
    return root


@pytest.fixture(scope="module")
def model(project):
    return build_model(project)


# ---------------------------------------------------------------------------
# Extraction — facts the YAML index does not carry
# ---------------------------------------------------------------------------


def test_aura_annotations_are_recovered_from_source(model):
    names = [f"{cls.name}.{method.name}" for cls, method in model.aura_methods()]
    assert names == [
        "LegacyOrderApi.submitOrder",
        "OrderController.listOrders",
        "OrderController.purge",
        "OrderController.submitOrder",
    ]

    controller = model.apex["OrderController"]
    assert controller.sharing == "with sharing"
    assert controller.method("listOrders").is_cacheable
    assert not controller.method("submitOrder").is_cacheable


def test_dml_targets_resolve_to_sobjects_with_access_mode(model):
    submit = model.apex["OrderController"].method("submitOrder")
    described = {op.describe() for op in submit.db_ops}
    assert "QUERY Order__c [security_enforced]" in described
    assert "UPDATE Order__c [user]" in described

    # DML on a local collection resolves through its declared element type.
    purge = model.apex["OrderController"].method("purge")
    assert [op.describe() for op in purge.db_ops] == ["DELETE Order__c"]


def test_security_markers_and_exceptions_are_captured(model):
    submit = model.apex["OrderController"].method("submitOrder")
    assert "WITH SECURITY_ENFORCED" in submit.security
    assert "DML as user" in submit.security
    assert submit.throws == ["AuraHandledException"]
    assert submit.has_try
    assert submit.calls == ["AuditService.log"]

    purge = model.apex["OrderController"].method("purge")
    assert purge.security == []  # nothing to report, and nothing invented


def test_comments_and_strings_are_not_parsed_as_dml(model):
    """Prose must not become data: `// ... insert the ContactRole` is not a DML op."""
    shapes = model.apex["DmlShapes"].method("shapes")
    targets = {op.sobject for op in shapes.db_ops}
    assert "the" not in targets
    assert "ContactRole" not in targets
    assert "Invoice__c" not in targets  # it only appears inside a string literal


def test_inline_construction_resolves_to_the_object(model):
    shapes = model.apex["DmlShapes"].method("shapes")
    described = {op.describe() for op in shapes.db_ops}
    assert "INSERT Order__c" in described                       # insert new Order__c(...)
    assert "UPDATE Account" in described                        # new Map<Id, Account>()
    # The AccessLevel argument declares the mode, so it is reported with the op.
    assert "INSERT Contact [system]" in described


def test_system_mode_is_reported_but_not_counted_as_enforcement(model):
    """`AccessLevel.SYSTEM_MODE` is an explicit opt-out, not an access check."""
    shapes = model.apex["DmlShapes"].method("shapes")
    assert "AccessLevel.SYSTEM_MODE" in shapes.security
    assert shapes.enforced_security == []

    submit = model.apex["OrderController"].method("submitOrder")
    assert "WITH SECURITY_ENFORCED" in submit.enforced_security


def test_untyped_dml_target_is_left_unresolved(model):
    """An unresolvable variable yields no object name rather than a fabricated one."""
    shapes = model.apex["DmlShapes"].method("shapes")
    unresolved = [op for op in shapes.db_ops if not op.sobject]
    assert unresolved and all(op.kind in {"update", "insert"} for op in unresolved)
    assert "mystery" not in {op.sobject for op in shapes.db_ops}
    assert unresolved[0].describe() == "UPDATE"


def test_lwc_wiring_is_extracted(model):
    console = model.lwc["orderConsole"]
    assert console.is_ui and console.exposed
    assert console.targets == ["lightning__RecordPage"]
    assert set(console.apex_calls) == {
        "OrderController.submitOrder",
        "OrderController.listOrders",
    }
    assert console.wired == ["listOrders"]
    assert console.store_dispatches == ["submitOrderThunk"]
    assert console.events_published == ["ordersubmitted"]
    assert console.events_for("handleSubmit") == ["click"]
    assert console.uses_toast
    assert console.objects == ["Order__c"]


def test_store_only_bundles_are_not_screens(model):
    assert not model.lwc["orderSlice"].is_ui
    assert not model.lwc["orderStore"].is_ui


def test_redux_constructs_are_found_outside_the_index(model):
    assert [s.name for s in model.slices] == ["order"]
    assert set(model.slices[0].reducers) == {"setOrders", "reset"}
    assert [t.name for t in model.thunks] == ["submitOrderThunk"]
    assert [e.qualified for e in model.endpoints] == [
        "orderApi.getOrders",
        "orderApi.touchOrder",
    ]


def test_apex_attribution_is_scoped_to_the_construct(model):
    """A file importing two Apex methods must not credit both to every thunk."""
    thunk = model.thunks[0]
    assert thunk.apex_calls == ["OrderController.submitOrder"]

    endpoints = {e.name: e.apex_calls for e in model.endpoints}
    assert endpoints["getOrders"] == ["OrderController.listOrders"]
    assert endpoints["touchOrder"] == []


def test_caller_lookups_join_the_layers(model):
    assert model.lwc_callers_of("OrderController.submitOrder") == ["orderConsole", "orderSlice"]
    assert model.thunk_callers_of("OrderController.submitOrder") == ["submitOrderThunk"]
    assert model.endpoint_callers_of("OrderController.listOrders") == ["orderApi.getOrders"]
    assert model.endpoint_callers_of("OrderController.submitOrder") == []


def test_schema_facts_include_formulas_and_picklists(model):
    order = model.objects["Order__c"]
    assert order.label == "Order"
    fields = {f.name: f for f in order.fields}
    assert fields["Status__c"].picklist_values == ["Draft", "Submitted"]
    assert fields["Status__c"].required
    assert "Opportunity__r.Amount" in fields["Total__c"].formula
    assert fields["Opportunity__c"].reference_to == "Opportunity"
    assert [r.name for r in order.validation_rules] == ["Status_Required"]


def test_same_named_fields_stay_on_their_own_object(model):
    """
    Field specs carry only a basename, so `Status__c` exists under several
    objects. Resolving by basename attributed a field — with its label, picklist
    and required flag — to whichever object the filesystem walk reached first.
    """
    order_status = [f for f in model.objects["Order__c"].fields if f.name == "Status__c"]
    invoice_status = [f for f in model.objects["Invoice__c"].fields if f.name == "Status__c"]

    assert len(order_status) == 1 and len(invoice_status) == 1
    assert order_status[0].label == "Status"
    assert order_status[0].required
    assert order_status[0].picklist_values == ["Draft", "Submitted"]
    assert invoice_status[0].label == "Invoice Status"
    assert not invoice_status[0].required
    assert invoice_status[0].picklist_values == ["Unpaid"]


def test_no_object_lists_a_field_twice(model):
    for obj in model.objects.values():
        names = [f.name for f in obj.fields]
        assert len(names) == len(set(names)), f"{obj.name} has duplicate fields"


def test_triggers_and_flows_are_modeled(model):
    trigger = model.triggers[0]
    assert trigger.name == "OrderTrigger"
    assert trigger.sobject == "Order__c"
    assert trigger.events == ["before update", "after update"]
    assert trigger.handler_calls == ["OrderTriggerHandler.run"]
    assert model.triggers_on("order__c")  # case-insensitive lookup
    assert model.flows[0].writes


def test_unindexed_project_yields_an_empty_model(tmp_path):
    assert build_model(tmp_path).is_empty


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def test_every_doc_type_has_a_generator():
    assert set(DOC_TYPES) == set(GENERATORS) | {"all"}
    for module in GENERATORS.values():
        assert module.OUTPUT and hasattr(module, "generate")


def test_export_all_writes_every_document(project, tmp_path):
    result = export_documentation("all", output_dir=tmp_path, project_root=project)
    assert result.startswith("✅")

    written = {p.name for p in tmp_path.iterdir()}
    assert written == {module.OUTPUT for module in GENERATORS.values()}
    assert all((tmp_path / name).stat().st_size > 0 for name in written)


def test_export_returns_a_summary_not_the_document(project, tmp_path):
    result = export_documentation("function_matrix", output_dir=tmp_path, project_root=project)
    # The point of direct-to-disk generation: the body never reaches the caller.
    assert len(result.splitlines()) <= 4
    assert "submitOrder" not in result
    assert (tmp_path / "FUNCTION_MATRIX.md").read_text(encoding="utf-8").count("submitOrder") > 0


def test_unknown_doc_type_is_rejected(project, tmp_path):
    result = export_documentation("nonsense", output_dir=tmp_path, project_root=project)
    assert result.startswith("❌") and "nonsense" in result


def test_export_requires_an_index(tmp_path):
    result = export_documentation("all", output_dir=tmp_path / "out", project_root=tmp_path)
    assert result.startswith("❌") and "rtk-sf index" in result


def test_sequence_diagram_traces_the_full_boundary(project, tmp_path):
    export_documentation("sequence_diagrams", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "SEQUENCE_DIAGRAMS.md").read_text(encoding="utf-8")

    assert "```mermaid" in body and "sequenceDiagram" in body
    assert "participant LWC as LWC (orderConsole)" in body
    assert "Redux thunk (submitOrderThunk)" in body
    assert "RTK->>Apex: submitOrder(Id orderId)" in body
    assert "Apex->>SFDB: UPDATE Order__c [user]" in body
    assert "SFDB->>OrderTrigger: before update, after update" in body
    assert "Note over Apex: throws AuraHandledException" in body
    # The handler label names only the event bound to that handler.
    assert "handleSubmit() — onclick" in body


def test_wired_read_does_not_borrow_another_flows_dispatch(project, tmp_path):
    export_documentation("sequence_diagrams", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "SEQUENCE_DIAGRAMS.md").read_text(encoding="utf-8")
    listing = body.split("orderConsole → OrderController.listOrders")[1].split("###")[0]
    assert "useQuery(orderApi.getOrders)" in listing
    assert "submitOrderThunk" not in listing


def test_function_matrix_reports_wiring_gaps(project, tmp_path):
    export_documentation("function_matrix", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "FUNCTION_MATRIX.md").read_text(encoding="utf-8")

    assert "`OrderController.purge` — exposed to clients, but no caller found" in body
    assert "None — every Apex import resolves" in body
    # Known absence renders as an em dash, not as "undetermined".
    assert "| `orderApi.touchOrder` | mutation | — |" in body


def test_actors_are_matched_on_the_qualified_name(project, tmp_path):
    """Two classes can expose the same method name; actors must not bleed across."""
    export_documentation("function_usecases", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "FUNCTION_USECASES.md").read_text(encoding="utf-8")

    legacy = body.split("## LegacyOrderApi.submitOrder")[1].split("\n## ")[0]
    assert "orderConsole" not in legacy  # nothing imports LegacyOrderApi.submitOrder
    assert "no permission set or profile in the index grants" in legacy or "PermissionSet" in legacy

    real = body.split("## OrderController.submitOrder")[1].split("\n## ")[0]
    assert "Users of LWC `orderConsole`" in real


def test_use_cases_derive_actors_and_conditions(project, tmp_path):
    export_documentation("function_usecases", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "FUNCTION_USECASES.md").read_text(encoding="utf-8")

    assert "PermissionSet `Order_Manager` — grants Order__c" in body
    assert "Users of LWC `orderConsole` on lightning__RecordPage" in body
    assert "validation rule `Status_Required` must pass" in body
    assert "Trigger `OrderTrigger` fires" in body
    assert "Formula field(s) on `Order__c` recalculate" in body
    assert "class-level summary" in body  # a method without ApexDoc says so


def test_business_scenarios_flag_unenforced_writes(project, tmp_path):
    export_documentation("business_scenarios", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "BUSINESS_SCENARIOS.md").read_text(encoding="utf-8")

    assert "Order journey" in body
    assert "Status is required before submitting an order." in body
    assert "`Order_Manager` grants read, create, edit on `Order__c`" in body
    assert "OrderController.purge" in body  # deletes without any security marker


def test_erd_is_raw_mermaid_with_relationships(project, tmp_path):
    export_documentation("erd", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "ERD.mmd").read_text(encoding="utf-8")

    assert body.startswith("%% Generated by rtk-sf")
    assert "```" not in body  # a .mmd file must not be fenced
    assert "erDiagram" in body
    assert "Order__c {" in body
    assert 'Opportunity }o--|| Order__c : "Opportunity__c"' in body
    # Opportunity has no spec here, so it is drawn as an external entity rather
    # than invented with fields.
    assert "referenced only — not indexed here" in body


def test_system_doc_links_the_set_and_scores_security(project, tmp_path):
    export_documentation("system_doc", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "SYSTEM_DOCUMENT.md").read_text(encoding="utf-8")

    assert "[Function Matrix](FUNCTION_MATRIX.md)" in body
    assert "flowchart TD" in body
    assert "Unenforced write paths" in body
    assert "Living memory not supplied" in body  # no history in this fixture


def test_documents_carry_a_regeneration_banner(project, tmp_path):
    export_documentation("all", output_dir=tmp_path, project_root=project)
    for module in GENERATORS.values():
        body = (tmp_path / module.OUTPUT).read_text(encoding="utf-8")
        assert "Generated by rtk-sf" in body


def test_timeline_sections_render_when_history_exists(project, tmp_path):
    from rtk_sf.memory import HistoryManager

    history = HistoryManager(project)
    history.record_turn("added submit flow", files=["OrderController.cls"], insertions=12)

    export_documentation("system_doc", output_dir=tmp_path, project_root=project)
    body = (tmp_path / "SYSTEM_DOCUMENT.md").read_text(encoding="utf-8")
    assert "added submit flow" in body
    assert "Trailing week" in body

    history.path.unlink()  # keep the module-scoped project clean for other tests
