import importlib.util
import types
import unittest
from pathlib import Path


COLLECTOR = Path(__file__).resolve().parents[1] / "collectors" / "postgresql" / "collect_postgresql.py"
spec = importlib.util.spec_from_file_location("configbackup_postgresql_collector", COLLECTOR)
pg = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pg)


class PostgreSqlCollectorTests(unittest.TestCase):
    def test_safe_path_segment_is_cross_platform_and_deterministic(self):
        first = pg.safe_path_segment('Sales/2026:Prod')
        second = pg.safe_path_segment('Sales/2026:Prod')
        self.assertEqual(first, second)
        self.assertNotIn('/', first)
        self.assertNotIn(':', first)
        self.assertRegex(first, r'__[0-9a-f]{8}$')

    def test_secret_redaction(self):
        self.assertEqual(pg.redact_text('hunter2', name='password'), '<REDACTED>')
        self.assertIn('password=<REDACTED>', pg.redact_text('host=db password=hunter2 user=x'))
        self.assertEqual(pg.redact_text('postgres://u:p@db/app'), 'postgres://u:<REDACTED>@db/app')
        self.assertEqual(pg.redact_text('Server=db;Password=x', name='connection_string'), '<REDACTED>')

    def test_dump_normalization_removes_only_banner_timestamps(self):
        text = (
            '-- PostgreSQL database dump\n'
            '-- Started on 2026-09-21 01:00:00 EDT\n'
            'CREATE TABLE x(id integer);\n'
            '-- Completed on 2026-09-21 01:00:01 EDT\n'
        )
        normalized = pg.normalize_dump(text)
        self.assertNotIn('Started on', normalized)
        self.assertNotIn('Completed on', normalized)
        self.assertIn('CREATE TABLE x', normalized)

    def test_database_selection_supports_patterns_and_templates(self):
        rows = [
            {'database_name': 'postgres', 'allow_connections': True, 'is_template': False},
            {'database_name': 'app_prod', 'allow_connections': True, 'is_template': False},
            {'database_name': 'app_test', 'allow_connections': True, 'is_template': False},
            {'database_name': 'template1', 'allow_connections': True, 'is_template': True},
        ]
        args = types.SimpleNamespace(
            database=['app*'],
            exclude_database=['*_test'],
            include_template_databases=False,
        )
        selected = pg.select_databases(rows, args)
        self.assertEqual([x['database_name'] for x in selected], ['app_prod'])

    def test_user_mapping_option_name_controls_redaction(self):
        self.assertEqual(pg.redact_text('secret-value', name='password'), '<REDACTED>')
        self.assertEqual(pg.redact_text('readonly', name='role'), 'readonly')


if __name__ == '__main__':
    unittest.main()
