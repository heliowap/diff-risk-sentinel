import ast
import re
from typing import Dict, List, Any, Optional, Tuple

try:
    import lizard
    HAS_LIZARD = True
except ImportError:
    HAS_LIZARD = False


JS_TS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
# Languages lizard can parse, enabled only when lizard is installed.
LIZARD_EXTENSIONS = (
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".go", ".java", ".kt", ".kts",
    ".rb", ".rs", ".scala", ".swift", ".php", ".lua", ".m", ".vue",
)


def supported_extensions() -> Tuple[str, ...]:
    exts = (".py",) + JS_TS_EXTENSIONS
    return exts + LIZARD_EXTENSIONS if HAS_LIZARD else exts


def _dedupe_names(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Suffixes repeated qualified names (#2, #3...) so before/after versions can be matched."""
    seen: Dict[str, int] = {}
    for fn in functions:
        count = seen.get(fn["name"], 0) + 1
        seen[fn["name"]] = count
        if count > 1:
            fn["name"] = f"{fn['name']}#{count}"
    return functions


# ---------------------------------------------------------------------------
# Python (stdlib ast)
# ---------------------------------------------------------------------------

class PythonComplexityVisitor(ast.NodeVisitor):
    """
    Cyclomatic complexity following radon's rules, so scores match the reference tool.
    Nested functions and classes are scored separately.
    """

    def __init__(self):
        self.complexity = 1

    def _branch(self, node, weight=1):
        self.complexity += weight
        self.generic_visit(node)

    def visit_If(self, node):
        self._branch(node)

    def visit_IfExp(self, node):
        self._branch(node)

    def _loop(self, node):
        self._branch(node, 1 + bool(node.orelse))

    visit_For = visit_AsyncFor = visit_While = _loop

    def visit_Try(self, node):
        self._branch(node, len(node.handlers) + bool(node.orelse))

    visit_TryStar = visit_Try

    def visit_Assert(self, node):
        self.complexity += 1  # radon does not descend into assert expressions

    def visit_comprehension(self, node):
        self._branch(node, 1 + len(node.ifs))

    def visit_Match(self, node):
        wildcard = any(getattr(case.pattern, "pattern", False) is None for case in node.cases)
        self._branch(node, max(0, len(node.cases) - wildcard))

    def visit_BoolOp(self, node):
        self._branch(node, len(node.values) - 1)

    def visit_FunctionDef(self, node):
        pass  # scored as its own function

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


def analyze_python_functions(file_path: str, code: str) -> Optional[List[Dict[str, Any]]]:
    """Returns None when the file cannot be parsed by the running interpreter."""
    try:
        tree = ast.parse(code, filename=file_path)
    except (SyntaxError, ValueError):
        return None

    functions: List[Dict[str, Any]] = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visitor = PythonComplexityVisitor()
                for stmt in child.body:
                    visitor.visit(stmt)
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                functions.append({
                    "name": prefix + child.name,
                    "start_line": start,
                    "end_line": getattr(child, "end_lineno", child.lineno),
                    "ccn": visitor.complexity,
                })
                walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    functions.sort(key=lambda f: f["start_line"])
    return _dedupe_names(functions)


# ---------------------------------------------------------------------------
# JS / TS scanner
# ---------------------------------------------------------------------------

_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "with", "function", "return", "do", "else",
    "try", "finally", "typeof", "new", "await", "yield", "case", "delete", "void", "in",
    "of", "instanceof", "import", "export", "class", "throw",
}
_STRING_PREV_WORDS = {
    "return", "case", "from", "import", "typeof", "in", "of", "export", "default",
    "yield", "await", "throw", "else", "as", "extends", "satisfies",
}
_STRING_PREV_CHARS = set("=([{,:;!&|?+-*/%<>~^")
_BRANCH_WORDS = {"if", "for", "while", "case", "catch"}

_FUNCTION_KW_RE = re.compile(r'(?:^|[^\w$.])function\b\s*\*?\s*([\w$]*)\s*(?:<[^()]*>)?\s*\(.*\)\s*(?::.*)?$', re.S)
_ASSIGNED_NAME_RE = re.compile(
    r'^(?:export\s+)?(?:default\s+)?'
    r'(?:(?:public|private|protected|static|readonly|override|declare|abstract|accessor)\s+)*'
    r'(?:(?:const|let|var)\s+)?(#?[\w$]+)\s*(?::[^=]*)?(?:=(?!>)|:)\s*(?:async\b)?', re.S
)
_CONTROL_HEADER_RE = re.compile(r'^(?:else\s+)?(?:if|for|while|switch|catch|with)\b|^(?:else|do|try|finally)$')
# Line ends / line starts that continue a statement across a newline (no ASI break).
_CONTINUES_AT_END = tuple(",([=:?&|+-*/%<>.!~^")
_CONTINUES_AT_START = tuple(".?)]},:&|+-*/%=<>{")
_CONTINUATION_WORDS = {"extends", "implements", "as", "satisfies", "in", "of", "instanceof", "new",
                       "typeof", "await", "yield", "async", "function", "class", "const", "let", "var",
                       "export", "default", "return", "throw", "get", "set", "static", "keyof"}
_METHOD_RE = re.compile(
    r'^(?:(?:public|private|protected|static|async|get|set|readonly|override|abstract|export|default)\s+)*'
    r'\*?\s*(#?[\w$]+)\s*(?:<[^()]*>)?\s*\((.*)\)\s*(?::\s*.+?)?\s*$', re.S
)
_CLASS_RE = re.compile(r'\bclass\s+([\w$]+)')
_CALL_ASSIGN_RE = re.compile(r'(?:const|let|var)\s+([\w$]+)\s*(?::[^=]*)?=\s*[\w$.]+\s*(?:<[^()]*>)?\s*\($', re.S)


def _prev_significant(chars: List[str]) -> Tuple[str, str]:
    """Returns (previous non-space char, previous word) of already emitted clean text."""
    i = len(chars) - 1
    while i >= 0 and chars[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return "", ""
    ch = chars[i]
    j = i
    while j >= 0 and (chars[j].isalnum() or chars[j] in "_$"):
        j -= 1
    return ch, "".join(chars[j + 1:i + 1])


def _ends_with_arrow(chars: List[str]) -> bool:
    i = len(chars) - 1
    while i >= 0 and chars[i] in " \t\r\n":
        i -= 1
    return i >= 1 and chars[i] == ">" and chars[i - 1] == "="


def _ends_with_postfix(chars: List[str]) -> bool:
    i = len(chars) - 1
    while i >= 0 and chars[i] in " \t\r\n":
        i -= 1
    return i >= 1 and chars[i] in "+-" and chars[i - 1] == chars[i]


def _strip_decorators(seg: str) -> str:
    """Drops leading `@decorator` / `@decorator(args)` from a method header."""
    while seg.startswith("@"):
        m = re.match(r'@[\w$.]+\s*', seg)
        if not m:
            break
        rest = seg[m.end():]
        if rest.startswith("("):
            depth = 0
            for idx, ch in enumerate(rest):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0:
                    rest = rest[idx + 1:]
                    break
            else:
                break
        seg = rest.lstrip()
    return seg


def _statement_continues(header: str, text: str, nl_index: int) -> bool:
    """Whether the statement in `header` continues on the line after the newline at nl_index."""
    tail = header.rstrip()
    if not tail:
        return True
    if tail.endswith(_CONTINUES_AT_END):
        return True
    last_word = re.search(r'[\w$]+$', tail)
    if last_word and last_word.group(0) in _CONTINUATION_WORDS:
        return True
    rest = text[nl_index + 1:nl_index + 200].lstrip()
    if not rest or rest.startswith(_CONTINUES_AT_START):
        return True
    first_word = re.match(r'[\w$]+', rest)
    return bool(first_word and first_word.group(0) in {"extends", "implements", "as", "satisfies", "in", "of", "instanceof"})


def _strip_js(code: str) -> str:
    """
    Blanks comments, string/regex literals and template literal text, preserving newlines.
    Template `${...}` expressions are kept (as code) so their branches still count; their
    delimiters become `(` `)` so they neither look like blocks nor hide a following string.
    """
    out: List[str] = []
    i, n = 0, len(code)
    # Mode stack: "tpl" = inside template text; int = brace depth inside a ${...} expression.
    modes: List[Any] = []

    def blank(ch: str):
        out.append("\n" if ch == "\n" else " ")

    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if modes and modes[-1] == "tpl":
            if c == "\\":
                blank(c)
                if nxt:
                    blank(nxt)
                i += 2
            elif c == "`":
                out.append(c)
                modes.pop()
                i += 1
            elif c == "$" and nxt == "{":
                out.extend(" (")
                modes.append(0)
                i += 2
            else:
                blank(c)
                i += 1
            continue

        if modes and c in "{}":
            if c == "{":
                modes[-1] += 1
            elif modes[-1] == 0:
                out.append(")")  # closes ${...}: back to template text
                modes.pop()
                i += 1
                continue
            else:
                modes[-1] -= 1
            out.append(c)
            i += 1
            continue

        if c == "/" and nxt == "/":
            while i < n and code[i] != "\n":
                blank(code[i])
                i += 1
        elif c == "/" and nxt == "*":
            stop = code.find("*/", i + 2)
            stop = n if stop == -1 else stop + 2
            while i < stop:
                blank(code[i])
                i += 1
        elif c == "`":
            out.append(c)
            modes.append("tpl")
            i += 1
        elif c in "'\"/":
            prev_ch, prev_word = _prev_significant(out)
            if c == "/":
                # Regex literal vs division / JSX closing tag ('</div>', '<br />').
                if prev_ch in "+-" and _ends_with_postfix(out):
                    starts_literal = False  # `a++ / 2`
                elif prev_ch == ">" and _ends_with_arrow(out):
                    starts_literal = True  # `x => /re/`
                elif prev_ch in ("<", ">"):
                    starts_literal = False
                elif prev_ch == "" or prev_ch in _STRING_PREV_CHARS:
                    starts_literal = True
                else:
                    starts_literal = prev_word in _STRING_PREV_WORDS
            else:
                # A quote after an identifier is JSX text (e.g. <p>Don't</p>), not a string.
                starts_literal = (
                    prev_ch == ""
                    or prev_ch in _STRING_PREV_CHARS
                    or prev_word in _STRING_PREV_WORDS
                )
            if not starts_literal:
                out.append(c)
                i += 1
                continue
            j = i + 1
            in_class = False
            closed = False
            while j < n:
                ch = code[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break  # unterminated single-line literal: stop at end of line
                if c == "/" and ch == "[":
                    in_class = True
                elif c == "/" and ch == "]":
                    in_class = False
                elif ch == c and not in_class:
                    closed = True
                    break
                j += 1
            j = min(j, n)
            out.append(c)
            for ch in code[i + 1:j]:
                blank(ch)
            i = j
            if closed:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _last_segment_start(header: str) -> int:
    """
    Index where the tail after the last top-level ',' or ';' begins. Balanced (), [] and {}
    (type/object literals kept in the header) are skipped; an unbalanced opener ends the scan.
    """
    depth = 0
    angle = 0
    i = len(header) - 1
    while i >= 0:
        ch = header[i]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            if ch == ">" and not (i > 0 and header[i - 1] == "="):
                angle += 1
            elif ch == "<" and angle > 0:
                angle -= 1
            elif ch == ";" or (ch == "," and angle == 0):
                break
        i -= 1
    return i + 1


def _classify_brace(header: str, paren_depth: int) -> Tuple[str, Optional[str], int]:
    """
    Decides whether a '{' opens a function body, a plain block, or an object literal.
    Returns (kind, name, index in header where the function signature starts).
    """
    seg_start = _last_segment_start(header)
    kind, name = _classify_segment(header, seg_start, paren_depth)
    return kind, name, seg_start


def _classify_segment(header: str, seg_start: int, paren_depth: int) -> Tuple[str, Optional[str]]:
    h = " ".join(header.split())
    seg = " ".join(header[seg_start:].split())
    prefix = " ".join(header[:seg_start].split())

    # '{' right after ':', '<', '|' or '&' starts a type or object literal
    # (e.g. `): { a: T } {`, `Promise<{ ... }>`, `key: {`), never a body.
    if seg.endswith((":", "<", "|", "&")):
        return "obj", None
    if seg.endswith("=>"):  # arrow body, even after `if (…) return`
        m = _ASSIGNED_NAME_RE.match(seg)
        if m and m.group(1) not in _JS_KEYWORDS:
            return "func", m.group(1)
        m = _CALL_ASSIGN_RE.search(prefix)
        return "func", (m.group(1) if m else "<anonymous>")

    # Control-flow headers (`if (a < b)`, `for (…; i < n; …)`) are blocks; checked before the
    # generics heuristic below, which would read their `<` operator as an open `<T …`.
    if _CONTROL_HEADER_RE.match(seg):
        return "block", None
    # ... or inside an unclosed generic parameter list (`<T extends { id: string }>`).
    if seg.count("<") > seg.count(">") - seg.count("=>"):
        return "obj", None

    m = _FUNCTION_KW_RE.search(seg)
    if m:
        if m.group(1):
            return "func", m.group(1)
        m2 = _ASSIGNED_NAME_RE.match(seg)
        return "func", (m2.group(1) if m2 and m2.group(1) not in _JS_KEYWORDS else "<anonymous>")

    m = _METHOD_RE.match(_strip_decorators(seg))
    if m and m.group(1) not in _JS_KEYWORDS:
        return "func", m.group(1)

    if paren_depth > 0:
        return "obj", None
    m = _CLASS_RE.search(seg)
    if m:
        return "block", m.group(1)
    if not h:
        return "block", None  # a statement cannot start with an object literal
    if h.endswith(("=", ":", ",", "(", "[", "?", "&&", "||", "??", "...")) or re.search(r'\b(return|yield|await)$', h):
        return "obj", None
    return "block", None


def _next_non_space(text: str, i: int) -> str:
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return text[i] if i < n else ""


def analyze_js_ts_fallback(file_path: str, code: str) -> List[Dict[str, Any]]:
    """
    Brace-aware JS/TS scanner. Block-bodied functions (declarations, expressions, methods,
    accessors, arrow functions with `{}`) are reported with qualified names; expression-bodied
    arrows are not separate functions and their branches count toward the enclosing function.
    """
    text = _strip_js(code)
    functions: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []   # frames: kind, name, start_line, ccn, saved_paren
    header: List[str] = []
    header_lines: List[int] = []  # line number of each header token
    paren = 0
    line = 1
    i, n = 0, len(text)

    def push(tok: str):
        header.append(tok)
        header_lines.append(line)

    def reset_header():
        header.clear()
        header_lines.clear()

    def signature_line(start_idx: int) -> int:
        pos = 0
        for tok, tok_line in zip(header, header_lines):
            if pos + len(tok) > start_idx and tok.strip():
                return tok_line
            pos += len(tok)
        return line

    def innermost_func():
        for frame in reversed(stack):
            if frame["kind"] == "func":
                return frame
        return None

    def qualifier():
        for frame in reversed(stack):
            if frame["kind"] != "obj" and frame.get("name"):
                return frame["name"] + "."  # already fully qualified
        return ""

    while i < n:
        c = text[i]
        if c == "\n":
            if paren == 0 and not (stack and stack[-1]["kind"] == "obj") \
                    and not _statement_continues("".join(header), text, i):
                reset_header()  # automatic semicolon insertion
                line += 1
                i += 1
                continue
            line += 1
            push(" ")
        elif c.isalpha() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            if word in _BRANCH_WORDS and (i == 0 or text[i - 1] != "."):
                fn = innermost_func()
                if fn:
                    fn["ccn"] += 1
            push(word)
            i = j
            continue
        elif c in "([":
            paren += 1
            push(c)
        elif c in ")]":
            paren = max(0, paren - 1)
            push(c)
        elif c == ";":
            if paren == 0 and not (stack and stack[-1]["kind"] == "obj"):
                reset_header()
            else:
                push(c)
        elif c in "&|" and text[i + 1:i + 2] == c:
            fn = innermost_func()
            if fn and text[i + 2:i + 3] != "=":
                fn["ccn"] += 1
            push(c + c)
            i += 2
            continue
        elif c == "?":
            fn = innermost_func()
            if text[i + 1:i + 2] == "?":
                if fn and text[i + 2:i + 3] != "=":
                    fn["ccn"] += 1
                push("??")
                i += 2
                continue
            if fn and text[i + 1:i + 2] != "." and _next_non_space(text, i + 1) not in ":),;=":
                fn["ccn"] += 1
            push(c)
        elif c == "{":
            kind, name, seg_start = _classify_brace("".join(header), paren)
            if kind == "obj":
                stack.append({"kind": "obj", "saved_paren": paren})
                push(c)
            else:
                stack.append({
                    "kind": kind,
                    "name": qualifier() + name if name else None,
                    "start_line": signature_line(seg_start) if kind == "func" else line,
                    "ccn": 1,
                    "saved_paren": paren,
                })
                paren = 0
                reset_header()
        elif c == "}":
            if stack:
                frame = stack.pop()
                paren = frame["saved_paren"]
                if frame["kind"] == "obj":
                    push(c)
                else:
                    if frame["kind"] == "func":
                        functions.append({
                            "name": frame["name"],
                            "start_line": frame["start_line"],
                            "end_line": line,
                            "ccn": frame["ccn"],
                        })
                    reset_header()
        else:
            push(c)
        i += 1

    for frame in stack:  # unbalanced input: close at EOF
        if frame["kind"] == "func":
            functions.append({"name": frame["name"], "start_line": frame["start_line"],
                              "end_line": line, "ccn": frame["ccn"]})

    functions.sort(key=lambda f: (f["start_line"], -f["end_line"]))
    return _dedupe_names(functions)


def extract_functions(file_path: str, code: str) -> Optional[List[Dict[str, Any]]]:
    """Functions of a file, or None when it could not be parsed."""
    if file_path.endswith(".py"):
        return analyze_python_functions(file_path, code)

    # The built-in JS/TS scanner is used even when lizard is installed: measured against
    # the TypeScript compiler AST it matches function ranges far more closely than lizard,
    # which runs past functions containing regex/template literals (evals/js_parser_benchmark.py).
    if file_path.endswith(JS_TS_EXTENSIONS):
        return analyze_js_ts_fallback(file_path, code)

    if HAS_LIZARD:
        try:
            analysis = lizard.analyze_file.analyze_source_code(file_path, code)
            return _dedupe_names([
                {
                    "name": f.name,
                    "start_line": f.start_line,
                    "end_line": f.end_line,
                    "ccn": f.cyclomatic_complexity,
                }
                for f in analysis.function_list
            ])
        except Exception:
            pass

    return []
