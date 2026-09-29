"""
extract.py — behavioral fact extraction for the rtk-sf documentation engine.

The YAML spec index is structural: it knows a class has a method, but not that
the method is `@AuraEnabled`, updates Opportunity in user mode, or is reached
from an LWC through an RTK Query endpoint. Sequence diagrams, use cases and
business scenarios need exactly those facts, so this module re-reads the source
files the index points at and extracts the execution-boundary signals:

    Apex   → annotations, DML/SOQL per method, security posture, call graph
    LWC    → @salesforce/apex imports, @wire adapters, events, store dispatches
    Redux  → createSlice / createAsyncThunk / createApi endpoints
    Schema → objects, fields (incl. formulas + picklists), validation rules

Everything is assembled into a ProjectModel that the generators render. Nothing
here writes files, and nothing is invented: a signal absent from the source is
absent from the model, so generators can mark it as undetermined rather than
guess.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rtk_sf.skeleton import _METHOD_SIG, _find_block_end

logger = logging.getLogger(__name__)

RTK_DIR = ".rtk-sf"
SPECS_DIR = "specs"

# Directories never worth walking when hunting for source files.
_SKIP_DIRS = {
    ".git", ".rtk-sf", "node_modules", "dist", "build", "coverage",
    "__pycache__", ".venv", "venv", ".sfdx", ".sf", ".localdevserver",
}

# Files larger than this are skipped — generated bundles, not hand-written code.
_MAX_SOURCE_BYTES = 400_000

# ---------------------------------------------------------------------------
# Apex regexes
# ---------------------------------------------------------------------------

_APEX_CLASS_DECL = re.compile(
    r"(?P<mods>(?:(?:public|global|private|protected|virtual|abstract|static)\s+)*)"
    r"(?P<sharing>with\s+sharing|without\s+sharing|inherited\s+sharing)?\s*"
    r"class\s+(?P<name>\w+)",
    re.IGNORECASE,
)
_ANNOTATION = re.compile(r"@(\w+)\s*(\([^)]*\))?")
_SOQL = re.compile(r"\[\s*(SELECT\b.*?)\]", re.DOTALL | re.IGNORECASE)
_SOQL_FROM = re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_DML = re.compile(
    r"\b(insert|update|upsert|delete|undelete)\s+(?:(as\s+(?:user|system))\s+)?"
    r"(new\s+[^;]+|[A-Za-z_][\w.]*)",
    re.IGNORECASE,
)
_DATABASE_DML = re.compile(
    r"\bDatabase\.(insert|update|upsert|delete|undelete|merge|convertLead|query)\s*\(([^;]*)",
    re.IGNORECASE,
)
_ACCESS_LEVEL = re.compile(r"AccessLevel\.(USER|SYSTEM)_MODE", re.IGNORECASE)
_DECL_COLLECTION = re.compile(r"\b(?:List|Set)\s*<\s*([A-Za-z_]\w*)\s*>\s+(\w+)", re.IGNORECASE)
_DECL_MAP = re.compile(r"\bMap\s*<[^,]+,\s*([A-Za-z_]\w*)\s*>\s+(\w+)", re.IGNORECASE)
_DECL_NEW = re.compile(r"\b([A-Z]\w*)\s+(\w+)\s*=\s*new\s+\1\s*\(")
_DECL_PLAIN = re.compile(r"\b([A-Z]\w*)\s+(\w+)\s*(?:=|,|\)|;)")
_NEW_COLLECTION = re.compile(
    r"new\s+(?:List|Set|Map)\s*<\s*([\w.]+)\s*(?:,\s*([\w.]+)\s*)?>", re.IGNORECASE
)
_NEW_INSTANCE = re.compile(r"new\s+([A-Za-z_]\w*)\s*[({]")
_SOBJECT_SUFFIX = re.compile(r"__(c|x|mdt|e|b|share|history|feed|tag)$", re.IGNORECASE)

# Tokens that can appear where a DML target is expected but name no object.
_NOT_A_TARGET = {
    "new", "this", "super", "null", "true", "false", "void", "return",
    "the", "a", "an", "and", "or", "if", "else", "for", "while", "all",
}

_THROW = re.compile(r"\bthrow\s+new\s+(\w+)")
_CALL = re.compile(r"\b([A-Z]\w+)\.(\w+)\s*\(")
_CALLOUT = re.compile(r"\b(?:HttpRequest|HttpResponse|WebServiceCallout)\b")

# Security-relevant markers, in the order we want them reported.
_SECURITY_MARKERS = (
    ("WITH SECURITY_ENFORCED", re.compile(r"WITH\s+SECURITY_ENFORCED", re.IGNORECASE)),
    ("WITH USER_MODE", re.compile(r"WITH\s+USER_MODE", re.IGNORECASE)),
    ("WITH SYSTEM_MODE", re.compile(r"WITH\s+SYSTEM_MODE", re.IGNORECASE)),
    ("stripInaccessible", re.compile(r"stripInaccessible", re.IGNORECASE)),
    ("AccessLevel.USER_MODE", re.compile(r"AccessLevel\.USER_MODE", re.IGNORECASE)),
    (
        "DML as user",
        re.compile(r"\b(?:insert|update|upsert|delete|undelete)\s+as\s+user\b", re.IGNORECASE),
    ),
    (
        "DML as system",
        re.compile(r"\b(?:insert|update|upsert|delete|undelete)\s+as\s+system\b", re.IGNORECASE),
    ),
    ("AccessLevel.SYSTEM_MODE", re.compile(r"AccessLevel\.SYSTEM_MODE", re.IGNORECASE)),
    ("CRUD/FLS describe check", re.compile(r"\.is(?:Accessible|Createable|Updateable|Deletable)\s*\(", re.IGNORECASE)),
)

# Markers that declare an access mode without enforcing anything. They are worth
# reporting, but counting them as enforcement would overstate a project's
# posture: `WITH SYSTEM_MODE` is an explicit opt-out of FLS, not a check.
_NON_ENFORCING_MARKERS = {"WITH SYSTEM_MODE", "DML as system", "AccessLevel.SYSTEM_MODE"}

# System namespaces that are not project components — excluded from call graphs.
_SYSTEM_NAMESPACES = {
    "system", "database", "schema", "string", "math", "json", "test", "date",
    "datetime", "decimal", "integer", "boolean", "long", "double", "blob",
    "list", "map", "set", "http", "userinfo", "limits", "trigger", "apexpages",
    "label", "accesslevel", "security", "messaging", "type", "encodingutil",
    "crypto", "url", "pattern", "matcher", "cache", "approval", "auth",
    "search", "eventbus", "flow", "id", "sobject", "object", "exception",
    "page", "site", "network", "connectapi", "quickaction", "reports",
    "sobjecttype", "sobjectfield", "describefieldresult", "describesobjectresult",
    "string", "sobjectaccessdecision",
}

# ---------------------------------------------------------------------------
# LWC / Redux regexes
# ---------------------------------------------------------------------------

_LWC_APEX_IMPORT = re.compile(r"import\s+(\w+)\s+from\s+['\"]@salesforce/apex/([\w.]+)['\"]")
_LWC_SCHEMA_IMPORT = re.compile(r"from\s+['\"]@salesforce/schema/([\w.]+)['\"]")
_LWC_WIRE = re.compile(r"@wire\s*\(\s*(\w+)")
_LWC_CUSTOM_EVENT = re.compile(r"new\s+CustomEvent\s*\(\s*['\"]([\w.-]+)['\"]")
_LWC_DISPATCH = re.compile(r"\bdispatch\s*\(\s*(\w+)\s*\(")
_LWC_HTML_HANDLER = re.compile(r"\son([a-z]+)=\{(\w+)\}")
_LWC_UI_API = re.compile(
    r"\b(createRecord|updateRecord|deleteRecord|getRecord|getRecords|getListUi|getObjectInfo)\b"
)
_LWC_RTK_HOOK = re.compile(r"\b(useSelector|useDispatch|use\w+(?:Query|Mutation))\s*\(")

_REDUX_SLICE = re.compile(r"createSlice\s*\(\s*\{(?P<body>.*?)\n\s*\}\s*\)", re.DOTALL)
_REDUX_SLICE_NAME = re.compile(r"name\s*:\s*['\"](\w+)['\"]")
_REDUX_REDUCERS_BLOCK = re.compile(r"reducers\s*:\s*\{")
_REDUX_THUNK = re.compile(
    r"(?:export\s+)?const\s+(\w+)\s*=\s*createAsyncThunk\s*(?:<[^>]*>)?\s*\(\s*['\"]([\w/.-]+)['\"]"
)
_RTK_CREATE_API = re.compile(r"(?:export\s+)?const\s+(\w+)\s*=\s*createApi\s*\(\s*\{")
_RTK_REDUCER_PATH = re.compile(r"reducerPath\s*:\s*['\"]([\w.-]+)['\"]")
_RTK_ENDPOINT = re.compile(r"(\w+)\s*:\s*builder\.(query|mutation)\s*[(<]")
_JS_KEY = re.compile(r"^\s*(?:async\s+)?(\w+)\s*[:(]", re.MULTILINE)


# ---------------------------------------------------------------------------
# Fact containers
# ---------------------------------------------------------------------------


@dataclass
class DbOp:
    """One database interaction inside an Apex method."""

    kind: str          # insert | update | upsert | delete | undelete | query | merge
    sobject: str       # resolved sObject name, or the raw variable when unresolvable
    mode: str = ""     # user | system | security_enforced | "" when not stated

    def describe(self) -> str:
        label = f"{self.kind.upper()} {self.sobject}".strip()
        return f"{label} [{self.mode}]" if self.mode else label


@dataclass
class ApexMethod:
    name: str
    returns: str = ""
    params: list[str] = field(default_factory=list)
    description: str = ""
    annotations: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    db_ops: list[DbOp] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    throws: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    has_try: bool = False
    has_callout: bool = False

    @property
    def is_aura_enabled(self) -> bool:
        return any(a.lower().startswith("auraenabled") for a in self.annotations)

    @property
    def is_cacheable(self) -> bool:
        return any("cacheable=true" in a.lower().replace(" ", "") for a in self.annotations)

    @property
    def is_invocable(self) -> bool:
        return any(a.lower().startswith("invocablemethod") for a in self.annotations)

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(self.params)})"

    @property
    def enforced_security(self) -> list[str]:
        """Markers that actually enforce access, excluding system-mode opt-outs."""
        return [m for m in self.security if m not in _NON_ENFORCING_MARKERS]

    @property
    def writes(self) -> list[DbOp]:
        return [op for op in self.db_ops if op.kind != "query"]

    @property
    def reads(self) -> list[DbOp]:
        return [op for op in self.db_ops if op.kind == "query"]

    @property
    def sobjects(self) -> list[str]:
        return list(dict.fromkeys(op.sobject for op in self.db_ops if op.sobject))


@dataclass
class ApexClassFacts:
    name: str
    file: str = ""
    path: Path | None = None
    summary: str = ""
    sharing: str = ""
    methods: list[ApexMethod] = field(default_factory=list)
    is_test: bool = False
    annotations: list[str] = field(default_factory=list)

    def method(self, name: str) -> ApexMethod | None:
        for candidate in self.methods:
            if candidate.name == name:
                return candidate
        return None

    @property
    def aura_methods(self) -> list[ApexMethod]:
        return [m for m in self.methods if m.is_aura_enabled]

    @property
    def sobjects(self) -> list[str]:
        out: list[str] = []
        for method in self.methods:
            out.extend(method.sobjects)
        return list(dict.fromkeys(out))


@dataclass
class LwcFacts:
    name: str
    file: str = ""
    path: Path | None = None
    targets: list[str] = field(default_factory=list)
    exposed: bool = False
    api_properties: list[str] = field(default_factory=list)
    public_methods: list[str] = field(default_factory=list)
    apex_calls: list[str] = field(default_factory=list)      # "Controller.method"
    wired: list[str] = field(default_factory=list)
    schema_refs: list[str] = field(default_factory=list)     # "Object.Field"
    events_published: list[str] = field(default_factory=list)
    events_handled: list[str] = field(default_factory=list)
    handler_bindings: list[tuple[str, str]] = field(default_factory=list)  # (event, handler)
    store_dispatches: list[str] = field(default_factory=list)
    rtk_hooks: list[str] = field(default_factory=list)
    ui_api_calls: list[str] = field(default_factory=list)
    child_refs: list[str] = field(default_factory=list)
    uses_navigation: bool = False
    uses_toast: bool = False
    has_template: bool = False

    @property
    def objects(self) -> list[str]:
        return list(dict.fromkeys(ref.split(".")[0] for ref in self.schema_refs if ref))

    @property
    def uses_store(self) -> bool:
        return bool(self.store_dispatches or self.rtk_hooks)

    def events_for(self, handler: str) -> list[str]:
        """Template events bound to one handler method."""
        return [event for event, bound in self.handler_bindings if bound == handler]

    @property
    def is_ui(self) -> bool:
        """True for renderable components; false for store/util-only bundles."""
        return self.has_template or self.exposed or bool(self.targets)


@dataclass
class ReduxThunk:
    name: str
    action_type: str = ""
    file: str = ""
    apex_calls: list[str] = field(default_factory=list)


@dataclass
class ReduxSlice:
    name: str
    file: str = ""
    reducers: list[str] = field(default_factory=list)
    thunks: list[str] = field(default_factory=list)


@dataclass
class RtkEndpoint:
    api: str
    name: str
    kind: str                                                # query | mutation
    file: str = ""
    apex_calls: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.api}.{self.name}"


@dataclass
class FieldFacts:
    name: str
    object_name: str = ""
    field_type: str = ""
    label: str = ""
    required: bool = False
    description: str = ""
    formula: str = ""
    picklist_values: list[str] = field(default_factory=list)
    reference_to: str = ""
    length: str = ""
    default_value: str = ""


@dataclass
class ValidationRuleFacts:
    name: str
    object_name: str = ""
    active: bool = True
    description: str = ""
    error_message: str = ""
    formula: str = ""


@dataclass
class ObjectFacts:
    name: str
    label: str = ""
    plural_label: str = ""
    description: str = ""
    fields: list[FieldFacts] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    validation_rules: list[ValidationRuleFacts] = field(default_factory=list)
    record_types: list[str] = field(default_factory=list)

    @property
    def lookups(self) -> list[FieldFacts]:
        return [f for f in self.fields if f.reference_to]


@dataclass
class TriggerFacts:
    name: str
    sobject: str = ""
    events: list[str] = field(default_factory=list)
    file: str = ""
    handler_calls: list[str] = field(default_factory=list)


@dataclass
class FlowFacts:
    name: str
    label: str = ""
    process_type: str = ""
    status: str = ""
    description: str = ""
    elements: dict[str, int] = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return any(
            self.elements.get(key)
            for key in ("recordCreates", "recordUpdates", "recordDeletes")
        )


@dataclass
class GenericComponent:
    """Any indexed component we only need shallow spec data for."""

    name: str
    type: str
    file: str = ""
    spec: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectModel:
    """Everything the generators need, assembled once per export run."""

    project_root: Path
    apex: dict[str, ApexClassFacts] = field(default_factory=dict)
    lwc: dict[str, LwcFacts] = field(default_factory=dict)
    slices: list[ReduxSlice] = field(default_factory=list)
    thunks: list[ReduxThunk] = field(default_factory=list)
    endpoints: list[RtkEndpoint] = field(default_factory=list)
    objects: dict[str, ObjectFacts] = field(default_factory=dict)
    triggers: list[TriggerFacts] = field(default_factory=list)
    flows: list[FlowFacts] = field(default_factory=list)
    others: dict[str, list[GenericComponent]] = field(default_factory=dict)
    annotations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # -- lookups shared by several generators ------------------------------

    def components_of(self, type_name: str) -> list[GenericComponent]:
        return self.others.get(type_name, [])

    def aura_methods(self) -> list[tuple[ApexClassFacts, ApexMethod]]:
        """Every `@AuraEnabled` method in the project, class-then-method order."""
        pairs: list[tuple[ApexClassFacts, ApexMethod]] = []
        for cls in self.apex.values():
            for method in cls.aura_methods:
                pairs.append((cls, method))
        return sorted(pairs, key=lambda pair: (pair[0].name, pair[1].name))

    def lwc_callers_of(self, qualified: str) -> list[str]:
        """LWC bundles importing a given 'Controller.method'."""
        return sorted(c.name for c in self.lwc.values() if qualified in c.apex_calls)

    def thunk_callers_of(self, qualified: str) -> list[str]:
        return sorted(t.name for t in self.thunks if qualified in t.apex_calls)

    def endpoint_callers_of(self, qualified: str) -> list[str]:
        return sorted(e.qualified for e in self.endpoints if qualified in e.apex_calls)

    def triggers_on(self, sobject: str) -> list[TriggerFacts]:
        return [t for t in self.triggers if t.sobject.lower() == (sobject or "").lower()]

    def annotation_text(self, component: str) -> list[str]:
        return [
            f"{row.get('key')}: {row.get('value')}"
            for row in self.annotations.get(component, [])
            if row.get("value")
        ]

    def counts(self) -> dict[str, int]:
        counts = {
            "ApexClass": len(self.apex),
            "LightningComponentBundle": len(self.lwc),
            "CustomObject": len(self.objects),
            "ApexTrigger": len(self.triggers),
            "Flow": len(self.flows),
        }
        for type_name, items in self.others.items():
            counts[type_name] = len(items)
        return {k: v for k, v in sorted(counts.items()) if v}

    @property
    def is_empty(self) -> bool:
        return not (self.apex or self.lwc or self.objects or self.others or self.triggers)


# ---------------------------------------------------------------------------
# Source file discovery
# ---------------------------------------------------------------------------


def _walk(project_root: Path):
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        yield root, dirs, files


def _build_file_index(project_root: Path) -> dict[str, list[Path]]:
    """Map basename → paths, walking the project once."""
    index: dict[str, list[Path]] = {}
    for root, _dirs, files in _walk(project_root):
        for fname in files:
            index.setdefault(fname, []).append(Path(root) / fname)
    return index


def _read(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        if path.is_dir() or path.stat().st_size > _MAX_SOURCE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resolve_under_object(
    object_name: str,
    file_ref: str,
    file_index: dict[str, list[Path]],
) -> Path | None:
    """
    Resolve a field/record-type file inside a specific object's folder.

    Field specs record only a basename, and the same field API name (Status__c,
    Name, …) exists under many objects. Resolving by basename alone attributes a
    field to whichever object the filesystem walk happened to reach first, which
    silently moves fields — and their labels, picklists and required flags —
    onto the wrong object.
    """
    if not object_name or not file_ref:
        return None
    for candidate in file_index.get(Path(file_ref).name, []):
        parts = candidate.parts
        if "objects" in parts:
            idx = parts.index("objects")
            if idx + 1 < len(parts) and parts[idx + 1] == object_name:
                return candidate
    return None


def _candidate_owners(file_ref: str, file_index: dict[str, list[Path]]) -> list[str]:
    """Every object whose folder holds a file with this basename."""
    owners = []
    for candidate in file_index.get(Path(file_ref).name, []):
        owner = _owning_object(candidate)
        if owner and owner not in owners:
            owners.append(owner)
    return owners


def _resolve(file_ref: str, project_root: Path, file_index: dict[str, list[Path]]) -> Path | None:
    """Resolve a spec's `file` value (often a bare basename) to a real path."""
    if not file_ref:
        return None
    cleaned = file_ref.rstrip("/")
    candidate = Path(cleaned)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    relative = project_root / cleaned
    if relative.exists():
        return relative
    matches = file_index.get(candidate.name, [])
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Apex extraction
# ---------------------------------------------------------------------------


