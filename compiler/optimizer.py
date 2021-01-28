import ast
import operator
from ast import Constant, Num, Str, Bytes, Ellipsis, NameConstant, copy_location
from compiler.peephole import safe_multiply, safe_power, safe_mod, safe_lshift
from compiler.visitor import ASTRewriter


def is_const(node):
    return isinstance(node, (Constant, Num, Str, Bytes, Ellipsis, NameConstant))


def get_const_value(node):
    if isinstance(node, (Constant, NameConstant)):
        return node.value
    elif isinstance(node, Num):
        return node.n
    elif isinstance(node, (Str, Bytes)):
        return node.s
    elif isinstance(node, Ellipsis):
        return ...

    raise TypeError("Bad constant value")


class Py37Limits:
    MAX_INT_SIZE = 128
    MAX_COLLECTION_SIZE = 256
    MAX_STR_SIZE = 4096
    MAX_TOTAL_ITEMS = 1024


UNARY_OPS = {
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
INVERSE_OPS = {
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: lambda l, r: safe_multiply(l, r, Py37Limits),
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: lambda l, r: safe_mod(l, r, Py37Limits),
    ast.Pow: lambda l, r: safe_power(l, r, Py37Limits),
    ast.LShift: lambda l, r: safe_lshift(l, r, Py37Limits),
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.BitAnd: operator.and_,
}


class AstOptimizer(ASTRewriter):
    def visitUnaryOp(self, node: ast.UnaryOp) -> ast.expr:
        op = self.visit(node.operand)
        if is_const(op):
            conv = UNARY_OPS[type(node.op)]
            val = get_const_value(op)
            try:
                return copy_location(Constant(conv(val)), node)
            except:
                pass
        elif (
            isinstance(node.op, ast.Not)
            and isinstance(node.operand, ast.Compare)
            and len(node.operand.ops) == 1
        ):
            cmp_op = node.operand.ops[0]
            new_op = INVERSE_OPS.get(type(cmp_op))
            if new_op is not None:
                return self.update_node(node.operand, ops=[new_op()])

        return self.update_node(node, operand=op)

    def visitBinOp(self, node: ast.BinOp) -> ast.expr:
        l = self.visit(node.left)
        r = self.visit(node.right)

        if is_const(l) and is_const(r):
            handler = BIN_OPS.get(type(node.op))
            if handler is not None:
                lval = get_const_value(l)
                rval = get_const_value(r)
                try:
                    return copy_location(Constant(handler(lval, rval)), node)
                except:
                    pass

        return self.update_node(node, left=l, right=r)
