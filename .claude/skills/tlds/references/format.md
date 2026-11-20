# Page format

Each page is a Markdown file at `<bundle>/pages/<slug>.md`. It uses ordinary Markdown plus a few `:::` blocks and diagram fences. `build.py` turns every page into one HTML document and checks the claims against the surveyed repository. Raw HTML is not supported; it is escaped and shown as text. Write pages with the Write tool, because shell heredocs break on backticks.

## Front matter

```markdown
---
title: Ingestion pipeline
summary: How a scanned contract becomes a list of items, and where each step lives.
scope: kgb/load.py, kgb/triage.py, kgb/extract.py
group: Subsystems
---
```

| Key | Required | Use |
|---|---|---|
| `title` | yes | Page title, shown as the H1. Never write `# Title` in the body. |
| `summary` | yes | One line, used in the start page's contents table. Inline code allowed. |
| `scope` | no | Comma-separated paths the page covers, shown under the title as "Covers ...". |
| `surveyed` | no | ISO date. Defaults to the survey's date. |
| `group` | no | Navbox row that this page belongs to, such as "Overview" or "Subsystems". |

`pages/index.md` is the **start page** of a bundle of two or more pages. Its front matter takes `title`, `surveyed` and `order` (comma-separated slugs in reading order). It holds the map of the whole repository, the codemap, Safe to skip and the reading order. Place `:::pages` followed by `:::` where the contents table should appear; without it, the table goes at the end.

The build expects, on the first page (the start page, or the only page): a `graph` and a section whose heading contains "skip" (or "ignore"). It expects at least one `:::interfaces` block somewhere in the bundle.

## Body

The **lead** is everything before the first `##`. Use `##` for sections and `###` and `####` below them; `##` and `###` appear in the contents sidebar with numbers.

Supported Markdown: paragraphs, `**bold**`, `*italic*`, `` `code` ``, `[links](url "title")`, `<https://autolinks>`, `-` and `1.` lists (nested by indenting two or more spaces), `>` quotes, `---` rules, pipe tables with an alignment row, `![alt](url "caption")` images on their own line, and definition lists:

```markdown
Term
: Definition, which may span lines indented by two spaces.
```

Straight quotes become curly quotes in prose, and `...` becomes an ellipsis. Code is never changed.

**Links**:

- to another page: `[Pipeline](pipeline.md)`, `[Its map](pipeline.md#the-map)`, or `[a section here](#the-map)`;
- to the code: `[the loader](kgb/load.py:27-37)` or `[schema](kgb/schema.py)`. The build checks the file and the range, and links to the commit on the forge (GitHub, GitLab, Gitea, Bitbucket) or to the local file when the file has uncommitted changes;
- elsewhere: a full `https://` URL.

## Citations

Write `[^key]` after a claim and define the key anywhere on the page, usually at the bottom. Citations are numbered in order of first use; reusing a key reuses its number.

```markdown
`load` picks a loader by file extension.[^dispatch] Only `llm` talks to the network.[^net]

[^dispatch]: "loader = LOADERS.get(path.suffix.lower())" kgb/load.py:27-33
[^net]: kgb/llm.py:1-9
[^why]: "keep triage cheap" commit:c9a4c7a
```

| Form | Checked by the build |
|---|---|
| `path:lines` (or `path:line`, or `path`) optionally followed by `(label)` | the file exists in the repository and the range is inside it |
| `"exact text" path:lines (label)` | the text appears within those lines (2 lines of slack); whitespace and quote styles don't count, case does; `...` skips |
| `commit:<sha>` or `"exact text" commit:<sha>` | the commit exists and its message contains the text (case-insensitive) |
| `"text" https://...` or `https://... (label)` | not checked (shown as a plain reference); prefer repository files |
| anything else | a note, shown as written; use rarely |

Keep quotes to one claim and at most 30 words. Write a double quote inside a quote as `\"`. Point anchors at the lines that prove the claim; ranges over 80 lines get a NOTE.

## Blocks

A block opens with `:::name` and closes with a line holding only `:::`. Blocks can contain any Markdown, including other blocks.

### `:::infobox [title]`