def _strip_noise(source: str) -> str:
    """
    Blank out Apex comments and string literals before pattern scanning.

    Without this, prose is mistaken for code: a real project contained the
    comment `// Get data for insert the ContactRole`, which the DML pattern read
    as an insert whose target was the word "the". String bodies go too, so a
    query held in a literal cannot masquerade as an inline SOQL statement.
    """
    out: list[str] = []
    i = 0
    length = len(source)
    while i < length:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < length else ""

        if ch == "/" and nxt == "/":
            while i < length and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < length and not (source[i] == "*" and source[i + 1 : i + 2] == "/"):
                i += 1
            i += 2
            continue
        if ch == "'":
            i += 1
            while i < length:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _method_bodies(source: str) -> list[tuple[Any, str]]:
    """Pair each method signature match with its balanced-brace body."""
    out: list[tuple[Any, str]] = []
    for match in _METHOD_SIG.finditer(source):
        name = match.group("name")
        if name.lower() in {"if", "for", "while", "catch", "switch", "else", "try"}:
            continue
        open_pos = match.start("body_start")
        end = _find_block_end(source, open_pos)
        out.append((match, source[open_pos + 1 : end]))
    return out


def _var_types(text: str) -> dict[str, str]:
    """Best-effort variable → type map, used to resolve DML targets to sObjects."""
    types: dict[str, str] = {}
    for pattern in (_DECL_COLLECTION, _DECL_MAP, _DECL_NEW, _DECL_PLAIN):
        for type_name, var in pattern.findall(text):
            types.setdefault(var, type_name)
    return types


