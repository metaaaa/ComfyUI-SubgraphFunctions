# ComfyUI-SubgraphFunctions

Use a saved subgraph as a **shared function**. Edit it once, and every workflow
that calls it changes — no workflow re-saving, no ComfyUI restart.

[日本語版はこちら](./README.ja.md)

## The problem

ComfyUI subgraphs are **copied by value**. When you insert a subgraph blueprint,
its full definition is embedded into that workflow file:

```
my_workflow.json
   definitions.subgraphs[0]  id=4c314f31-…  nodes=15   ← the actual definition lives here
   nodes[#105].type = "4c314f31-…"                     ← the instance just names the UUID
```

`app/subgraph_manager.py` only serves the blueprint file; it keeps no reference
and performs no version check. So fixing the "original" never reaches the
workflows that already use it. If ten workflows share a loader stack, you edit
it ten times.

## What this does

It keeps the definition **out** of the workflow. You place one node, and the
graph is built at execution time from the file on disk.

```
user/default/subgraphs/functions/<name>.json   ← written by the UI's publish / edit
      │  rescanned whenever /object_info is requested
      ▼
SubgraphFn_<name>  — a single node                ← this is what goes in the workflow
      │  the file is re-read on every run
      ▼
ComfyUI node expansion ("expand")                 ← the real graph appears at run time
```

There is no per-subgraph Python. Drop a `.json` in the directory and a node
appears; delete it and the node goes away.

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/metaaaa/ComfyUI-SubgraphFunctions
```

Restart ComfyUI once. No dependencies beyond ComfyUI itself.

## Usage

### Make a function

Build a subgraph as usual, select it, and **publish** it — but type the name as
**`functions/<name>`**. The blueprint then lands in
`user/default/subgraphs/functions/`, which is the directory this pack scans.

> Only `subgraphs/functions/` is scanned, not `subgraphs/` itself. Without that
> split, *every* blueprint you publish would silently become a node.

Reload the browser. `fn: <name>` appears under **Subgraph Functions**.

To try it without building anything, copy the bundled example:

```bash
cp examples/subgraphs/add.json <ComfyUI>/user/default/subgraphs/functions/
```

### Call it

Place the `fn: <name>` node. Its inputs and outputs come from the subgraph's own
input/output slots, and each widget inherits the spec of whatever it is wired to
inside — combo choices, int min/max/step, multiline strings, tooltips.

### Edit it

In the **Node Library** sidebar, open `Subgraph Blueprints/User`, hover the
entry, and click the **pencil** button. ComfyUI opens the blueprint for editing;
save it normally. Every workflow using `fn: <name>` picks up the change on its
next run.

Editing the file on disk works identically. `IS_CHANGED` hashes the file, so the
outer workflow being unchanged will not serve you a stale cached result.

### Two entries, opposite meaning

The same subgraph shows up twice in node search. They do different things:

| Entry | Category | Inserting it |
|---|---|---|
| `<name>` | `Subgraph Blueprints/User` | **copies** the definition (stock behaviour) |
| **`fn: <name>`** | `Subgraph Functions` | **references** the file (this pack) |

### What needs a refresh

| Change | Action |
|---|---|
| Contents only | **nothing** — takes effect on the next run |
| Inputs/outputs added or removed | **reload the browser** |
| File added or removed | **reload the browser** |

A middleware rescans the directory just before `/object_info` is served, keyed on
name + mtime + size, so a browser reload is enough. ComfyUI never needs a
restart. `GET /subgraph_functions/reload` forces a rescan if you replaced a
file's contents without changing its mtime.

## How it works

The node returns `{"expand": <api graph>, "result": (...)}`. ComfyUI's executor
splices that graph into the running prompt
([`execution.py`](https://github.com/comfyanonymous/ComfyUI/blob/master/execution.py),
the `expand` branch). Converting the stored UI-format subgraph into API format
means resolving `widgets_values` against each node's `INPUT_TYPES` and rebuilding
links from litegraph slot indices.

Two consequences worth knowing:

- **Expanded nodes are cached individually.** Changing one widget inside a
  function re-runs only the nodes downstream of it, not the whole function.
- **Output nodes inside a function work.** `SaveImage` / `SaveVideo` placed
  inside the subgraph are detected and executed.

Link-typed inputs (MODEL, IMAGE, …) are declared `rawLink`, so the link itself is
spliced into the expanded graph rather than the materialised value. Upstream
caching keeps working.

## Limitations

All of these raise a clear error rather than misbehaving silently:

- **Nested subgraphs** inside a function are not supported
- **Muted (mode 2) / bypassed (mode 4) nodes** cannot be reproduced when expanding
- **Unconnected subgraph outputs**
- You **cannot open a function on the canvas**. That is the price of not
  embedding it; edit it through the blueprint instead.
- Expanded nodes skip `validate_inputs` (validation only runs on the original
  prompt), so configuration mistakes surface as **runtime** errors
- Two files with the same basename in `subgraphs/` and `subgraphs/functions/`
  collide in ComfyUI's own blueprint list, which keys on basename

## A trap worth documenting

The frontend inserts an **extra slot** into `widgets_values` right after any
widget carrying `control_after_generate` (seeds, most notably). Any code reading
`widgets_values` positionally has to skip the same amount, or every later value
shifts by one — silently, with no error, just different numbers. See
`has_control_widget` in `subgraph_functions.py`.

## License

MIT