Facts the reader looks up first. Lines are `Label: value`, `## Group heading`, or an image. Place it right after the front matter. The build adds a "This summary" group with the repository, its commit, and how many anchors matched the code.

```markdown
:::infobox
Kind: Batch pipeline, Python 3.12
Entry: `python -m kgb.extract {{file}}`
Tests: `uv run pytest`
Size: 6 modules, 700 lines
## Outside world
Network: OpenAI API, only from `llm`[^net]
Config: 13 `KGB_*` environment variables
:::
```

### `:::interfaces [title]`

The parts to learn, as functions. One line per part: `name : Domain -> Codomain  # note`. Optional `## Group` lines.

```markdown
:::interfaces What goes in and out
`load` : Path -> list[Page]  # pure: no network, no writes[^dispatch]
`triage` : list[Page] -> list[PageLabel]  # one cheap model call per page
`extract_pages` : list[Page] * list[int] -> dict[int, PageItems]  # parallel vision calls
run : File |-> Item[]  # = extract ∘ triage ∘ load
:::
```

Rendered with `->` as →, `=>` as ⇒, `<-` as ←, `|->` as ↦ and ` * ` as ×. A name in backticks is a code symbol, and the build warns when it appears nowhere in the repository; a plain name is a concept and is not checked. Notes accept inline code and citations.

### Hatnotes (one line, no closing `:::`)

```markdown
:::main [kgb/extract.py](kgb/extract.py)
:::see [Pipeline](pipeline.md), [Schema](schema.md)
:::hatnote This page covers ingestion. For search, see [Search](search.md).
```

`:::main` goes under a heading whose section condenses one file or module, the way Wikipedia points to the full treatment. `:::hatnote` goes at the top of a page for scope.

### `:::note`, `:::tip`, `:::warning` [title]

Use **note** for information that isn't obvious, **tip** for optional but useful information, and **warning** for loss of data or work, security problems, or anything hard to undo. Use them sparingly.

### `:::background [title]`

Context that does not come from the repository: a library's concept, a protocol, a pattern the code assumes. It is labeled "General knowledge, not from the source documentation." Keep it to standard, stable facts, never claims about this code.

### `:::example [title]`

A worked example: prose and fenced code alternate, prose beside the code that follows it. An `output` fence directly after a code fence joins the same row. Output comes from the repository (docs, tests, recorded fixtures) or from a command you actually ran.

### `:::recipes`

Common tasks in the tldr-pages style. Each item is `- Description ending in a colon:` followed by a line holding one inline-code command or by a fenced block. `{{placeholder}}` marks a part the reader replaces. Keep at most 8, and take them from manifests, the README or CI.

```markdown
:::recipes
- Run the tests:
  `uv run pytest`

- Extract items from one contract:
  `uv run python -m kgb.extract {{path/to/contract.pdf}}`
:::
```

### `:::exercise [title]`

A hands-on drill of under 10 minutes on the reader's own checkout, such as "find where X is decided". Include a line starting with `Check:` that says what the reader should observe.

### `:::recall`

Self-check questions. Each question on one line, its answer on the next line starting with `: `. Answers stay hidden until opened.

### `:::pages`

Start page only. Close it with `:::` on the next line. It marks where the contents table goes.

## Code fences

` ```lang label ` adds a caption bar with the label. **A label of the form `path:lines` makes the block an excerpt**: the build checks that the code appears in those lines (whitespace doesn't count) and links the caption to the code. Mark cuts with a line holding only `...` (or `# ...`, `// ...`). A label without line numbers is checked only if it names a repository file; otherwise it is a plain caption, such as an illustrative file name.

````markdown
```python kgb/load.py:27-33
def load(path: str | Path) -> list[Page]:
    ...
    loader = LOADERS.get(path.suffix.lower())
```
````

Highlighting is deliberately small: comments, keys in YAML, TOML and JSON, `$ ` prompts, `+` and `-` lines in `diff`. An `output` fence shows program output, dashed and muted.

## Diagrams

Diagram fences render as inline SVG or HTML that follows the light and dark themes. Text after the kind is the caption; write one that says what to notice. Lines starting with `//` are ignored.

### `graph`: the map (nodes and edges, nothing else)