def _security_markers(text: str) -> list[str]:
    return [label for label, pattern in _SECURITY_MARKERS if pattern.search(text)]


def _resolve_sobject(target: str, var_types: dict[str, str]) -> str:
    """
    Turn a DML target expression into an sObject name, or "" when unresolvable.

    Handles inline construction (`insert new Order__c(...)`,
    `Database.insert(new List<Contact>{ c })`) and refuses to guess: a local
    variable whose declared type was not found yields "" rather than being
    printed as though it were an object name.
    """
    text = target.strip()

    collection = _NEW_COLLECTION.search(text)
    if collection:
        # Map<Id, Account> — the value type is the object being written.
        return collection.group(2) or collection.group(1)
    instance = _NEW_INSTANCE.search(text)
    if instance:
        return instance.group(1)

    # Otherwise the target is the first argument; later ones are flags such as
    # allOrNone or AccessLevel.
    token = text.split(",")[0].strip("()").split(".")[0].strip()
    if not token or token.lower() in _NOT_A_TARGET:
        return ""
    if token in var_types:
        return var_types[token]
    # sObject names are capitalized, or namespaced like ns__Object__c.
    if token[0].isupper() or _SOBJECT_SUFFIX.search(token):
        return token
    return ""


def _db_ops(body: str, var_types: dict[str, str]) -> list[DbOp]:
    ops: list[DbOp] = []

    for raw_query in _SOQL.findall(body):
        from_match = _SOQL_FROM.search(raw_query)
        mode = ""
        if re.search(r"WITH\s+USER_MODE", raw_query, re.IGNORECASE):
            mode = "user"
        elif re.search(r"WITH\s+SYSTEM_MODE", raw_query, re.IGNORECASE):
            mode = "system"
        elif re.search(r"WITH\s+SECURITY_ENFORCED", raw_query, re.IGNORECASE):
            mode = "security_enforced"
        ops.append(DbOp("query", from_match.group(1) if from_match else "", mode))

    for kind, as_mode, target in _DML.findall(body):
        mode = ""
        if as_mode:
            mode = "user" if "user" in as_mode.lower() else "system"
        ops.append(DbOp(kind.lower(), _resolve_sobject(target, var_types), mode))

    for kind, args in _DATABASE_DML.findall(body):
        resolved = _resolve_sobject(args.strip().strip("'\""), var_types)
        # Database.insert(rows, true, AccessLevel.USER_MODE) states its mode.
        level = _ACCESS_LEVEL.search(args)
        mode = level.group(1).lower() if level else ""
        ops.append(DbOp("query" if kind.lower() == "query" else kind.lower(), resolved, mode))

    seen: set[tuple[str, str, str]] = set()
    unique: list[DbOp] = []
    for op in ops:
        key = (op.kind, op.sobject, op.mode)
        if key not in seen:
            seen.add(key)
            unique.append(op)
    return unique


