"""Public evidence paths retain raw locators while resolving for the caller."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/rtl-pack/lib'))
from rtl_source_path import project_runtime_evidence, source_path


class SourcePathTest(unittest.TestCase):
    def test_project_subdirectory_and_relocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first' / 'project'
            second = Path(tmp) / 'relocated' / 'project'
            for project in (first, second):
                source = project / 'inputs/chip/rtl'
                source.mkdir(parents=True)
                (source / 'unit.sv').write_text('module unit; endmodule\n')
                path, root = source_path(project, project / 'inputs/chip', 'rtl/unit.sv')
                self.assertEqual((path, root), ('inputs/chip/rtl/unit.sv', 'project'))
                self.assertEqual(hashlib.sha256((project / path).read_bytes()).hexdigest(),
                                 hashlib.sha256((source / 'unit.sv').read_bytes()).hexdigest())

    def test_runtime_nested_evidence_preserves_locator_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            source = project / 'design/rtl'
            source.mkdir(parents=True)
            (source / 'unit.sv').write_text('module unit; endmodule\n')
            raw = {'record': {'evidence': {'status': 'source-backed', 'path': 'rtl/unit.sv',
                                          'sha256': hashlib.sha256((source / 'unit.sv').read_bytes()).hexdigest(),
                                          'byte_start': 0, 'line_start': 1,
                                          'configuration_identity': 'build',
                                          'logical_location': {'path': 'virtual.sv', 'line': 1}},
                              'path': 'unit.path'}}
            projected = project_runtime_evidence(raw, project, project / 'design')
            evidence = projected['record']['evidence']
            self.assertEqual((evidence['path'], evidence['locator'], evidence['path_root']),
                             ('design/rtl/unit.sv', 'rtl/unit.sv', 'project'))
            self.assertEqual(evidence['logical_location']['path'], 'virtual.sv')
            self.assertEqual(projected['record']['path'], 'unit.path')
            self.assertEqual(project_runtime_evidence(projected, project, project / 'design'), projected)

    def test_external_source_and_unsafe_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            source = Path(tmp) / 'external'
            project.mkdir()
            source.mkdir()
            (source / 'unit.sv').write_text('module unit; endmodule\n')
            path, root = source_path(project, source, 'unit.sv')
            self.assertEqual((path, root), (str(source / 'unit.sv'), 'external-source'))
            for locator in ('../unit.sv', '/etc/passwd', 'missing.sv'):
                with self.subTest(locator=locator), self.assertRaises(ValueError):
                    source_path(project, source, locator)
            (source / 'escape.sv').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                source_path(project, source, 'escape.sv')


@unittest.skipUnless(os.environ.get('RTL_QUERY_Q2_PROJECT') and os.environ.get('RTL_ANALYSIS_TOOL_ROOT'),
                     'approved Q2 Project and locked tool root not installed')
class PublicRuntimePathTest(unittest.TestCase):
    def test_cli_matches_existing_knowledge_path_and_source_hash(self):
        project = Path(os.environ['RTL_QUERY_Q2_PROJECT'])
        command = [str(ROOT / 'src/rtl-pack/bin/rtl'), '--project', str(project), 'query',
                   '--build', str(project / 'runtime-query-build.json')]
        request = {'id': 'path', 'op': 'inspect', 'path': 'ibex_core.id_stage_i.controller_i'}
        result = subprocess.run(command, input=json.dumps(request) + '\n', text=True,
                                capture_output=True, check=True)
        evidence = json.loads(result.stdout.splitlines()[-1])['record']['evidence']
        self.assertEqual(evidence['path'], 'source/rtl/ibex_id_stage.sv')
        self.assertEqual(evidence['locator'], 'rtl/ibex_id_stage.sv')
        self.assertEqual(evidence['path_root'], 'project')
        self.assertEqual(hashlib.sha256((project / evidence['path']).read_bytes()).hexdigest(),
                         evidence['sha256'])
        self.assertGreater(evidence['line_end'], evidence['line_start'] - 1)
        prebuilt = subprocess.run([
            str(ROOT / 'src/rtl-pack/bin/rtl'), 'model', 'query', '--project', str(project),
            '--model', 'analysis/model/rtl-knowledge-module-q2-whole-stage1-v6.sqlite',
            '--view', 'module-knowledge', '--target-name', 'ibex_controller',
            '--build-sha256', '0994de23d7f61b3829504686d35a167ffc48bd8597fa2328fd4bbc8a30898866',
            '--request-kind', 'deterministic-instance-of',
        ], text=True, capture_output=True, check=True)
        anchors = json.loads(prebuilt.stdout)['evidence']
        self.assertTrue(any(item['path'] == evidence['path']
                            and item['locator'] == evidence['locator']
                            and item['path_root'] == evidence['path_root'] for item in anchors))

    def test_public_cli_explicit_external_source_root(self):
        external = ROOT / 'tests/fixtures/runtime_module_candidate'
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'analysis').mkdir()
            (project / 'rtl-project.json').write_text(json.dumps({
                'schema': 'rtl-analysis-agent/project-v1', 'name': 'external-source-test',
                'source': str(external), 'analysis': 'analysis', 'revision': None,
                'entry': 'top.sv',
            }))
            (project / 'build.json').write_bytes((external / 'build.json').read_bytes())
            env = {**os.environ, 'RTL_ANALYSIS_ALLOW_EXTERNAL_SOURCE': '1'}
            result = subprocess.run([
                str(ROOT / 'src/rtl-pack/bin/rtl'), '--project', str(project), 'query',
                '--build', str(project / 'build.json'),
            ], input=json.dumps({'id': 'external', 'op': 'inspect',
                                  'path': 'fixture_top.u_low'}) + '\n',
                text=True, capture_output=True, check=True, env=env)
            evidence = json.loads(result.stdout.splitlines()[-1])['record']['evidence']
            self.assertEqual(evidence['path_root'], 'external-source')
            self.assertEqual(evidence['locator'], 'top.sv')
            self.assertEqual(evidence['path'], str(external / 'top.sv'))
            self.assertEqual(hashlib.sha256(Path(evidence['path']).read_bytes()).hexdigest(),
                             evidence['sha256'])
