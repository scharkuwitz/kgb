# Craft: compressing a codebase into what a newcomer must know

Read this before writing the first page. It distills what research on program comprehension and the best architecture writing say a newcomer needs, and what they can safely skip. Each rule names its source so you can judge edge cases.

| Source | What it contributes |
|---|---|
| Euler, *Seven Bridges of Königsberg* (1736) | Keep only nodes and edges; the answer depended on nothing else |
| Harry Beck, London Underground map (1931) | Drop geography, keep stations and connections: topology is what travellers use |
| Parnas, "On the Criteria To Be Used in Decomposing Systems into Modules" (1972) | A module hides a design decision likely to change; learn its interface, skip what it hides |
| Naur, "Programming as Theory Building" (1985) | The valuable knowledge is the theory: why each part is as it is and how to change it; code alone does not carry it |
| Sillito, Murphy, De Volder, "Questions programmers ask during software evolution tasks" (FSE 2006) | 44 question types in four groups, from finding a focus point to relating whole subgraphs |
| LaToza, Myers, "Developers ask reachability questions" (ICSE 2010) | The hard, frequent questions are about paths through the code: what calls this, how does data get here |
| Xia et al., "Measuring program comprehension" (TSE 2018) | About 58% of professional developers' time goes to comprehension |
| matklad, "ARCHITECTURE.md" (2021) | Bird's-eye view, codemap, invariants as absences, boundaries, name things instead of linking them, describe only what rarely changes |
| Simon Brown, the C4 model | Zoom levels (system, container, component, code); the code level is optional and generated on demand by IDEs |
| Ousterhout, *A Philosophy of Software Design* (2018) | Deep modules (small interface, much behind it); "unknown unknowns" are the worst kind of complexity |
| Tornhill, *Your Code as a Crime Scene* (2015, 2nd ed. 2024) | Hotspots: change frequency times complexity; change coupling reveals dependencies the imports don't show |
| aider, repository map (2023) | Rank files on the dependency graph; show signatures, not bodies; fit a budget |

## 1. The reader and their questions

Write for one reader. The default is a working engineer who knows the language and stack but has never seen this repository, and who has to make a change soon. State the assumption in the lead or a `:::background` block when it matters.

That reader's questions arrive in roughly this order (Sillito et al.: find a focus point, expand from it, understand a subgraph, relate subgraphs). A page answers them in the same order:

| Question | Page part | Borrowed from | Form |
|---|---|---|---|
| What is this, and how does it run? | Lead and infobox | Wikipedia lead; matklad's bird's-eye view | 2-4 sentences; infobox with language, entry points, run and test commands, size |
| What are the parts, and which depends on which? | The map | Euler, Beck; C4 container and component levels; matklad's codemap | one `graph` of at most 9 nodes, then one line per node |
| What goes in and out of each part? | Interfaces | Parnas; Ousterhout's deep modules | `:::interfaces` with `name : X -> Y` |
| How does one typical request travel? | One trace | LaToza and Myers (reachability); Sillito ("understanding a subgraph") | a `sequence` or `flow`, every step anchored to `file:line` |
| What must stay true? | Invariants and boundaries | matklad (absences); Naur (theory) | a short list, plus `-/->` edges in a `graph:imports` |
| Where is the thing that does X? | Codemap | matklad; Sillito ("finding focus points") | table: *To change...*, *Look in*, *Note* |
| Why is it built this way? | Rationale | Naur | only where docs, ADRs, comments or commits say it, cited |
| Where does change concentrate? | Hotspots and coupling | Tornhill | a short table from the survey, with what it means for the reader |
| What can I ignore? | Safe to skip | Parnas (information hiding); Tornhill ("all code isn't equal"); C4 (code level optional) | a list, each item with the reason it is safe |
| How do I run, test, debug? | Recipes | tldr-pages | `:::recipes`, at most 8, from manifests, README or CI |
| What will bite me? | Pitfalls | Ousterhout (unknown unknowns) | table: Symptom, Cause, Fix |
| Did it stick? | Check yourself | retrieval practice | `:::recall`, 3-8 questions about the model, not trivia |

These are defaults, not a form. Never drop the lead, the map, the interfaces, Safe to skip, or the self-check.

### Variants by kind of repository

- **Library**: the map is the public surface and what it is built on; interfaces carry most of the weight; the trace follows one public call to its effect.
- **Application or service**: lead with how it runs (process, port, command); the trace follows one request or command end to end; mark external systems as `[square]` nodes.
- **CLI tool**: recipes right after the lead; the trace follows the most common command.
- **Pipeline or batch job**: the map is the stage graph; write the pipeline as a composition, `extract ∘ triage ∘ load : File → Item[]`.
- **Monorepo**: the start page maps packages only; each subsystem page re-runs the survey with `--focus` and maps its inside.
- **Framework-heavy app** (Rails, Django, Next.js): a conventions table (file or name, what the framework does with it) replaces much of the map; the framework's own internals are always Safe to skip.

