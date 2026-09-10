import ast
import json
from pathlib import Path
import unittest
import tempfile


class ColabNotebookTests(unittest.TestCase):
    def test_notebook_clean_compilable_and_worker_matches(self):
        root = Path(__file__).resolve().parents[2]
        notebook = json.loads((root/'notebooks/Frozen_stock_research.ipynb').read_text())
        self.assertEqual(notebook['nbformat'], 4)
        worker = (root/'backend/scripts/run_colab_research.py').read_text()
        found = False
        setup = ''.join(notebook['cells'][1]['source'])
        self.assertNotIn('assert sys.version_info[:2] in', setup)
        self.assertIn('UV + ["venv", "--python", "3.12"', setup)
        self.assertIn('UV + ["pip", "install", "--python", PYTHON', setup)
        for cell in notebook['cells']:
            if cell['cell_type'] != 'code':
                continue
            self.assertEqual(cell['outputs'], [])
            self.assertIsNone(cell['execution_count'])
            source = ''.join(cell['source'])
            tree = ast.parse(source)
            compile(tree, '<notebook>', 'exec')
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'WORKER_SOURCE' for t in node.targets):
                    self.assertEqual(ast.literal_eval(node.value).rstrip(), worker.rstrip())
                    found = True
        self.assertTrue(found)

    def test_worker_rejects_policy_before_loading_files(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        from run_colab_research import run
        with self.assertRaisesRegex(ValueError, 'Unknown fixed job policy'):
            run('/missing/model', 'bad-pin', '/missing/output', 'live', 'random_forest')

    def test_input_packaging_rejects_tampered_pin_without_export(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        from prepare_colab_input import package
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            source.mkdir()
            (source/'manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Manifest pin mismatch'):
                package(source, '0'*64, root/'export.zip')
            self.assertFalse((root/'export.zip').exists())

    def test_source_cannot_be_output(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        from run_colab_research import run
        from prepare_colab_input import package
        with self.assertRaisesRegex(ValueError, 'outside'):
            run('/missing/model', 'pin', '/missing/model/output', 'baseline', 'random_forest')
        with self.assertRaisesRegex(ValueError, 'outside'):
            package('/missing/model', 'pin', '/missing/model/output.zip')
