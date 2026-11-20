#!/usr/bin/env python3
"""Render a tlds bundle into one self-contained HTML document. Stdlib only.

Usage: build.py <bundle_dir> [-o OUT.html]

Reads   <bundle>/pages/*.md        page sources (syntax: references/format.md); pages/index.md is the start page
        <bundle>/src/survey*.json  written by survey.py: the repository root, commit, web URL and module graph.
                                   Anchors (path:lines), quotes, code excerpts, commit quotes, interface names
                                   and graph:imports edges are checked against the repository itself.
        <skill>/assets/            style.css and app.js, inlined into the output
Writes  <bundle>/index.html        every page in one file, shown one page at a time
Prints  ERROR (exit status 1: broken syntax or references), WARN (quality problems to fix before
        handing the document over), NOTE (information only), then a one-line summary.
"""
import argparse
import datetime as dt
import html
import json
import math
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
    '&family=IBM+Plex+Sans:ital,wght@0,400;0,600;1,400&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600'
    '&display=swap">'
)

CALLOUTS = {"note": "Note", "tip": "Tip", "warning": "Warning"}
HATNOTES = {"main": "Main article: ", "see": "See also: ", "hatnote": ""}
CONTAINERS = set(CALLOUTS) | {"background", "infobox", "example", "recipes", "exercise", "recall", "pages",
                               "interfaces"}
DIAGRAM_LIMITS = {"flow": 8, "sequence": 12, "stack": 8, "tree": 30, "nest": 12, "svg": 0, "graph": 9}
MAX_GRAPH_EDGES = 12
MAX_QUOTE_WORDS = 30
MAX_ANCHOR_LINES = 80
MAX_RECIPES = 8
MAX_LEAD_WORDS = 160
LONG_PAGE_MIN = 20
WPM = 220
UNCITED_OK = re.compile(r"(?i)check yourself|not covered|further reading|glossary|terms|see also|exercise|practice|"
                        r"try it|recipes|skip|ignore|reading order|start here|run it|commands")
SKIP_HEADING = re.compile(r"(?i)\b(skip|ignore|noise|not covered)\b")
ELISION = re.compile(r"^\s*(?:\.\.\.|…|#\s*\.\.\.|//\s*\.\.\.|/\*\s*\.\.\.\s*\*/|<!--\s*\.\.\.\s*-->)\s*$")
ANCHOR = re.compile(r"^(?P<p>[\w.@+/-]*[\w@+-][\w.@+/-]*?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$")
SEP = " \u00b7 "
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")

FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*(.*)$")
DIRECTIVE = re.compile(r"^:::\s*([A-Za-z][\w-]*)\s*(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FOOTDEF = re.compile(r"^\[\^([^\]\s]+)\]:\s*(.*)$")
LIST = re.compile(r"^( {0,3})([-*+]|\d{1,3}[.)])(\s+)(.*)$")
TABLE_SEP = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$")
IMAGE = re.compile(r'^!\[([^\]]*)\]\(\s*(\S+?)(?:\s+"([^"]*)")?\s*\)$')
HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
PH = re.compile(r"\{\{(.+?)\}\}")
NOTE_QUOTE = re.compile(r'^[“"](?P<q>.+)[”"]\s+(?P<u>https?://\S+?)(?:\s+\((?P<l>[^)]*)\))?\s*$', re.S)
NOTE_URL = re.compile(r"^(?P<u>https?://\S+?)(?:\s+\((?P<l>[^)]*)\))?\s*$")
NOTE_CODE_QUOTE = re.compile(r'^[“"](?P<q>.+)[”"]\s+(?P<p>[\w.@+/-]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?(?:\s+\((?P<l>[^)]*)\))?\s*$', re.S)
NOTE_CODE = re.compile(r"^(?P<p>[\w.@+/-]*[./][\w.@+/-]*?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?(?:\s+\((?P<l>[^)]*)\))?\s*$")
NOTE_COMMIT = re.compile(r'^(?:[“"](?P<q>.+)[”"]\s+)?commit:(?P<h>[0-9a-fA-F]{6,40})(?:\s+\((?P<l>[^)]*)\))?\s*$', re.S)
GRAPH_OP = re.compile(r"\s+(-/->|-->|->)\s+")
INLINE = re.compile(r"""
  (?P<code>(?P<tk>`+)(?P<ct>.+?)(?P=tk))
| (?P<fn>\[\^(?P<fk>[^\]\s]+)\])
| (?P<img>!\[(?P<ia>[^\]]*)\]\((?P<iu>[^)\s]+)\))
| (?P<link>\[(?P<lt>(?:[^\[\]]|\[[^\[\]]*\])+)\]\((?P<lu>[^()\s]+(?:\([^()\s]*\)[^()\s]*)*)(?:\s+"(?P<ltitle>[^"]*)")?\))
| (?P<auto><(?P<au>https?://[^\s<>]+)>)
| (?P<strong>\*\*(?=\S)(?P<st>.+?)(?<=\S)\*\*)
| (?P<em>(?<![\w*\\])\*(?=[^\s*])(?P<et>.+?)(?<=[^\s*\\])\*(?![\w*]))
| (?P<em2>(?<![\w\\])_(?=\S)(?P<e2t>.+?)(?<=\S)_(?!\w))
| (?P<esc>\\(?P<ec>[\\`*_{}\[\]()#+\-.!|<>~]))
""", re.X | re.S)

HASH_LANGS = {"sh", "bash", "shell", "zsh", "fish", "console", "python", "py", "yaml", "yml", "toml", "ruby",
              "rb", "r", "perl", "make", "makefile", "dockerfile", "conf", "ini", "gitignore", "nginx", "hcl",
              "terraform", "tf", "powershell", "ps1", "elixir", "ex", "julia", "nix", "cmake", "env", "dotenv"}
SLASH_LANGS = {"js", "javascript", "mjs", "cjs", "ts", "typescript", "jsx", "tsx", "go", "rust", "rs", "c", "h",
               "cpp", "c++", "cc", "java", "kotlin", "kt", "swift", "jsonc", "json5", "css", "scss", "less",
               "php", "cs", "csharp", "dart", "scala", "groovy", "gradle", "proto", "zig", "solidity", "graphql"}
DASH_LANGS = {"sql", "lua", "haskell", "hs", "elm", "ada"}
TAG_LANGS = {"html", "xml", "svg", "vue", "svelte"}
SHELL_LANGS = {"sh", "bash", "shell", "zsh", "fish", "console", "shell-session", "powershell", "ps1", ""}
KEY_LANGS = {"yaml", "yml", "toml", "ini", "conf", "env", "dotenv"}
JSON_LANGS = {"json", "jsonc", "json5"}
TRANSLATE = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0x00A0: " ",
             0x2026: "..."}


# ---------- log ----------

class Log:
    def __init__(self):
        self.items = []

    def add(self, level, where, msg):
        self.items.append((level, where, msg))

    def count(self, level):
        return sum(1 for item in self.items if item[0] == level)


LOG = Log()


# ---------- small helpers ----------

def esc(s):
    return html.escape(s, quote=False)


def attr(s):
    return html.escape(s, quote=True)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def words(s):
    return len(re.findall(r"\S+", s))


def slugify(text):
    t = html.unescape(re.sub(r"<[^>]+>", "", text)).strip().lower()
    t = re.sub(r"[^\w\- ]+", "", t)
    return re.sub(r"\s+", "-", t).strip("-") or "section"


def canon(url):
    p = urlsplit(url.strip())
    return p.netloc.lower() + re.sub(r"\.md$", "", p.path.rstrip("/"))


def norm(s):
    """Normalize text for quote matching: markup, quotes, dashes and whitespace don't count."""
    s = html.unescape(s).translate(TRANSLATE)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", s)
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"[*_`]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def plain(s):
    return re.sub(r"[`*_]", "", s).strip()


def smart(s, prev=" "):
    """Curly quotes and ellipses for prose; `prev` is the character before this run of text."""
    out = []
    for k, ch in enumerate(s):
        p = s[k - 1] if k else prev
        opening = p.isspace() or p in "([{—–/-"
        if ch == '"':
            out.append("“" if opening else "”")
        elif ch == "'":
            out.append("‘" if opening else "’")
        else:
            out.append(ch)
    return esc("".join(out).replace("...", "…"))


def nice_date(s):
    try:
        d = dt.date.fromisoformat(s)
        return f"{d.day} {d.strftime('%B')} {d.year}"
    except (TypeError, ValueError):
        return s or ""


def short_url(url):
    p = urlsplit(url)
    return (p.netloc + p.path).rstrip("/")


def split_note(s):
    label, _, note = s.partition(" : ")
    return label.strip(), note.strip() or None


def front_matter(raw):
    lines = raw.splitlines()
    meta = {}
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                for line in lines[1:j]:
                    key, sep, value = line.partition(":")
                    if sep and key.strip() and not line.startswith((" ", "\t", "#")):
                        value = value.strip()
                        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                            value = value[1:-1]
                        meta[key.strip().lower()] = value
                return meta, lines[j + 1:], j + 2
    return meta, lines, 1


def dedent(lines, n):
    """Remove up to n leading spaces from each line (the indentation of the opening fence)."""
    return [l[min(n, len(l) - len(l.lstrip(" "))):] for l in lines]


def fence_end(lines, i, marker):
    for j in range(i + 1, len(lines)):
        t = lines[j].strip()
        if t.startswith(marker) and set(t) == {marker[0]}:
            return j
    return None


def directive_end(lines, i):
    depth, fence = 0, None
    for j in range(i + 1, len(lines)):
        t = lines[j].strip()
        if fence:
            if t.startswith(fence) and set(t) == {fence[0]}:
                fence = None
            continue
        m = FENCE.match(lines[j])
        if m:
            fence = m.group(2)
            continue
        if t == ":::":
            if depth == 0:
                return j
            depth -= 1
        else:
            d = DIRECTIVE.match(t)
            if d and d.group(1).lower() not in HATNOTES:
                depth += 1
    return None


def sanitize_svg(s):
    s = re.sub(r"(?is)<(script|foreignObject|iframe)\b.*?</\1\s*>", "", s)
    s = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", s)
    s = re.sub(r"(?i)((?:xlink:)?href\s*=\s*[\"'])\s*javascript:[^\"']*", r"\1#", s)
    return s.strip()


# ---------- code highlighting (deliberately small: comments, keys, prompts, placeholders) ----------

def comment_start(line, lang):
    if lang in HASH_LANGS:
        marks = ("#",)
    elif lang in SLASH_LANGS:
        marks = ("//", "/*")
    elif lang in DASH_LANGS:
        marks = ("--",)
    elif lang in TAG_LANGS:
        marks = ("<!--",)
    else:
        return None
    quote = None
    for k, ch in enumerate(line):
        if quote:
            if ch == quote and line[k - 1] != "\\":
                quote = None
            continue
        if ch in "\"`" or (ch == "'" and lang not in KEY_LANGS):
            quote = ch
            continue
        for mark in marks:
            if line.startswith(mark, k):
                if mark == "#" and k and not line[k - 1].isspace():
                    break
                if mark == "//" and k and line[k - 1] == ":":
                    break
                return k
    return None


def hl_line(line, lang, ph):
    cut = comment_start(line, lang)
    code, com = (line[:cut], line[cut:]) if cut is not None else (line, "")
    e = esc(code)
    if lang in KEY_LANGS:
        e = re.sub(r"^(\s*(?:-\s+)?)([A-Za-z_][\w.\-]*|\"[^\"]*\")(?=\s*[:=](?:\s|$))", r'\1<span class="k">\2</span>', e)
    elif lang in JSON_LANGS:
        e = re.sub(r"(\"(?:[^\"\\]|\\.)*\")(?=\s*:)", r'<span class="k">\1</span>', e)
    if ph:
        e = PH.sub(r"<var>\1</var>", e)
    if com:
        e += f'<span class="c">{esc(com)}</span>'
    return e


