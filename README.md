# rtk-sf

**Multi-Language Token Reduction Framework for Enterprise AI Agents**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.8.0-brightgreen)](https://github.com/furuCRM-Inc/rtk-sf/releases)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](https://python.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green)](https://modelcontextprotocol.io)
[![furuCRM](https://img.shields.io/badge/by-furuCRM%20Inc.-0066cc)](https://www.furucrm.com)

> **Stop wasting tokens on raw file reads. Give your AI agent a pre-indexed, multi-language knowledge layer instead.**

rtk-sf started as a Salesforce token-reduction tool and has grown into a **full multi-language framework**. It indexes your codebase, compresses class structure into structural skeletons, and serves everything via MCP stdio — so Claude Code reads 150 tokens instead of 15,000.

**v0.8.0** adds Java support alongside Python, TypeScript, and Kotlin — covering the full enterprise stack.

---

## Language Support

| Language | Extensions | MCP Tools | Skeleton savings | Build/Test masker |
|---|---|---|---|---|
| **Salesforce (Apex)** | `.cls`, `.trigger`, `.flow` | 14 tools | 85–92% | `sf_command` |
| **Java** | `.java` | `get_java_skeleton`, `run_java_build` | 80–90% | Maven + Gradle |
| **Kotlin** | `.kt`, `.kts` | `get_kotlin_skeleton`, `run_gradle` | 75–88% | Gradle |
| **TypeScript / JS** | `.ts`, `.tsx`, `.js`, `.jsx` | `get_ts_skeleton`, `run_js_tests` | 70–85% | Jest / Vitest |
| **Python** | `.py` | `get_python_skeleton`, `run_python_tests` | 65–80% | pytest |

---

## Before vs. After

```
❌  WITHOUT rtk-sf                      ✅  WITH rtk-sf
─────────────────────────────────────   ─────────────────────────────────────
Claude: "Show me OrderService.java"     Claude: "Show me OrderService.java"
  → reads OrderService.java (800 lines)  → calls get_java_skeleton()
  → reads related entity classes         → returns 80-line skeleton instantly
  → reads repository interfaces
  → reads test class for context
                                        Tokens consumed:  ~400
Tokens consumed:  ~12,000               Time:             <0.1 s
Cost (@$3/1M):    $0.036                Cost (@$3/1M):    $0.0012

                                        Savings:  97%
```

---

## Quick Start

```bash
# Install (Salesforce + all language tracks)
pip install "rtk-sf[all] @ git+https://github.com/furuCRM-Inc/rtk-sf.git"

# Or from PyPI when available
pip install rtk-sf

# Salesforce: index your project
python3 -m rtk_sf index

# Start MCP server
python3 -m rtk_sf serve
```

Add to your Claude Code MCP config (`~/.claude.json` or project `.claude.json`):

```json
{
  "mcpServers": {
    "rtk-sf": {
      "command": "python3",
      "args": ["-m", "rtk_sf", "serve"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

---

## How Each Language Track Works

### Java — Structural Skeleton

For a 800-line Spring Boot `OrderService.java`, `get_java_skeleton` returns:

```java
// Java skeleton: OrderService.java
// Tokens: ~420 (vs ~2,100 raw, 80% saved)

package com.example.service;

import com.example.model.Order;
import com.example.repository.OrderRepository;
import org.springframework.stereotype.Service;

@Service
public class OrderService {
  private final OrderRepository repository;

  public OrderService(OrderRepository repository) {
    this.repository = repository;
  }

  public Order findById(String id) { /* logic hidden */ }
  public List<Order> findAll() { /* boilerplate */ }
  public Order save(Order order) { /* logic hidden */ }
  public void delete(String id) { /* logic hidden */ }
  public String getId() { /* boilerplate */ }
  public void setId(String id) { /* boilerplate */ }
}
```

Getter/setter boilerplate is automatically detected and annotated `/* boilerplate */`. Real logic shows `/* logic hidden */`. Constructors are always shown in full.

### Kotlin — Structural Skeleton

For a Kotlin data class + service:

```kotlin
// Kotlin skeleton: OrderService.kt
// Tokens: ~180 (vs ~900 raw, 80% saved)

package com.example

data class Order(
  val id: String,
  val customerId: String,
  val items: List<OrderItem>,
  val status: OrderStatus
)

class OrderService(private val db: MutableMap<String, Order> = mutableMapOf()) {
  fun save(order: Order) { /* logic hidden */ }
  fun findById(id: String): Order { /* logic hidden */ }
  fun findAll(): List<Order> { /* logic hidden */ }
  fun delete(id: String) { /* logic hidden */ }

  companion object {
    fun create(): OrderService { /* logic hidden */ }
  }
}
```

Expression-body functions (`fun f() = expr`) are detected and collapsed. Data class constructor parameters are always shown in full.

### TypeScript — Structural Skeleton

```typescript
// TypeScript skeleton: PaymentService.ts
// Tokens: ~210 (vs ~1,400 raw, 85% saved)

import { Injectable } from '@nestjs/common';
import { Order } from './order.model';

@Injectable()
export class PaymentService {
  constructor(private readonly stripe: StripeClient) {}

  async charge(order: Order, currency: string): Promise<PaymentResult> { /* logic hidden */ }
  async refund(paymentId: string): Promise<void> { /* logic hidden */ }
  private validateCurrency(currency: string): boolean { /* logic hidden */ }
}
```

Arrow functions, decorators, and JSDoc are preserved in the skeleton header.

### Python — Structural Skeleton

```python
# Python skeleton: order_service.py
# Tokens: ~160 (vs ~800 raw, 80% saved)

from dataclasses import dataclass
from typing import List, Optional

@dataclass
class Order:
    id: str
    customer_id: str
    items: List[str]
    status: str

class OrderService:
    def __init__(self, db): ...
    def save(self, order: Order) -> Order: ...
    def find_by_id(self, order_id: str) -> Optional[Order]: ...
    def find_all(self) -> List[Order]: ...
    def delete(self, order_id: str) -> bool: ...
```

---

## `focus_names` — Expose Any Method Body On Demand

Every skeleton tool accepts `focus_names` (or `focus_methods`) to show a specific method's full body while keeping the rest collapsed:

```
get_java_skeleton(
  file_path="/src/OrderService.java",
  focus_names=["processPayment"]
)
```

```java
public class OrderService {
  public Order findById(String id) { /* logic hidden */ }

  // ← full body exposed because it's in focus_names
  public PaymentResult processPayment(Order order, String currency) {
    validateCurrency(currency);
    Payment payment = paymentGateway.charge(order.getTotal(), currency);
    order.setStatus(OrderStatus.PAID);
    return repository.save(payment);
  }

  public void delete(String id) { /* logic hidden */ }
}
```

---

## Language Router

rtk-sf automatically routes to the correct track based on file extension or CLI keyword:

| Extension / keyword | Track |
|---|---|
| `.cls`, `.trigger`, `.flow`, `sf `, `sfdx` | Salesforce |
| `.java`, `mvn`, `maven`, `javac` | Java |
| `.kt`, `.kts`, `gradle`, `gradlew` | Kotlin |
| `.ts`, `.tsx`, `.js`, `.jsx`, `npm`, `jest` | TypeScript |
| `.py`, `python`, `pytest`, `pip` | Python |

---

## MCP Tools Reference (23 tools)

### Salesforce (14 tools)

| Tool | Description |
|---|---|
| `query_compressed_spec` | Return compressed YAML spec for an Apex class or custom object |
| `search_codebase` | Keyword search across all indexed Salesforce components |
| `get_relations` | Blast-radius graph: what references this component |
| `list_components` | List all components of a given type (ApexClass, CustomObject, Flow, …) |
| `get_class_skeleton` | Apex class structural skeleton with optional focus methods |
| `sf_command` | Deploy, retrieve, run tests, or execute anonymous Apex |
| `get_object_schema` | Describe all fields on a Salesforce object |
| `soql_query` | Run a SOQL query and return sample records |
| `compact_prompt` | NLP-compress a user prompt before sending to the LLM |
| `validate_apex` | Static validation for Apex code snippets |
| `validate_soql` | Static validation for SOQL queries |
| `get_roi_stats` | Token/cost savings report for this session |
| `extract_image_text` | OCR text extraction from a screenshot or image |
| `annotate_component` | Write business-logic annotations back to the index |

### Java (2 tools)

| Tool | Description |
|---|---|
| `get_java_skeleton` | Java class skeleton — collapses method bodies, detects getters/setters, shows constructors in full |
| `run_java_build` | Run Maven or Gradle build/test and return compacted output (strips JVM framework frames) |

### Kotlin (2 tools)

| Tool | Description |
|---|---|
| `get_kotlin_skeleton` | Kotlin class skeleton — handles data classes, companion objects, expression-body functions |
| `run_gradle` | Run Gradle task and return compacted output |

### TypeScript / JavaScript (2 tools)

| Tool | Description |
|---|---|
| `get_ts_skeleton` | TypeScript/JS structural skeleton — handles arrow functions, decorators, JSDoc |
| `run_js_tests` | Run Jest or Vitest and return compacted output (strips node_modules frames) |

### Python / Utility (3 tools)

| Tool | Description |
|---|---|
| `get_python_skeleton` | Python structural skeleton using AST — handles dataclasses, type hints, decorators |
| `run_python_tests` | Run pytest and return compacted output |
| `read_data_file` | Read CSV, JSON, or YAML data files with row limits |

---

## ROI Calculator

> **Plug in your team size — the numbers speak for themselves.**

| Team size | Sessions/month | Without rtk-sf | With rtk-sf | **Monthly savings** |
|---|---|---|---|---|
| Solo dev | 20 | $19.20 | $0.58 | **$18.62** |
| 3-dev team | 60 | $57.60 | $1.73 | **$55.87** |
| 5-dev team | 100 | $96.00 | $2.88 | **$93.12** |
| 10-dev team | 200 | $192.00 | $5.76 | **$186.24** |
| 20-dev team | 400 | $384.00 | $11.52 | **$372.48** |
| **20 devs, annual** | — | **$4,608/yr** | **$138/yr** | **🔥 $4,470/yr saved** |

*Assumptions: Claude Sonnet 4 @ $3/1M input tokens · 80 component lookups per session · 15,000 tokens without rtk-sf vs. 450 tokens with.*

---

## Project Structure

```
rtk_sf/
├── __init__.py               # Package root (v0.8.0)
├── __main__.py               # CLI entry point
├── mcp_server.py             # MCP stdio server — all 23 tools
├── core_router.py            # Language detection router
├── indexer.py                # Salesforce DX project indexer
├── search.py                 # Keyword + semantic search
├── skeleton.py               # Apex skeleton generator
├── sf_runner.py              # Salesforce CLI wrapper
├── data_tools.py             # CSV/JSON/YAML reader
├── nlp_compactor.py          # Prompt NLP compressor
├── java/
│   ├── java_skeletonizer.py  # Java structural skeleton (getter/setter detection)
│   └── build_masker.py       # Maven + Gradle output compactor
├── kotlin/
│   ├── kt_skeletonizer.py    # Kotlin skeleton (expression-body, companion objects)
│   └── gradle_masker.py      # Gradle output compactor
├── typescript/
│   ├── ts_skeletonizer.py    # TypeScript/JS skeleton (arrow fns, decorators)
│   └── jest_masker.py        # Jest/Vitest output compactor
├── python/
│   ├── ast_skeletonizer.py   # Python AST skeleton (dataclasses, type hints)
│   └── pytest_masker.py      # pytest output compactor
└── hooks/
    ├── compact_prompt.py     # Pre-submit hook: NLP compress prompts
    └── ocr_intercept.py      # Pre-submit hook: OCR image → text
```

---

## Installation Options

```bash
# Salesforce only (minimal)
pip install rtk-sf

# Add vector search
pip install "rtk-sf[vector]"

# Add OCR (PaddleOCR)
pip install "rtk-sf[ocr]"

# Add OCR fallback (EasyOCR)
pip install "rtk-sf[ocr-fallback]"

# Everything
pip install "rtk-sf[all]"
```

All language tracks (Java, Kotlin, TypeScript, Python) are included in the base install — no extra dependencies needed.

---

## Salesforce Setup

```bash
# Index your project (run from the Salesforce project root)
python3 -m rtk_sf index

# Re-index after code changes
python3 -m rtk_sf index

# Dry-run: see what would be indexed
python3 -m rtk_sf dry-run
```

The indexer scans for:
- **Apex classes** (`.cls`) — methods, fields, annotations, test coverage
- **Custom objects** (`.object-meta.xml`) — fields, picklist values, relationships
- **Flows** (`.flow-meta.xml`) — decision nodes, variables, entry conditions
- **Triggers** (`.trigger`) — events, entity references

---

## Roadmap

- [x] Salesforce Apex token reduction (v0.4.x)
- [x] Python language track — `get_python_skeleton`, `run_python_tests` (v0.5.0)
- [x] TypeScript/JS language track — `get_ts_skeleton`, `run_js_tests` (v0.6.0)
- [x] Kotlin language track — `get_kotlin_skeleton`, `run_gradle` (v0.7.0)
- [x] Java language track — `get_java_skeleton`, `run_java_build` (v0.8.0)
- [ ] Go language track (planned)
- [ ] Rust language track (planned)
- [ ] Ruby language track (planned)
- [ ] Semantic vector search across all tracks

---

## Contributing

PRs welcome. The codebase follows a consistent pattern for each language track:

```
rtk_sf/<language>/
├── <lang>_skeletonizer.py   # Two-pass skeleton extractor
└── <tool>_masker.py         # Build/test output compactor
```

To add a new language track:
1. Create `rtk_sf/<language>/` with `__init__.py`
2. Implement `skeletonize(source, focus_names)` → `str`
3. Implement `run_build(...)` → `str`
4. Register 2 tools in `mcp_server.py`
5. Add extension/keyword routing in `core_router.py`

---

## License

MIT — see [LICENSE](LICENSE).

Built by [furuCRM Inc.](https://www.furucrm.com)
