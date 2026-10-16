"""Conservative HTML-to-domain parser. Never executes templates or fetches image URLs."""

import ast
import hashlib
import json
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlparse

from algo_coach.models import Example, Problem

PARSER_VERSION = "leetcode-cn-html-v1"


def clean_text(text: str) -> str:
    return "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))


class StatementHTML(HTMLParser):
    BLOCKS = {"p", "div", "pre", "li", "ul", "ol", "h1", "h2", "h3", "h4", "table", "tr"}
    HIDDEN = {"script", "style", "iframe", "object", "embed", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN or self.hidden:
            self.hidden.append(tag)
            return
        if tag in self.BLOCKS or tag == "br":
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "sup":
            self.parts.append("^(")
        if tag == "sub":
            self.parts.append("_(")
        if tag in ("td", "th"):
            self.parts.append(" | ")
        if tag == "img":
            source = dict(attrs).get("src") or ""
            parsed = urlparse(source)
            if (
                parsed.scheme == "https"
                and parsed.hostname
                in {"assets.leetcode.com", "assets.leetcode.cn", "pic.leetcode.cn"}
                and not parsed.username
                and not parsed.password
                and parsed.port in (None, 443)
                and not parsed.query
                and not parsed.fragment
            ):
                marker = f"[图片来源：{source}]"
                self.images.append(marker)
                self.parts.append("\n" + marker + "\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                while self.hidden:
                    current = self.hidden.pop()
                    if current == tag:
                        break
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in ("sup", "sub"):
            self.parts.append(")")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        text = clean_text("".join(self.parts)).replace("\xa0", " ")
        return re.sub(r"\n{3,}", "\n\n", text).strip()


CONSTRAINTS = re.compile(r"(?im)^[ \t]*(?:提示|约束(?:条件)?|Constraints)[ \t]*[:：][ \t]*$")
EXAMPLE = re.compile(r"(?im)^[ \t]*(?:示例|样例|Example)[ \t]*\d*[ \t]*[:：][ \t]*")
FOLLOWUP = re.compile(r"(?im)^[ \t]*(?:进阶|Follow[ -]?up)[ \t]*[:：]")
INPUT_OUTPUT = re.compile(
    r"(?:输入|Input)\s*[:：]\s*(.+?)(?:输出|Output)\s*[:：]\s*(.+?)"
    r"(?=(?:解释|说明|Explanation)\s*[:：]|\Z)",
    re.S | re.I,
)


def parse_statement(markup: str) -> tuple[str, list[str], list[Example]]:
    if not isinstance(markup, str) or not markup.strip() or len(markup) > 120000:
        raise ValueError("题面为空或过长")
    parser = StatementHTML()
    parser.feed(markup)
    text = parser.text()
    boundary = CONSTRAINTS.search(text)
    if not boundary:
        raise ValueError("未识别约束区域")
    body, tail = text[: boundary.start()], text[boundary.end() :]
    follow = FOLLOWUP.search(tail)
    constraints_text = tail[: follow.start()] if follow else tail
    constraints = [
        line.strip().removeprefix("- ") for line in constraints_text.splitlines() if line.strip()
    ]
    markers = list(EXAMPLE.finditer(body))
    if not markers or not constraints:
        raise ValueError("未识别样例或约束")
    statement = body[: markers[0].start()].strip()
    if follow:
        statement += "\n\n" + tail[follow.start() :].strip()
    for image in dict.fromkeys(parser.images):
        if image not in statement:
            statement += "\n" + image
    examples = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        chunk = re.sub(r"\[图片来源：[^\]\n]+\]", "", body[marker.end() : end])
        follow_example = FOLLOWUP.search(chunk)
        if follow_example:
            statement += "\n\n" + chunk[follow_example.start() :].strip()
            chunk = chunk[: follow_example.start()]
        match = INPUT_OUTPUT.search(chunk)
        if not match:
            raise ValueError("样例缺少可识别的输入或输出")
        examples.append(Example(input=match[1].strip(), output=match[2].strip()))
        explanation = re.search(r"(?:解释|说明|Explanation)\s*[:：]\s*(.+)", chunk, re.S | re.I)
        if explanation:
            statement += f"\n\n样例 {index + 1} 说明：" + explanation[1].strip()
    if not statement:
        raise ValueError("题目主体为空")
    return statement, constraints, examples


def python_signature(question: dict) -> str:
    snippets = question.get("codeSnippets")
    if not isinstance(snippets, list):
        raise ValueError("缺少语言模板")
    codes = [
        s.get("code") for s in snippets if isinstance(s, dict) and s.get("langSlug") == "python3"
    ]
    if len(codes) != 1 or not isinstance(codes[0], str) or len(codes[0]) > 16000:
        raise ValueError("缺少唯一 Python3 模板")
    metadata = json.loads(question.get("metaData") or "{}")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("params"), list):
        raise ValueError("函数元数据无效")
    # Parse syntax only. Appending a pass completes LeetCode's intentionally empty method body.
    tree = ast.parse(codes[0].rstrip() + "\n        pass\n")
    methods = [
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "Solution"
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    ]
    if len(methods) != 1:
        raise ValueError("仅支持 Solution 单方法题型")
    method = methods[0]
    args = method.args
    names = [arg.arg for arg in args.posonlyargs + args.args]
    if (
        method.name != metadata.get("name")
        or not names
        or names[0] != "self"
        or names[1:] != [p["name"] for p in metadata["params"]]
        or args.defaults
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or method.decorator_list
    ):
        raise ValueError("函数模板与参数元数据不一致")
    suffix = " -> " + ast.unparse(method.returns) if method.returns else ""
    return f"class Solution: def {method.name}({ast.unparse(args)}){suffix}:"


def parse_question(question: dict) -> tuple[Problem, str, str]:
    if question.get("isPaidOnly") is not False:
        raise ValueError("仅支持明确标为非付费的公开题目")
    identifier, slug = question.get("questionFrontendId"), question.get("titleSlug")
    if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", identifier):
        raise ValueError("首版仅支持普通数字题号")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug):
        raise ValueError("题目 slug 不受支持")
    translated = question.get("translatedContent")
    markup = translated if translated else question.get("content")
    language = "zh-CN" if translated else "en"
    statement, constraints, examples = parse_statement(markup)
    signature = python_signature(question)
    title = question.get("translatedTitle") if translated else question.get("title")
    if not title:
        raise ValueError("题目标题缺失")
    problem = Problem(
        site="leetcode_cn",
        frontend_id=identifier,
        slug=slug,
        title=clean_text(title),
        statement=statement,
        constraints=constraints,
        examples=examples,
        signature=signature,
        url=f"https://leetcode.cn/problems/{slug}/",
        attribution="LeetCode 中国站公开题面；网页内部 GraphQL 匿名获取并解析，非平台判题结果。",
    )
    raw_fields = {
        name: question.get(name)
        for name in (
            "questionFrontendId",
            "titleSlug",
            "title",
            "translatedTitle",
            "content",
            "translatedContent",
            "isPaidOnly",
            "metaData",
        )
    }
    raw_fields["python3_signature"] = signature
    raw_hash = hashlib.sha256(
        json.dumps(raw_fields, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return problem, language, raw_hash