def highlight(code, lang, ph=False):
    lines = code.split("\n")
    session = lang in SHELL_LANGS and any(l.startswith("$ ") for l in lines)
    front, cont, out = False, False, []
    for idx, line in enumerate(lines):
        if lang == "diff":
            cls = ("ins" if line.startswith("+") and not line.startswith("+++")
                   else "del" if line.startswith("-") and not line.startswith("---") else "")
            out.append(f'<span class="{cls}">{esc(line)}</span>' if cls else esc(line))
        elif lang in ("md", "markdown", "mdx"):
            if idx == 0 and line.strip() == "---":
                front = True
                out.append(esc(line))
            elif front:
                front = line.strip() != "---"
                out.append(hl_line(line, "yaml", ph) if front else esc(line))
            else:
                out.append(hl_line(line, "html", ph))
        elif session:
            if line.startswith("$ ") or cont:
                body = line[2:] if line.startswith("$ ") and not cont else line
                prompt = '<span class="p">$ </span>' if line.startswith("$ ") and not cont else ""
                out.append(prompt + hl_line(body, "bash", ph))
                cont = line.rstrip().endswith("\\")
            else:
                out.append(f'<span class="o">{esc(line)}</span>')
        else:
            out.append(hl_line(line, lang, ph))
    return "\n".join(out)


# ---------- the repository (for anchor, quote, excerpt and graph checks) ----------

def code_norm(s):
    """Normalize code or prose for matching: whitespace and quote styles don't count, case does."""
    return re.sub(r"\s+", " ", html.unescape(s).translate(TRANSLATE)).strip()


def find_parts(parts, text):
    pos = 0
    for part in parts:
        k = text.find(part, pos)
        if k < 0:
            return False
        pos = k + len(part)
    return True