def _calls(body: str, own_class: str) -> list[str]:
    out: list[str] = []
    for cls, method in _CALL.findall(body):
        if cls.lower() in _SYSTEM_NAMESPACES or cls == own_class:
            continue
        out.append(f"{cls}.{method}")
    return list(dict.fromkeys(out))


def _format_annotation(match: Any) -> str:
    args = (match.group(2) or "").strip()
    return f"{match.group(1)}{args}"


def extract_apex(name: str, spec: dict[str, Any], path: Path | None) -> ApexClassFacts:
    """Build Apex facts from the YAML spec plus, when reachable, the source."""
    facts = ApexClassFacts(
        name=name,
        file=str(spec.get("file", "")),
        path=path,
        summary=str(spec.get("summary", "") or "").strip(),
        is_test=name.lower().endswith("test") or name.lower().startswith("test"),
    )
    if facts.summary == "(no description)":
        facts.summary = ""

    # Spec-only baseline: names, return types, params, ApexDoc descriptions.
    spec_methods: dict[str, ApexMethod] = {}
    for entry in spec.get("methods") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        spec_methods[str(entry["name"])] = ApexMethod(
            name=str(entry["name"]),
            returns=str(entry.get("returns", "")),
            params=[str(p) for p in entry.get("params", []) or []],
            description=str(entry.get("description", "")),
        )

    source = _strip_noise(_read(path))
    if not source:
        facts.methods = list(spec_methods.values())
        return facts

    decl = _APEX_CLASS_DECL.search(source)
    if decl:
        facts.sharing = (decl.group("sharing") or "").strip()
        header = source[: decl.start()]
        class_scope = source[: decl.end()]
    else:
        header = source[:2000]
        class_scope = source[:2000]
    facts.annotations = [_format_annotation(m) for m in _ANNOTATION.finditer(header)]
    facts.is_test = facts.is_test or any(a.lower().startswith("istest") for a in facts.annotations)

    class_var_types = _var_types(class_scope)

    methods: list[ApexMethod] = []
    for match, body in _method_bodies(source):
        method_name = match.group("name")
        method = spec_methods.pop(method_name, None) or ApexMethod(name=method_name)
        if not method.returns:
            method.returns = (match.group("ret") or "").strip()
        if not method.params and match.group("params").strip():
            method.params = [p.strip() for p in match.group("params").split(",") if p.strip()]

        mods = match.group("mods") or ""
        method.annotations = [_format_annotation(m) for m in _ANNOTATION.finditer(mods)]
        method.modifiers = re.findall(
            r"\b(public|global|private|protected|static|override|virtual|abstract)\b",
            mods,
            re.IGNORECASE,
        )
        var_types = dict(class_var_types)
        var_types.update(_var_types(match.group("params") + "\n" + body))
        method.db_ops = _db_ops(body, var_types)
        method.security = _security_markers(body)
        method.throws = list(dict.fromkeys(_THROW.findall(body)))
        method.calls = _calls(body, name)
        method.has_try = "try" in body and "catch" in body
        method.has_callout = bool(_CALLOUT.search(body))
        methods.append(method)

    # Signatures the brace scanner could not reach (interface/abstract stubs).
    methods.extend(spec_methods.values())
    facts.methods = methods
    return facts


