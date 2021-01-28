from __future__ import print_function

import ast
from typing import Any, List

# XXX should probably rename ASTVisitor to ASTWalker
# XXX can it be made even more generic?

class ASTVisitor:
    """Performs a depth-first walk of the AST

    The ASTVisitor is responsible for walking over the tree in the
    correct order.  For each node, it checks the visitor argument for
    a method named 'visitNodeType' where NodeType is the name of the
    node's class, e.g. Class.  If the method exists, it is called
    with the node as its sole argument.

    This is basically the same as the built-in ast.NodeVisitor except
    for the following differences:
        It accepts extra parameters through the visit methods for flowing state
        It uses "visitNodeName" instead of "visit_NodeName"
        It accepts a list to the generic_visit function rather than just nodes
    """

    VERBOSE = 0

    def __init__(self):
        self.node = None
        self._cache = {}

    def generic_visit(self, node, *args):
        """Called if no explicit visitor function exists for a node."""
        if isinstance(node, list):
            for item in node:
                if isinstance(item, ast.AST):
                    self.visit(item, *args)
            return

        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item, *args)
            elif isinstance(value, ast.AST):
                self.visit(value, *args)


    def visit(self, node, *args):
        self.node = node
        klass = node.__class__
        meth = self._cache.get(klass, None)
        if meth is None:
            className = klass.__name__
            meth = getattr(self, 'visit' + className, self.generic_visit)
            self._cache[klass] = meth
##        if self.VERBOSE > 0:
##            className = klass.__name__
##            if self.VERBOSE == 1:
##                if meth == 0:
##                    print "visit", className
##            else:
##                print "visit", className, (meth and meth.__name__ or '')
        return meth(node, *args)


class ExampleASTVisitor(ASTVisitor):
    """Prints examples of the nodes that aren't visited

    This visitor-driver is only useful for development, when it's
    helpful to develop a visitor incrementally, and get feedback on what
    you still have to do.
    """
    examples = {}

    def visit(self, node, *args):
        self.node = node
        meth = self._cache.get(node.__class__, None)
        className = node.__class__.__name__
        if meth is None:
            meth = getattr(self, 'visit' + className, 0)
            self._cache[node.__class__] = meth
        if self.VERBOSE > 1:
            print("visit", className, meth and meth.__name__ or '')
        if meth:
            meth(node, *args)
        elif self.VERBOSE > 0:
            klass = node.__class__
            if klass not in self.examples:
                self.examples[klass] = klass
                print()
                print(self)
                print(klass)
                for attr in dir(node):
                    if attr[0] != '_':
                        print("\t", "%-12.12s" % attr, getattr(node, attr))
                print()
            return self.default(node, *args)

# XXX this is an API change

def walk(tree, visitor):
    return visitor.visit(tree)

def dumpNode(node):
    print(node.__class__)
    for attr in dir(node):
        if attr[0] != '_':
            print("\t", "%-10.10s" % attr, getattr(node, attr))
