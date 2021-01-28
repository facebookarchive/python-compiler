from __future__ import print_function
import imp
import linecache
import os
import marshal
import struct
import sys
from io import StringIO
import ast
from compiler import walk
from compiler import pyassem, misc, future, symbols
from compiler.consts import SC_LOCAL, SC_GLOBAL_IMPLICIT, SC_GLOBAL_EXPLICIT, \
     SC_FREE, SC_CELL
from compiler.consts import (CO_VARARGS, CO_VARKEYWORDS, CO_NEWLOCALS,
     CO_NESTED, CO_GENERATOR, CO_FUTURE_DIVISION,
     CO_FUTURE_ABSIMPORT, CO_FUTURE_WITH_STATEMENT, CO_FUTURE_PRINT_FUNCTION,
     CO_COROUTINE, CO_ASYNC_GENERATOR, CO_FUTURE_BARRY_AS_BDFL, CO_FUTURE_GENERATOR_STOP,
     CO_FUTURE_ANNOTATIONS)
from compiler.unparse import to_expr
from .visitor import ASTVisitor

from . import config

callfunc_opcode_info = {
    # (Have *args, Have **args) : opcode
    (0,0) : "CALL_FUNCTION",
    (1,0) : "CALL_FUNCTION_VAR",
    (0,1) : "CALL_FUNCTION_KW",
    (1,1) : "CALL_FUNCTION_VAR_KW",
}

LOOP = 1
EXCEPT = 2
TRY_FINALLY = 3
END_FINALLY = 4

def compileFile(filename, display=0):
    # compile.c uses marshal to write a long directly, with
    # calling the interface that would also generate a 1-byte code
    # to indicate the type of the value.  simplest way to get the
    # same effect is to call marshal and then skip the code.
    fileinfo = os.stat(filename)

    f = open(filename, 'U')
    buf = f.read()
    f.close()
    code = compile(buf, filename, 'exec')
    f = open(filename + "c", "wb")

    hdr = struct.pack('<II', int(fileinfo.st_mtime), fileinfo.st_size)
    f.write(imp.get_magic())
    f.write(hdr)
    marshal.dump(code, f)
    f.close()


def compile(source, filename, mode, flags=None, dont_inherit=None):
    """Replacement for builtin compile() function"""
    if flags is not None or dont_inherit is not None:
        raise RuntimeError("not implemented yet")

    return make_compiler(source, filename, mode, get_default_generator()).getCode()


def make_compiler(source, filename, mode, generator=None):
    if mode not in ("single", "exec", "eval"):
        raise ValueError("compile() 3rd arg must be 'exec' or "
                         "'eval' or 'single'")

    if isinstance(source, ast.AST):
        tree = source
    else:
        tree = ast.parse(source, filename, mode)

    if generator is None:
        generator = get_default_generator()

    return generator.make_code_gen("<module>", tree, filename)


def get_bool_const(node):
    """Return True if node represent constantly true value, False if
    constantly false value, and None otherwise (non-constant)."""
    if isinstance(node, ast.Num):
        return bool(node.n)
    if isinstance(node, ast.NameConstant):
        return bool(node.value)
    if isinstance(node, ast.Str):
        return bool(node.s)
    if isinstance(node, ast.Name):
        if node.id == "__debug__":
            return not OPTIMIZE


def is_constant_false(node):
    if isinstance(node, ast.Num):
        if not node.n:
            return 1
    return 0


def is_constant_true(node):
    if isinstance(node, ast.Num):
        if node.n:
            return 1
    return 0

def is_const(node):
    # This is the Python 3.6 definition of constant
    return isinstance(node, (ast.Num, ast.Str, ast.Ellipsis, ast.Bytes, ast.NameConstant, ast.Constant)) or (isinstance(node, ast.Name) and node.id == '__debug__')

# TODO: We need to implement optimized code gen
OPTIMIZE = False

def const_value(node):
    if isinstance(node, (ast.NameConstant, ast.Constant)):
        return node.value
    elif isinstance(node, ast.Num):
        return node.n
    elif isinstance(node, (ast.Str, ast.Bytes)):
        return node.s
    elif isinstance(node, ast.Ellipsis):
        return ...
    else:
        assert isinstance(node, ast.Name) and node.id == '__debug__'
        return not OPTIMIZE

def all_items_const(seq, begin, end):
    for item in seq[begin:end]:
        if not is_const(item):
            return False
    return True

CONV_STR = ord('s')
CONV_REPR = ord('r')
CONV_ASCII = ord('a')

