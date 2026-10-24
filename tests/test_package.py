"""The package holds only code the tool runs: a function only the tests call reads as how
the tool works, and a fix in it changes nothing on the printer."""
import ast
import glob
import os

PACKAGE = os.path.join(os.path.dirname(__file__), '..', 'chopper_autotune')


def test_every_function_and_class_of_the_package_is_used_by_it():
    trees = {}
    for path in sorted(glob.glob(os.path.join(PACKAGE, '*.py'))):
        with open(path) as source:
            trees[os.path.basename(path)] = ast.parse(source.read())
    used = {getattr(node, 'id', None) or getattr(node, 'attr', None) or node.name
            for tree in trees.values() for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute, ast.alias))}
    assert ['%s %s' % (name, node.name) for name, tree in trees.items() for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name not in used] == []