````markdown
```graph:imports Only llm talks to the network; config and schema depend on nothing
*extract : entry
extract -> triage : labels pages
triage -> llm
llm -> load : Page
load -> config
load -> schema
llm --> [OpenAI] : HTTPS
schema -/-> llm : pure data
```
````

| Line | Meaning |
|---|---|
| `name` or `name : note` | a node; the note is a small label beside it (at most five words) |
| `a -> b` | `a` depends on `b` (imports or calls it) |
| `a --> b` | a runtime or data link with no import: HTTP, a queue, a shared file |
| `a -/-> b` | `a` must not depend on `b`: an invariant, drawn dotted with a cross |
| `a -> b -> c` | a chain; a ` : label` applies to its last edge |
| `... : label` on an edge | a small label at the edge's middle: what crosses it (a type or a verb) |
| `*name` | filled dot: where to start reading |
| `[name]` | square: something outside the code (a service, a database, the user) |
| `` `name` `` | label set in code type |

Nodes are created by their first mention, in the order they appear. The layout puts dependents above what they depend on (left of it with `graph:h`), orders each layer to reduce crossings, and draws an edge that closes a cycle dashed (the build then adds a NOTE).

Options after a colon, comma-separated: `graph:h` lays out left to right (good for pipelines of up to 5 nodes); `graph:imports` checks the drawing against the survey. With `imports`, node names must be module names or file paths the survey printed (`[square]` nodes are exempt); each `->` must be an import between those modules, directly or through others (a NOTE says when it is only indirect); each `-/->` must be absent, directly and through others; `-->` edges are not checked.

Keep a graph to 9 nodes and 12 edges; draw the transitive reduction.

### `flow`: steps, decisions, loops

````markdown
```flow One contract through the pipeline
File : pdf, image, docx, xlsx
-> `load`
Pages
-> `triage`
Item pages | Key-term pages
-> `extract_pages`
Items
```
````

Each line is a box, with `label : note`. A `-> text` line labels the next arrow. Use `a | b` for alternatives within one step, and `loop: text` for a return arrow. `flow:v` or `flow:h` forces a direction.

### `sequence`: who calls whom, in order

````markdown
```sequence Extracting one page
extract -> load: shrink(page)
extract -> llm: ask_page(prompt, PageItems, page)
llm -> OpenAI: responses.parse
OpenAI --> llm: PageItems
llm --> extract: PageItems
note extract: 8 pages in parallel
```
````

`A -> B: message` draws a solid arrow and `A --> B: reply` a dashed one. `note A: text` adds a note. Keep labels under about 50 characters.

### `stack`: layers and precedence

````markdown
```stack Settings: the first source that sets a key wins
^ wins
* Environment variables
`.env` file : read by `load_dotenv`
Defaults in `config.py`
```
````

### `tree`: directory layout

Indentation sets depth; text after ` # ` is a muted comment. Use it only when the layout itself matters; a codemap table usually serves better.

### `nest`: containment

Indentation sets containment; children sit side by side inside their parent.

### `svg`: last resort

A fence holding one hand-written `<svg>` element. Scripts and event handlers are stripped. Use `currentColor`.

## What the build checks

**ERROR** (exit status 1): missing title, unclosed fences or blocks, unknown blocks, citations with no definition, `#` used as a heading, a graph with no nodes.

**WARN**:
- anchors to missing files or out-of-range lines; quotes not found in their lines; excerpts that don't match; commit quotes not in the message;
- `graph:imports` edges with no import, broken `-/->` invariants, nodes that are not surveyed modules;
- backticked interface names that appear nowhere in the repository;
- no map or no Safe to skip section on the first page; no `:::interfaces` anywhere;
- no lead or a lead over 160 words; no infobox, fewer than 3 recall questions, nothing hands-on, no citations;
- diagrams with no caption or too many items (graph: 9 nodes, 12 edges);
- more than 8 recipes, exercises without `Check:`, unused footnotes, links that point nowhere, emoji.

**NOTE**: sections without citations, cycles in a graph, edges that hold only through other modules, anchors over 80 lines, quotes of web pages (not checked), pages that read in over 20 minutes.

Fix every ERROR and WARN before reporting. Read each NOTE and decide whether it needs a fix.