class CodeGenerator(ASTVisitor):
    """Defines basic code generator for Python bytecode

    This class is an abstract base class.  Concrete subclasses must
    define an __init__() that defines self.graph and then calls the
    __init__() defined in this class.
    """

    optimized = 0 # is namespace access optimized?
    __initialized = None
    class_name = None # provide default for instance variable
    future_flags = 0
    flow_graph = pyassem.PyFlowGraph

    def __init__(self, node, scopes, module = None, graph = None):
        super().__init__()
        self.tree = node
        self.scopes = scopes
        self.module = module or self
        if graph is not None:
            self.graph = graph
        self.setups = misc.Stack()
        self.last_lineno = None
        self._setupGraphDelegation()
        self._div_op = "BINARY_DIVIDE"
        self.interactive = False
        self.graph.setFlag(self.module.future_flags)
        self.scope = self.scopes[node]
        self.graph.setFreeVars(self.scope.get_free_vars())
        self.graph.setCellVars(self.scope.get_cell_vars())

    def _setupGraphDelegation(self):
        self.emit = self.graph.emit
        self.newBlock = self.graph.newBlock
        self.nextBlock = self.graph.nextBlock

    def getCode(self):
        """Return a code object"""
        return self.graph.getCode()

    def mangle(self, name):
        if self.class_name is not None:
            return misc.mangle(name, self.class_name)
        else:
            return name

    def get_module(self):
        raise RuntimeError("should be implemented by subclasses")

    # Next five methods handle name access

    def storeName(self, name):
        self._nameOp('STORE', name)

    def loadName(self, name):
        self._nameOp('LOAD', name)

    def delName(self, name):
        self._nameOp('DELETE', name)

    def _nameOp(self, prefix, name):
        name = self.mangle(name)
        scope = self.scope.check_name(name)
        if scope == SC_LOCAL:
            if not self.optimized:
                self.emit(prefix + '_NAME', name)
            else:
                self.emit(prefix + '_FAST', name)
        elif scope == SC_GLOBAL_EXPLICIT:
            self.emit(prefix + '_GLOBAL', name)
        elif scope == SC_GLOBAL_IMPLICIT:
            if not self.optimized:
                self.emit(prefix + '_NAME', name)
            else:
                self.emit(prefix + '_GLOBAL', name)
        elif scope == SC_FREE or scope == SC_CELL:
            if isinstance(self.scope, symbols.ClassScope):
                if prefix == "STORE" and name not in self.scope.nonlocals:
                    self.emit(prefix + '_NAME', name)
                    return

            if isinstance(self.scope, symbols.ClassScope) and prefix == "LOAD":
                self.emit(prefix + '_CLASSDEREF', name)
            else:
                self.emit(prefix + '_DEREF', name)
        else:
            raise RuntimeError("unsupported scope for var %s: %d" % \
                  (name, scope))

    def _implicitNameOp(self, prefix, name):
        """Emit name ops for names generated implicitly by for loops

        The interpreter generates names that start with a period or
        dollar sign.  The symbol table ignores these names because
        they aren't present in the program text.
        """
        if self.optimized:
            self.emit(prefix + '_FAST', name)
        else:
            self.emit(prefix + '_NAME', name)

    def set_lineno(self, node):
        if hasattr(node, "lineno"):
            self.graph.lineno = node.lineno
            self.graph.lineno_set = False

    def update_lineno(self, node):
        if hasattr(node, "lineno") and node.lineno > self.graph.lineno:
            self.set_lineno(node)

    def get_docstring(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) \
           and isinstance(node.body[0].value, ast.Str):
            return node.body[0].value.s

    def skip_docstring(self, body):
        """Given list of statements, representing body of a function, class,
        or module, skip docstring, if any.
        """
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Str):
            return body[1:]
        return body

    # The first few visitor methods handle nodes that generator new
    # code objects.  They use class attributes to determine what
    # specialized code generators to use.

    def visitInteractive(self, node):
        self.interactive = True
        self.visit(node.body)
        self.emit('LOAD_CONST', None)
        self.emit('RETURN_VALUE')

    def findFutures(self, node):
        future_flags = 0
        for feature in future.find_futures(node):
            if feature == "generator_stop":
                future_flags |= CO_FUTURE_GENERATOR_STOP
            elif feature == "barry_as_FLUFL":
                future_flags |= CO_FUTURE_BARRY_AS_BDFL
        return future_flags

    def visitModule(self, node):
        self.future_flags = self.findFutures(node)
        self.graph.setFlag(self.future_flags)

        if node.body:
            self.set_lineno(node.body[0])

        # Set current line number to the line number of first statement.
        # This way line number for SETUP_ANNOTATIONS will always
        # coincide with the line number of first "real" statement in module.
        # If body is empy, then lineno will be set later in assemble.
        if self.findAnn(node.body):
            self.emit("SETUP_ANNOTATIONS")
            self.did_setup_annotations = True
        else:
            self.did_setup_annotations = False
        doc = self.get_docstring(node)
        if doc is not None:
            self.emit('LOAD_CONST', doc)
            self.storeName('__doc__')
        self.visit(self.skip_docstring(node.body))

        # See if the was a live statement, to later set its line number as
        # module first line. If not, fall back to first line of 1.
        if not self.graph.first_inst_lineno:
            self.graph.first_inst_lineno = 1

        self.emit('LOAD_CONST', None)
        self.emit('RETURN_VALUE')

    def visitExpression(self, node):
        self.visit(node.body)
        self.emit('RETURN_VALUE')

    def visitFunctionDef(self, node):
        self.set_lineno(node)
        self._visitFuncOrLambda(node, isLambda=0)
        self.storeName(node.name)

    visitAsyncFunctionDef = visitFunctionDef

    def visitJoinedStr(self, node):
        self.update_lineno(node)
        for value in node.values:
            self.visit(value)
        if len(node.values) != 1:
            self.emit('BUILD_STRING', len(node.values))

    def visitFormattedValue(self, node):
        self.update_lineno(node)
        self.visit(node.value)

        if node.conversion == CONV_STR: oparg = pyassem.FVC_STR
        elif node.conversion == CONV_REPR: oparg = pyassem.FVC_REPR
        elif node.conversion == CONV_ASCII: oparg = pyassem.FVC_ASCII
        else:
            assert node.conversion == -1, str(node.conversion)
            oparg = pyassem.FVC_NONE

        if node.format_spec:
            self.visit(node.format_spec)
            oparg |= pyassem.FVS_HAVE_SPEC
        self.emit('FORMAT_VALUE', oparg)

    def visitLambda(self, node):
        self.update_lineno(node)
        self._visitFuncOrLambda(node, isLambda=1)

    def processBody(self, body, gen):
        if isinstance(body, list):
            for stmt in body:
                gen.visit(stmt)
        else:
            gen.visit(body)

    def _visitAnnotation(self, node):
        return self.visit(node)

    def _visitFuncOrLambda(self, node, isLambda=0):
        if not isLambda and node.decorator_list:
            for decorator in node.decorator_list:
                self.visit(decorator)
            ndecorators = len(node.decorator_list)
        else:
            ndecorators = 0
        flags = 0
        gen = self.make_func_codegen(node, self.graph.filename, self.scopes, isLambda,
                               self.class_name, self.module)
        body = node.body
        if not isLambda:
            body = self.skip_docstring(body)

        self.processBody(body, gen)

        gen.finishFunction()
        if node.args.defaults:
            for default in node.args.defaults:
                self.visit(default)
                flags |= 0x01
            self.emit('BUILD_TUPLE', len(node.args.defaults))

        kwdefaults = []
        for kwonly, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None:
                kwdefaults.append(self.mangle(kwonly.arg))
                self.visit(default)

        if kwdefaults:
            self.emit('LOAD_CONST', tuple(kwdefaults))
            self.emit('BUILD_CONST_KEY_MAP', len(kwdefaults))
            flags |= 0x02

        ann_args = []
        ann_num = 0
        for arg in node.args.args:
            if arg.annotation:
                self._visitAnnotation(arg.annotation)
                ann_args.append(self.mangle(arg.arg))
        if node.args.vararg:
            if node.args.vararg.annotation:
                self._visitAnnotation(node.args.vararg.annotation)
                ann_args.append(self.mangle(node.args.vararg.arg))
        for arg in node.args.kwonlyargs:
            if arg.annotation:
                self._visitAnnotation(arg.annotation)
                ann_args.append(self.mangle(arg.arg))
        if node.args.kwarg:
            if node.args.kwarg.annotation:
                self._visitAnnotation(node.args.kwarg.annotation)
                ann_args.append(self.mangle(node.args.kwarg.arg))
        # Cannot annotate return type for lambda
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            self._visitAnnotation(node.returns)
            ann_args.append("return")
        if ann_args:
            flags |= 0x04
            self.emit('LOAD_CONST', tuple(ann_args))
            self.emit('BUILD_CONST_KEY_MAP', len(ann_args))

        self._makeClosure(gen, flags)

        for i in range(ndecorators):
            self.emit('CALL_FUNCTION', 1)

    def visitClassDef(self, node):
        self.set_lineno(node)
        for decorator in node.decorator_list:
            self.visit(decorator)

        gen = self.make_class_codegen(node, self.graph.filename, self.scopes,
                            self.module)
        gen.emit("LOAD_NAME", "__name__")
        gen.storeName("__module__")
        gen.emit("LOAD_CONST", gen.get_qual_prefix(gen) + gen.name)
        gen.storeName("__qualname__")
        if gen.findAnn(node.body):
            gen.did_setup_annotations = True
            gen.emit("SETUP_ANNOTATIONS")
        else:
            gen.did_setup_annotations = False

        doc = gen.get_docstring(node)
        if doc is not None:
            gen.update_lineno(node.body[0])
            gen.emit("LOAD_CONST", doc)
            gen.storeName('__doc__')

        walk(self.skip_docstring(node.body), gen)

        gen.graph.startExitBlock()
        if '__class__' in gen.scope.cells:
            gen.emit('LOAD_CLOSURE', '__class__')
            gen.emit('DUP_TOP')
            gen.emit('STORE_NAME', '__classcell__')
        else:
            gen.emit('LOAD_CONST', None)
        gen.emit('RETURN_VALUE')

        self.emit('LOAD_BUILD_CLASS')
        self._makeClosure(gen, 0)
        self.emit('LOAD_CONST', node.name)

        self._call_helper(2, node.bases, node.keywords)

        for i in range(len(node.decorator_list)):
            self.emit('CALL_FUNCTION', 1)

        self.storeName(node.name)

    # The rest are standard visitor methods

    # The next few implement control-flow statements

    def visitIf(self, node):
        self.set_lineno(node)
        test = node.test
        test_const = get_bool_const(test)
        end = self.newBlock("if_end")
        orelse = None
        if node.orelse:
            orelse = self.newBlock("if_else")

        if test_const is None:
            self.compileJumpIf(test, orelse or end, False)

        if test_const != False:
            self.nextBlock()
            self.visit(node.body)

        if node.orelse:
            if test_const is None:
                self.emit('JUMP_FORWARD', end)
            if test_const != True:
                self.nextBlock(orelse)
                self.visit(node.orelse)

        self.nextBlock(end)

    def visitWhile(self, node):
        self.set_lineno(node)

        test_const = get_bool_const(node.test)
        if test_const == False:
            if node.orelse:
                self.visit(node.orelse)
            return

        loop = self.newBlock("while_loop")
        else_ = self.newBlock("while_else")

        after = self.newBlock("while_after")
        self.emit('SETUP_LOOP', after)

        self.nextBlock(loop)
        self.setups.push((LOOP, loop))

        if test_const != True:
            self.compileJumpIf(node.test, else_ or after, False)

        self.nextBlock(label='while_body')
        self.visit(node.body)
        self.emit('JUMP_ABSOLUTE', loop)

        if not is_constant_true(node.test):
            self.nextBlock(else_ or after) # or just the POPs if not else clause

        self.emit('POP_BLOCK')
        self.setups.pop()
        if node.orelse:
            self.visit(node.orelse)
        self.nextBlock(after)

    def visitFor(self, node):
        start = self.newBlock()
        anchor = self.newBlock()
        after = self.newBlock()
        self.setups.push((LOOP, start))

        self.set_lineno(node)
        self.emit('SETUP_LOOP', after)
        self.visit(node.iter)
        self.emit('GET_ITER')

        self.nextBlock(start)
        self.emit('FOR_ITER', anchor)
        self.visit(node.target)
        self.visit(node.body)
        self.emit('JUMP_ABSOLUTE', start)
        self.nextBlock(anchor)
        self.emit('POP_BLOCK')
        self.setups.pop()
        if node.orelse:
            self.visit(node.orelse)
        self.nextBlock(after)

    def visitAsyncFor(self, node):
        try_ = self.newBlock('async_for_try')
        except_ = self.newBlock('except')
        end = self.newBlock('end')
        after_try = self.newBlock('after_try')
        try_cleanup = self.newBlock('try_cleanup')
        after_loop_else = self.newBlock('after_loop_else')

        self.set_lineno(node)

        self.emit('SETUP_LOOP', end)
        self.setups.push((LOOP, try_))

        self.visit(node.iter)
        self.emit('GET_AITER')
        self.emit('LOAD_CONST', None)
        self.emit('YIELD_FROM')

        self.nextBlock(try_)

        self.emit('SETUP_EXCEPT', except_)
        self.setups.push((EXCEPT, try_))

        self.emit('GET_ANEXT')
        self.emit('LOAD_CONST', None)
        self.emit('YIELD_FROM')
        self.visit(node.target)
        self.emit('POP_BLOCK')
        self.setups.pop()
        self.emit('JUMP_FORWARD', after_try)

        self.nextBlock(except_)
        self.emit('DUP_TOP')
        self.emit('LOAD_GLOBAL', 'StopAsyncIteration')

        self.emit('COMPARE_OP', 'exception match')
        self.emit('POP_JUMP_IF_TRUE', try_cleanup)
        self.emit('END_FINALLY')

        self.nextBlock(after_try)
        self.visit(node.body)
        self.emit('JUMP_ABSOLUTE', try_)

        self.nextBlock(try_cleanup)
        self.emit('POP_TOP')
        self.emit('POP_TOP')
        self.emit('POP_TOP')
        self.emit('POP_EXCEPT')
        self.emit('POP_TOP')
        self.emit('POP_BLOCK')
        self.setups.pop()

        self.nextBlock(after_loop_else)

        if node.orelse:
            self.visit(node.orelse)
        self.nextBlock(end)

    def visitBreak(self, node):
        if not self.setups:
            raise SyntaxError("'break' outside loop", self.syntax_error_position(node))
        self.set_lineno(node)
        self.emit('BREAK_LOOP')

    def visitContinue(self, node):
        if not self.setups:
            raise SyntaxError("'continue' not properly in loop", self.syntax_error_position(node))
        self.set_lineno(node)
        kind, block = self.setups.top()
        if kind == LOOP:
            self.emit('JUMP_ABSOLUTE', block)
            self.nextBlock()
        elif kind == EXCEPT or kind == TRY_FINALLY:
            # find the block that starts the loop
            top = len(self.setups)
            while top > 0:
                top = top - 1
                kind, loop_block = self.setups[top]
                if kind == LOOP:
                    break
                elif kind == END_FINALLY:
                    raise SyntaxError("'continue' not supported inside 'finally' clause", self.syntax_error_position(node))
            if kind != LOOP:
                raise SyntaxError("'continue' not properly in loop", self.syntax_error_position(node))
            self.emit('CONTINUE_LOOP', loop_block)
            self.nextBlock()
        elif kind == END_FINALLY:
            raise SyntaxError("'continue' not supported inside 'finally' clause", self.syntax_error_position(node))

    def syntax_error_position(self, node):
        source_line = linecache.getline(self.graph.filename, node.lineno)
        return self.graph.filename, node.lineno, node.col_offset, source_line or None

    def syntax_error(self, msg, node):
        source_line = linecache.getline(self.graph.filename, node.lineno)
        return SyntaxError(
            msg,
            (self.graph.filename, node.lineno, node.col_offset, source_line or None))

    def visitTest(self, node, jump):
        end = self.newBlock()
        for child in node.values[:-1]:
            self.visit(child)
            self.emit(jump, end)
            self.nextBlock()
        self.visit(node.values[-1])
        self.nextBlock(end)

    _boolop_opcode = {
        ast.And: "JUMP_IF_FALSE_OR_POP",
        ast.Or: "JUMP_IF_TRUE_OR_POP",
    }

    def visitBoolOp(self, node):
        opcode = self._boolop_opcode[type(node.op)]
        self.visitTest(node, opcode)

    _cmp_opcode = {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }

    def compileJumpIf(self, test, next, is_if_true):
        self.visit(test)
        self.emit('POP_JUMP_IF_TRUE' if is_if_true else 'POP_JUMP_IF_FALSE', next)

    def visitIfExp(self, node):
        endblock = self.newBlock()
        elseblock = self.newBlock()
        self.compileJumpIf(node.test, elseblock, False)
        self.visit(node.body)
        self.emit('JUMP_FORWARD', endblock)
        self.nextBlock(elseblock)
        self.visit(node.orelse)
        self.nextBlock(endblock)

    def emitChainedCompareStep(self, op, value, cleanup, jump='JUMP_IF_FALSE_OR_POP'):
        self.visit(value)
        self.emit('DUP_TOP')
        self.emit('ROT_THREE')
        self.emit('COMPARE_OP', self._cmp_opcode[type(op)])
        self.emit(jump, cleanup)
        self.nextBlock(label='compare_or_cleanup')

    def visitCompare(self, node):
        self.update_lineno(node)
        self.visit(node.left)
        cleanup = self.newBlock('cleanup')
        for op, code in zip(node.ops[:-1], node.comparators[:-1]):
            self.emitChainedCompareStep(op, code, cleanup)
        # now do the last comparison
        if node.ops:
            op = node.ops[-1]
            code = node.comparators[-1]
            self.visit(code)
            self.emit('COMPARE_OP', self._cmp_opcode[type(op)])
        if len(node.ops) > 1:
            end = self.newBlock('end')
            self.emit('JUMP_FORWARD', end)
            self.nextBlock(cleanup)
            self.emit('ROT_TWO')
            self.emit('POP_TOP')
            self.nextBlock(end)

    def get_qual_prefix(self, gen):
        prefix = ""
        if gen.scope.global_scope:
            return prefix
        # Construct qualname prefix
        parent = gen.scope.parent
        while not isinstance(parent, symbols.ModuleScope):
            # Only real functions use "<locals>", nested scopes like
            # comprehensions don't.
            if type(parent) in (symbols.FunctionScope, symbols.LambdaScope):
                prefix = parent.name + ".<locals>." + prefix
            else:
                prefix = parent.name + "." + prefix
            if parent.global_scope:
                break
            parent = parent.parent
        return prefix

    def _makeClosure(self, gen, flags):
        prefix = ""
        if not isinstance(gen.tree, ast.ClassDef):
            prefix = self.get_qual_prefix(gen)

        frees = gen.scope.get_free_vars()
        if frees:
            for name in frees:
                self.emit('LOAD_CLOSURE', name)
            self.emit('BUILD_TUPLE', len(frees))
            flags |= 0x08

        self.emit('LOAD_CONST', gen)
        self.emit('LOAD_CONST', prefix + gen.name)  # py3 qualname
        self.emit('MAKE_FUNCTION', flags)

    def visitDelete(self, node):
        self.set_lineno(node)
        self.visit(node.targets)

    def compile_comprehension(self, node, name, elt, val):
        class Holder: pass
        node.args = Holder()
        arg1 = Holder()
        arg1.arg = ".0"
        node.args.args = (arg1,)
        node.args.kwonlyargs = ()
        node.args.vararg = None
        node.args.kwarg = None
        node.body = []
        self.update_lineno(node)
        gen = self.make_generator_codegen(node, self.graph.filename, self.scopes, self.class_name,
                                   self.module, name)

        if isinstance(node, ast.ListComp):
            gen.emit('BUILD_LIST')
        elif isinstance(node, ast.SetComp):
            gen.emit('BUILD_SET')
        elif isinstance(node, ast.DictComp):
            gen.emit('BUILD_MAP')
        elif not isinstance(node, ast.GeneratorExp):
            raise SystemError(f"Unknown comprehension type: {type(node).__name__}")

        gen.compile_comrehension_generator(node.generators, 0, elt, val, type(node))

        if not isinstance(node, ast.GeneratorExp):
            gen.emit('RETURN_VALUE')

        gen.finishFunction()

        self._makeClosure(gen, 0)

        # precomputation of outmost iterable
        self.visit(node.generators[0].iter)
        if node.generators[0].is_async:
            self.emit('GET_AITER')
            self.emit('LOAD_CONST', None)
            self.emit('YIELD_FROM')
        else:
            self.emit('GET_ITER')
        self.emit('CALL_FUNCTION', 1)

        if gen.scope.coroutine and type(node) is not ast.GeneratorExp:
            self.emit('GET_AWAITABLE')
            self.emit('LOAD_CONST', None)
            self.emit('YIELD_FROM')


    def visitGeneratorExp(self, node):
        self.compile_comprehension(node, "<genexpr>", node.elt, None)

    def visitListComp(self, node):
        self.compile_comprehension(node, "<listcomp>", node.elt, None)

    def visitSetComp(self, node):
        self.compile_comprehension(node, "<setcomp>", node.elt, None)

    def visitDictComp(self, node):
        self.compile_comprehension(node, "<dictcomp>", node.key, node.value)

    def compile_comrehension_generator(self, generators, gen_index, elt, val, type):
        if generators[gen_index].is_async:
            self.compile_async_comprehension(generators, gen_index, elt, val, type)
        else:
            self.compile_sync_comprehension(generators, gen_index, elt, val, type)

    def compile_async_comprehension(self, generators, gen_index, elt, val, type):
        try_ = self.newBlock("try")
        after_try = self.newBlock("after_try")
        except_ = self.newBlock("except")
        if_cleanup = self.newBlock("if_cleanup")
        try_cleanup = self.newBlock("try_cleanup")

        gen = generators[gen_index]
        if gen_index == 0:
            self.loadName('.0')
        else:
            self.visit(gen.iter)
            self.emit('GET_AITER')
            self.emit('LOAD_CONST', None)
            self.emit('YIELD_FROM')

        self.nextBlock(try_)
        self.emit('SETUP_EXCEPT', except_)
        self.setups.push((EXCEPT, try_))
        self.emit('GET_ANEXT')
        self.emit('LOAD_CONST', None)
        self.emit('YIELD_FROM')
        self.visit(gen.target)
        self.emit('POP_BLOCK')
        self.setups.pop()
        self.emit('JUMP_FORWARD', after_try)

        self.nextBlock(except_)
        self.emit('DUP_TOP')
        self.emit('LOAD_GLOBAL', 'StopAsyncIteration')
        self.emit('COMPARE_OP', 'exception match')
        self.emit('POP_JUMP_IF_TRUE', try_cleanup)
        self.emit('END_FINALLY')

        self.nextBlock(after_try)
        for if_ in gen.ifs:
            self.compileJumpIf(if_, if_cleanup, False)
            self.newBlock()

        gen_index += 1
        if gen_index < len(generators):
            self.compile_comrehension_generator(generators, gen_index, elt, val, type)
        elif type is ast.GeneratorExp:
            self.visit(elt)
            self.emit('YIELD_VALUE')
            self.emit('POP_TOP')
        elif type is ast.ListComp:
            self.visit(elt)
            self.emit('LIST_APPEND', gen_index + 1)
        elif type is ast.SetComp:
            self.visit(elt)
            self.emit('SET_ADD', gen_index + 1)
        elif type is ast.DictComp:
            self.visit(val)
            self.visit(elt)
            self.emit('MAP_ADD', gen_index + 1)
        else:
            raise NotImplementedError('unknown comprehension type')

        self.nextBlock(if_cleanup)
        self.emit('JUMP_ABSOLUTE', try_)

        self.nextBlock(try_cleanup)
        self.emit('POP_TOP')
        self.emit('POP_TOP')
        self.emit('POP_TOP')
        self.emit('POP_EXCEPT') # for SETUP_EXCEPT
        self.emit('POP_TOP')

    def compile_sync_comprehension(self, generators, gen_index, elt, val, type):
        start = self.newBlock("start")
        skip = self.newBlock("skip")
        if_cleanup = self.newBlock("if_cleanup")
        anchor = self.newBlock("anchor")

        gen = generators[gen_index]
        if gen_index == 0:
            self.loadName('.0')
        else:
            self.visit(gen.iter)
            self.emit('GET_ITER')

        self.nextBlock(start)
        self.emit('FOR_ITER', anchor)
        self.nextBlock()
        self.visit(gen.target)

        for if_ in gen.ifs:
            self.compileJumpIf(if_, if_cleanup, False)
            self.newBlock()

        gen_index += 1
        if gen_index < len(generators):
            self.compile_comrehension_generator(generators, gen_index, elt, val, type)
        else:
            if type is ast.GeneratorExp:
                self.visit(elt)
                self.emit('YIELD_VALUE')
                self.emit('POP_TOP')
            elif type is ast.ListComp:
                self.visit(elt)
                self.emit('LIST_APPEND', gen_index + 1)
            elif type is ast.SetComp:
                self.visit(elt)
                self.emit('SET_ADD', gen_index + 1)
            elif type is ast.DictComp:
                self.visit(val)
                self.visit(elt)
                self.emit('MAP_ADD', gen_index + 1)
            else:
                raise NotImplementedError('unknown comprehension type')

            self.nextBlock(skip)
        self.nextBlock(if_cleanup)
        self.emit('JUMP_ABSOLUTE', start)
        self.nextBlock(anchor)

    # exception related

    def visitAssert(self, node):
        # XXX would be interesting to implement this via a
        # transformation of the AST before this stage
        if __debug__:
            end = self.newBlock()
            self.set_lineno(node)
            # XXX AssertionError appears to be special case -- it is always
            # loaded as a global even if there is a local name.  I guess this
            # is a sort of renaming op.
            self.nextBlock()
            self.compileJumpIf(node.test, end, True)

            self.nextBlock()
            self.emit('LOAD_GLOBAL', 'AssertionError')
            if node.msg:
                self.visit(node.msg)
                self.emit('CALL_FUNCTION', 1)
                self.emit('RAISE_VARARGS', 1)
            else:
                self.emit('RAISE_VARARGS', 1)
            self.nextBlock(end)

    def visitRaise(self, node):
        self.set_lineno(node)
        n = 0
        if node.exc:
            self.visit(node.exc)
            n = n + 1
        if node.cause:
            self.visit(node.cause)
            n = n + 1
        self.emit('RAISE_VARARGS', n)

    def visitTry(self, node):
        self.set_lineno(node)
        if node.finalbody:
            self.visitTryFinally(node)
            return

        self.visitTryExcept(node)

    def visitTryExcept(self, node):
        body = self.newBlock('try_body')
        handlers = self.newBlock('try_handlers')
        end = self.newBlock('try_end')
        if node.orelse:
            lElse = self.newBlock('try_else')
        else:
            lElse = end

        self.emit('SETUP_EXCEPT', handlers)
        self.nextBlock(body)
        self.setups.push((EXCEPT, body))
        self.visit(node.body)
        self.emit('POP_BLOCK')
        self.setups.pop()
        self.emit('JUMP_FORWARD', lElse)
        self.nextBlock(handlers)

        last = len(node.handlers) - 1
        for i in range(len(node.handlers)):
            handler = node.handlers[i]
            expr = handler.type
            target = handler.name
            body = handler.body
            self.set_lineno(handler)
            if expr:
                self.emit('DUP_TOP')
                self.visit(expr)
                self.emit('COMPARE_OP', 'exception match')
                next = self.newBlock()
                self.emit('POP_JUMP_IF_FALSE', next)
                self.nextBlock()
            elif i < last:
                raise SyntaxError("default 'except:' must be last", self.syntax_error_position(handler))
            else:
                self.set_lineno(handler)
            self.emit('POP_TOP')
            if target:
                self.visit(ast.Name(id=target, ctx=ast.Store(), lineno=expr.lineno))
            else:
                self.emit('POP_TOP')
            self.emit('POP_TOP')

            if target:
                protected = ast.Try(
                    body=body, handlers=[], orelse=[],
                    finalbody=[
                        ast.Assign(targets=[ast.Name(id=target, ctx=ast.Store())], value=ast.NameConstant(value=None)),
                        ast.Delete(ast.Name(id=target, ctx=ast.Del())),
                    ]
                )
                self.visitTryFinally(protected, except_protect=True)
            else:
                # "block" param shouldn't matter, so just pass None
                self.setups.push((EXCEPT, None))
                self.visit(body)
                self.emit('POP_EXCEPT')
                self.setups.pop()

            self.emit('JUMP_FORWARD', end)
            if expr:
                self.nextBlock(next)
            else:
                self.nextBlock(label='handler_end')
        self.emit('END_FINALLY')
        if node.orelse:
            self.nextBlock(lElse)
            self.visit(node.orelse)
        self.nextBlock(end)

    def visitTryFinally(self, node, except_protect=False):
        body = self.newBlock()
        final = self.newBlock()
        self.emit('SETUP_FINALLY', final)
        self.nextBlock(body)
        self.setups.push((TRY_FINALLY, body))
        if node.handlers:
            self.visitTryExcept(node)
        else:
            self.visit(node.body)
        self.emit('POP_BLOCK')
        self.setups.pop()
        if except_protect:
            self.emit('POP_EXCEPT')
        self.emit('LOAD_CONST', None)
        self.nextBlock(final)
        self.setups.push((END_FINALLY, final))
        self.visit(node.finalbody)
        self.emit('END_FINALLY')
        self.setups.pop()

    __with_count = 0

    def visitWith(self, node):
        self.set_lineno(node)
        body = self.newBlock()
        stack = []
        for withitem in node.items:
            final = self.newBlock()
            stack.append(final)
            self.__with_count += 1
            valuevar = "_[%d]" % self.__with_count
            self.visit(withitem.context_expr)

            py2 = 0

            if py2:
                self.emit('DUP_TOP')
                self.emit('LOAD_ATTR', '__exit__')
                self.emit('ROT_TWO')
                self.emit('LOAD_ATTR', '__enter__')
                self.emit('CALL_FUNCTION', 0)
            else:
                self.emit('SETUP_WITH', final)

            if withitem.optional_vars is None:
                self.emit('POP_TOP')
            else:
                if py2:
                    self._implicitNameOp('STORE', valuevar)
                else:
                    self.visit(withitem.optional_vars)

            if py2:
                self.emit('SETUP_FINALLY', final)

            self.setups.push((TRY_FINALLY, body))

            if py2 and withitem.optional_vars is not None:
                self._implicitNameOp('LOAD', valuevar)
                self._implicitNameOp('DELETE', valuevar)
                self.visit(withitem.optional_vars)

        self.nextBlock(body)
        self.visit(node.body)

        while stack:
            final = stack.pop()
            self.emit('POP_BLOCK')
            self.setups.pop()
            self.emit('LOAD_CONST', None)
            self.nextBlock(final)
            self.setups.push((END_FINALLY, final))
            self.emit('WITH_CLEANUP_START')
            self.emit('WITH_CLEANUP_FINISH')
            self.emit('END_FINALLY')
            self.setups.pop()
            self.__with_count -= 1

    def visitAsyncWith(self, node):
        self.set_lineno(node)
        body = self.newBlock()
        stack = []
        for withitem in node.items:
            final = self.newBlock()
            stack.append(final)
            self.__with_count += 1
            valuevar = "_[%d]" % self.__with_count
            self.visit(withitem.context_expr)

            self.emit('BEFORE_ASYNC_WITH')
            self.emit('GET_AWAITABLE')
            self.emit('LOAD_CONST', None)
            self.emit('YIELD_FROM')
            self.emit('SETUP_ASYNC_WITH', final)

            if withitem.optional_vars is None:
                self.emit('POP_TOP')
            else:
                self.visit(withitem.optional_vars)

            self.setups.push((TRY_FINALLY, body))

        self.nextBlock(body)
        self.visit(node.body)

        while stack:
            final = stack.pop()
            self.emit('POP_BLOCK')
            self.setups.pop()
            self.emit('LOAD_CONST', None)
            self.nextBlock(final)
            self.setups.push((END_FINALLY, final))
            self.emit('WITH_CLEANUP_START')
            self.emit('GET_AWAITABLE')
            self.emit('LOAD_CONST', None)
            self.emit('YIELD_FROM')
            self.emit('WITH_CLEANUP_FINISH')
            self.emit('END_FINALLY')
            self.setups.pop()
            self.__with_count -= 1

    # misc

    def visitExpr(self, node):
        self.set_lineno(node)
        # CPy3.6 discards lots of constants
        if self.interactive:
            self.visit(node.value)
            self.emit('PRINT_EXPR')
        elif not is_const(node.value):
            self.visit(node.value)
            self.emit('POP_TOP')

    def visitNum(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', node.n)

    def visitStr(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', node.s)

    def visitBytes(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', node.s)

    def visitNameConstant(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', node.value)

    def visitConst(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', node.value)

    def visitKeyword(self, node):
        self.emit('LOAD_CONST', node.name)
        self.visit(node.expr)

    def visitGlobal(self, node):
        self.set_lineno(node)
        # no code to generate

    def visitNonlocal(self, node):
        self.set_lineno(node)
        # no code to generate

    def visitName(self, node):
        self.update_lineno(node)
        if isinstance(node.ctx, ast.Store):
            self.storeName(node.id)
        elif isinstance(node.ctx, ast.Del):
            self.delName(node.id)
        elif node.id == "__debug__":
            self.emit("LOAD_CONST", not OPTIMIZE)
        else:
            self.loadName(node.id)

    def visitPass(self, node):
        self.set_lineno(node)

    def visitImport(self, node):
        self.set_lineno(node)
        level = 0
        for alias in node.names:
            name = alias.name
            asname = alias.asname
            self.emit('LOAD_CONST', level)
            self.emit('LOAD_CONST', None)
            self.emit('IMPORT_NAME', self.mangle(name))
            mod = name.split(".")[0]
            if asname:
                self.emitImportAs(name, asname)
            else:
                self.storeName(mod)

    def visitImportFrom(self, node):
        self.set_lineno(node)
        level = node.level
        fromlist = tuple(alias.name for alias in node.names)
        self.emit('LOAD_CONST', level)
        self.emit('LOAD_CONST', fromlist)
        self.emit('IMPORT_NAME', node.module or '')
        for alias in node.names:
            name = alias.name
            asname = alias.asname
            if name == '*':
                self.namespace = 0
                self.emit('IMPORT_STAR')
                # There can only be one name w/ from ... import *
                assert len(node.names) == 1
                return
            else:
                self.emit('IMPORT_FROM', name)
                self.storeName(asname or name)
        self.emit('POP_TOP')

    def emitImportAs(self, name: str, asname: str):
        elts = name.split(".")
        if len(elts) == 1:
            self.storeName(asname)
            return
        for elt in elts[1:]:
            self.emit('LOAD_ATTR', elt)
        self.storeName(asname)

    def visitAttribute(self, node):
        self.update_lineno(node)
        self.visit(node.value)
        if isinstance(node.ctx, ast.Store):
            self.emit('STORE_ATTR', self.mangle(node.attr))
        elif isinstance(node.ctx, ast.Del):
            self.emit('DELETE_ATTR', self.mangle(node.attr))
        else:
            self.emit('LOAD_ATTR', self.mangle(node.attr))

    # next five implement assignments

    def visitAssign(self, node):
        self.set_lineno(node)
        self.visit(node.value)
        dups = len(node.targets) - 1
        for i in range(len(node.targets)):
            elt = node.targets[i]
            if i < dups:
                self.emit('DUP_TOP')
            if isinstance(elt, ast.AST):
                self.visit(elt)

    def checkAnnExpr(self, node):
        self._visitAnnotation(node)
        self.emit('POP_TOP')

    def checkAnnSlice(self, node):
        if isinstance(node, ast.Index):
            self.checkAnnExpr(node.value)
        else:
            if node.lower:
                self.checkAnnExpr(node.lower)
            if node.upper:
                self.checkAnnExpr(node.upper)
            if node.step:
                self.checkAnnExpr(node.step)

    def checkAnnSubscr(self, node):
        if isinstance(node, (ast.Index, ast.Slice)):
            self.checkAnnSlice(node)
        elif isinstance(node, ast.ExtSlice):
            for v in node.dims:
                self.checkAnnSlice(v)

    def checkAnnotation(self, node):
        if isinstance(self.tree, (ast.Module, ast.ClassDef)):
            self.checkAnnExpr(node.annotation)

    def findAnn(self, stmts):
        for stmt in stmts:
            if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                # Don't recurse into definitions looking for annotations
                continue
            elif isinstance(stmt, ast.AnnAssign):
                return True
            elif isinstance(stmt, ast.stmt):
                for field in stmt._fields:
                    child = getattr(stmt, field)
                    if isinstance(child, list):
                        if self.findAnn(child):
                            return True

        return False

    def emitStoreAnnotation(self, name: str, annotation: ast.expr):
        assert self.did_setup_annotations
        self._visitAnnotation(annotation)
        mangled = self.mangle(name)
        self.emit('STORE_ANNOTATION', mangled)

    def visitAnnAssign(self, node):
        self.set_lineno(node)
        if node.value:
            self.visit(node.value)
            self.visit(node.target)
        if isinstance(node.target, ast.Name):
            # If we have a simple name in a module or class, store the annotation
            if node.simple and isinstance(self.tree, (ast.Module, ast.ClassDef)):
                self.emitStoreAnnotation(node.target.id, node.annotation)
        elif isinstance(node.target, ast.Attribute):
            if not node.value:
                self.checkAnnExpr(node.target.value)
        elif isinstance(node.target, ast.Subscript):
            if not node.value:
                self.checkAnnExpr(node.target.value)
                self.checkAnnSubscr(node.target.slice)
        else:
            raise SystemError(f"invalid node type {type(node).__name__} for annotated assignment")

        if not node.simple:
            self.checkAnnotation(node)

    def visitAssName(self, node):
        if node.flags == 'OP_ASSIGN':
            self.storeName(node.name)
        elif node.flags == 'OP_DELETE':
            self.set_lineno(node)
            self.delName(node.name)
        else:
            print("oops", node.flags)
            assert 0

    def visitAssAttr(self, node):
        self.visit(node.expr)
        if node.flags == 'OP_ASSIGN':
            self.emit('STORE_ATTR', self.mangle(node.attrname))
        elif node.flags == 'OP_DELETE':
            self.emit('DELETE_ATTR', self.mangle(node.attrname))
        else:
            print("warning: unexpected flags:", node.flags)
            print(node)
            assert 0

    def _visitAssSequence(self, node, op='UNPACK_SEQUENCE'):
        if findOp(node) != 'OP_DELETE':
            self.emit(op, len(node.nodes))
        for child in node.nodes:
            self.visit(child)

    visitAssTuple = _visitAssSequence
    visitAssList = _visitAssSequence

    # augmented assignment

    def visitAugAssign(self, node):
        self.set_lineno(node)
        aug_node = wrap_aug(node.target)
        self.visit(aug_node, "load")
        self.visit(node.value)
        self.emit(self._augmented_opcode[type(node.op)])
        self.visit(aug_node, "store")

    _augmented_opcode = {
        ast.Add: 'INPLACE_ADD',
        ast.Sub: 'INPLACE_SUBTRACT',
        ast.Mult: 'INPLACE_MULTIPLY',
        ast.MatMult: 'INPLACE_MATRIX_MULTIPLY',
        ast.Div: 'INPLACE_TRUE_DIVIDE',
        ast.FloorDiv: 'INPLACE_FLOOR_DIVIDE',
        ast.Mod: 'INPLACE_MODULO',
        ast.Pow: 'INPLACE_POWER',
        ast.RShift: 'INPLACE_RSHIFT',
        ast.LShift: 'INPLACE_LSHIFT',
        ast.BitAnd: 'INPLACE_AND',
        ast.BitXor: 'INPLACE_XOR',
        ast.BitOr: 'INPLACE_OR',
        }

    def visitAugName(self, node, mode):
        if mode == "load":
            self.loadName(node.id)
        elif mode == "store":
            self.storeName(node.id)

    def visitAugAttribute(self, node, mode):
        if mode == "load":
            self.visit(node.value)
            self.emit('DUP_TOP')
            self.emit('LOAD_ATTR', self.mangle(node.attr))
        elif mode == "store":
            self.emit('ROT_TWO')
            self.emit('STORE_ATTR', self.mangle(node.attr))

    def visitAugSubscript(self, node, mode):
        if mode == "load":
            self.visitSubscript(node, 1)
        elif mode == "store":
            self.emit('ROT_THREE')
            self.emit('STORE_SUBSCR')

    def visitExec(self, node):
        self.visit(node.expr)
        if node.locals is None:
            self.emit('LOAD_CONST', None)
        else:
            self.visit(node.locals)
        if node.globals is None:
            self.emit('DUP_TOP')
        else:
            self.visit(node.globals)
        self.emit('EXEC_STMT')

    def compiler_subkwargs(self, kwargs, begin, end):
        nkwargs = end - begin
        if nkwargs > 1:
            for i in range(begin, end):
                self.visit(kwargs[i].value)
            self.emit('LOAD_CONST', tuple(arg.arg for arg in kwargs[begin:end]))
            self.emit('BUILD_CONST_KEY_MAP', nkwargs)
        else:
            for i in range(begin, end):
                self.emit('LOAD_CONST', kwargs[i].arg)
                self.visit(kwargs[i].value)
            self.emit('BUILD_MAP', nkwargs)


    def _call_helper(self, argcnt, args, kwargs):
        mustdictunpack = any(arg.arg is None for arg in kwargs)
        nelts = len(args)
        nkwelts = len(kwargs)
        # the number of tuples and dictionaries on the stack
        nsubkwargs = nsubargs = 0
        nseen = argcnt #  the number of positional arguments on the stack
        for arg in args:
            if isinstance(arg, ast.Starred):
                if nseen:
                    self.emit('BUILD_TUPLE', nseen)
                    nseen = 0
                    nsubargs += 1
                self.visit(arg.value)
                nsubargs += 1
            else:
                self.visit(arg)
                nseen += 1

        if nsubargs or mustdictunpack:
            if nseen:
                self.emit('BUILD_TUPLE', nseen)
                nsubargs += 1
            if nsubargs > 1:
                self.emit('BUILD_TUPLE_UNPACK_WITH_CALL', nsubargs)
            elif nsubargs == 0:
                self.emit('BUILD_TUPLE', 0)

            nseen = 0 # the number of keyword arguments on the stack following
            for i, kw in enumerate(kwargs):
                if kw.arg is None:
                    if nseen:
                        # A keyword argument unpacking.
                        self.compiler_subkwargs(kwargs, i - nseen, i)
                        nsubkwargs += 1
                        nseen = 0
                    self.visit(kw.value)
                    nsubkwargs += 1
                else:
                    nseen += 1
            if nseen:
                self.compiler_subkwargs(kwargs, nkwelts - nseen, nkwelts)
                nsubkwargs += 1
            if nsubkwargs > 1:
                self.emit('BUILD_MAP_UNPACK_WITH_CALL', nsubkwargs)
            self.emit('CALL_FUNCTION_EX', int(nsubkwargs > 0))
        elif nkwelts:
            for kw in kwargs:
                self.visit(kw.value)
            self.emit('LOAD_CONST', tuple(arg.arg for arg in kwargs))
            self.emit('CALL_FUNCTION_KW', nelts + nkwelts + argcnt)
        else:
            self.emit('CALL_FUNCTION', nelts + argcnt)

    def visitCall(self, node):
        self.update_lineno(node)
        self.visit(node.func)
        self._call_helper(0, node.args, node.keywords)

    def visitPrint(self, node, newline=0):
        self.set_lineno(node)
        if node.dest:
            self.visit(node.dest)
        for child in node.nodes:
            if node.dest:
                self.emit('DUP_TOP')
            self.visit(child)
            if node.dest:
                self.emit('ROT_TWO')
                self.emit('PRINT_ITEM_TO')
            else:
                self.emit('PRINT_ITEM')
        if node.dest and not newline:
            self.emit('POP_TOP')

    def visitPrintnl(self, node):
        self.visitPrint(node, newline=1)
        if node.dest:
            self.emit('PRINT_NEWLINE_TO')
        else:
            self.emit('PRINT_NEWLINE')

    def visitReturn(self, node):
        if not isinstance(self.tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise SyntaxError("'return' outside function", self.syntax_error_position(node))
        elif self.scope.coroutine and self.scope.generator and node.value:
            raise SyntaxError("'return' with value in async generator", self.syntax_error_position(node))

        self.set_lineno(node)
        if node.value:
            self.visit(node.value)
        else:
            self.emit('LOAD_CONST', None)
        self.emit('RETURN_VALUE')

    def visitYield(self, node):
        if not isinstance(self.tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)):
            raise SyntaxError("'yield' outside function", self.syntax_error_position(node))
        self.update_lineno(node)
        if node.value:
            self.visit(node.value)
        else:
            self.emit('LOAD_CONST', None)
        self.emit('YIELD_VALUE')

    def visitYieldFrom(self, node):
        if not isinstance(self.tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)):
            raise SyntaxError("'yield' outside function", self.syntax_error_position(node))
        elif self.scope.coroutine:
            raise SyntaxError("'yield from' inside async function", self.syntax_error_position(node))

        self.update_lineno(node)
        self.visit(node.value)
        self.emit('GET_YIELD_FROM_ITER')
        self.emit('LOAD_CONST', None)
        self.emit('YIELD_FROM')

    def visitAwait(self, node):
        self.update_lineno(node)
        self.visit(node.value)
        self.emit('GET_AWAITABLE')
        self.emit('LOAD_CONST', None)
        self.emit('YIELD_FROM')

    # slice and subscript stuff
    def visitSubscript(self, node, aug_flag=None):
        self.update_lineno(node)
        self.visit(node.value)
        self.visit(node.slice)

        if isinstance(node.ctx, ast.Load):
            self.emit('BINARY_SUBSCR')
        elif isinstance(node.ctx, ast.Store):
            if aug_flag:
                self.emit('DUP_TOP_TWO')
                self.emit('BINARY_SUBSCR')
            else:
                self.emit('STORE_SUBSCR')
        elif isinstance(node.ctx, ast.Del):
            self.emit('DELETE_SUBSCR')
        else:
            assert 0

    # binary ops

    def binaryOp(self, node, op):
        self.visit(node.left)
        self.visit(node.right)
        self.emit(op)

    _binary_opcode = {
        ast.Add: "BINARY_ADD",
        ast.Sub: "BINARY_SUBTRACT",
        ast.Mult: "BINARY_MULTIPLY",
        ast.MatMult: "BINARY_MATRIX_MULTIPLY",
        ast.Div: "BINARY_TRUE_DIVIDE",
        ast.FloorDiv: "BINARY_FLOOR_DIVIDE",
        ast.Mod: "BINARY_MODULO",
        ast.Pow: "BINARY_POWER",
        ast.LShift: "BINARY_LSHIFT",
        ast.RShift: "BINARY_RSHIFT",
        ast.BitOr: "BINARY_OR",
        ast.BitXor: "BINARY_XOR",
        ast.BitAnd: "BINARY_AND",
    }

    def visitBinOp(self, node):
        self.update_lineno(node)
        self.visit(node.left)
        self.visit(node.right)
        op = self._binary_opcode[type(node.op)]
        self.emit(op)

    # unary ops

    def unaryOp(self, node, op):
        self.visit(node.operand)
        self.emit(op)

    _unary_opcode = {
        ast.Invert: "UNARY_INVERT",
        ast.USub: "UNARY_NEGATIVE",
        ast.UAdd: "UNARY_POSITIVE",
        ast.Not: "UNARY_NOT",
    }

    def visitUnaryOp(self, node):
        self.update_lineno(node)
        self.unaryOp(node, self._unary_opcode[type(node.op)])

    def visitBackquote(self, node):
        return self.unaryOp(node, 'UNARY_CONVERT')

    # object constructors

    def visitEllipsis(self, node):
        self.update_lineno(node)
        self.emit('LOAD_CONST', Ellipsis)

    def _visitUnpack(self, node):
        before = 0
        after = 0
        starred = None
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                if starred is not None:
                    raise SyntaxError("two starred expressions in assignment", self.syntax_error_position(elt))
                elif before >= 256 or len(node.elts) - before - 1 >= (1<<31)>>8:
                    raise SyntaxError("too many expressions in star-unpacking assignment", self.syntax_error_position(elt))
                starred = elt.value
            elif starred:
                after += 1
            else:
                before += 1
        if starred:
            self.emit('UNPACK_EX', after << 8 | before)
        else:
            self.emit('UNPACK_SEQUENCE', before)

    def hasStarred(self, elts):
        for elt in elts:
            if isinstance(elt, ast.Starred):
                return True
        return False

    def _visitSequence(self, node, build_op, build_inner_op, build_ex_op, ctx):
        self.update_lineno(node)
        if isinstance(ctx, ast.Store):
            self._visitUnpack(node)
            starred_load = False
        else:
            starred_load = self.hasStarred(node.elts)

        chunks = 0
        in_chunk = 0

        def out_chunk():
            nonlocal chunks, in_chunk
            if in_chunk:
                self.emit(build_inner_op, in_chunk)
                in_chunk = 0
                chunks += 1

        for elt in node.elts:
            if starred_load:
                if isinstance(elt, ast.Starred):
                    out_chunk()
                    chunks += 1
                else:
                    in_chunk += 1

            if isinstance(elt, ast.Starred):
                self.visit(elt.value)
            else:
                self.visit(elt)
        # Output trailing chunk, if any
        out_chunk()

        if isinstance(ctx, ast.Load):
            if starred_load:
                self.emit(build_ex_op, chunks)
            else:
                self.emit(build_op, len(node.elts))

    def visitStarred(self, node):
        if isinstance(node.ctx, ast.Store):
            raise SyntaxError("starred assignment target must be in a list or tuple", self.syntax_error_position(node))
        else:
            raise SyntaxError("can't use starred expression here", self.syntax_error_position(node))

    def visitTuple(self, node):
        self._visitSequence(node, 'BUILD_TUPLE', 'BUILD_TUPLE', 'BUILD_TUPLE_UNPACK', node.ctx)

    def visitList(self, node):
        self._visitSequence(node, 'BUILD_LIST', 'BUILD_TUPLE', 'BUILD_LIST_UNPACK', node.ctx)

    def visitSet(self, node):
        self._visitSequence(node, 'BUILD_SET', 'BUILD_SET','BUILD_SET_UNPACK', ast.Load())

    def visitSlice(self, node):
        num = 2
        if node.lower:
            self.visit(node.lower)
        else:
            self.emit('LOAD_CONST', None)
        if node.upper:
            self.visit(node.upper)
        else:
            self.emit('LOAD_CONST', None)
        if node.step:
            self.visit(node.step)
            num += 1
        self.emit('BUILD_SLICE', num)

    def visitExtSlice(self, node):
        for d in node.dims:
            self.visit(d)
        self.emit('BUILD_TUPLE', len(node.dims))

    # Create dict item by item. Saves interp stack size at the expense
    # of bytecode size/speed.
    def visitDict_by_one(self, node):
        self.update_lineno(node)
        self.emit('BUILD_MAP', 0)
        for k, v in zip(node.keys, node.values):
            self.emit('DUP_TOP')
            self.visit(k)
            self.visit(v)
            self.emit('ROT_THREE')
            self.emit('STORE_SUBSCR')

    def compile_subdict(self, node, begin, end):
        n = end - begin
        if n > 1 and all_items_const(node.keys, begin, end):
            for i in range(begin, end):
                self.visit(node.values[i])

            self.emit('LOAD_CONST', tuple(const_value(x) for x in node.keys[begin:end]))
            self.emit('BUILD_CONST_KEY_MAP', n)
        else:
            for i in range(begin, end):
                self.visit(node.keys[i])
                self.visit(node.values[i])

            self.emit('BUILD_MAP', n)

    def visitDict(self, node):
        self.update_lineno(node)
        containers = elements = 0
        is_unpacking = False

        for i, (k, v) in enumerate(zip(node.keys, node.values)):
            is_unpacking = k is None
            if elements == 0xFFFF or (elements and is_unpacking):
                self.compile_subdict(node, i - elements, i)
                containers += 1
                elements = 0

            if is_unpacking:
                self.visit(v)
                containers += 1
            else:
                elements += 1

        if elements or containers == 0:
            self.compile_subdict(node, len(node.keys) - elements, len(node.keys))
            containers += 1

        while containers > 1 or is_unpacking:
            oparg = min(containers, 255)
            self.emit('BUILD_MAP_UNPACK', oparg)
            containers -= (oparg - 1)
            is_unpacking = False

    @property
    def name(self):
        if isinstance(self.tree, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            return self.tree.name
        elif isinstance(self.tree, ast.SetComp):
            return "<setcomp>"
        elif isinstance(self.tree, ast.ListComp):
            return "<listcomp>"
        elif isinstance(self.tree, ast.DictComp):
            return "<dictcomp>"
        elif isinstance(self.tree, ast.GeneratorExp):
            return "<genexpr>"
        elif isinstance(self.tree, ast.Lambda):
            return "<lambda>"

    def finishFunction(self):
        if self.graph.current.returns:
            return
        self.graph.startExitBlock()
        if not isinstance(self.tree, ast.Lambda):
            self.emit('LOAD_CONST', None)
        self.emit('RETURN_VALUE')

    @classmethod
    def make_func_codegen(cls, func, filename, scopes, isLambda, class_name, mod):
        if isLambda:
            name = "<lambda>"
        else:
            name = func.name
        graph = cls.make_function_graph(func, filename, scopes, class_name, mod, name)
        res = cls(func, scopes, mod, graph)
        res.optimized = 1
        res.class_name = class_name
        return res

    @classmethod
    def make_generator_codegen(cls, func, filename, scopes, class_name, mod, name):
        graph = cls.make_function_graph(func, filename, scopes, class_name, mod, name)
        res = cls(func, scopes, mod, graph)
        res.optimized = 1
        res.class_name = class_name
        return res

    @classmethod
    def make_class_codegen(cls, klass, filename, scopes, module):
        graph = cls.flow_graph(klass.name, filename,
                                           optimized=0, klass=1)

        doc = get_docstring(klass)
        if doc is not None:
            graph.setDocstring(doc)
        graph.firstline = klass.lineno

        res = cls(klass, scopes, module, graph)
        res.class_name = klass.name
        return res

    @classmethod
    def make_function_graph(cls, func, filename, scopes, class_name, mod, name):
        isLambda = isinstance(func, ast.Lambda)
        args = [misc.mangle(elt.arg, class_name) for elt in func.args.args]
        kwonlyargs = [misc.mangle(elt.arg, class_name) for elt in func.args.kwonlyargs]

        starargs = []
        if func.args.vararg:
            starargs.append(func.args.vararg.arg)
        if func.args.kwarg:
            starargs.append(func.args.kwarg.arg)

        graph = cls.flow_graph(
            name, filename,
            args=args, kwonlyargs=kwonlyargs, starargs=starargs,
            optimized=1
        )

        if not isLambda:
            doc = get_docstring(func)
            if doc is not None:
                graph.setDocstring(doc)

        scope = scopes[func]
        if func.args.vararg:
            graph.setFlag(CO_VARARGS)
        if func.args.kwarg:
            graph.setFlag(CO_VARKEYWORDS)
        if scope.nested:
            graph.setFlag(CO_NESTED)
        if scope.generator and not scope.coroutine:
            graph.setFlag(CO_GENERATOR)
        if not scope.generator and scope.coroutine:
            graph.setFlag(CO_COROUTINE)
        if scope.generator and scope.coroutine:
            graph.setFlag(CO_ASYNC_GENERATOR)

        graph.firstline = func.lineno
        return graph

    @classmethod
    def make_code_gen(cls, name, tree, filename):
        s = symbols.SymbolVisitor()
        walk(tree, s)

        graph = cls.flow_graph(name, filename)
        code_gen = cls(tree, s.scopes, graph = graph)
        walk(tree, code_gen)
        return code_gen


class CodeGeneratorNoPeephole(CodeGenerator):
    @classmethod
    def flow_graph(cls, name, filename, args=(), kwonlyargs=(), starargs=(), optimized=0, klass=None):
        return pyassem.PyFlowGraph(name, filename, args, kwonlyargs, starargs, optimized, klass, peephole_enabled=False)


class Python37CodeGenerator(CodeGenerator):
    def visitCall(self, node):
        if (node.keywords or
            not isinstance(node.func, ast.Attribute) or
            not isinstance(node.func.ctx, ast.Load) or
            any(isinstance(arg, ast.Starred) for arg in node.args)):
            # We cannot optimize this call
            return super().visitCall(node)

        self.update_lineno(node)
        self.visit(node.func.value)
        self.emit('LOAD_METHOD', self.mangle(node.func.attr))
        for arg in node.args:
            self.visit(arg)
        self.emit('CALL_METHOD', len(node.args))

    def findFutures(self, node):
        future_flags = 0
        for feature in future.find_futures(node):
            if feature == "barry_as_FLUFL":
                future_flags |= CO_FUTURE_BARRY_AS_BDFL
            elif feature == "annotations":
                future_flags |= CO_FUTURE_ANNOTATIONS
        return future_flags

    def _visitAnnotation(self, node):
        if self.module.future_flags & CO_FUTURE_ANNOTATIONS:
            self.emit('LOAD_CONST', to_expr(node))
        else:
            self.visit(node)

    def emitStoreAnnotation(self, name: str, annotation: ast.expr):
        assert self.did_setup_annotations

        self._visitAnnotation(annotation)
        self.emit("LOAD_NAME", "__annotations__")
        mangled = self.mangle(name)
        self.emit("LOAD_CONST", mangled)
        self.emit('STORE_SUBSCR')

    def emitImportAs(self, name: str, asname: str):
        elts = name.split(".")
        if len(elts) == 1:
            self.storeName(asname)
            return
        first = True
        for elt in elts[1:]:
            if not first:
                self.emit('ROT_TWO')
                self.emit('POP_TOP')
            self.emit('IMPORT_FROM', elt)
            first = False
        self.storeName(asname)
        self.emit('POP_TOP')

    def compileJumpIf(self, test, next, is_if_true):
        if isinstance(test, ast.UnaryOp):
            if isinstance(test.op, ast.Not):
                # Compile to remove not operation
                self.compileJumpIf(test.operand, next, not is_if_true)
                return
        elif isinstance(test, ast.BoolOp):
            is_or = isinstance(test.op, ast.Or)
            skip_jump = next
            if is_if_true != is_or:
                skip_jump = self.newBlock()

            for node in test.values[:-1]:
                self.compileJumpIf(node, skip_jump, is_or)

            self.compileJumpIf(test.values[-1], next, is_if_true)

            if skip_jump is not next:
                self.nextBlock(skip_jump)
            return
        elif isinstance(test, ast.IfExp):
            end = self.newBlock('end')
            orelse = self.newBlock('orelse')
            # Jump directly to orelse if test matches
            self.compileJumpIf(test.test, orelse, 0)
            # Jump directly to target if test is true and body is matches
            self.compileJumpIf(test.body, next, is_if_true)
            self.emit('JUMP_FORWARD', end)
            # Jump directly to target if test is true and orelse matches
            self.nextBlock(orelse)
            self.compileJumpIf(test.orelse, next, is_if_true)

            self.nextBlock(end)
            return
        elif isinstance(test, ast.Compare):
            if len(test.ops) > 1:
                cleanup = self.newBlock()
                self.visit(test.left)
                for op, comparator in zip(test.ops[:-1], test.comparators[:-1]):
                    self.emitChainedCompareStep(op, comparator, cleanup, "POP_JUMP_IF_FALSE")
                self.visit(test.comparators[-1])
                self.emit('COMPARE_OP', self._cmp_opcode[type(test.ops[-1])])
                self.emit('POP_JUMP_IF_TRUE' if is_if_true else 'POP_JUMP_IF_FALSE', next)
                end = self.newBlock()
                self.emit('JUMP_FORWARD', end)
                self.nextBlock(cleanup)
                self.emit('POP_TOP')
                if not is_if_true:
                    self.emit('JUMP_FORWARD', next)
                self.nextBlock(end)
                return

        self.visit(test)
        self.emit('POP_JUMP_IF_TRUE' if is_if_true else 'POP_JUMP_IF_FALSE', next)
        return True


def get_default_generator():
    if sys.version_info >= (3, 7):
        return Python37CodeGenerator

    return CodeGenerator

def get_docstring(node):
    if node.body and isinstance(node.body[0], ast.Expr) \
       and isinstance(node.body[0].value, ast.Str):
        return node.body[0].value.s

def findOp(node):
    """Find the op (DELETE, LOAD, STORE) in an AssTuple tree"""
    v = OpFinder()
    v.VERBOSE = 0
    walk(node, v)
    return v.op

class OpFinder:
    def __init__(self):
        self.op = None
    def visitAssName(self, node):
        if self.op is None:
            self.op = node.flags
        elif self.op != node.flags:
            raise ValueError("mixed ops in stmt")
    visitAssAttr = visitAssName
    visitSubscript = visitAssName

class Delegator:
    """Base class to support delegation for augmented assignment nodes

    To generator code for augmented assignments, we use the following
    wrapper classes.  In visitAugAssign, the left-hand expression node
    is visited twice.  The first time the visit uses the normal method
    for that node .  The second time the visit uses a different method
    that generates the appropriate code to perform the assignment.
    These delegator classes wrap the original AST nodes in order to
    support the variant visit methods.
    """
    def __init__(self, obj):
        self.obj = obj

    def __getattr__(self, attr):
        return getattr(self.obj, attr)

class AugAttribute(Delegator):
    pass

class AugName(Delegator):
    pass

class AugSubscript(Delegator):
    pass

class CompInner(Delegator):

    def __init__(self, obj, nested_scope, init_inst, elt_nodes, elt_insts):
        Delegator.__init__(self, obj)
        self.nested_scope = nested_scope
        self.init_inst = init_inst
        self.elt_nodes = elt_nodes
        self.elt_insts = elt_insts

wrapper = {
    ast.Attribute: AugAttribute,
    ast.Name: AugName,
    ast.Subscript: AugSubscript,
}

def wrap_aug(node):
    return wrapper[node.__class__](node)

if __name__ == "__main__":
    for file in sys.argv[1:]:
        compileFile(file)