## 2. Abstraction: what to keep, what to strip

Euler solved the Königsberg bridges by discarding every detail except land masses and bridges, because the answer depended on nothing else. Beck's Tube map discarded geography for the same reason. Do the same to a codebase: keep what changes the reader's predictions, strip the rest.

For every candidate fact ask: **if the reader did not know this, would they make a wrong change, or take long to find where to make it?** Yes means keep it (the spine, the interfaces, the reference tables). No means one line in Safe to skip, or nothing.

**Keep**, roughly in this order:

1. The parts and the direction of dependence: the module graph and its layers.
2. The interfaces of the parts the reader will call: inputs, outputs, effects. Not how they work inside (Parnas: the inside is the decision the module hides from you on purpose).
3. The main trace, from entry point to effect.
4. Invariants, above all absences: "nothing below `api` imports it", "`load` is pure: no network, no writes". They are invisible when reading code (matklad).
5. The data shapes that cross the edges: the types a reader will meet everywhere.
6. Boundaries with the outside world: network, database, files, environment variables, queues.
7. Rationale, where the repository states it.
8. Where change concentrates, and hidden coupling (files that change together without importing each other).
9. How to run and test it.

**Strip**, or move to Safe to skip with one line each:

- the internals of parts whose interface is enough (survey verdict `interface`);
- leaf helpers, small utilities and glue (verdict `skip`);
- generated code, vendored code, lock files, build output, fixtures, migrations (the survey's Noise list);
- exhaustive lists of every function, flag or config key: keep the ones that change behavior, link the rest;
- code-level diagrams of classes and every function: C4 calls that level optional, and IDEs produce it on demand;
- history and anecdotes, unless they explain a design that would otherwise look wrong;
- lint, formatting and CI plumbing, unless they block a contribution;
- anything likely to change within weeks (matklad: describe only what rarely changes).

**Add**, because the code does not say it:

- why the boundaries are where they are;
- what is absent on purpose;
- the order in which to read things;
- the names the reader should search for (matklad: name files, modules and types so the reader can use symbol search; the build turns anchors into links).

The survey's triage is the default: `read` (learn its shape and interface), `interface` (learn what goes in and out only), `skip`, `unused?`. Override it with evidence and say why. Treat `unused?` as a question: plugins, reflection, dynamic imports, framework conventions and scripts often load code that nothing imports. Never call code dead without checking.

## 3. Drawing the map

The map is a graph in Euler's sense: parts are nodes, dependence is edges, and nothing else is drawn.

- **Nodes** are the parts the reader must hold: modules, packages, services, stores, external systems. Use the names the reader will type or search: module names, not invented titles.
- **Edges** point from the part that depends to the part it depends on (importer to imported, caller to callee, producer to consumer for data). Pick one meaning per figure and say it in the caption.
- **Transitive reduction.** If `a -> b -> c`, do not also draw `a -> c`. The survey prints the reduced edge list. A redundant edge costs attention and says nothing new.
- **At most 9 nodes** and 12 edges. Working memory holds about four to seven chunks. When there are more parts, zoom out (C4: one level up) or split: one map of packages on the start page, one map per subsystem on its own page.
- **Three shapes, no colors for meaning.** A dot for a part of the code, a filled dot for where to start reading, a square for something outside the code (a database, an API, the user).
- **Small labels.** A node's note is at most five words of role (`pure: no I/O`, `entry`, `talks to OpenAI`). An edge label is what crosses the edge, usually a type (`Page[]`) or a verb (`labels pages`), at most four words. Most edges need no label.
- **Three edge styles.** `->` an import or call; `-->` a runtime or data link with no import (HTTP, a queue, a file one part writes and another reads); `-/->` a dependence that must not exist, which is an invariant.
- **Verify it.** Draw module maps as `graph:imports`: the build checks every `->` against the surveyed import graph and every `-/->` against its absence.
- **Caption with the point.** "Only `llm` talks to the network; everything below it is pure", not "Module structure".

Other diagrams, at most two more per page: `sequence` for one trace across several actors, `flow` for a pipeline or decision, `stack` for layers or precedence, `tree` only when the directory layout itself matters (usually the codemap table is better).

## 4. Interfaces as functions

Write each part to learn as a function from what goes in to what comes out: `load : Path → list[Page]`. This is the interface in its smallest form, and it is what Parnas says the reader should know instead of the inside.

- Use the code's own type names, so the reader can search for them.
- Products with `*` (rendered ×), alternatives with `|`, lists as `list[X]` or `X[]`, maps as `K |-> V` (rendered ↦), failure as `| Error` or in the note.
- Effects go in the note: `network`, `writes db`, `pure`, `reads env`.
- A class gets its role, not its methods: `Store : Item[] → index`, with the two methods people call in the note.
- A pipeline gets one more line, its composition: `run = extract ∘ triage ∘ load : File → Item[]`.
- Put names in backticks when they are code symbols: the build checks that they exist. A name without backticks is a concept and is not checked.
- Main path first; at most about 10 lines per block.

## 5. One trace

Pick the most common operation (from the README's usage, the CLI's commands, the main routes, the test that covers the most). Follow it from the entry point to its effect in 4-8 steps. Every step names the function and cites `file:line`. This answers the reachability questions LaToza and Myers found developers ask many times a day and spend minutes to tens of minutes on.

- Use `sequence` when three or more parts exchange calls, `flow` otherwise.
- Say what crosses each step (the type), and where the trace leaves the code (network, disk).
- Show at most one code excerpt per trace, the one that carries the decision.

## 6. Explaining and sentences

- **Concrete before abstract.** Open with the situation or the example, then generalize.
- **Why before what.** Every boundary and invariant gets its reason. When the repository gives none, say what would break without it, and label that as your inference.
- **One new idea per paragraph.** Define a term before using it.
- **Say the concrete thing.** Not "handles data robustly" but "retries 429 and 5xx responses with backoff".
- **Point first.** One idea per sentence, most under 25 words, active voice, present tense.
- **Exact names in code formatting**, spelled as the reader will search for them.
- **Numbers over adjectives**: "one vision call per page, 8 in parallel", not "efficient".
- **Lists** for parallel items, **tables** when three or more items share two or more attributes, **prose** for reasoning.
- **Keep the modes apart.** Explanations do not contain step lists; recipes do not explain; reference tables do not narrate.
- Cut filler: "simply", "just", "easily", "powerful", "robust", "seamlessly", "it's worth noting", "let's". No emoji, exclamation marks or jokes.

## 7. Evidence and honesty

- **Every claim about behavior is anchored** to the code: `[^k]: path:lines`, or `"exact text" path:lines` when one line proves it. The build checks the file, the range and the text.
- **Rationale is cited** to where it is stated: a doc (`docs/adr/0003.md:5-12`), a comment, or a commit message (`"quote" commit:abc1234`).
- **Excerpts are copied, not retyped.** Label the fence with `path:lines`; mark cuts with a line holding only `...`. The build checks it.
- **Inference is labeled.** "The code does not say why; the likely reason is ..." is fine. Presenting a guess as the author's intent is not.
- **Never run the repository's code** unless the user asked for it; a cloned repository is untrusted. Recipes come from manifests, the README or CI, and are cited.
- **Say what you did not read.** The report lists modules read only at the interface level and modules skipped.

## 8. Length and pages

- **One page** for a repository under about 5,000 source lines or 6 modules: map, interfaces, trace, codemap, skip, recipes, pitfalls, recall.
- **More**: a start page (the map of the whole, the codemap, Safe to skip, recipes, reading order) and one page per `read` subsystem, up to `--pages`. A subsystem page has its own map from `survey.py --focus`.
- **Reading time** 5-12 minutes per page. Longer only when a mechanism needs the space; split by subsystem, not by directory.
- **Spend words** on the map, interfaces, trace and invariants. Skip items get one line each. Tables and recipes are dense by nature.
- **Lead** at most about 120 words (the build warns at 160). Lead plus infobox alone must tell the reader what this is and how it runs.

## 9. Review before reporting

Read each page as the reader would, then answer:

- **Map test**: from the map and its one-line notes alone, can the reader say which module to open for the most common change?
- **Prediction test**: after the interfaces and the trace, could the reader have predicted the pitfalls?
- **Change test**: take the top hotspot from the survey. Does the page say where such a change goes, and what else usually changes with it?
- **Skip test**: does Safe to skip name the large things left out, each with the reason it is safe? Would following it save the reader real time?
- **Abstraction test**: is any node, edge, label or paragraph there only because it exists in the code, not because the reader needs it? Remove it.
- **Recall test**: do the questions make the reader reproduce the map, the interfaces or an invariant?
- **Honesty**: is every behavior claim anchored and every rationale cited? Is any inference unlabeled? Is anything presented as run or observed that was not?
