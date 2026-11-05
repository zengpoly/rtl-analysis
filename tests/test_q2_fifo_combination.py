"""Synthetic-only combination approval, publication and serving contract."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.test_module_q2_publication import ModuleQ2PublicationTest, artifact, digest, p, manual
from tests.test_manual_v6_build1 import ManualV6Build1Test
import rtl_query_v02 as query


class Q2FifoCombinationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old = ModuleQ2PublicationTest('runTest')
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        old_store, old_overlay, old_root, _, _ = self.old._prepared()
        old_store.close()
        self.old_batch = {key: value for key, value in old_overlay.items() if key != 'review'}
        self.old_root = old_root
        self.fixture = ManualV6Build1Test('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        store, ids = f._make_store(real_q2=True, publication_contract=p.Q2_FIFO_V2_PUBLICATION_PATH)
        scope = f._register(store, mode='ACTUAL_INSTANCE_ROOT',
                            roots=[f._root(ids['root_a'], store.contract_id('entities', 'INSTANCE'))],
                            source_paths=['rtl/root_a.sv', 'rtl/leaf.sv'])
        self.scope = scope
        self.contract, _, _ = p._load_publication_contract(p.Q2_FIFO_V2_PUBLICATION_PATH)
        self.candidate, self.publication = store._publication_review_context(scope, self.contract)
        self.actual = ids['root_a']
        self.module = store.connection.execute(
            "SELECT entity_id FROM entity WHERE qualified_name='root_a'"
        ).fetchone()[0]
        store.close()
        self.store = p.PrototypeStore.open_publication(f.database, publication_contract=p.Q2_FIFO_V2_PUBLICATION_PATH)
        self.root = f.root / 'combo-review'
        (self.root / 'components').mkdir(parents=True)
        self.batches = self._batches()
        self.approved, self.overlays = self._approval()
        self.patch = mock.patch.dict(p.IBEX_OLD_Q2_COMPONENT_SHA256, {
            key: hashlib.sha256((self.root / f'components/{key}.json').read_bytes()).hexdigest()
            for key in p.IBEX_OLD_Q2_COMPONENT_SHA256
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.store.close)

    def _batches(self):
        q2 = copy.deepcopy(self.old_batch)
        q2['candidate'] = self.candidate
        q2['contract'] = self.publication
        q2['coverage'][0]['profile'] = self.contract['profile']
        sources = q2['claims'][0]['direct_source_evidence']
        fifo_claims, fifo_coverage = [], []
        for obligation in sorted(key for key in self.contract['obligations'] if key.startswith('fifo.')):
            shape = self.contract['claim_shapes'][obligation]
            fields = {}
            for key, spec in shape['fields'].items():
                if spec['kind'] == 'scalar':
                    fields[key] = 'fixture configuration' if key == 'config_boundary' else f'fixture {key}'
                elif spec['kind'] == 'list':
                    fields[key] = ([self.actual] if spec.get('item_kind') == 'entity_ref' else
                                   [f'fixture-{index}' for index in range(spec.get('min_items', 1))])
            gap = {'schema': 'rtl-analysis-agent/fact-gap-v1', 'gap_kind': 'COMPILER_FACT_ABSENT',
                   'missing_fact_content': ['fixture lacks content Fact'],
                   'affected_fields': sorted(shape['fact_content']['required_fields']),
                   'affected_obligations': [obligation], 'direct_source_rationale': 'fixture source line',
                   'remaining_limitations': ['synthetic fixture only']}
            claim = {'target_kind': 'SUBJECT', 'behavior_key': None, 'target_subject_id': self.module,
                     'claim_key': obligation, 'knowledge_type': shape['knowledge_type'],
                     'text': 'Synthetic ' + obligation, 'structured_schema_id': obligation,
                     'structured_fields': fields, 'applicability': {'instance_id': [self.actual]},
                     'limitations': [gap]}
            claim['ai_id'] = 'ai:' + digest(claim)
            claim.update(fact_support=[], direct_source_evidence=sources)
            fifo_claims.append(claim)
            fifo_coverage.append({'target_kind': 'SUBJECT', 'target_behavior_key': None,
                                  'target_subject_id': self.module, 'obligation_id': obligation,
                                  'profile': self.contract['profile'],
                                  'vocabulary': self.contract['vocabulary'],
                                  'claim_members': [claim['ai_id']],
                                  'claim_set_digest': digest([claim['ai_id']]),
                                  'applicability': {'instance_id': [self.actual]},
                                  'closure_status': 'PARTIAL', 'limitations': ['fixture only'],
                                  'conflicts': [], 'unresolved': []})
        fifo = {'schema': p.PUBLICATION_REVIEW_SCHEMA, 'candidate': self.candidate,
                'contract': self.publication, 'behavior': None,
                'claims': sorted(fifo_claims, key=lambda item: item['ai_id']),
                'coverage': fifo_coverage}
        return sorted([q2, fifo], key=digest)

    def _approval(self):
        for name in ('I', 'R', 'A'):
            shutil.copyfile(self.old_root / f'{name}.json', self.root / f'components/old_q2_{"input" if name == "I" else "review" if name == "R" else "approval"}.json')
        equivalence = {'schema': 'fixture-equivalence', 'same_payload': True}
        artifact(self.root / 'components/q2_equivalence.json', equivalence)
        artifact(self.root / 'components/q4_review_input.json', {
            'schema': 'fixture-q4', 'candidate_id': self.candidate['candidate_id'],
            'scope_id': self.scope['scope_id'], 'profile': self.contract['profile'],
            'profile_sha256': p.Q2_FIFO_V2_PUBLICATION_SHA256,
            'build_sha256': self.scope['build_identity']['build_sha256'],
            'source_revision': self.scope['build_identity']['source_revision'],
            'subject': {'actual_id': self.actual, 'module_id': self.module,
                        'configuration': 'fixture configuration'},
        })
        components = {name: {'path': f'components/{name}.json',
                             'sha256': hashlib.sha256((self.root / f'components/{name}.json').read_bytes()).hexdigest()}
                      for name in ('old_q2_input', 'old_q2_review', 'old_q2_approval',
                                   'q2_equivalence', 'q4_review_input')}
        expected = {'claim_ids': sorted(c['ai_id'] for b in self.batches for c in b['claims']),
                    'coverage_ids': sorted(p._identity('cov', self.scope['scope_id'], coverage)
                                           for b in self.batches for coverage in b['coverage']),
                    'behavior_ids': []}
        old_ai = self.old_batch['claims'][0]['ai_id']
        fifo_ids = sorted(c['ai_id'] for b in self.batches for c in b['claims']
                          if c['structured_schema_id'].startswith('fifo.'))
        dispositions = [{'goal_id': goal, 'status': 'COVERED', 'claim_ids': [old_ai],
                         'reason': 'fixture'} for goal in p.MODULE_Q2_GOALS]
        dispositions += [{'goal_id': f'Q4-F{i:02d}', 'status': 'COVERED', 'claim_ids': fifo_ids,
                          'reason': 'fixture'} for i in range(1, 21)]
        dispositions += [{'goal_id': f'Q4-R{i:02d}', 'status': 'PAGE_ONLY', 'claim_ids': [],
                          'reason': 'fixture page abstraction'} for i in range(1, 34)]
        sources = [self.old_batch['claims'][0]['direct_source_evidence'][0]]
        whole = {'schema': 'rtl-analysis-agent/q2-fifo-complete-input-v1',
                 'candidate': self.candidate, 'contract': self.publication,
                 'profile_sha256': p.Q2_FIFO_V2_PUBLICATION_SHA256,
                 'intake': sources, 'batches': self.batches, 'dispositions': dispositions,
                 'component_evidence': components, 'expected_members': expected}
        input_sha = artifact(self.root / 'I.json', whole)
        batch_hashes = [digest(batch) for batch in self.batches]
        review = {'schema': 'rtl-analysis-agent/q2-fifo-independent-review-v1',
                  'decision': 'VERIFIED', 'input_sha256': input_sha,
                  'batch_sha256': batch_hashes, 'reviewer': 'fixture-reviewer',
                  'generator': 'fixture-writer', 'findings': []}
        review_sha = artifact(self.root / 'R.json', review)
        approval = {'schema': 'rtl-analysis-agent/q2-fifo-approval-v1',
                    'decision': 'APPROVED', 'input_path': 'I.json', 'input_sha256': input_sha,
                    'review_path': 'R.json', 'review_sha256': review_sha,
                    'profile_sha256': p.Q2_FIFO_V2_PUBLICATION_SHA256,
                    'candidate': self.candidate, 'intake_sha256': digest(sources),
                    'batch_sha256': batch_hashes, 'goal_ids': list(p.Q2_FIFO_GOALS),
                    'component_evidence': components, 'expected_members': expected}
        approved = artifact(self.root / 'A.json', approval)
        overlays = []
        for batch in self.batches:
            item = {'status': 'VERIFIED', 'input_sha256': digest(batch),
                    'artifact_sha256': review_sha, 'reviewer': review['reviewer'],
                    'generator': review['generator'], 'locator': 'R.json'}
            item['evidence_id'] = p._identity('ev', 'provenance', 'R.json', None, None,
                                              p._canonical(p._publication_review_attrs(batch, item)))
            overlays.append({**batch, 'review': item})
        return approved, overlays

    def _publish(self, overlay):
        return self.store.publish_reviewed_semantic_overlay(
            overlay, source_root=self.fixture.source_root, review_root=self.root,
            publication_contract=p.Q2_FIFO_V2_PUBLICATION_PATH,
            approval_file='A.json', approved_sha256=self.approved)

    def _rebind_fixture(self, whole):
        """Create internally consistent fake R/A so negatives exercise complete-set gates."""
        whole['batches'].sort(key=digest)
        input_sha = artifact(self.root / 'I.json', whole)
        hashes = [digest(batch) for batch in whole['batches']]
        review = json.loads((self.root / 'R.json').read_text())
        review.update(input_sha256=input_sha, batch_sha256=hashes)
        review_sha = artifact(self.root / 'R.json', review)
        approval = json.loads((self.root / 'A.json').read_text())
        approval.update(input_sha256=input_sha, review_sha256=review_sha, batch_sha256=hashes,
                        expected_members=whole['expected_members'])
        self.approved = artifact(self.root / 'A.json', approval)
        overlays = []
        for batch in whole['batches']:
            meta = {'status': 'VERIFIED', 'input_sha256': digest(batch),
                    'artifact_sha256': review_sha, 'reviewer': review['reviewer'],
                    'generator': review['generator'], 'locator': 'R.json'}
            meta['evidence_id'] = p._identity('ev', 'provenance', 'R.json', None, None,
                                              p._canonical(p._publication_review_attrs(batch, meta)))
            overlays.append({**batch, 'review': meta})
        return overlays

    def test_complete_combination_query_mapping_and_partial_gate(self):
        self._publish(self.overlays[0])
        with self.assertRaises(p.StoreError):
            read = p.PrototypeStore.open(self.fixture.database, readonly=True)
            try:
                read.connection.execute('BEGIN')
                read.audit_real_publication_bundle(self.contract)
            finally:
                read.close()
        self._publish(self.overlays[1])
        self.store.close()
        read = p.PrototypeStore.open(self.fixture.database, readonly=True)
        try:
            read.connection.execute('BEGIN')
            bundle = read.audit_real_publication_bundle(self.contract)
            self.assertEqual(len(bundle['expected_claim_ids']), 6)
            selected = read._read_published_semantic_snapshot({'instance_id': self.actual}, self.contract)
            self.assertEqual(len(selected['claims']), 6)
            self.assertTrue(all(item['closure_status'] == 'PARTIAL' for item in selected['coverage']))
        finally:
            read.close()
        for obligation in ('module-q2.module-role@2', 'fifo.entry-storage@1'):
            read = p.PrototypeStore.open(self.fixture.database, readonly=True)
            try:
                result = query.route_v02(
                    read, database=self.fixture.database, project_root=self.fixture.root,
                    source_root=self.fixture.source_root, target_id=self.module,
                    build_sha256='a' * 64, actual_id=self.actual,
                    obligations=[obligation], request_kind='module-understanding')
                self.assertEqual(result['route'], 'CONTEXT')
                self.assertEqual(result['coverage']['partial_obligations'], [obligation])
                self.assertEqual(len(result['knowledge']), 1)
            finally:
                read.close()
        model = manual.load_formal_q2(self.fixture.database, self.fixture.source_root, combination=True)
        module = next(item for item in model['modules'] if item['qualified_name'] == 'root_a')
        self.assertEqual(len(module['formal_q2']['sections']['fifo']), 5)
        output = self.fixture.root / 'combo-manual'
        manifest = manual.build_site(model, output)
        html = (output / module['output_path']).read_text()
        self.assertIn('id="fifo"', html)
        self.assertIn('<h2>FIFO</h2>', html)
        self.assertNotIn('Fetch FIFO', html)
        self.assertIn('data-ai-id=', html)
        self.assertEqual(module['structured_detail']['semantic'], module['formal_q2'])
        self.assertEqual(manifest['formal_q2']['modules'][self.module], module['formal_q2'])
        self.assertEqual(json.loads((output / 'manifest.json').read_text())['formal_q2']['modules'][self.module],
                         module['formal_q2'])

    def test_merged_disposition_accepts_approved_claim_and_publishes_both_batches(self):
        whole = json.loads((self.root / 'I.json').read_text())
        merged = next(item for item in whole['dispositions'] if item['goal_id'] == 'Q4-F01')
        merged['status'] = 'MERGED'
        self.assertTrue(merged['claim_ids'])
        overlays = self._rebind_fixture(whole)
        for overlay in overlays:
            self._publish(overlay)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM ai_knowledge').fetchone()[0], 6)
        read = p.PrototypeStore.open(self.fixture.database, readonly=True)
        try:
            read.connection.execute('BEGIN')
            bundle = read.audit_real_publication_bundle(self.contract)
            self.assertEqual(next(item for item in bundle['goal_dispositions']
                                  if item['goal_id'] == 'Q4-F01')['status'], 'MERGED')
        finally:
            read.close()

    def test_invalid_disposition_claim_mappings_leave_no_batch(self):
        original = json.loads((self.root / 'I.json').read_text())
        approved_id = next(item for item in original['dispositions'] if item['goal_id'] == 'Q4-F01')['claim_ids'][0]
        other_id = next(item for item in original['dispositions'] if item['goal_id'] == 'Q4-F01')['claim_ids'][-1]
        for name in ('merged-empty', 'merged-unknown', 'merged-duplicate', 'merged-unsorted',
                     'page-only-with-claim', 'deferred-with-claim', 'page-only-no-reason',
                     'deferred-no-reason', 'q2-merged'):
            with self.subTest(name=name):
                whole = copy.deepcopy(original)
                goal = ('Q4-F01' if name.startswith('merged-') else
                        'Q4-R01' if name.startswith(('page-only', 'deferred')) else p.MODULE_Q2_GOALS[0])
                item = next(item for item in whole['dispositions'] if item['goal_id'] == goal)
                if name.startswith('merged-'):
                    item['status'] = 'MERGED'
                    item['claim_ids'] = {'merged-empty': [], 'merged-unknown': ['ai:' + '0' * 64],
                                         'merged-duplicate': [approved_id, approved_id],
                                         'merged-unsorted': sorted([approved_id, other_id], reverse=True)}[name]
                elif name == 'q2-merged':
                    item['status'] = 'MERGED'
                else:
                    item['status'] = 'DEFERRED' if name.startswith('deferred') else 'PAGE_ONLY'
                    if name.endswith('no-reason'):
                        item['reason'] = ''
                    else:
                        item['claim_ids'] = [approved_id]
                overlays = self._rebind_fixture(whole)
                with self.assertRaises(p.StoreError):
                    self._publish(overlays[0])
                self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM ai_knowledge').fetchone()[0], 0)

    def test_wrong_approval_or_changed_q2_limit_rejected_without_rows(self):
        with self.assertRaises(p.StoreError):
            self.store.publish_reviewed_semantic_overlay(
                self.overlays[0], source_root=self.fixture.source_root, review_root=self.root,
                publication_contract=p.Q2_FIFO_V2_PUBLICATION_PATH,
                approval_file='A.json', approved_sha256='0' * 64)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM ai_knowledge').fetchone()[0], 0)

    def test_old_q2_profile_cannot_serve_fifo_obligation(self):
        claim = self.old_batch['claims'][0]
        target = claim.get('target_subject_id') or self.module
        read = p.PrototypeStore.open(self.old.fixture.database, readonly=True)
        try:
            result = query.route_v02(
                read, database=self.old.fixture.database, project_root=self.old.fixture.root,
                source_root=self.old.fixture.source_root, target_id=target,
                build_sha256='a' * 64, actual_id=self.actual,
                obligations=['fifo.entry-storage@1'], request_kind='module-understanding')
            self.assertEqual(result['route'], 'BYPASS')
            self.assertEqual(result['returned_items'], 0)
        finally:
            read.close()

    def test_rebound_incomplete_or_changed_combination_rejected(self):
        original = json.loads((self.root / 'I.json').read_text())
        for name in ('missing-claim', 'extra-claim', 'missing-coverage', 'extra-coverage',
                     'changed-q2-limitation', 'wrong-scope'):
            with self.subTest(name=name):
                whole = copy.deepcopy(original)
                fifo = next(batch for batch in whole['batches'] if
                            batch['claims'][0]['structured_schema_id'].startswith('fifo.'))
                q2 = next(batch for batch in whole['batches'] if
                          batch['claims'][0]['structured_schema_id'].startswith('module-q2.'))
                if name == 'missing-claim':
                    fifo['claims'].pop()
                elif name == 'extra-claim':
                    fifo['claims'].append(copy.deepcopy(fifo['claims'][0]))
                elif name == 'missing-coverage':
                    fifo['coverage'].pop()
                elif name == 'extra-coverage':
                    fifo['coverage'].append(copy.deepcopy(fifo['coverage'][0]))
                elif name == 'changed-q2-limitation':
                    q2['claims'][0]['limitations'][0]['remaining_limitations'].append('changed')
                elif name == 'wrong-scope':
                    whole['candidate']['scope_id'] = 'scope:' + '0' * 64
                overlays = self._rebind_fixture(whole)
                with self.assertRaises(p.StoreError):
                    self._publish(overlays[0])
                self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM ai_knowledge').fetchone()[0], 0)

    def test_cross_package_review_and_same_id_change_rejected(self):
        bad = copy.deepcopy(self.overlays[0])
        bad['review']['artifact_sha256'] = '0' * 64
        with self.assertRaises(p.StoreError):
            self._publish(bad)
        self._publish(self.overlays[0])
        bad = copy.deepcopy(self.overlays[0])
        bad['claims'][0]['text'] += ' changed'
        with self.assertRaises(p.StoreError):
            self._publish(bad)

    def test_recontextualized_q2_change_rejected_even_with_new_identity_and_fake_approval(self):
        whole = json.loads((self.root / 'I.json').read_text())
        q2 = next(batch for batch in whole['batches'] if
                  batch['claims'][0]['structured_schema_id'].startswith('module-q2.'))
        claim = q2['claims'][0]
        old_id = claim['ai_id']
        claim['limitations'][0]['remaining_limitations'].append('changed by synthetic fixture')
        payload = {key: value for key, value in claim.items()
                   if key not in {'ai_id', 'fact_support', 'direct_source_evidence'}}
        claim['ai_id'] = 'ai:' + digest(payload)
        q2['coverage'][0]['claim_members'] = [claim['ai_id']]
        q2['coverage'][0]['claim_set_digest'] = digest([claim['ai_id']])
        for item in whole['dispositions'][:len(p.MODULE_Q2_GOALS)]:
            item['claim_ids'] = [claim['ai_id']]
        whole['expected_members']['claim_ids'].remove(old_id)
        whole['expected_members']['claim_ids'].append(claim['ai_id'])
        whole['expected_members']['claim_ids'].sort()
        whole['expected_members']['coverage_ids'] = [p._identity('cov', self.scope['scope_id'], coverage)
                                                     for batch in whole['batches'] for coverage in batch['coverage']]
        whole['expected_members']['coverage_ids'].sort()
        overlays = self._rebind_fixture(whole)
        with self.assertRaisesRegex(p.StoreError, 'Q2 payload'):
            self._publish(next(item for item in overlays if item['claims'][0]['structured_schema_id'].startswith('module-q2.')))
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM ai_knowledge').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