# ---------------------------------------------------------------------------
# LWC extraction
# ---------------------------------------------------------------------------


def extract_lwc(name: str, spec: dict[str, Any], bundle_dir: Path | None) -> LwcFacts:
    facts = LwcFacts(
        name=name,
        file=str(spec.get("file", "")),
        path=bundle_dir,
        targets=[str(t) for t in spec.get("targets", []) or []],
        api_properties=[str(p) for p in spec.get("apiProperties", []) or []],
        public_methods=[str(p) for p in spec.get("publicMethods", []) or []],
        child_refs=[str(c) for c in spec.get("componentRefs", []) or []],
    )
    if bundle_dir is None or not bundle_dir.is_dir():
        return facts

    for file_path in sorted(bundle_dir.iterdir()):
        if file_path.suffix in {".js", ".ts"} and not file_path.name.endswith(
            (".test.js", ".test.ts")
        ):
            js = _read(file_path)
            if not js:
                continue
            facts.apex_calls.extend(q for _local, q in _LWC_APEX_IMPORT.findall(js))
            facts.schema_refs.extend(_LWC_SCHEMA_IMPORT.findall(js))
            facts.wired.extend(_LWC_WIRE.findall(js))
            facts.events_published.extend(_LWC_CUSTOM_EVENT.findall(js))
            facts.store_dispatches.extend(_LWC_DISPATCH.findall(js))
            facts.rtk_hooks.extend(_LWC_RTK_HOOK.findall(js))
            facts.ui_api_calls.extend(_LWC_UI_API.findall(js))
            facts.uses_navigation = facts.uses_navigation or "lightning/navigation" in js
            facts.uses_toast = facts.uses_toast or "ShowToastEvent" in js

        elif file_path.suffix == ".html":
            facts.has_template = True
            for event, handler in _LWC_HTML_HANDLER.findall(_read(file_path)):
                facts.events_handled.append(event)
                facts.handler_bindings.append((event, handler))

        elif file_path.name.endswith("-meta.xml"):
            meta = _read(file_path)
            facts.exposed = "<isExposed>true</isExposed>" in meta.replace(" ", "")
            if not facts.targets:
                facts.targets = re.findall(r"<target>(.*?)</target>", meta)

    facts.handler_bindings = list(dict.fromkeys(facts.handler_bindings))
    for attr in (
        "apex_calls", "schema_refs", "wired", "events_published",
        "events_handled", "store_dispatches", "rtk_hooks", "ui_api_calls",
    ):
        setattr(facts, attr, list(dict.fromkeys(getattr(facts, attr))))
    return facts