class Repo:
    """The surveyed repository. Every check answers True, False, or None (nothing to check against)."""

    def __init__(self, src):
        self.surveys, self.root, self.main = [], None, {}
        for p in sorted(src.glob("survey*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                LOG.add("WARN", f"src/{p.name}", "unreadable survey; anchors are not checked")
                continue
            d["_edges"] = {(a, b) for a, b, *_ in d.get("module_edges", [])}
            self.surveys.append(d)
            if p.name == "survey.json" or not self.main:
                self.main = d
        if self.main.get("root"):
            self.root = Path(self.main["root"])
            if not self.root.is_dir():
                LOG.add("WARN", "src/survey.json", f"repository root {self.root} is gone; anchors are not checked")
                self.root = None
        self.web, self.host = self.main.get("web"), self.main.get("host")
        self.commit, self.dirty = self.main.get("commit"), set(self.main.get("dirty") or [])
        self.name = self.main.get("name") or "repository"
        self.date = self.main.get("date")
        self.cache, self.idents, self.messages = {}, None, {}

    def lines(self, rel):
        rel = re.sub(r"^\./", "", rel.strip())
        if rel in self.cache:
            return self.cache[rel]
        out = None
        if self.root:
            p = (self.root / rel).resolve()
            try:
                p.relative_to(self.root.resolve())
                if p.is_file():
                    out = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except (ValueError, OSError):
                out = None
        self.cache[rel] = out
        return out

    def anchor(self, rel, a, b):
        """-> (ok, problem)"""
        if not self.root:
            return None, None
        ls = self.lines(rel)
        if ls is None:
            return False, f"{rel} is not a file in the repository"
        if a and (a > len(ls) or (b and (b < a or b > len(ls)))):
            return False, f"lines {a}-{b or a} are outside {rel} ({len(ls)} lines)"
        return True, None

    def contains(self, quote, rel, a=None, b=None, slack=2):
        ls = self.lines(rel) if self.root else None
        if ls is None:
            return None
        window = ls[max(0, a - 1 - slack):(b or a) + slack] if a else ls
        parts = [code_norm(p) for p in re.split(r"\s*(?:\.\.\.|…)\s*", quote)]
        return find_parts([p for p in parts if p], code_norm("\n".join(window)))

    def excerpt(self, code_lines, rel, a=None, b=None):
        """A code block shown as an excerpt of rel:a-b; lines holding only '...' mark elisions."""
        ls = self.lines(rel) if self.root else None
        if ls is None:
            return None
        segs, cur = [], []
        for line in code_lines:
            if ELISION.match(line):
                segs.append(cur)
                cur = []
            else:
                cur.append(line)
        segs.append(cur)
        parts = [code_norm("\n".join(s)) for s in segs if any(x.strip() for x in s)]
        window = ls[max(0, a - 3):(b or a) + 2] if a else ls
        return find_parts(parts, code_norm("\n".join(window)))

    def commit_message(self, sha):
        if not self.root:
            return None
        if sha not in self.messages:
            try:
                r = subprocess.run(["git", "-C", str(self.root), "show", "-s", "--format=%H%n%B", sha],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                self.messages[sha] = r.stdout if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                self.messages[sha] = None
        return self.messages[sha]

    def ident_exists(self, name):
        if not self.root:
            return None
        if self.idents is None:
            self.idents = set()
            for rel, f in (self.main.get("files") or {}).items():
                if f.get("lang") and f.get("role") in ("source", "test", "example", "migration", "tooling"):
                    for line in self.lines(rel) or []:
                        self.idents.update(re.findall(r"[A-Za-z_$][\w$]*", line))
        return name in self.idents

    def link(self, rel, a=None, b=None):
        rel = re.sub(r"^\./", "", rel)
        if self.web and self.commit and self.host and rel not in self.dirty:
            base = {"github": f"{self.web}/blob/{self.commit}/{rel}", "gitlab": f"{self.web}/-/blob/{self.commit}/{rel}",
                    "gitea": f"{self.web}/src/commit/{self.commit}/{rel}",
                    "bitbucket": f"{self.web}/src/{self.commit}/{rel}"}.get(self.host)
            if base:
                if not a:
                    return base
                b = b if b and b != a else None
                frag = {"github": f"#L{a}" + (f"-L{b}" if b else ""), "gitea": f"#L{a}" + (f"-L{b}" if b else ""),
                        "gitlab": f"#L{a}" + (f"-{b}" if b else ""), "bitbucket": f"#lines-{a}" + (f":{b}" if b else "")}
                return base + frag[self.host]
        if self.root:
            return (self.root / rel).resolve().as_uri()
        return "#"

    def commit_link(self, sha):
        if self.web and self.host:
            path = {"github": "commit", "gitea": "commit", "gitlab": "-/commit", "bitbucket": "commits"}[self.host]
            return f"{self.web}/{path}/{sha}"
        return "#"

    # module graph (graph:imports)

    def modules_named(self, name):
        name = plain(name).strip("[]* ").replace("\\", "/")
        hits = []
        for i, s in enumerate(self.surveys):
            for mid, m in (s.get("modules") or {}).items():
                if name in (mid, m.get("path"), mid.split("/")[-1]) or name in (m.get("files") or []):
                    hits.append((i, mid))
        return hits

    def reaches(self, i, a, b):
        edges = self.surveys[i]["_edges"]
        seen, todo = set(), [a]
        while todo:
            v = todo.pop()
            for x, y in edges:
                if x == v and y not in seen:
                    if y == b:
                        return True
                    seen.add(y)
                    todo.append(y)
        return False


# ---------- graph: a layered drawing of a small directed graph (Sugiyama, simplified) ----------

def svg_text(s):
    """Label text for SVG <text>: HTML is not allowed there, so `code` becomes a monospace tspan."""
    out = []
    for part in re.split(r"(`[^`]+`)", s or ""):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f'<tspan class="g-code">{esc(part[1:-1])}</tspan>')
        else:
            out.append(esc(re.sub(r"\*\*|__", "", part).replace("->", "→")))
    return "".join(out)


def text_w(s, size):
    s = s or ""
    mono = s.startswith("`") and s.endswith("`")
    return len(plain(s)) * size * (0.61 if mono else 0.56)


def parse_graph(lines, warn):
    nodes, edges = {}, []

    def node(raw):
        s = raw.strip()
        shape = None
        if s.startswith("*"):
            shape, s = "start", s[1:].strip()
        if len(s) > 1 and s.startswith("[") and s.endswith("]"):
            shape, s = "ext", s[1:-1].strip()
        if not s:
            return None
        n = nodes.setdefault(s, {"shape": "dot", "note": None})
        if shape:
            n["shape"] = shape
        return s

    for line in lines:
        s = line.strip()
        body, _, label = s.partition(" : ")
        parts = GRAPH_OP.split(body)
        if len(parts) == 1:
            name = node(parts[0])
            if name and label:
                nodes[name]["note"] = label.strip()
            continue
        names, ops = [node(p) for p in parts[0::2]], parts[1::2]
        if None in names:
            warn(f"graph line not understood: {s[:50]}")
            continue
        for k, op in enumerate(ops):
            if names[k] == names[k + 1]:
                warn(f"graph edge from {names[k]} to itself is not drawn")
                continue
            edges.append({"a": names[k], "b": names[k + 1], "style": {"->": "dep", "-->": "weak", "-/->": "never"}[op],
                          "label": label.strip() if label and k == len(ops) - 1 else None})
    return nodes, edges


def graph_layout(nodes, edges, horizontal):
    """Assign layers (longest path, cycles broken by DFS), order each layer by barycenters to cut
    crossings, then place nodes. Returns geometry for render_graph."""
    names = list(nodes)
    out = defaultdict(list)
    for k, e in enumerate(edges):
        if e["style"] != "never":  # an absence is not a dependency: it must not shape the layers
            out[e["a"]].append(k)
    state, back = {}, set()

    def dfs(u):
        state[u] = 1
        for k in out[u]:
            v = edges[k]["b"]
            if state.get(v) == 1:
                back.add(k)
            elif not state.get(v):
                dfs(v)
        state[u] = 2

    for n in names:
        if not state.get(n):
            dfs(n)
    dag = [(e["b"], e["a"]) if k in back else (e["a"], e["b"]) for k, e in enumerate(edges)]
    preds, succs = defaultdict(set), defaultdict(set)
    for k, (a, b) in enumerate(dag):
        if edges[k]["style"] != "never":
            preds[b].add(a)
            succs[a].add(b)
    layer = {}

    def lay(v):
        if v not in layer:
            layer[v] = 1 + max((lay(p) for p in preds[v]), default=-1)
        return layer[v]

    for n in names:
        lay(n)
    for n in names:  # pull sources down next to their first successor: shorter edges
        if not preds[n] and succs[n]:
            layer[n] = max(layer[n], min(layer[s] for s in succs[n]) - 1)
    upward = set()  # forbidden edges drawn from a lower node up to a higher one: not cycles
    for k, e in enumerate(edges):
        if e["style"] == "never":
            if layer[e["a"]] > layer[e["b"]]:
                dag[k] = (e["b"], e["a"])
                upward.add(k)
    nl = max(layer.values(), default=0) + 1
    vlayer = dict(layer)
    chains = []
    for k, (a, b) in enumerate(dag):
        chain = [a]
        for L in range(layer[a] + 1, layer[b]):
            d = f"\x00{k}.{L}"
            vlayer[d] = L
            chain.append(d)
        chains.append(chain + [b])
    rows = [[] for _ in range(nl)]
    for v in list(names) + [v for v in vlayer if v not in nodes]:
        rows[vlayer[v]].append(v)
    up, down = defaultdict(list), defaultdict(list)
    for ch in chains:
        for x, y in zip(ch, ch[1:]):
            down[x].append(y)
            up[y].append(x)
    pos = {v: i for row in rows for i, v in enumerate(row)}

    def crossings():
        c = 0
        for L in range(nl - 1):
            es = [(pos[x], pos[y]) for x in rows[L] for y in down[x]]
            for i in range(len(es)):
                for j in range(i + 1, len(es)):
                    c += (es[i][0] - es[j][0]) * (es[i][1] - es[j][1]) < 0
        return c

    best = (crossings(), [r[:] for r in rows])
    for it in range(16):
        sweep = range(1, nl) if it % 2 == 0 else range(nl - 2, -1, -1)
        nb = up if it % 2 == 0 else down
        for L in sweep:
            rows[L].sort(key=lambda v: sum(pos[u] for u in nb[v]) / len(nb[v]) if nb[v] else pos[v])
            for i, v in enumerate(rows[L]):
                pos[v] = i
        c = crossings()
        if c < best[0]:
            best = (c, [r[:] for r in rows])
    rows = best[1]

    R, gap = 5.0, (16.0 if horizontal else 22.0)
    lo, hi, half = {}, {}, {}
    for v in vlayer:
        if v in nodes:
            n = nodes[v]
            lw = max(text_w(v, 12.5), text_w(n["note"], 11) if n["note"] else 0)
            if horizontal:
                lo[v], hi[v], half[v] = R + 2, R + 18 + (13 if n["note"] else 0), max(R, lw / 2)
            else:
                lo[v], hi[v], half[v] = R + 2, R + 7 + lw, R
        else:
            lo[v] = hi[v] = 3.0
            half[v] = 3.0
    o = {}
    for row in rows:
        x = 0.0
        for i, v in enumerate(row):
            x = x + (hi[row[i - 1]] + gap + lo[v] if i else 0)
            o[v] = x
    for it in range(24):
        for L in (range(nl) if it % 2 == 0 else range(nl - 1, -1, -1)):
            row = rows[L]
            want = {v: (sum(o[u] for u in up[v] + down[v]) / len(up[v] + down[v])) if up[v] + down[v] else o[v] for v in row}
            xs = []
            for i, v in enumerate(row):
                x = want[v]
                if i:
                    x = max(x, xs[-1] + hi[row[i - 1]] + gap + lo[v])
                xs.append(x)
            shift = sum(want[v] - xs[i] for i, v in enumerate(row)) / len(row) if row else 0
            for i, v in enumerate(row):
                o[v] = xs[i] + shift
    pad = 10.0
    omin = min((o[v] - lo[v] for v in o), default=0)
    for v in o:
        o[v] += pad - omin
    elabel = defaultdict(float)  # widest edge label leaving each layer
    for k, e in enumerate(edges):
        if e["label"]:
            a = dag[k][0]
            elabel[layer[a]] = max(elabel[layer[a]], text_w(e["label"], 10.5))
    r = {}
    if horizontal:
        x, prev = pad, None
        for L in range(nl):
            w = max((half[v] for v in rows[L]), default=R)
            x = (x + w) if prev is None else (x + prev + max(56.0, elabel[L - 1] + 20) + w)
            for v in rows[L]:
                r[v] = x
            prev = w
    else:
        for L in range(nl):
            for v in rows[L]:
                r[v] = pad + 12 + L * 76.0
    xy = {v: ((r[v], o[v]) if horizontal else (o[v], r[v])) for v in vlayer}
    return {"xy": xy, "chains": chains, "back": back, "upward": upward, "lo": lo, "hi": hi, "half": half, "R": R, "pad": pad}


def render_graph(nodes, edges, horizontal, label_html):
    g = graph_layout(nodes, edges, horizontal)
    xy, R, pad = g["xy"], g["R"], g["pad"]
    parts, marks, labels, extent = [], [], [], [0.0, 0.0]

    def grow(x, y):
        extent[0], extent[1] = max(extent[0], x), max(extent[1], y)

    # boxes (x, y, w, h) already taken by text: node names and notes first, then edge labels as placed
    taken = []
    for name, n in nodes.items():
        x, y = xy[name]
        if horizontal:
            w = text_w(name, 12.5)
            taken.append((x - w / 2, y + R + 3, w, 13))
            if n["note"]:
                w = text_w(n["note"], 11)
                taken.append((x - w / 2, y + R + 17, w, 12))
        else:
            taken.append((x + R + 6, y - 8, text_w(name, 12.5), 13))
            if n["note"]:
                taken.append((x + R + 6, y + 7, text_w(n["note"], 11), 12))

    def free(box):
        x, y, w, h = box
        return all(x + w <= bx or bx + bw <= x or y + h <= by or by + bh <= y for bx, by, bw, bh in taken)

    def curve_at(a, b, t):
        """Point at t on the cubic drawn between layer points a and b (same control points as the path)."""
        (xa, ya), (xb, yb) = a, b
        if horizontal:
            c1, c2 = ((xa + xb) / 2, ya), ((xa + xb) / 2, yb)
        else:
            c1, c2 = (xa, (ya + yb) / 2), (xb, (ya + yb) / 2)
        u = 1 - t
        return tuple(u**3 * p + 3 * u * u * t * q + 3 * u * t * t * r + t**3 * s for p, q, r, s in zip(a, c1, c2, b))

    def place_label(chain_pts, label, gap):
        """First spot along the edge, middle segment first, where the label covers no other text."""
        w = text_w(label, 10.5)
        segs = list(zip(chain_pts, chain_pts[1:]))
        order = sorted(range(len(segs)), key=lambda i: abs(i - (len(segs) - 1) / 2))
        first = None
        for i in order:
            for t in (0.5, 0.38, 0.62, 0.26, 0.74):
                px, py = curve_at(*segs[i], t)
                if horizontal:
                    spots = [(px - w / 2, py - 15, "middle", px, py - 7), (px - w / 2, py + 4, "middle", px, py + 12)]
                else:
                    spots = [(px + gap, py - 5, "start", px + gap, py + 3.5), (px - gap - w, py - 5, "end", px - gap, py + 3.5)]
                for bx, by, anchor, tx, ty in spots:
                    if bx < 2 or by < 2:
                        continue
                    box = (bx, by, w, 11)
                    first = first or (box, anchor, tx, ty)
                    if free(box):
                        return box, anchor, tx, ty
        return first

    for k, e in enumerate(edges):
        chain = g["chains"][k]
        pts = [xy[v] for v in chain]
        rev = k in g["back"] or k in g["upward"]
        ax, ay = (1, 0) if horizontal else (0, 1)  # rank direction
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        if len(pts) == 2 and (x0 == x1 if horizontal else y0 == y1):
            # same layer (only a forbidden edge can do this): a shallow arc below the row
            ox, oy = (ay * (R + 1.5), ax * (R + 1.5)) if horizontal else (0, R + 1.5)
            sx, sy, ex, ey = x0 + ox, y0 + oy, x1 + ox, y1 + oy
            cx, cy = (sx + ex) / 2 + (30 if horizontal else 0), (sy + ey) / 2 + (0 if horizontal else 30)
            parts.append(f'<path class="g-edge {e["style"]}" d="M{sx:.1f} {sy:.1f}Q{cx:.1f} {cy:.1f} {ex:.1f} {ey:.1f}"/>')
            mx, my = (sx + 2 * cx + ex) / 4, (sy + 2 * cy + ey) / 4
            marks.append(f'<path class="g-x" d="M{mx - 4:.1f} {my - 4:.1f}L{mx + 4:.1f} {my + 4:.1f}M{mx - 4:.1f} {my + 4:.1f}L{mx + 4:.1f} {my - 4:.1f}"/>')
            if e["label"]:
                labels.append(f'<text class="g-elab" x="{mx + 8:.1f}" y="{my + 4:.1f}">{label_html(e["label"])}</text>')
            grow(max(sx, ex) + 30, my + 12)
            continue
        start_gap = R + (8 if rev else 1.5)
        end_gap = R + (1.5 if rev else 8)
        pts[0] = (x0 + ax * start_gap, y0 + ay * start_gap)
        pts[-1] = (x1 - ax * end_gap, y1 - ay * end_gap)
        d = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            if horizontal:
                xm = (xa + xb) / 2
                d += f" C{xm:.1f} {ya:.1f} {xm:.1f} {yb:.1f} {xb:.1f} {yb:.1f}"
            else:
                ym = (ya + yb) / 2
                d += f" C{xa:.1f} {ym:.1f} {xb:.1f} {ym:.1f} {xb:.1f} {yb:.1f}"
        cls = e["style"] if e["style"] != "dep" else ""
        cls += " back" if k in g["back"] else ""
        parts.append(f'<path class="g-edge {cls.strip()}" d="{d}"/>')
        # arrowhead along the rank axis: at the end, or at the start for a reversed (cycle) edge
        tip, dx, dy = ((pts[0], -ax, -ay) if rev else (pts[-1], ax, ay))
        tx, ty = tip[0] + dx * 7, tip[1] + dy * 7
        bx, by = tip[0] + dx * 0.5, tip[1] + dy * 0.5
        px, py = -dy * 3.4, dx * 3.4
        hc = "g-head never" if e["style"] == "never" else "g-head"
        marks.append(f'<path class="{hc}" d="M{tx:.1f} {ty:.1f}L{bx + px:.1f} {by + py:.1f}L{bx - px:.1f} {by - py:.1f}Z"/>')
        if len(chain) > 2:
            mx, my = xy[chain[len(chain) // 2]]
        else:
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if e["style"] == "never":
            marks.append(f'<path class="g-x" d="M{mx - 4:.1f} {my - 4:.1f}L{mx + 4:.1f} {my + 4:.1f}M{mx - 4:.1f} {my + 4:.1f}L{mx + 4:.1f} {my - 4:.1f}"/>')
        if e["label"]:
            spot = place_label([xy[v] for v in chain], e["label"], 9 if e["style"] == "never" else 5)
            if spot:
                box, anchor, tx, ty = spot
                taken.append(box)
                anchor_attr = "" if anchor == "start" else f' text-anchor="{anchor}"'
                labels.append(f'<text class="g-elab" x="{tx:.1f}" y="{ty:.1f}"{anchor_attr}>{label_html(e["label"])}</text>')
                grow(box[0] + box[2], box[1] + box[3])
    for name, n in nodes.items():
        x, y = xy[name]
        if n["shape"] == "ext":
            parts.append(f'<rect class="g-ext" x="{x - 4.6:.1f}" y="{y - 4.6:.1f}" width="9.2" height="9.2"/>')
        else:
            parts.append(f'<circle class="{"g-start" if n["shape"] == "start" else "g-dot"}" cx="{x:.1f}" cy="{y:.1f}" r="{R}"/>')
        mono = " g-mono" if name.startswith("`") and name.endswith("`") else ""
        if horizontal:
            labels.append(f'<text class="g-lab{mono}" x="{x:.1f}" y="{y + R + 13:.1f}" text-anchor="middle">{label_html(name)}</text>')
            if n["note"]:
                labels.append(f'<text class="g-note" x="{x:.1f}" y="{y + R + 26:.1f}" text-anchor="middle">{label_html(n["note"])}</text>')
            grow(x + g["half"][name], y + g["hi"][name])
        else:
            labels.append(f'<text class="g-lab{mono}" x="{x + R + 6:.1f}" y="{y + 4.3:.1f}">{label_html(name)}</text>')
            if n["note"]:
                labels.append(f'<text class="g-note" x="{x + R + 6:.1f}" y="{y + 17.5:.1f}">{label_html(n["note"])}</text>')
            grow(x + g["hi"][name], y + (22 if n["note"] else 10))
    w, h = extent[0] + pad, extent[1] + pad
    names = ", ".join(f"{plain(e['a'])} to {plain(e['b'])}" for e in edges[:16])
    svg = (f'<svg class="graph-svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}" role="img" '
           f'aria-label="{attr("Graph: " + names)}">' + "".join(parts) + "".join(marks) + "".join(labels) + "</svg>")
    used = {n["shape"] for n in nodes.values()} | {e["style"] for e in edges} | ({"back"} if g["back"] else set())
    legend = []
    if "start" in used:
        legend.append('<span><svg viewBox="0 0 12 12"><circle class="g-start" cx="6" cy="6" r="4.5"/></svg>start here</span>')
    if "ext" in used:
        legend.append('<span><svg viewBox="0 0 12 12"><rect class="g-ext" x="1.5" y="1.5" width="9" height="9"/></svg>outside the code</span>')
    if "weak" in used:
        legend.append('<span><svg viewBox="0 0 24 12"><path class="g-edge weak" d="M1 6H23"/></svg>indirect or optional</span>')
    if "never" in used:
        legend.append('<span><svg viewBox="0 0 24 12"><path class="g-edge never" d="M1 6H23"/><path class="g-x" d="M9 2L15 10M9 10L15 2"/></svg>must not depend</span>')
    if "back" in used:
        legend.append('<span><svg viewBox="0 0 24 12"><path class="g-edge back" d="M1 6H23"/></svg>closes a cycle</span>')
    legend_html = f'<div class="g-legend">{"".join(legend)}</div>' if legend else ""
    return f'<div class="dg-scroll">{svg}</div>{legend_html}', len(g["back"])


# ---------- page ----------

class Page:
    def __init__(self, path, bundle):
        self.bundle, self.slug = bundle, path.stem
        self.where = f"pages/{path.name}"
        self.is_start = self.slug == "index"
        self.id = "start" if self.is_start else self.slug
        raw = path.read_text(encoding="utf-8")
        self.meta, body, first = front_matter(raw)
        self.title = self.meta.get("title", "")
        if not self.title:
            self.log("ERROR", 1, "front matter needs a title (--- / title: ... / ---)")
            self.title = self.slug
        self.heads, self.ids, self.sections, self.internal = [], set(), [], []
        self.notes, self.uses, self.num, self.parsed = {}, {}, {}, {}
        self.figures = self.code_words = self.code_blocks = 0
        self.infobox = None
        self.has = {"recall": 0, "practice": 0, "background": 0, "graph": 0, "interfaces": 0, "skip": 0}
        self.verified = self.quotes = 0
        fenced = None
        for i, line in enumerate(raw.splitlines(), 1):
            m = FENCE.match(line)
            if m and (fenced is None or (m.group(2).startswith(fenced) and not m.group(3))):
                fenced = None if fenced else m.group(2)
                continue
            if not fenced and EMOJI.search(re.sub(r"(`+).+?\1", "", line)):
                self.log("WARN", i, "emoji or pictograph in prose; use words (Yes/No, Note, Warning)")
        self.body = "\n".join(self.blocks(body, first, top=True))

    def log(self, level, line, msg):
        LOG.add(level, f"{self.where}:{line}" if line else self.where, msg)

    def unique_id(self, base):
        hid, k = f"{self.id}--{base}", 2
        while hid in self.ids:
            hid, k = f"{self.id}--{base}-{k}", k + 1
        self.ids.add(hid)
        return hid

    # ----- blocks -----

    def interrupts(self, line):
        s = line.strip()
        return bool(FENCE.match(line) or DIRECTIVE.match(s) or s == ":::" or HEADING.match(s)
                    or FOOTDEF.match(s) or LIST.match(line) or s.startswith((">", "|")) or IMAGE.match(s))

    def blocks(self, lines, ln0, top=False):
        out, i, n = [], 0, len(lines)
        while i < n:
            line = lines[i]
            s = line.strip()
            ln = ln0 + i
            if not s:
                i += 1
                continue
            m = FENCE.match(line)
            if m:
                j = fence_end(lines, i, m.group(2))
                if j is None:
                    self.log("ERROR", ln, "code fence is never closed")
                    j = n
                out.append(self.fence(m.group(3), dedent(lines[i + 1:j], len(m.group(1))), ln))
                i = j + 1
                continue
            m = DIRECTIVE.match(s)
            if m:
                kind, arg = m.group(1).lower(), m.group(2).strip()
                if kind in HATNOTES:
                    out.append(f'<div class="hatnote">{HATNOTES[kind]}{self.inline(arg)}</div>')
                    i += 1
                    continue
                j = directive_end(lines, i)
                if j is None:
                    self.log("ERROR", ln, f":::{kind} is never closed by a line containing only :::")
                    j = n
                out.append(self.directive(kind, arg, lines[i + 1:j], ln + 1, ln))
                i = j + 1
                continue
            if s == ":::":
                self.log("ERROR", ln, "stray ::: with no open block")
                i += 1
                continue
            m = HEADING.match(s)
            if m:
                out.append(self.heading(len(m.group(1)), m.group(2), ln, top))
                i += 1
                continue
            m = FOOTDEF.match(s)
            if m:
                buf = [m.group(2)]
                i += 1
                while i < n and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                    buf.append(lines[i].strip())
                    i += 1
                if m.group(1) in self.notes:
                    self.log("ERROR", ln, f"footnote [^{m.group(1)}] is defined twice")
                self.notes[m.group(1)] = (" ".join(buf), ln)
                continue
            if s.startswith("|") and i + 1 < n and TABLE_SEP.match(lines[i + 1].strip()):
                j = i
                while j < n and lines[j].strip().startswith("|"):
                    j += 1
                out.append(self.table(lines[i:j], ln))
                i = j
                continue
            if s.startswith(">"):
                j, buf = i, []
                while j < n and lines[j].strip():
                    t = lines[j].lstrip()
                    buf.append(re.sub(r"^>\s?", "", t) if t.startswith(">") else t)
                    j += 1
                out.append("<blockquote>" + "\n".join(self.blocks(buf, ln)) + "</blockquote>")
                i = j
                continue
            if LIST.match(line):
                block, i = self.list_block(lines, i, ln0)
                out.append(block)
                continue
            m = IMAGE.match(s)
            if m:
                out.append(self.image(m.group(1), m.group(2), m.group(3), ln))
                i += 1
                continue
            if HR.match(s):
                out.append("<hr>")
                i += 1
                continue
            if i + 1 < n and lines[i + 1].startswith(": "):
                pairs, i = self.dl_pairs(lines, i, ln0)
                out.append(self.dl(pairs))
                continue
            j, buf = i, []
            while j < n and lines[j].strip():
                if j > i and (self.interrupts(lines[j]) or (j + 1 < n and lines[j + 1].startswith(": "))):
                    break
                buf.append(lines[j].strip())
                j += 1
            out.append(f"<p>{self.inline(chr(10).join(buf))}</p>")
            i = j
        return out

    def heading(self, level, text, ln, top):
        if level == 1:
            self.log("ERROR", ln, "use ## for sections; the page title comes from front matter")
            level = 2
        if level > 4:
            self.log("WARN", ln, "headings deeper than #### are shown as ####")
            level = 4
        h = self.inline(text)
        hid = self.unique_id(slugify(text))
        if level <= 3:
            self.heads.append((level, hid, h))
        if level <= 3 and SKIP_HEADING.search(text):
            self.has["skip"] += 1
        if level == 2 and top:
            self.sections.append([plain(text), 0, ln])
        return f'<h{level} id="{hid}">{h}</h{level}>'

    def list_block(self, lines, i, ln0):
        m = LIST.match(lines[i])
        ind, ordered = len(m.group(1)), m.group(2)[0].isdigit()
        start = int(re.match(r"\d+", m.group(2)).group()) if ordered else 1
        items, n, j = [], len(lines), i
        while j < n:
            line = lines[j]
            mm = LIST.match(line)
            if mm and len(mm.group(1)) == ind and mm.group(2)[0].isdigit() == ordered:
                gap = len(mm.group(3)) if len(mm.group(3)) <= 4 else 1
                items.append({"ln": ln0 + j, "cind": ind + len(mm.group(2)) + gap, "lines": [mm.group(4)]})
                j += 1
                continue
            cur = items[-1]
            nind = len(line) - len(line.lstrip())
            if not line.strip():
                k = j + 1
                while k < n and not lines[k].strip():
                    k += 1
                if k < n:
                    nxt = lines[k]
                    sib = LIST.match(nxt)
                    deeper = len(nxt) - len(nxt.lstrip()) >= min(cur["cind"], ind + 2)
                    if deeper or (sib and len(sib.group(1)) == ind and sib.group(2)[0].isdigit() == ordered):
                        cur["lines"].append("")
                        j += 1
                        continue
                break
            if nind >= min(cur["cind"], ind + 2):
                cur["lines"].append(line[min(nind, cur["cind"]):])
                j += 1
                continue
            if cur["lines"][-1].strip() and not self.interrupts(line):
                cur["lines"].append(line.strip())
                j += 1
                continue
            break
        lis = []
        for it in items:
            parts = self.blocks(it["lines"], it["ln"])
            if parts and parts[0].startswith("<p>"):
                parts[0] = parts[0][3:-4]
            lis.append("<li>" + "\n".join(parts) + "</li>")
        tag = "ol" if ordered else "ul"
        st = f' start="{start}"' if ordered and start != 1 else ""
        return f"<{tag}{st}>" + "".join(lis) + f"</{tag}>", j

    def dl_pairs(self, lines, i, ln0):
        pairs, n = [], len(lines)
        while i < n:
            if not lines[i].strip():
                k = i
                while k < n and not lines[k].strip():
                    k += 1
                if k + 1 < n and lines[k + 1].startswith(": ") and not self.interrupts(lines[k]):
                    i = k
                    continue
                break
            if not (i + 1 < n and lines[i + 1].startswith(": ")):
                break
            term, tln, defs = lines[i].strip(), ln0 + i, []
            i += 1
            while i < n and lines[i].startswith(": "):
                buf, dln = [lines[i][2:]], ln0 + i
                i += 1
                while i < n and (lines[i].startswith("  ") or
                                 (not lines[i].strip() and i + 1 < n and lines[i + 1].startswith("  "))):
                    buf.append(lines[i][2:])
                    i += 1
                defs.append((buf, dln))
            pairs.append((term, tln, defs))
        return pairs, i

    def dl(self, pairs):
        out = []
        for term, _, defs in pairs:
            out.append(f"<dt>{self.inline(term)}</dt>")
            out.extend("<dd>" + "\n".join(self.blocks(buf, dln)) + "</dd>" for buf, dln in defs)
        return "<dl>" + "".join(out) + "</dl>"

    def table(self, rows, ln):
        def cells(row):
            row = row.strip()
            row = row[1:] if row.startswith("|") else row
            row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
            out, buf, code, k = [], [], False, 0
            while k < len(row):
                c = row[k]
                if c == "\\" and k + 1 < len(row) and row[k + 1] == "|":
                    buf.append("|")
                    k += 2
                    continue
                if c == "`":
                    code = not code
                if c == "|" and not code:
                    out.append("".join(buf).strip())
                    buf = []
                else:
                    buf.append(c)
                k += 1
            out.append("".join(buf).strip())
            return out

        head = cells(rows[0])
        aligns = []
        for c in cells(rows[1]):
            aligns.append(' style="text-align:center"' if c.startswith(":") and c.endswith(":")
                          else ' style="text-align:right"' if c.endswith(":") else "")
        aligns += [""] * len(head)
        out = ['<div class="table-wrap"><table class="wikitable"><thead><tr>']
        out += [f'<th scope="col"{aligns[k]}>{self.inline(c)}</th>' for k, c in enumerate(head)]
        out.append("</tr></thead><tbody>")
        for r, row in enumerate(rows[2:]):
            c = cells(row)
            if len(c) != len(head):
                self.log("WARN", ln + 2 + r, f"table row has {len(c)} cells; the header has {len(head)}")
            c = (c + [""] * len(head))[:len(head)]
            out.append("<tr>" + "".join(f"<td{aligns[k]}>{self.inline(x)}</td>" for k, x in enumerate(c)) + "</tr>")
        out.append("</tbody></table></div>")
        return "".join(out)

    def image(self, alt, url, title, ln):
        self.figures += 1
        if not alt:
            self.log("WARN", ln, "image needs alt text")
        host = urlsplit(url).netloc
        credit = f' <span class="credit">Image: {esc(host)}.</span>' if host else ""
        return (f'<figure class="image"><img src="{attr(url)}" alt="{attr(alt)}" loading="lazy">'
                f'<figcaption><span class="fig-n">Figure {self.figures}.</span>{self.inline(title or alt)}'
                f'{credit}</figcaption></figure>')

    # ----- inline -----

    def inline(self, text, ph=False, in_link=False):
        out, pos = [], 0
        for m in INLINE.finditer(text):
            if m.start() > pos:
                out.append(smart(text[pos:m.start()], text[pos - 1] if pos else " "))
            pos = m.end()
            g = m.group
            if g("code") is not None:
                c = g("ct")
                if len(c) > 2 and c[0] == " " and c[-1] == " " and c.strip():
                    c = c[1:-1]
                nw = ' class="nw"' if len(c) <= 28 else ""
                c = esc(c)
                out.append(f"<code{nw}>" + (PH.sub(r"<var>\1</var>", c) if ph else c) + "</code>")
            elif g("fn") is not None:
                out.append(self.cite(g("fk")))
            elif g("img") is not None:
                out.append(f'<img class="inline" src="{attr(g("iu"))}" alt="{attr(g("ia"))}">')
            elif g("link") is not None:
                if in_link:
                    out.append(esc(m.group(0)))
                    continue
                title = f' title="{attr(g("ltitle"))}"' if g("ltitle") else ""
                out.append(f'<a href="{attr(self.href(g("lu")))}"{title}>{self.inline(g("lt"), ph, True)}</a>')
            elif g("auto") is not None:
                out.append(f'<a href="{attr(g("au"))}">{esc(g("au"))}</a>')
            elif g("strong") is not None:
                out.append(f"<strong>{self.inline(g('st'), ph, in_link)}</strong>")
            elif g("em") is not None:
                out.append(f"<em>{self.inline(g('et'), ph, in_link)}</em>")
            elif g("em2") is not None:
                out.append(f"<em>{self.inline(g('e2t'), ph, in_link)}</em>")
            elif g("esc") is not None:
                out.append(esc(g("ec")))
        if pos < len(text):
            out.append(smart(text[pos:], text[pos - 1] if pos else " "))
        return "".join(out)

    def cite(self, key):
        self.uses[key] = self.uses.get(key, 0) + 1
        if self.sections:
            self.sections[-1][1] += 1
        return f"\x00F{key}\x01{self.uses[key]}\x00"

    def href(self, url):
        u = html.unescape(url.strip())
        if re.match(r"(?i)^\s*(javascript|vbscript|data):", u):
            self.log("WARN", 0, f"unsafe link removed: {u[:40]}")
            return "#"
        m = re.match(r"^(?:\./)?([\w.-]+)\.md(?:#([\w-]+))?$", u)
        if m:
            pid = "start" if m.group(1) == "index" else m.group(1)
            target = f"{pid}--{m.group(2)}" if m.group(2) else pid
            self.internal.append(target)
            return "#" + target
        if u.startswith("#") and len(u) > 1:
            frag = u[1:]
            target = frag if frag in self.bundle.slugs or "--" in frag else f"{self.id}--{frag}"
            self.internal.append(target)
            return "#" + target
        m = ANCHOR.match(u)
        if m and not re.match(r"(?i)^[a-z][a-z0-9+.-]*:", u):
            a, b = int(m.group("a") or 0), int(m.group("b") or 0)
            ok, problem = self.bundle.repo.anchor(m.group("p"), a, b)
            if ok is False:
                self.log("WARN", 0, f"link {u}: {problem}")
            return self.bundle.repo.link(m.group("p"), a, b)
        return u

    # ----- fenced code and diagrams -----

    def fence(self, info, body, ln, ph=False, extra=""):
        lang, _, rest = info.strip().partition(" ")
        lang, rest = lang.lower(), rest.strip()
        base, _, opt = lang.partition(":")
        base = "sequence" if base == "seq" else base
        if base in DIAGRAM_LIMITS:
            return self.diagram(base, opt, rest, body, ln)
        code = "\n".join(body)
        self.code_words += words(code)
        self.code_blocks += 1
        if base == "output":
            return (f'<figure class="code output"><figcaption><span>{esc(rest or "Output")}</span></figcaption>'
                    f"<pre><code>{esc(code)}</code></pre></figure>")
        cap = ""
        if rest:
            lang_tag = f'<span class="code-lang">{esc(base)}</span>' if base else ""
            label = esc(rest)
            m = ANCHOR.match(rest)
            repo = self.bundle.repo
            if m and ("/" in m.group("p") or "." in m.group("p")) and repo.root:
                a, b = int(m.group("a") or 0), int(m.group("b") or 0)
                ok, problem = repo.anchor(m.group("p"), a, b)
                if ok is False and a:
                    self.log("WARN", ln, f"excerpt label {rest}: {problem}")
                elif ok:
                    self.quotes += 1
                    if repo.excerpt(body, m.group("p"), a, b):
                        self.verified += 1
                        if self.sections:
                            self.sections[-1][1] += 1
                    else:
                        self.log("WARN", ln, f"excerpt does not match {rest}; copy the lines exactly, mark cuts with a '...' line")
                    label = f'<a href="{attr(repo.link(m.group("p"), a, b))}">{esc(rest)}</a>'
            cap = f'<figcaption><span class="code-label">{label}</span><span class="cap-right">{lang_tag}</span></figcaption>'
        return f'<figure class="code{extra}">{cap}<pre><code>{highlight(code, base, ph)}</code></pre></figure>'

    def diagram(self, kind, opt, caption, body, ln):
        lines = [l for l in body if l.strip() and not l.strip().startswith("//")]
        extra, count = "", 0
        if kind == "svg":
            inner = sanitize_svg("\n".join(body))
            if not inner.lower().startswith("<svg"):
                self.log("ERROR", ln, "svg block must contain a single <svg> element")
        elif kind == "graph":
            opts = {o.strip() for o in opt.split(",") if o.strip()}
            nodes, edges = parse_graph(lines, lambda msg: self.log("WARN", ln, msg))
            if not nodes:
                self.log("ERROR", ln, "graph has no nodes")
                return ""
            if len(edges) > MAX_GRAPH_EDGES:
                self.log("WARN", ln, f"graph has {len(edges)} edges; keep it to {MAX_GRAPH_EDGES}: draw the transitive reduction, or split the figure")
            if "imports" in opts:
                self.check_imports(nodes, edges, ln)
            inner, cycles = render_graph(nodes, edges, "h" in opts, svg_text)
            if cycles:
                self.log("NOTE", ln, f"graph has {cycles} edge(s) closing a cycle, drawn dashed; merge the nodes unless the cycle is the point")
            self.has["graph"] += 1
            count = len(nodes)
        elif kind == "flow":
            inner, count, vertical = self.d_flow(lines, opt)
            extra = " v" if vertical else ""
        elif kind == "sequence":
            inner, count = self.d_seq(lines, ln)
        elif kind == "stack":
            inner, count = self.d_stack(lines)
        elif kind == "tree":
            inner, count = self.d_tree(lines)
        else:
            inner, count = self.d_nest(lines)
        limit = DIAGRAM_LIMITS[kind]
        if limit and count > limit:
            self.log("WARN", ln, f"{kind} diagram has {count} items; keep it to {limit} or split it")
        if not caption:
            self.log("WARN", ln, f"{kind} diagram has no caption; say what the reader should notice")
        self.figures += 1
        cap = f'<figcaption><span class="fig-n">Figure {self.figures}.</span>{self.inline(caption)}</figcaption>'
        return f'<figure class="dg {kind}{extra}">{inner}{cap}</figure>'

    def check_imports(self, nodes, edges, ln):
        """graph:imports: every -> must be a real import between modules, every -/-> a real absence."""
        repo = self.bundle.repo
        if not repo.surveys:
            self.log("WARN", ln, "graph:imports needs src/survey.json; run survey.py")
            return
        found = {}
        for name, n in nodes.items():
            if n["shape"] == "ext":
                continue
            found[name] = repo.modules_named(name)
            if not found[name]:
                self.log("WARN", ln, f"graph node '{plain(name)}' is not a surveyed module (use the module or file names survey.py printed, or [brackets] for things outside the code)")
        for e in edges:
            ha, hb = found.get(e["a"]), found.get(e["b"])
            if not ha or not hb or e["style"] == "weak":
                continue
            pairs = [(i, ma, mb) for i, ma in ha for j, mb in hb if i == j and ma != mb]
            direct = any((ma, mb) in repo.surveys[i]["_edges"] for i, ma, mb in pairs)
            path = any(repo.reaches(i, ma, mb) for i, ma, mb in pairs)
            a, b = plain(e["a"]), plain(e["b"])
            self.quotes += 1
            before = self.verified
            if e["style"] == "never":
                if path:
                    self.log("WARN", ln, f"invariant broken: {a} does depend on {b} (directly or through other modules)")
                else:
                    self.verified += 1
            elif direct:
                self.verified += 1
            elif path:
                self.verified += 1
                self.log("NOTE", ln, f"{a} reaches {b} only through other modules; fine for a reduced map")
            else:
                self.log("WARN", ln, f"no import from {a} to {b} in the survey; draw it with --> if it is a runtime or data link")
            if self.verified > before and self.sections:
                self.sections[-1][1] += 1

    def flow_item(self, label, note):
        n = f'<span class="fl-note">{self.inline(note)}</span>' if note else ""
        return f'<span class="fl-label">{self.inline(label)}</span>{n}'

    def d_flow(self, lines, opt):
        nodes, labels, loop, pending = [], {}, None, None
        for line in lines:
            s = line.strip()
            if s.startswith("->"):
                pending = s[2:].strip()
                continue
            if s.lower().startswith("loop:"):
                loop = s[5:].strip()
                continue
            if nodes and pending:
                labels[len(nodes) - 1] = pending
            pending = None
            nodes.append([split_note(a) for a in re.split(r"\s+\|\s+", s)])
        size = sum(len(a) + len(b or "") for alts in nodes for a, b in alts)
        vertical = opt == "v" or (opt != "h" and (len(nodes) > 5 or size > 280))
        arrow = ('<svg class="fl-arrow" viewBox="0 0 32 12" aria-hidden="true">'
                 '<path d="M1 6H30M25 1.5L30 6L25 10.5"/></svg>')
        parts = []
        for k, alts in enumerate(nodes):
            if k:
                lab = labels.get(k - 1)
                lab = f'<span class="fl-elabel">{self.inline(lab)}</span>' if lab else ""
                parts.append(f'<div class="fl-edge">{lab}{arrow}</div>')
            if len(alts) == 1:
                parts.append(f'<div class="fl-node">{self.flow_item(*alts[0])}</div>')
            else:
                inner = '<span class="fl-or">or</span>'.join(
                    f'<span class="fl-alt">{self.flow_item(*a)}</span>' for a in alts)
                parts.append(f'<div class="fl-node">{inner}</div>')
        loop_html = ""
        if loop:
            loop_html = (f'<div class="fl-loop" aria-hidden="true"></div>'
                         f'<div class="fl-loop-label">↺ {self.inline(loop)}</div>')
        return f'<div class="fl-track">{"".join(parts)}</div>{loop_html}', len(nodes), vertical

    def d_seq(self, lines, ln):
        parts, rows = [], []

        def pid(name):
            name = name.strip()
            if name not in parts:
                parts.append(name)
            return parts.index(name)

        for line in lines:
            s = line.strip()
            m = re.match(r"(?i)^participant\s+(.+)$", s)
            if m:
                pid(m.group(1))
                continue
            m = re.match(r"(?i)^note\s+(.+?)\s*:\s*(.+)$", s)
            if m:
                who = [pid(x) for x in m.group(1).split(",")]
                rows.append(("note", min(who), max(who), plain(m.group(2)), False))
                continue
            m = re.match(r"^(.+?)\s*(-->|->)\s*(.+?)\s*:\s*(.*)$", s)
            if m:
                rows.append(("msg", pid(m.group(1)), pid(m.group(3)), plain(m.group(4)), m.group(2) == "-->"))
                continue
            self.log("WARN", ln, f"sequence line not understood: {s[:50]}")
        if not parts:
            return "", 0
        cw, lw, padx, top, bh = 7.6, 7.0, 6, 8, 30
        for kind, a, b, text, _ in rows:
            if len(text) > 56:
                self.log("WARN", ln, f"sequence label over 56 characters: {text[:40]}...")
        span = max(max(90, len(p) * cw + 26) for p in parts) + 24
        for kind, a, b, text, _ in rows:
            w = len(text) * lw + 30
            if kind == "msg" and a != b:
                span = max(span, w / abs(b - a))
            elif kind == "note":
                span = max(span, (w + 10) / (b - a + 1))

        def x(i):
            return padx + span / 2 + i * span

        y, g = top + bh + 24, []
        extra = 0.0
        for kind, a, b, text, reply in rows:
            rc = " reply" if reply else ""
            if kind == "note":
                if a == b:
                    w = len(text) * lw + 22
                    x0 = x(a) - w / 2
                else:
                    x0, w = x(a) - span / 2 + 10, x(b) - x(a) + span - 20
                g.append(f'<rect class="sq-note" x="{x0:.1f}" y="{y - 15:.1f}" width="{w:.1f}" height="24"/>')
                g.append(f'<text class="sq-lab" x="{x0 + w / 2:.1f}" y="{y + 1.5:.1f}" text-anchor="middle">{esc(text)}</text>')
                y += 36
            elif a == b:
                xa = x(a)
                extra = max(extra, xa + 40 + len(text) * lw - (x(len(parts) - 1) + span / 2))
                g.append(f'<text class="sq-lab" x="{xa + 40:.1f}" y="{y + 6:.1f}">{esc(text)}</text>')
                g.append(f'<path class="sq-msg{rc}" d="M{xa + 1:.1f} {y - 6:.1f}H{xa + 30:.1f}V{y + 12:.1f}H{xa + 9:.1f}"/>')
                g.append(f'<path class="sq-head{rc}" d="M{xa + 1:.1f} {y + 12:.1f}L{xa + 9:.1f} {y + 8:.1f}L{xa + 9:.1f} {y + 16:.1f}Z"/>')
                y += 42
            else:
                x1, x2 = x(a), x(b)
                d = 1 if x2 > x1 else -1
                g.append(f'<text class="sq-lab" x="{(x1 + x2) / 2:.1f}" y="{y - 7:.1f}" text-anchor="middle">{esc(text)}</text>')
                g.append(f'<path class="sq-msg{rc}" d="M{x1 + d:.1f} {y:.1f}H{x2 - 9 * d:.1f}"/>')
                g.append(f'<path class="sq-head{rc}" d="M{x2 - d:.1f} {y:.1f}L{x2 - 9 * d:.1f} {y - 4:.1f}L{x2 - 9 * d:.1f} {y + 4:.1f}Z"/>')
                y += 40
        height = y - 10
        width = x(len(parts) - 1) + span / 2 + padx + max(0.0, extra + 8)
        frame = []
        for i, p in enumerate(parts):
            bw = max(90, len(p) * cw + 26)
            frame.append(f'<line class="sq-life" x1="{x(i):.1f}" y1="{top + bh:.1f}" x2="{x(i):.1f}" y2="{height:.1f}"/>')
            frame.append(f'<rect class="sq-box" x="{x(i) - bw / 2:.1f}" y="{top}" width="{bw:.1f}" height="{bh}"/>')
            frame.append(f'<text class="sq-who" x="{x(i):.1f}" y="{top + bh / 2 + 4.5:.1f}" text-anchor="middle">{esc(plain(p))}</text>')
        label = "Sequence diagram: " + ", ".join(plain(p) for p in parts)
        svg = (f'<svg class="seq-svg" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
               f'style="min-width:{width * 0.62:.0f}px" role="img" aria-label="{attr(label)}">'
               + "".join(frame) + "".join(g) + "</svg>")
        return f'<div class="dg-scroll">{svg}</div>', len(rows)

    def d_stack(self, lines):
        axis, items = None, []
        for line in lines:
            s = line.strip()
            if s[:2] in ("^ ", "v "):
                axis = ("up" if s[0] == "^" else "down", s[2:].strip())
                continue
            hi = s.startswith("* ")
            items.append((hi,) + split_note(s[2:] if hi else s))
        axis_html = f'<div class="st-axis {axis[0]}"><span>{self.inline(axis[1])}</span></div>' if axis else ""
        lis = []
        for hi, label, note in items:
            cls = ' class="hi"' if hi else ""
            n = f'<span class="st-note">{self.inline(note)}</span>' if note else ""
            lis.append(f'<li{cls}><span class="st-label">{self.inline(label)}</span>{n}</li>')
        return f'<div class="st-wrap">{axis_html}<ol class="st">{"".join(lis)}</ol></div>', len(items)

    def d_tree(self, lines):
        items = []
        for line in lines:
            raw = line.rstrip()
            body = raw.strip()
            m = re.match(r"^(.*?)\s+#\s?(.*)$", body)
            name, com = (m.group(1), m.group(2)) if m else (body, "")
            items.append([len(raw) - len(raw.lstrip()), name, com])
        levels = sorted({it[0] for it in items})
        for it in items:
            it[0] = levels.index(it[0])
        n = len(items)

        def last(idx, depth):
            for j in range(idx + 1, n):
                if items[j][0] < depth:
                    return True
                if items[j][0] == depth:
                    return False
            return True

        rows = []
        for k, (d, name, com) in enumerate(items):
            prefix = ""
            if d:
                for dd in range(1, d):
                    anc = [j for j in range(k) if items[j][0] == dd]
                    prefix += "    " if not anc or last(anc[-1], dd) else "│   "
                prefix += "└── " if last(k, d) else "├── "
            rows.append((prefix, name, com))
        width = max((len(p) + len(nm) for p, nm, _ in rows), default=0)
        out = []
        for p, nm, com in rows:
            cls = "t-dir" if nm.endswith("/") else "t-name"
            line = f'<span class="t-pre">{esc(p)}</span><span class="{cls}">{esc(nm)}</span>'
            if com:
                line += " " * (width - len(p) - len(nm) + 3) + f'<span class="t-com">{esc(com)}</span>'
            out.append(line)
        return f'<pre class="tree-pre">{chr(10).join(out)}</pre>', n

    def d_nest(self, lines):
        root = {"kids": [], "ind": -1}
        stack = [root]
        for line in lines:
            ind = len(line) - len(line.lstrip())
            label, note = split_note(line.strip())
            node = {"label": label, "note": note, "kids": [], "ind": ind}
            while stack[-1]["ind"] >= ind:
                stack.pop()
            stack[-1]["kids"].append(node)
            stack.append(node)

        def render(node, depth):
            note = f'<span class="nest-note">{self.inline(node["note"])}</span>' if node["note"] else ""
            kids = "".join(render(k, depth + 1) for k in node["kids"])
            kids = f'<div class="nest-kids">{kids}</div>' if kids else ""
            return (f'<div class="nest-box d{min(depth, 3)}"><div class="nest-head">'
                    f'<span class="nest-label">{self.inline(node["label"])}</span>{note}</div>{kids}</div>')

        body = "".join(render(k, 0) for k in root["kids"])
        if len(root["kids"]) > 1:
            body = f'<div class="nest-kids">{body}</div>'
        return body, len(lines)

    # ----- containers -----

    def directive(self, kind, arg, inner, iln, ln):
        if kind in CALLOUTS or kind == "background":
            parts = self.blocks(inner, iln)
            if kind == "background":
                self.has["background"] += 1
                head = ('<span class="label">Background</span>'
                        '<span class="aside">General knowledge, not from the source documentation.</span>')
                title = f'<h4 class="callout-title">{self.inline(arg)}</h4>' if arg else ""
                return f'<aside class="background"><p class="callout-head">{head}</p>{title}{"".join(parts)}</aside>'
            label = f'<span class="label">{CALLOUTS[kind]}</span>'
            if arg:
                head = f'<p class="callout-head">{label}<span class="callout-title">{self.inline(arg)}</span></p>'
                return f'<aside class="callout {kind}">{head}{"".join(parts)}</aside>'
            if parts and parts[0].startswith("<p>"):
                parts[0] = "<p>" + label + parts[0][3:]
            else:
                parts.insert(0, f'<p class="callout-head">{label}</p>')
            return f'<aside class="callout {kind}">{"".join(parts)}</aside>'
        if kind == "infobox":
            if self.infobox is not None:
                self.log("ERROR", ln, "only one :::infobox per page")
            self.infobox = (arg, inner, iln)
            return ""
        if kind == "example":
            return self.example(arg, inner, iln, ln)
        if kind == "recipes":
            return self.recipes(inner, iln, ln)
        if kind == "exercise":
            self.has["practice"] += 1
            if not any(re.match(r"^[*_]*check[*_]*\s*:", l.strip(), re.I) for l in inner):
                self.log("WARN", ln, "exercise has no 'Check:' line saying how the reader knows it worked")
            title = f'<span class="callout-title">{self.inline(arg)}</span>' if arg else ""
            body = "\n".join(self.blocks(inner, iln))
            return f'<section class="exercise"><p class="callout-head"><span class="label">Exercise</span>{title}</p>{body}</section>'
        if kind == "recall":
            k = 0
            while k < len(inner) and not inner[k].strip():
                k += 1
            pairs, end = self.dl_pairs(inner, k, iln)
            if any(l.strip() for l in inner[end:]) or not pairs:
                self.log("WARN", ln, "recall expects question lines, each followed by an ': answer' line")
            items = []
            for term, _, defs in pairs:
                ans = "".join("\n".join(self.blocks(buf, dln)) for buf, dln in defs)
                items.append(f'<li><details><summary>{self.inline(term)}</summary><div class="answer">{ans}</div></details></li>')
            self.has["recall"] += len(items)
            return f'<ol class="recall">{"".join(items)}</ol>'
        if kind == "pages":
            return "\x00PAGES\x00"
        if kind == "interfaces":
            return self.interfaces(arg, inner, iln, ln)
        self.log("ERROR", ln, f"unknown block :::{kind} (known: {', '.join(sorted(CONTAINERS | set(HATNOTES)))})")
        return "\n".join(self.blocks(inner, iln))

    def example(self, title, inner, iln, ln):
        self.has["practice"] += 1
        rows, prose, pln, k = [], [], iln, 0
        while k < len(inner):
            m = FENCE.match(inner[k])
            if not m:
                prose.append(inner[k])
                k += 1
                continue
            end = fence_end(inner, k, m.group(2))
            if end is None:
                self.log("ERROR", iln + k, "code fence is never closed")
                end = len(inner)
            info = m.group(3).strip()
            code = self.fence(info, dedent(inner[k + 1:end], len(m.group(1))), iln + k)
            if info.split(" ")[0].lower() == "output" and rows and not any(l.strip() for l in prose):
                rows[-1][1].append(code)
            else:
                rows.append([(prose, pln), [code]])
            prose, pln, k = [], iln + end + 1, end + 1
        if any(l.strip() for l in prose):
            rows.append([(prose, pln), []])
        if not any(codes for _, codes in rows):
            self.log("WARN", ln, "example has no code; write plain paragraphs instead")
        out = []
        for (p, pl), codes in rows:
            docs = "\n".join(self.blocks(p, pl))
            cls = "ex-code" if codes else "ex-code empty"
            out.append(f'<div class="ex-row"><div class="ex-docs">{docs}</div><div class="{cls}">{"".join(codes)}</div></div>')
        t = f'<span class="callout-title">{self.inline(title)}</span>' if title else ""
        return f'<figure class="example"><figcaption><span class="label">Example</span>{t}</figcaption>{"".join(out)}</figure>'

    def signature(self, s):
        """X -> Y in mathematical dress: arrows and products as symbols, code spans untouched."""
        out = []
        for part in re.split(r"(<code[^>]*>.*?</code>)", self.inline(s)):
            if not part.startswith("<code"):
                for a, b in (("|-&gt;", "↦"), ("-&gt;", "→"), ("=&gt;", "⇒"), ("&lt;-", "←"), (" * ", " × ")):
                    part = part.replace(a, f'<span class="if-op">{b}</span>')
            out.append(part)
        return "".join(out)

    def interfaces(self, title, inner, iln, ln):
        """name : Domain -> Codomain  # note. `name` in backticks is a code symbol and is checked."""
        rows, repo = [], self.bundle.repo
        for k, line in enumerate(inner):
            s = line.strip()
            if not s:
                continue
            if s.startswith("## "):
                rows.append(f'<tr class="if-group"><th colspan="4">{self.inline(s[3:])}</th></tr>')
                continue
            m = re.match(r"^(?P<name>.+?)\s+:\s+(?P<sig>.+?)(?:\s+#\s+(?P<note>.*))?$", s)
            if not m:
                self.log("WARN", iln + k, "interfaces lines are 'name : X -> Y  # note'")
                continue
            name = m.group("name").strip()
            cm = re.fullmatch(r"`([^`]+)`", name)
            if cm:
                ident = re.split(r"\.|::|#|/", re.sub(r"\(.*\)$", "", cm.group(1)).strip())[-1]
                if ident and repo.ident_exists(ident) is False:
                    self.log("WARN", iln + k, f"interface {name} is not named anywhere in the repository")
            note = f'<td class="if-note">{self.inline(m.group("note"))}</td>' if m.group("note") else "<td></td>"
            rows.append(f'<tr><td class="if-name">{self.inline(name)}</td><td class="if-colon">:</td>'
                        f'<td class="if-sig">{self.signature(m.group("sig"))}</td>{note}</tr>')
            self.has["interfaces"] += 1
        cap = f'<figcaption><span class="label">Interfaces</span><span class="callout-title">{self.inline(title)}</span></figcaption>' if title else ""
        return f'<figure class="interfaces">{cap}<div class="table-wrap"><table class="if-table"><tbody>{"".join(rows)}</tbody></table></div></figure>'

    def recipes(self, inner, iln, ln):
        self.has["practice"] += 1
        items, k = [], 0
        while k < len(inner):
            line, s = inner[k], inner[k].strip()
            if not s:
                k += 1
                continue
            m = FENCE.match(line)
            if m:
                end = fence_end(inner, k, m.group(2)) or len(inner)
                if items:
                    body = dedent(inner[k + 1:end], len(m.group(1)))
                    items[-1]["code"].append(self.fence(m.group(3) or "sh", body, iln + k, ph=True, extra=" cmd"))
                else:
                    self.log("WARN", iln + k, "recipe code before any '- description:' line")
                k = end + 1
                continue
            if s.startswith("- "):
                items.append({"desc": [s[2:]], "code": [], "ln": iln + k})
                k += 1
                continue
            cm = re.match(r"^(`+)\s?(.+?)\s?\1$", s)
            if cm and items:
                self.code_words += words(cm.group(2))
                self.code_blocks += 1
                items[-1]["code"].append(f'<figure class="code cmd"><pre><code>{highlight(cm.group(2), "sh", True)}</code></pre></figure>')
                k += 1
                continue
            if items and not items[-1]["code"]:
                items[-1]["desc"].append(s)
            else:
                self.log("WARN", iln + k, "text in :::recipes must be '- description:' followed by a `command` line")
            k += 1
        if len(items) > MAX_RECIPES:
            self.log("WARN", ln, f"{len(items)} recipes; keep the {MAX_RECIPES} most common and move the rest to a reference table")
        lis = []
        for it in items:
            if not it["code"]:
                self.log("WARN", it["ln"], "recipe has no command")
            lis.append(f'<li><p class="rc-desc">{self.inline(" ".join(it["desc"]))}</p>{"".join(it["code"])}</li>')
        return f'<ul class="recipes">{"".join(lis)}</ul>'

    # ----- infobox and references -----

    def render_infobox(self):
        if not self.infobox:
            return ""
        arg, inner, iln = self.infobox
        rows = []
        for k, line in enumerate(inner):
            s = line.strip()
            if not s:
                continue
            if s.startswith("## "):
                rows.append(f'<tr><th colspan="2" class="ib-group">{self.inline(s[3:])}</th></tr>')
                continue
            m = IMAGE.match(s)
            if m:
                cap = f"<figcaption>{self.inline(m.group(3))}</figcaption>" if m.group(3) else ""
                rows.append(f'<tr><td colspan="2" class="ib-image"><figure><img src="{attr(m.group(2))}" '
                            f'alt="{attr(m.group(1))}" loading="lazy">{cap}</figure></td></tr>')
                continue
            m = re.match(r"^([^:]{1,40}):\s+(.+)$", s)
            if not m:
                self.log("WARN", iln + k, "infobox lines are 'Label: value', '## Group' or an image")
                continue
            rows.append(f'<tr><th scope="row">{self.inline(m.group(1))}</th><td>{self.inline(m.group(2))}</td></tr>')
        return (f'<table class="infobox"><tbody><tr><th colspan="2" class="ib-title">{self.inline(arg) if arg else esc(self.title)}</th></tr>'
                + "".join(rows) + "\x00IBAUTO\x00</tbody></table>")

    def parse_note(self, key):
        if key in self.parsed:
            return self.parsed[key]
        text, ln = self.notes[key]
        repo = self.bundle.repo
        info = None

        def count(ok, what):
            if ok is None:
                return
            self.quotes += 1
            if ok:
                self.verified += 1
            else:
                self.log("WARN", ln, f"[^{key}]: {what}")

        m = NOTE_COMMIT.match(text)
        if m:
            sha, q = m.group("h"), (m.group("q") or "").replace('\\"', '"') or None
            msg = repo.commit_message(sha)
            if repo.root:
                ok = bool(msg) and (not q or find_parts([code_norm(p).lower() for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if p.strip()],
                                                         code_norm(msg).lower()))
                count(ok, f"commit {sha} not found" if not msg else f"quote not in the message of commit {sha}")
            info = {"q": q, "u": repo.commit_link(sha), "label": m.group("l") or f"commit {sha[:10]}", "ok": None}
        if not info:
            m = NOTE_CODE_QUOTE.match(text)
            if m and not m.group("p").startswith(("http", "commit:")):
                q, p = m.group("q").replace('\\"', '"'), m.group("p")
                a, b = int(m.group("a") or 0), int(m.group("b") or 0)
                ok, problem = repo.anchor(p, a, b)
                if ok:
                    found = repo.contains(q, p, a, b)
                    count(found, f"quote not found in {self.anchor_label(p, a, b)}: \"{q[:60]}\"")
                elif ok is False:
                    count(False, problem)
                if words(q) > MAX_QUOTE_WORDS:
                    self.log("WARN", ln, f"quote [^{key}] has {words(q)} words; quote one claim, at most {MAX_QUOTE_WORDS}")
                info = {"q": q, "u": repo.link(p, a, b), "label": m.group("l") or self.anchor_label(p, a, b), "ok": None}
        if not info:
            m = NOTE_CODE.match(text)
            if m and not m.group("p").startswith("http"):
                p, a, b = m.group("p"), int(m.group("a") or 0), int(m.group("b") or 0)
                ok, problem = repo.anchor(p, a, b)
                count(ok, problem or "")
                if ok and a and (b or a) - a + 1 > MAX_ANCHOR_LINES:
                    self.log("NOTE", ln, f"[^{key}] spans {(b or a) - a + 1} lines; point at the lines that prove the claim")
                info = {"q": None, "u": repo.link(p, a, b), "label": m.group("l") or self.anchor_label(p, a, b), "ok": None}
        if not info:
            m = NOTE_QUOTE.match(text)
            if m:
                self.log("NOTE", ln, f"[^{key}] quotes a web page; only repository files and commits are checked")
                info = {"q": m.group("q"), "u": m.group("u"), "label": m.group("l") or short_url(m.group("u")), "ok": None}
            else:
                m = NOTE_URL.match(text)
                if m:
                    info = {"q": None, "u": m.group("u"), "label": m.group("l") or short_url(m.group("u")), "ok": None}
                else:
                    info = {"q": None, "u": None, "label": None, "ok": None, "text": text}
        self.parsed[key] = info
        return info

    @staticmethod
    def anchor_label(p, a, b):
        return p + (f":{a}" + (f"-{b}" if b and b != a else "") if a else "")

    def number_refs(self, s):
        def rep(m):
            key, k = m.group(1), int(m.group(2))
            if key not in self.notes:
                return '<sup class="ref">[?]</sup>'
            if key not in self.num:
                self.num[key] = len(self.num) + 1
            n, info = self.num[key], self.parse_note(key)
            tip = (f"“{plain(info['q'])}” — {info['label']}" if info["q"]
                   else info["label"] or plain(info.get("text", "")))
            return f'<sup class="ref" id="{self.id}-r{n}-{k}"><a href="#{self.id}-n{n}" title="{attr(tip)}">[{n}]</a></sup>'
        return re.sub(r"\x00F(.+?)\x01(\d+)\x00", rep, s)

    def references(self):
        if not self.num:
            return ""
        items = []
        for key, n in sorted(self.num.items(), key=lambda kv: kv[1]):
            info, uses = self.parse_note(key), self.uses[key]
            if uses == 1:
                back = f'<a href="#{self.id}-r{n}-1" aria-label="Back to the text">^</a>'
            else:
                back = "^ " + " ".join(f'<a href="#{self.id}-r{n}-{k}">{chr(96 + k)}</a>' for k in range(1, uses + 1))
            if info["q"]:
                body = f'<q>{self.inline(info["q"])}</q> <cite><a href="{attr(info["u"])}">{esc(info["label"])}</a></cite>'
                if info["ok"] is False:
                    body += ' <span class="unverified">(does not match the code)</span>'
            elif info["u"]:
                body = f'<cite><a href="{attr(info["u"])}">{esc(info["label"])}</a></cite>'
            else:
                body = self.inline(info["text"])
            items.append(f'<li id="{self.id}-n{n}"><span class="back">{back}</span>{body}</li>')
        hid = self.unique_id("references")
        self.heads.append((2, hid, "References"))
        return f'<h2 id="{hid}">References</h2><ol class="references">{"".join(items)}</ol>'

    # ----- final assembly of one page -----

    def finish(self):
        """Number citations, run page-level checks, and return the article body pieces."""
        box = self.number_refs(self.render_infobox())
        body = self.number_refs(self.body)
        for key in self.uses:
            if key not in self.notes:
                self.log("ERROR", 0, f"[^{key}] is cited but never defined")
        for key, (_, ln) in self.notes.items():
            if key not in self.uses:
                self.log("WARN", ln, f"footnote [^{key}] is defined but never cited")
        text = strip_tags(body)
        prose = max(0, words(text) - self.code_words)
        self.minutes = max(1, math.ceil((prose + 1.5 * self.code_words) / WPM))
        lead = strip_tags(re.sub(r'<div class="hatnote">.*?</div>', "", body.split("<h2", 1)[0]))
        if not self.is_start:
            if not words(lead):
                self.log("WARN", 0, "no lead: open with 2-4 sentences that define the subject and say why it matters")
            elif words(lead) > MAX_LEAD_WORDS:
                self.log("WARN", 0, f"lead is {words(lead)} words; keep it under {MAX_LEAD_WORDS} and move detail into sections")
            if not self.infobox:
                self.log("WARN", 0, "no :::infobox with the facts a reader looks up first")
            if self.has["recall"] < 3:
                self.log("WARN", 0, f"{self.has['recall']} :::recall questions; add at least 3")
            if not self.has["practice"] and not self.code_blocks:
                self.log("WARN", 0, "nothing hands-on: add an :::example, :::recipes or :::exercise")
            if not self.uses:
                self.log("WARN", 0, "no citations; anchor claims about the code with [^key]: path:lines")
            uncited = [t for t, c, _ in self.sections if not c and not UNCITED_OK.search(t)]
            if uncited:
                self.log("NOTE", 0, "sections without citations: " + "; ".join(uncited))
            if not self.meta.get("summary"):
                self.log("WARN", 1, "front matter has no summary:")
            if self.minutes > LONG_PAGE_MIN:
                self.log("NOTE", 0, f"reads in about {self.minutes} min; consider splitting it")
        return box, body


# ---------- bundle ----------

class Bundle:
    def __init__(self, root):
        self.root = root
        self.repo = Repo(root / "src")
        files = sorted((root / "pages").glob("*.md"))
        self.slugs = {"start" if p.stem == "index" else p.stem for p in files}
        pages = [Page(p, self) for p in files]
        self.start = next((p for p in pages if p.is_start), None)
        rest = [p for p in pages if not p.is_start]
        order = [s.strip() for s in (self.start.meta.get("order", "") if self.start else "").split(",") if s.strip()]
        for s in order:
            if s not in {p.slug for p in rest}:
                LOG.add("WARN", "pages/index.md", f"order lists '{s}' but pages/{s}.md does not exist")
        rest.sort(key=lambda p: (order.index(p.slug) if p.slug in order else len(order), p.slug))
        self.rest = rest
        self.order = ([self.start] if self.start else []) + rest
        if not self.start and len(rest) > 1:
            LOG.add("WARN", "pages/", "several pages but no pages/index.md start page (title, order, the map)")
        if not self.repo.root:
            LOG.add("WARN", "src/", "no survey with a reachable repository; anchors, quotes and excerpts were not checked")
        first = self.start or (rest[0] if rest else None)
        if first:
            if not first.has["graph"]:
                LOG.add("WARN", first.where, "the first page has no ```graph map: draw the parts and how they connect")
            if not first.has["skip"]:
                LOG.add("WARN", first.where, "the first page has no 'Safe to skip' section: say what the reader can ignore, and why")
            if not any(p.has["interfaces"] for p in pages):
                LOG.add("WARN", "pages/", "no :::interfaces block anywhere: show what goes in and out of the parts to learn")
        self.title = (self.start.title if self.start else rest[0].title if len(rest) == 1 else f"{self.repo.name} in brief")
        self.source = self.repo.web or ""
        self.fetched = (first.meta.get("surveyed") if first else "") or self.repo.date or dt.date.today().isoformat()


def toc_sections(page):
    items, n2 = [], 0
    for level, hid, h in page.heads:
        if level == 2:
            n2, n3 = n2 + 1, 0
            items.append([f'<a href="#{hid}"><span class="tn">{n2}</span>{h}</a>', []])
        elif level == 3 and items:
            n3 += 1
            items[-1][1].append(f'<li><a href="#{hid}"><span class="tn">{n2}.{n3}</span>{h}</a></li>')
    if not items:
        return ""
    lis = "".join(f"<li>{a}" + (f'<ol class="toc-secs">{"".join(sub)}</ol>' if sub else "") + "</li>"
                  for a, sub in items)
    return f'<ol class="toc-secs">{lis}</ol>'


def navbox(b, current):
    if len(b.rest) < 2:
        return ""
    groups = {}
    for p in b.rest:
        groups.setdefault(p.meta.get("group") or "Pages", []).append(p)
    rows = []
    for g, ps in groups.items():
        links = '<span class="sep" aria-hidden="true">·</span>'.join(
            f'<span class="here">{esc(p.title)}</span>' if p is current else f'<a href="#{p.id}">{esc(p.title)}</a>'
            for p in ps)
        rows.append(f'<tr><th scope="row" class="nb-group">{esc(g)}</th><td>{links}</td></tr>')
    title = f'<a href="#start">{esc(b.title)}</a>' if b.start and current is not b.start else esc(b.title)
    return f'<table class="navbox"><tbody><tr><th colspan="2" class="nb-title">{title}</th></tr>{"".join(rows)}</tbody></table>'


def pager(b, page):
    k = b.order.index(page)
    prev = b.order[k - 1] if k else None
    nxt = b.order[k + 1] if k + 1 < len(b.order) else None
    if not prev and not nxt:
        return ""
    out = ""
    if prev:
        out += f'<a class="prev" href="#{prev.id}"><span>Previous:</span> {esc(prev.title)}</a>'
    if nxt:
        out += f'<a class="next" href="#{nxt.id}"><span>Next:</span> {esc(nxt.title)}</a>'
    return f'<nav class="pager" aria-label="Pages">{out}</nav>'


def contents_table(b, start):
    rows = []
    for k, p in enumerate(b.rest, 1):
        rows.append(f'<tr><td>{k}</td><td><a href="#{p.id}">{esc(p.title)}</a></td>'
                    f'<td>{p.inline(p.meta.get("summary", ""))}</td><td>{p.minutes} min</td></tr>')
    total = sum(p.minutes for p in b.rest)
    return ('<div class="table-wrap"><table class="wikitable contents-table"><thead><tr>'
            '<th scope="col">#</th><th scope="col">Page</th><th scope="col">What it covers</th>'
            f'<th scope="col">Time</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<p class="tagline">{len(b.rest)} pages, about {total} minutes in total.</p>')


def appearance():
    groups = [("size", "Text", [("s", "Small"), ("m", "Standard"), ("l", "Large")]),
              ("width", "Width", [("std", "Standard"), ("wide", "Wide")]),
              ("theme", "Color", [("auto", "Automatic"), ("light", "Light"), ("dark", "Dark")])]
    out = ['<form class="appearance" aria-label="Appearance"><div class="ap-title">Appearance</div>']
    for name, legend, opts in groups:
        out.append(f"<fieldset><legend>{legend}</legend>")
        out += [f'<label><input type="radio" name="{name}" value="{v}"> {t}</label>' for v, t in opts]
        out.append("</fieldset>")
    out.append("</form>")
    return "".join(out)


def document(b):
    articles, toc = [], []
    finished = {p.id: p.finish() for p in b.order}
    for p in b.order:
        box, body = finished[p.id]
        if p.is_start:
            table = contents_table(b, p)
            body = body.replace("\x00PAGES\x00", table) if "\x00PAGES\x00" in body else body + table
        auto = ""
        if box:
            r = b.repo
            auto = '<tr><th colspan="2" class="ib-group">This summary</th></tr>'
            if r.commit or r.root:
                name = f'<a href="{attr(r.web)}">{esc(r.name)}</a>' if r.web else esc(r.name)
                at = f' at <code>{esc(r.commit[:7])}</code>' if r.commit else ""
                dirty = " plus uncommitted changes" if r.dirty else ""
                auto += f'<tr><th scope="row">Code</th><td>{name}{at}{dirty}</td></tr>'
            if p.quotes:
                checked = (f"{p.verified} of {p.quotes} match the code" if r.root else f"{p.quotes}, not checked")
                auto += f'<tr><th scope="row">Anchors</th><td>{checked}</td></tr>'
            box = box.replace("\x00IBAUTO\x00", auto)
        cut = body.find("<h2")
        lead, rest = (body[:cut], body[cut:]) if cut >= 0 else (body, "")
        body = f'<div class="lead">{lead}</div>{rest}'
        refs = p.references()
        scope = p.meta.get("scope", "")
        bits = []
        if scope:
            bits.append("Covers " + ", ".join(p.inline(f"`{s.strip()}`") for s in scope.split(",") if s.strip()))
        date = p.meta.get("surveyed") or b.fetched
        bits.append(f"surveyed {esc(nice_date(date))}")
        bits.append(f"{sum(q.minutes for q in b.rest) if p.is_start else p.minutes} min read")
        tagline = '<p class="tagline">' + SEP.join(bits) + "</p>"
        articles.append(
            f'<article class="page" id="{p.id}" data-title="{attr(p.title)}">'
            f'<header><h1>{esc(p.title)}</h1>{tagline}</header>'
            f'<div class="page-body">{box}{body}</div>{refs}{pager(b, p)}{navbox(b, p)}</article>')
        active = ' class="active"' if p is b.order[0] else ""
        name = "Start here" if p.is_start else esc(p.title)
        toc.append(f'<li data-page="{p.id}"{active}><a href="#{p.id}">{name}</a>{toc_sections(p)}</li>')
    known = {p.id for p in b.order} | {hid for p in b.order for hid in p.ids}
    known |= {f"{p.id}-n{n}" for p in b.order for n in p.num.values()}
    for p in b.order:
        for target in p.internal:
            if target not in known:
                LOG.add("WARN", p.where, f"internal link #{target} points nowhere")

    total_q = sum(p.quotes for p in b.order)
    total_v = sum(p.verified for p in b.order)
    minutes = sum(p.minutes for p in b.rest) or sum(p.minutes for p in b.order)
    r = b.repo
    first = b.order[0].id if b.order else "start"
    where = f'<a href="{attr(r.web)}">{esc(r.name)}</a>' if r.web else esc(r.name)
    meta = [f"Surveyed from {where}" + (f" at <code>{esc(r.commit[:7])}</code>" if r.commit else "")]
    meta.append(f"{len(b.rest)} page{'s' if len(b.rest) != 1 else ''}")
    meta.append(f"{minutes} min")
    check = (f"{total_v} of {total_q} anchors, quotes and excerpts matched the code surveyed on {esc(nice_date(b.fetched))}."
             if r.root and total_q else "Anchors were not checked against the code.")
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    boot = ('document.documentElement.classList.add("js");'
            'try{var a=JSON.parse(localStorage.getItem("tlds-appearance"))||{},r=document.documentElement;'
            '["size","width","theme"].forEach(function(k){if(a[k])r.setAttribute("data-"+k,a[k])})}catch(e){}')
    return f"""<!doctype html>
<html lang="{attr(b.order[0].meta.get('lang', 'en') if b.order else 'en')}" data-theme="auto" data-size="m" data-width="std">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="tlds skill">
<title>{esc(b.title)}</title>
<script>{boot}</script>
{FONTS}
<style>
{css}
</style>
</head>
<body data-bundle="{attr(b.title)}">
<a class="skip" href="#content">Skip to content</a>
<header class="masthead"><a class="mh-title" href="#{first}">{esc(b.title)}</a><span class="mh-meta">{SEP.join(meta)}</span></header>
<div class="layout">
<nav class="toc" aria-label="Contents"><details open><summary>Contents</summary><ol class="toc-pages">{"".join(toc)}</ol>{appearance()}</details></nav>
<main id="content">
{chr(10).join(articles)}
</main>
</div>
<footer class="colophon"><p>A study summary of a code repository, written with the tlds skill. {check} The code is authoritative; follow the file and line links before relying on a detail.</p></footer>
<script>
{js}
</script>
</body>
</html>
"""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Render a tlds bundle (pages/*.md) into one HTML file.")
    ap.add_argument("bundle", help="bundle directory containing pages/ and src/")
    ap.add_argument("-o", "--out", help="output file (default: <bundle>/index.html)")
    a = ap.parse_args()
    root = Path(a.bundle)
    if not list((root / "pages").glob("*.md")):
        sys.exit(f"ERROR no pages/*.md in {root}")
    b = Bundle(root)
    out = Path(a.out) if a.out else root / "index.html"
    out.write_text(document(b), encoding="utf-8")
    for level, where, msg in LOG.items:
        print(f"{level:5} {where}: {msg}")
    q, v = sum(p.quotes for p in b.order), sum(p.verified for p in b.order)
    print(f"Wrote {out}: {len(b.order)} page(s), {sum(p.minutes for p in b.rest) or sum(p.minutes for p in b.order)} min, "
          f"{q} checks against the code ({v} passed), {LOG.count('ERROR')} errors, {LOG.count('WARN')} warnings")
    sys.exit(1 if LOG.count("ERROR") else 0)


if __name__ == "__main__":
    main()
