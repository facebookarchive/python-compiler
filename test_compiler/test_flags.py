import unittest
from unittest import TestCase
from .common import CompilerTest
import dis
from dis import opmap, opname
import ast
import sys
from compiler.consts import (
    CO_OPTIMIZED,
    CO_NOFREE,
    CO_NEWLOCALS,
    CO_NESTED,
    CO_GENERATOR,
    CO_COROUTINE,
    CO_ASYNC_GENERATOR,
    CO_FUTURE_BARRY_AS_BDFL,
    CO_FUTURE_GENERATOR_STOP,
)


class FlagTests(CompilerTest):
    def test_future_no_longer_relevant(self):
        f = self.run_code(
            """
        from __future__ import print_function
        def f(): pass"""
        )["f"]
        self.assertEqual(f.__code__.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS)

    def test_future_gen_stop(self):
        f = self.run_code(
            """
        from __future__ import generator_stop
        def f(): pass"""
        )["f"]
        expected = CO_FUTURE_GENERATOR_STOP | CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS
        if sys.version_info >= (3, 7):
            expected &= ~CO_FUTURE_GENERATOR_STOP
        self.assertEqual(f.__code__.co_flags, expected)

    def test_future_barry_as_bdfl(self):
        f = self.run_code(
            """
        from __future__ import barry_as_FLUFL
        def f(): pass"""
        )["f"]
        self.assertEqual(
            f.__code__.co_flags,
            CO_FUTURE_BARRY_AS_BDFL | CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS,
        )

    def test_braces(self):
        with self.assertRaisesRegex(SyntaxError, "not a chance"):
            f = self.run_code(
                """
            from __future__ import braces
            def f(): pass"""
            )

    def test_gen_func(self):
        f = self.run_code("def f(): yield")["f"]
        self.assertEqual(
            f.__code__.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_GENERATOR
        )

    def test_async_gen_func(self):
        f = self.run_code(
            """
        async def f():
            yield
            await foo"""
        )["f"]
        self.assertEqual(
            f.__code__.co_flags,
            CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_ASYNC_GENERATOR,
        )

    def test_gen_func_yield_from(self):
        f = self.run_code("def f(): yield from (1, 2, 3)")["f"]
        self.assertEqual(
            f.__code__.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_GENERATOR
        )

    def test_gen_exp(self):
        f = self.compile("x = (x for x in (1, 2, 3))")
        code = self.find_code(f)
        self.assertEqual(
            code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_GENERATOR
        )

    def test_list_comp(self):
        f = self.compile("x = [x for x in (1, 2, 3)]")
        code = self.find_code(f)
        self.assertEqual(code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS)

    def test_dict_comp(self):
        f = self.compile("x = {x:x for x in (1, 2, 3)}")
        code = self.find_code(f)
        self.assertEqual(code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS)

    def test_set_comp(self):
        f = self.compile("x = {x for x in (1, 2, 3)}")
        code = self.find_code(f)
        self.assertEqual(code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS)

    def test_class(self):
        f = self.compile("class C: pass")
        code = self.find_code(f)
        self.assertEqual(code.co_flags, CO_NOFREE)

    def test_coroutine(self):
        f = self.compile("async def f(): pass")
        code = self.find_code(f)
        self.assertEqual(
            code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_COROUTINE
        )

    def test_coroutine_await(self):
        f = self.compile("async def f(): await foo")
        code = self.find_code(f)
        self.assertEqual(
            code.co_flags, CO_NOFREE | CO_OPTIMIZED | CO_NEWLOCALS | CO_COROUTINE
        )

    def test_free_vars(self):
        f = self.compile(
            """
        def g():
            x = 2
            def f():
                return x"""
        )
        code = self.find_code(self.find_code(f))
        self.assertEqual(code.co_flags, CO_NESTED | CO_OPTIMIZED | CO_NEWLOCALS)


if __name__ == "__main__":
    unittest.main()