# ---------------------------------------------------------------------------
# Redux / RTK Query extraction
# ---------------------------------------------------------------------------


def _balanced(source: str, open_pos: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """Index of the delimiter closing the one at open_pos, ignoring string bodies."""
    depth = 0
    in_str = False
    quote = ""
    i = open_pos
    while i < len(source):
        ch = source[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                in_str = False
        elif ch in "'\"`":
            in_str = True
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(source) - 1


def _apex_refs_in(text: str, local_map: dict[str, str], fallback: list[str]) -> list[str]:
    """
    Qualified Apex methods referenced inside one construct.

    A file can import several Apex methods, so attributing all of them to every
    thunk or endpoint in that file would be wrong. Only imports whose local
    identifier actually appears in the construct's own body are credited; when a
    construct references none directly, the file-level imports are used as the
    (clearly weaker) fallback so indirection still shows a boundary.
    """
    hits = [
        qualified
        for local, qualified in local_map.items()
        if re.search(rf"\b{re.escape(local)}\b", text)
    ]
    return list(dict.fromkeys(hits)) or list(fallback)


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _reducer_keys(slice_body: str) -> list[str]:
    """Extract reducer names from a createSlice({ reducers: { … } }) block."""
    match = _REDUX_REDUCERS_BLOCK.search(slice_body)
    if not match:
        return []
    start = slice_body.find("{", match.end() - 1)
    if start == -1:
        return []
    end = _find_block_end(slice_body, start)
    inner = slice_body[start + 1 : end]
    keys = [k for k in _JS_KEY.findall(inner) if k not in {"state", "action", "return"}]
    return list(dict.fromkeys(keys))


def extract_redux(
    project_root: Path,
) -> tuple[list[ReduxSlice], list[ReduxThunk], list[RtkEndpoint]]:
    """
    Scan JS/TS sources for Redux Toolkit constructs.

    Redux state lives outside the Salesforce metadata index entirely, so this
    walks the project tree directly rather than reading specs.
    """
    slices: list[ReduxSlice] = []
    thunks: list[ReduxThunk] = []
    endpoints: list[RtkEndpoint] = []

    for root, _dirs, files in _walk(project_root):
        for fname in files:
            if not fname.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs")):
                continue
            if fname.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
                continue
            path = Path(root) / fname
            source = _read(path)
            if not source or not any(
                marker in source for marker in ("createSlice", "createAsyncThunk", "createApi")
            ):
                continue

            rel = _relpath(path, project_root)
            local_map = {local: q for local, q in _LWC_APEX_IMPORT.findall(source)}
            apex_in_file = list(dict.fromkeys(local_map.values()))
            file_thunks = _REDUX_THUNK.findall(source)

            for thunk_match in _REDUX_THUNK.finditer(source):
                open_paren = source.find("(", thunk_match.start())
                body = source[thunk_match.start() : _balanced(source, open_paren) + 1]
                thunks.append(
                    ReduxThunk(
                        name=thunk_match.group(1),
                        action_type=thunk_match.group(2),
                        file=rel,
                        apex_calls=_apex_refs_in(body, local_map, apex_in_file),
                    )
                )

            for slice_match in _REDUX_SLICE.finditer(source):
                body = slice_match.group("body")
                name_match = _REDUX_SLICE_NAME.search(body)
                slices.append(
                    ReduxSlice(
                        name=name_match.group(1) if name_match else path.stem,
                        file=rel,
                        reducers=_reducer_keys(body),
                        thunks=[t for t, _ in file_thunks],
                    )
                )

            for api_match in _RTK_CREATE_API.finditer(source):
                # Confine the scan to this createApi's own object literal so a
                # second API definition later in the file is not absorbed.
                brace = source.rfind("{", api_match.start(), api_match.end())
                block = source[brace : _balanced(source, brace, "{", "}") + 1]
                path_match = _RTK_REDUCER_PATH.search(block[:400])
                api_label = path_match.group(1) if path_match else api_match.group(1)
                for ep_match in _RTK_ENDPOINT.finditer(block):
                    ep_open = block.find("(", ep_match.end() - 1)
                    ep_body = block[ep_match.start() : _balanced(block, ep_open) + 1]
                    endpoints.append(
                        RtkEndpoint(
                            api=api_label,
                            name=ep_match.group(1),
                            kind=ep_match.group(2),
                            file=rel,
                            apex_calls=_apex_refs_in(ep_body, local_map, []),
                        )
                    )

    return slices, thunks, endpoints


# ---------------------------------------------------------------------------
# Schema extraction
# ---------------------------------------------------------------------------


def _owning_object(path: Path | None) -> str:
    """Derive the owning object name from a metadata file's path."""
    if path is None:
        return ""
    parts = path.parts
    if "objects" in parts:
        idx = parts.index("objects")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def _value_set(xml: str) -> str:
    match = re.search(r"<valueSet>(.*?)</valueSet>", xml, re.DOTALL)
    return match.group(1) if match else ""


def _field_from_spec(
    spec: dict[str, Any],
    path: Path | None,
    object_name: str = "",
) -> FieldFacts:
    facts = FieldFacts(
        name=str(spec.get("fullName") or spec.get("component") or ""),
        object_name=object_name or _owning_object(path),
        field_type=str(spec.get("field_type") or spec.get("type") or ""),
        label=str(spec.get("label", "")),
        required=bool(spec.get("required", False)),
        description=str(spec.get("description", "")),
        reference_to=str(spec.get("referenceTo", "")),
        length=str(spec.get("length", "")),
        default_value=str(spec.get("defaultValue", "")),
    )
    if facts.field_type == "CustomField":
        facts.field_type = ""

    # Formulas and picklist values are not carried in the index — read the XML.
    xml = _read(path)
    if xml:
        formula = re.search(r"<formula>(.*?)</formula>", xml, re.DOTALL)
        if formula:
            facts.formula = formula.group(1).strip()
        facts.picklist_values = re.findall(r"<fullName>([^<]+)</fullName>", _value_set(xml))
        if not facts.field_type:
            type_match = re.search(r"<type>(.*?)</type>", xml)
            facts.field_type = type_match.group(1).strip() if type_match else ""
    return facts


# ---------------------------------------------------------------------------
# Model assembly
# ---------------------------------------------------------------------------


def load_specs(rtk_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Read every YAML spec in the index."""
    import yaml

    specs_dir = rtk_dir / SPECS_DIR
    if not specs_dir.is_dir():
        return []

    out: list[tuple[str, dict[str, Any]]] = []
    for spec_file in sorted(specs_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Skipping unreadable spec %s: %s", spec_file, exc)
            continue
        if isinstance(data, dict):
            out.append((str(data.get("component") or spec_file.stem), data))
    return out


def _merge_field(into: FieldFacts, extra: FieldFacts) -> None:
    """
    Fold a duplicate field entry into the one already held.

    An object's inline `<fields>` block and its `fields/` directory can both
    describe the same field; the directory entry carries the richer data.
    """
    for attr in ("field_type", "label", "description", "formula", "reference_to",
                 "length", "default_value"):
        if not getattr(into, attr) and getattr(extra, attr):
            setattr(into, attr, getattr(extra, attr))
    into.required = into.required or extra.required
    if not into.picklist_values and extra.picklist_values:
        into.picklist_values = extra.picklist_values


def _find_lwc_dir(name: str, file_index: dict[str, list[Path]]) -> Path | None:
    for candidate in file_index.get(f"{name}.js", []):
        if candidate.parent.name == name:
            return candidate.parent
    return None


def _load_annotations(root: Path, names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Pull human-written annotations out of the search index, if present."""
    if not (root / RTK_DIR / "db.sqlite").exists():
        return {}
    try:
        from rtk_sf.search import SearchEngine
    except ImportError:  # pragma: no cover — search is a core module
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with SearchEngine(root) as engine:
            for name in names:
                rows = engine.get_annotations(name)
                if rows:
                    out[name] = rows
    except Exception as exc:  # sqlite problems must not break a doc build
        logger.warning("Could not read annotations: %s", exc)
    return out


def build_model(project_root: str | Path = ".") -> ProjectModel:
    """
    Assemble the full documentation model for a project.

    Reads `.rtk-sf/specs/*.yaml` for structure, the referenced source files for
    behavior, the project tree for Redux constructs, and the SQLite annotations
    table for human-written business logic. Returns an empty model when the
    project has not been indexed yet.
    """
    root = Path(project_root).resolve()
    rtk_dir = root / RTK_DIR
    model = ProjectModel(project_root=root)

    specs = load_specs(rtk_dir)
    if not specs:
        return model

    file_index = _build_file_index(root)
    objects: dict[str, ObjectFacts] = {}
    pending_fields: list[FieldFacts] = []
    pending_rules: list[ValidationRuleFacts] = []
    pending_record_types: list[tuple[str, str]] = []

    for name, spec in specs:
        type_name = str(spec.get("type", "Unknown"))
        path = _resolve(str(spec.get("file", "")), root, file_index)

        if type_name == "ApexClass":
            model.apex[name] = extract_apex(name, spec, path)

        elif type_name == "LightningComponentBundle":
            bundle = path if path and path.is_dir() else _find_lwc_dir(name, file_index)
            model.lwc[name] = extract_lwc(name, spec, bundle)

        elif type_name == "CustomObject":
            objects[name] = ObjectFacts(
                name=name,
                label=str(spec.get("label", "")),
                plural_label=str(spec.get("pluralLabel", "")),
                description=str(spec.get("description", "")),
                relationships=[str(r) for r in spec.get("relationships", []) or []],
                fields=[
                    FieldFacts(
                        name=str(f.get("name", "")),
                        object_name=name,
                        field_type=str(f.get("type", "")),
                        label=str(f.get("label", "")),
                    )
                    for f in spec.get("fields", []) or []
                    if isinstance(f, dict)
                ],
            )

        elif type_name == "CustomField":
            # Spec names are "Object.Field", which is the only reliable owner.
            owner = name.rpartition(".")[0]
            field_path = (
                _resolve_under_object(owner, str(spec.get("file", "")), file_index) or path
            )
            pending_fields.append(_field_from_spec(spec, field_path, owner))

        elif type_name == "ValidationRule":
            pending_rules.append(
                ValidationRuleFacts(
                    name=name.split(".", 1)[-1],
                    object_name=name.split(".")[0] if "." in name else _owning_object(path),
                    active=bool(spec.get("active", True)),
                    description=str(spec.get("description", "")),
                    error_message=str(spec.get("errorMessage", "")),
                    formula=str(spec.get("errorConditionFormula", "")),
                )
            )

        elif type_name == "RecordType":
            # The index keys record types by developer name only, so a name used
            # on several objects collapses to one spec. Every object whose folder
            # holds that file genuinely has the record type.
            label = str(spec.get("label") or name)
            owners = _candidate_owners(str(spec.get("file", "")), file_index) or [
                _owning_object(path)
            ]
            for owner in owners:
                pending_record_types.append((owner, label))

        elif type_name == "ApexTrigger":
            source = _strip_noise(_read(path))
            model.triggers.append(
                TriggerFacts(
                    name=name,
                    sobject=str(spec.get("sobject", "")),
                    events=[str(e) for e in spec.get("events", []) or []],
                    file=str(spec.get("file", "")),
                    handler_calls=_calls(source, name) if source else [],
                )
            )

        elif type_name == "Flow":
            model.flows.append(
                FlowFacts(
                    name=name,
                    label=str(spec.get("label", "")),
                    process_type=str(spec.get("processType", "")),
                    status=str(spec.get("status", "")),
                    description=str(spec.get("description", "")),
                    elements={
                        str(k): int(v)
                        for k, v in (spec.get("elements") or {}).items()
                        if isinstance(v, int)
                    },
                )
            )

        else:
            model.others.setdefault(type_name, []).append(
                GenericComponent(
                    name=name,
                    type=type_name,
                    file=str(spec.get("file", "")),
                    spec=spec,
                )
            )

    # Attach fields / rules / record types to their objects, creating entries for
    # standard objects (Account, Opportunity) that have no object-meta.xml file.
    for field_facts in pending_fields:
        owner = field_facts.object_name or "(unassigned)"
        obj = objects.setdefault(owner, ObjectFacts(name=owner))
        existing = next((f for f in obj.fields if f.name == field_facts.name), None)
        if existing is None:
            obj.fields.append(field_facts)
        else:
            _merge_field(existing, field_facts)
    for rule in pending_rules:
        owner = rule.object_name or "(unassigned)"
        objects.setdefault(owner, ObjectFacts(name=owner)).validation_rules.append(rule)
    for owner, label in pending_record_types:
        key = owner or "(unassigned)"
        objects.setdefault(key, ObjectFacts(name=key)).record_types.append(label)

    model.objects = dict(sorted(objects.items()))
    model.slices, model.thunks, model.endpoints = extract_redux(root)
    model.annotations = _load_annotations(root, [n for n, _ in specs])
    return model
