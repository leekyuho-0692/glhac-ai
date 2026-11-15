"""쓰기 경계 정적 검사의 벌크 대상/딕셔너리 흐름 추적(실행·DB 접근 없음)."""
import ast
from dataclasses import dataclass, field


@dataclass
class Value:
    kind: str = "unknown"
    model: str = ""
    keys: set = field(default_factory=set)
    unknown: bool = False
    column_models: set = field(default_factory=set)


def bulk_write_violations(tree, model, protected):
    class Writes(ast.NodeVisitor):
        def __init__(self):
            self.bindings = {}
            self.lines = set()

        def mapping(self, args=(), keywords=()):
            result = Value("dict")
            for arg in args:
                value = self.value(arg)
                result.keys.update(value.keys)
                result.column_models.update(value.column_models)
                result.unknown |= value.unknown or value.kind != "dict"
            for kw in keywords:
                if kw.arg is None:
                    extra = self.mapping([kw.value])
                    result.keys.update(extra.keys)
                    result.column_models.update(extra.column_models)
                    result.unknown |= extra.unknown
                else:
                    result.keys.add(kw.arg)
            return result

        def value(self, node):
            if isinstance(node, ast.Name):
                return self.bindings.get(node.id, Value("model", node.id) if node.id[:1].isupper() else Value())
            if isinstance(node, ast.Attribute):
                owner = self.value(node.value)
                if node.attr[:1].isupper():
                    return Value("model", node.attr)
                if node.attr == "__table__":
                    return owner
                if owner.kind == "model":
                    return Value("column", owner.model, {node.attr}, column_models={owner.model})
                return owner
            if isinstance(node, ast.Dict):
                result = self.mapping([value for key, value in zip(node.keys, node.values) if key is None])
                for key in node.keys:
                    if isinstance(key, ast.Constant):
                        result.keys.add(key.value)
                    elif key is not None:
                        column = self.value(key)
                        result.keys.update(column.keys)
                        result.column_models.update(column.column_models)
                        result.unknown |= column.kind != "column"
                return result
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name == "copy" and isinstance(node.func, ast.Attribute) and not node.args:
                    # dict.copy()는 위치 인자 대신 수신 딕셔너리의 키를 복사한다.
                    return self.mapping([node.func.value], node.keywords)
                if name in ("dict", "deepcopy", "copy"):
                    return self.mapping(node.args, node.keywords)
                if name in ("query", "update", "insert", "delete") and node.args:
                    target = self.value(node.args[0])
                    if target.kind == "model":
                        return Value("query", target.model)
                if isinstance(node.func, ast.Attribute):
                    return self.value(node.func.value)
                target = self.value(node.func)
                if target.kind == "model":
                    return Value("object", target.model)
            return Value()

        def visit_ImportFrom(self, node):
            for alias in node.names:
                if alias.name[:1].isupper():
                    self.bindings[alias.asname or alias.name] = Value("model", alias.name)

        def visit_FunctionDef(self, node):
            outer = self.bindings
            self.bindings = dict(outer)
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                self.bindings.pop(arg.arg, None)
            self.generic_visit(node)
            self.bindings = outer

        visit_AsyncFunctionDef = visit_FunctionDef

        def assign(self, target, value):
            if isinstance(target, ast.Name):
                self.bindings[target.id] = self.value(value)
            elif isinstance(target, ast.Subscript):
                mapping = self.value(target.value)
                if mapping.kind == "dict":
                    key = target.slice
                    if isinstance(key, ast.Constant):
                        mapping.keys.add(key.value)
                    else:
                        column = self.value(key)
                        mapping.keys.update(column.keys)
                        mapping.column_models.update(column.column_models)
                        mapping.unknown |= column.kind != "column"

        def visit_Assign(self, node):
            self.generic_visit(node)
            value = self.value(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = value
                else:
                    self.assign(target, node.value)

        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            self.assign(node.target, node.value)

        def visit_AugAssign(self, node):
            if isinstance(node.op, ast.BitOr):
                target, value = self.value(node.target), self.mapping([node.value])
                if target.kind == "dict":
                    target.keys.update(value.keys)
                    target.column_models.update(value.column_models)
                    target.unknown |= value.unknown
            self.generic_visit(node)

        def visit_Call(self, node):
            self.generic_visit(node)
            if not isinstance(node.func, ast.Attribute):
                return
            name, receiver = node.func.attr, self.value(node.func.value)
            if name in ("bulk_update_mappings", "bulk_insert_mappings"):
                targets = node.args[:1] + [kw.value for kw in node.keywords if kw.arg == "mapper"]
                if any(self.value(target).model == model for target in targets):
                    self.lines.add(node.lineno)
            if name not in ("update", "values", "setdefault", "__setitem__"):
                return
            value = self.mapping(node.args, node.keywords)
            if receiver.kind == "dict":
                if name in ("setdefault", "__setitem__") and node.args:
                    key = node.args[0]
                    if isinstance(key, ast.Constant):
                        receiver.keys.add(key.value)
                    else:
                        receiver.unknown = True
                elif name == "update":
                    receiver.keys.update(value.keys)
                    receiver.column_models.update(value.column_models)
                    receiver.unknown |= value.unknown
                return
            if name in ("update", "values"):
                target = receiver.model == model or (not receiver.model and model in value.column_models)
                if target and (value.keys & protected or value.unknown):
                    self.lines.add(node.lineno)

    visitor = Writes()
    visitor.visit(tree)
    return sorted(visitor.lines)
