"""Bounded syntactic evidence for code review. Never interpret or execute submissions."""

import ast
import hashlib


def _names_and_values(assignment):
    if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
        return None
    target, value = assignment.targets[0], assignment.value
    targets = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
    values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else [value]
    if len(targets) != len(values) or any(not isinstance(t, ast.Name) for t in targets):
        return None
    if any(
        not isinstance(n, (ast.Name, ast.Attribute, ast.Load)) for v in values for n in ast.walk(v)
    ):
        return None
    return {t.id: ast.unparse(v) for t, v in zip(targets, values, strict=True)}


def _same(left, right):
    if isinstance(left, ast.Name) and isinstance(right, ast.Name):
        return left.id == right.id
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _length_subscripts(nodes):
    """Record syntax only; len binding, value type and reachability remain conditional."""
    facts = []
    for node in nodes:
        value = node.value if isinstance(node, ast.Return) else None
        if not isinstance(value, ast.Subscript) or not isinstance(value.value, ast.Name):
            continue
        index = value.slice
        if (
            isinstance(index, ast.Call)
            and isinstance(index.func, ast.Name)
            and index.func.id == "len"
            and len(index.args) == 1
            and not index.keywords
            and _same(index.args[0], value.value)
        ):
            facts.append(
                {
                    "line": node.lineno,
                    "container": value.value.id,
                    "pattern": "return container[len(container)]",
                    "conditional_meaning": (
                        "若本行被执行，容器是标准list且len为内置函数，下标等于长度，"
                        "会抛IndexError；return不会返回元素或下标。"
                        "反事实：若改用合法下标，容器下标表达式取出的是元素值，不是位置编号。"
                    ),
                    "scope": "仅语法匹配，不证明可达、实参类型或len未被重定义；未执行代码。",
                }
            )
        if len(facts) == 12:
            break
    return facts


def _mapping_scans(nodes):
    """Recognize a narrow dictionary scan; do not evaluate AST expressions or inputs."""
    scans = []
    for function in nodes:
        if not isinstance(function, ast.FunctionDef) or function.decorator_list:
            continue
        body = function.body
        if len(body) not in (2, 3):
            continue
        initial, loop = body[:2]
        if (
            not isinstance(initial, ast.Assign)
            or len(initial.targets) != 1
            or not isinstance(initial.targets[0], ast.Name)
            or not isinstance(initial.value, ast.Dict)
            or initial.value.keys
            or not isinstance(loop, ast.For)
            or loop.orelse
            or (len(body) == 3 and not isinstance(body[2], (ast.Return, ast.Raise)))
        ):
            continue
        mapping = initial.targets[0]
        iterator = loop.iter
        if (
            not isinstance(iterator, ast.Call)
            or not isinstance(iterator.func, ast.Name)
            or iterator.func.id != "enumerate"
            or len(iterator.args) != 1
            or iterator.keywords
            or not isinstance(iterator.args[0], ast.Name)
            or not isinstance(loop.target, (ast.Tuple, ast.List))
            or len(loop.target.elts) != 2
            or any(not isinstance(n, ast.Name) for n in loop.target.elts)
        ):
            continue
        index, value = loop.target.elts
        params = {a.arg for a in function.args.posonlyargs + function.args.args}
        if iterator.args[0].id not in params:
            continue
        statements = loop.body
        complement = None
        if len(statements) == 3:
            assignment = statements[0]
            if (
                not isinstance(assignment, ast.Assign)
                or len(assignment.targets) != 1
                or not isinstance(assignment.targets[0], ast.Name)
            ):
                continue
            complement = assignment
            statements = statements[1:]
        if len(statements) != 2:
            continue
        condition = next((s for s in statements if isinstance(s, ast.If)), None)
        store = next((s for s in statements if isinstance(s, ast.Assign)), None)
        if (
            condition is None
            or store is None
            or condition.orelse
            or len(condition.body) != 1
            or not isinstance(condition.body[0], ast.Return)
            or len(store.targets) != 1
            or not isinstance(store.targets[0], ast.Subscript)
            or not _same(store.targets[0].value, mapping)
            or not _same(store.targets[0].slice, value)
            or not _same(store.value, index)
        ):
            continue
        test = condition.test
        if (
            not isinstance(test, ast.Compare)
            or len(test.ops) != 1
            or not isinstance(test.ops[0], ast.In)
            or not _same(test.comparators[0], mapping)
        ):
            continue
        need = test.left
        expression = need
        if complement:
            if not _same(need, complement.targets[0]):
                continue
            expression = complement.value
        if (
            not isinstance(expression, ast.BinOp)
            or not isinstance(expression.op, ast.Sub)
            or not isinstance(expression.left, ast.Name)
            or expression.left.id not in params
            or not _same(expression.right, value)
        ):
            continue
        # Rebound names invalidate the simple prefix argument, even in syntactically similar code.
        names = [mapping.id, index.id, value.id, iterator.args[0].id, expression.left.id]
        if complement:
            names.append(complement.targets[0].id)
        if len(set(names)) != len(names):
            continue
        returned = condition.body[0]
        pair = returned.value
        if (
            not isinstance(pair, (ast.List, ast.Tuple))
            or len(pair.elts) != 2
            or not isinstance(pair.elts[0], ast.Subscript)
            or not _same(pair.elts[0].value, mapping)
            or not _same(pair.elts[0].slice, need)
            or not _same(pair.elts[1], index)
        ):
            continue
        write_first = statements[0] is store
        scans.append(
            {
                "function_line": function.lineno,
                "loop_line": loop.lineno,
                "initial_empty_mapping_line": initial.lineno,
                "lookup_line": condition.lineno,
                "write_line": store.lineno,
                "return_line": returned.lineno,
                "order": "write_before_lookup" if write_first else "lookup_before_write",
                "mapping": mapping.id,
                "index": index.id,
                "value": value.id,
                "sequence": [
                    {
                        "line": s.lineno,
                        "kind": "write" if s is store else "lookup_then_return_if_hit",
                    }
                    for s in statements
                ],
                "on_hit": "return 结束本次函数调用；后续语句和下一轮均不再执行。",
                "first_iteration": (
                    f"首轮下标为0。若 {expression.left.id} == 2*{iterator.args[0].id}[0]，"
                    "首次写入空字典后查询命中，返回[0,0]；没有旧下标被覆盖，也不会进入第二轮。"
                    if write_first
                    else "首轮查询空字典不命中，然后写入下标0。"
                    "每轮查询时仅有此前位置；命中即return，"
                    "本轮后面的写入不执行，不能描述为命中后仍覆盖旧下标。"
                ),
                "scope": (
                    "有条件的局部控制流事实：enumerate为内置函数、输入为普通整数列表且目标为整数、"
                    "该循环正常进入；不执行提交，不证明整段程序正确或已通过测试。"
                ),
            }
        )
        if len(scans) >= 12:
            break
    return scans


def code_facts(code: str) -> dict:
    result = {
        "code_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "loops": [],
        "mapping_scans": [],
        "length_subscripts": [],
        "coverage": (
            "loops仅覆盖受支持的 while 局部赋值模式，mapping_scans仅覆盖精确字典扫描模板。"
            "loops=[] 只表示没有提取到匹配事实，"
            "不表示代码没有 for/while、不表示代码正确，也不是执行记录。"
        ),
    }
    if not code:
        return result
    try:
        module = ast.parse(code.removeprefix("\ufeff"))
    except (SyntaxError, ValueError, RecursionError):
        return {**result, "syntax_valid": False}
    result["syntax_valid"] = True
    nodes = list(ast.walk(module))
    if len(nodes) > 4000:
        return {**result, "analysis_skipped": "AST exceeds bounded evidence budget"}
    result["mapping_scans"] = _mapping_scans(nodes)
    result["length_subscripts"] = _length_subscripts(nodes)
    for owner in nodes:
        # Inspect only sibling statements: no cross-branch or cross-iteration inference.
        body = getattr(owner, "body", None)
        if not isinstance(body, list):
            continue
        for index, loop in enumerate(body):
            if not isinstance(loop, ast.While) or index == 0:
                continue
            aliases = _names_and_values(body[index - 1])
            if not aliases:
                continue
            for statement in loop.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    break
                if any(
                    isinstance(n, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield))
                    for n in ast.walk(statement)
                ):
                    break
                target = statement.targets[0]
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id not in aliases or not isinstance(statement.value, ast.Name):
                        break
                    result["loops"].append(
                        {
                            "loop_line": loop.lineno,
                            "condition": ast.unparse(loop.test),
                            "entry_bindings": aliases,
                            "first_attribute_write": {
                                "line": statement.lineno,
                                "statement": ast.unparse(statement),
                                "target_at_entry": f"({aliases[target.value.id]}).{target.attr}",
                                "value_at_entry": aliases.get(
                                    statement.value.id, statement.value.id
                                ),
                            },
                            "scope": (
                                "若循环进入，且普通属性读写无自定义副作用，这是首轮首个属性写入的入口别名；"
                                "不证明循环次数或算法正确性。"
                            ),
                        }
                    )
                    break
                if isinstance(target, ast.Attribute):
                    break
                updates = _names_and_values(statement)
                if not updates or set(updates) & set(aliases):
                    break
                # Keep only entry bindings; do not infer heap reads or propagate mutations.
                if any(name in aliases for value in updates.values() for name in value.split(".")):
                    continue
                break
            if len(result["loops"]) >= 12:
                return result
    return result
