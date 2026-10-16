import apt_pkg
import http.client
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from britney2 import Suites, Suite, SuiteClass, SourcePackage, BinaryPackageId, BinaryPackage
from britney2.excuse import Excuse
from britney2.hints import HintParser
from britney2.migrationitem import MigrationItemFactory, MigrationItem
from britney2.policies.policy import AgePolicy, RCBugPolicy, PiupartsPolicy, PolicyVerdict
from britney2.policies.autopkgtest import AutopkgtestPolicy

from . import MockObject, TEST_HINTER, HINTS_ALL, DEFAULT_URGENCY, new_pkg_universe_builder

POLICY_DATA_BASE_DIR = os.path.join(os.path.dirname(__file__), 'policy-test-data')
ARCH = 'amd64'


def initialize_policy(test_name, policy_class, *args, **kwargs):
    test_dir = os.path.join(POLICY_DATA_BASE_DIR, test_name)
    debci_data = os.path.join(test_dir, 'debci.json')
    target = 'testing'
    hints = []
    pkg_universe = None
    inst_tester = None
    if 'hints' in kwargs:
        hints = kwargs['hints']
        del kwargs['hints']
    if 'pkg_universe' in kwargs:
        pkg_universe = kwargs['pkg_universe']
        del kwargs['pkg_universe']
    if 'inst_tester' in kwargs:
        inst_tester = kwargs['inst_tester']
        del kwargs['inst_tester']
    options = MockObject(
        state_dir=test_dir,
        verbose=0,
        default_urgency=DEFAULT_URGENCY,
        dry_run=False,
        adt_shared_results_cache=False,
        series=target,
        adt_arches=ARCH,
        architectures=ARCH,
        adt_swift_url='file://' + debci_data,
        adt_ci_url='',
        adt_swift_user='',
        adt_swift_pass='',
        adt_swift_tenant='',
        adt_swift_auth_url='',
        adt_private_shared=[],
        adt_private_url='',
        adt_success_bounty=3,
        adt_regression_penalty=False,
        adt_retry_url_mech='run_id',
        fake_runtime=774000,
        has_arch_all_buildds=True,
        **kwargs)
    suite_info = Suites(
        Suite(SuiteClass.TARGET_SUITE, target, os.path.join(test_dir, target), ''),
        [Suite(SuiteClass.PRIMARY_SOURCE_SUITE, 'unstable', os.path.join(test_dir, 'unstable'), '')],
    )
    mi_factory = MigrationItemFactory(suite_info)
    policy = policy_class(options, suite_info, *args)
    fake_britney = MockObject(log=lambda x, y='I': None)
    hint_parser = HintParser(mi_factory)
    if pkg_universe and inst_tester:
        build_sources_from_universe_and_inst_tester(policy, pkg_universe, inst_tester)
    policy.initialise(fake_britney)
    if inst_tester:
        policy.britney._inst_tester = inst_tester
    if pkg_universe:
        policy.britney.pkg_universe = pkg_universe
    policy.register_hints(hint_parser)
    hint_parser.parse_hints(TEST_HINTER, HINTS_ALL, 'test-%s' % test_name, hints)
    policy.hints = hint_parser.hints
    return policy


def fake_clock(testcase):
    '''Make time.sleep() advance time.monotonic() instead of waiting

    The current time is testcase.now, the delays slept are in testcase.sleeps.
    '''
    testcase.now = 0
    testcase.sleeps = []

    def sleep(delay):
        testcase.sleeps.append(delay)
        testcase.now += delay

    for name, fake in (('monotonic', lambda: testcase.now), ('sleep', sleep)):
        patcher = patch('time.' + name, side_effect=fake)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def http_response(code, content=b'', read_error=None):
    '''Mock what urlopen() returns'''
    response = MagicMock()
    response.__enter__.return_value = response
    response.getcode.return_value = code
    response.read.return_value = content
    response.read.side_effect = read_error
    return response


def create_excuse(name, pkgs):
    excuse = Excuse(name)
    for pkg_id in pkgs:
        excuse.add_package(pkg_id)
    return excuse


def create_source_package(name, version, section='devel', binaries=None):
    if binaries is None:
        binaries = set()
    return SourcePackage(name, version, section, binaries, 'Random tester', False, None, None, ['autopkgtest'], [], 'fake-component')


def create_bin_package(pkg_id, source_name=None, depends=None, conflicts=None):
    name = pkg_id.package_name
    version = pkg_id.version
    source_version = version
    if source_name is None:
        source_name = name
    return BinaryPackage(
        version,
        'main',
        source_name,
        source_version,
        ARCH,
        None,
        depends,
        conflicts,
        None,
        False,
        pkg_id,
        [],
        'fake-component'
        )


def create_policy_objects(source_name, target_version='1.0', source_version='2.0', pkgs={}):
    return (
        create_source_package(source_name, target_version),
        create_source_package(source_name, source_version),
        create_excuse(source_name, pkgs),
    )


def apply_src_policy(policy, expected_verdict, src_name, *, suite='unstable', target_version='1.0', source_version='2.0'):
    suite_info = policy.suite_info
    if src_name in suite_info[suite].sources:
        src_u = suite_info[suite].sources[src_name]
        src_t = suite_info.target_suite.sources.get(src_name)
        _, _, excuse = create_policy_objects(src_name, pkgs=src_u.binaries)
    else:
        src_t, src_u, excuse = create_policy_objects(src_name, target_version, source_version)
    suite_info.target_suite.sources[src_name] = src_t
    suite_info[suite].sources[src_name] = src_u
    factory = MigrationItemFactory(suite_info)
    item = factory.parse_item(src_name, versioned=False, auto_correct=False)
    pinfo = {}
    verdict = policy.apply_src_policy_impl(pinfo, item, src_t, src_u, excuse)
    assert verdict == expected_verdict
    return pinfo


def build_sources_from_universe_and_inst_tester(policy, pkg_universe, inst_tester, suite='unstable'):
    suite_info = policy.suite_info
    src_universe = {}
    bin_universe = {}
    src_source = {}
    binaries_t = {}
    binaries_s = {}
    for pkg_id in pkg_universe:
        pkg_name = pkg_id.package_name
        src_universe[pkg_id] = create_source_package(pkg_id.package_name, pkg_id.version, binaries={pkg_id})
        bin_universe[pkg_id] = create_bin_package(pkg_id)
        if inst_tester.is_pkg_in_the_suite(pkg_id):
            if pkg_name in suite_info.target_suite.sources:
                # sanity check, this shouldn't happen
                raise(KeyError)
            suite_info.target_suite.sources[pkg_name] = src_universe[pkg_id]
            binaries_t.setdefault(ARCH, {}).setdefault(pkg_name, bin_universe[pkg_id])
        # We need to find the highest version of a package to add it to the
        # sources of the source suite
        if pkg_name not in src_source or \
           apt_pkg.version_compare(src_source[pkg_name].version, pkg_id.version) < 0:
            src_source[pkg_name] = pkg_id
    suite_info.target_suite.binaries = binaries_t
    for pkg_id in src_source.values():
        pkg_name = pkg_id.package_name
        suite_info[suite].sources[pkg_name] = src_universe[pkg_id]
        binaries_s.setdefault(ARCH, {}).setdefault(pkg_name, bin_universe[pkg_id])
    suite_info[suite].binaries = binaries_s


class TestRCBugsPolicy(unittest.TestCase):

    def test_no_bugs(self):
        src_name = 'has-no-bugs'
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == set()
        assert set(bug_policy_info['unique-target-bugs']) == set()
        assert set(bug_policy_info['shared-bugs']) == set()

    def test_regression(self):
        src_name = 'regression'
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == {'123458'}
        assert set(bug_policy_info['unique-target-bugs']) == set()
        assert set(bug_policy_info['shared-bugs']) == set()

    def test_regression_but_fixes_more_bugs(self):
        src_name = 'regression-but-fixes-more-bugs'
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == {'100003'}
        assert set(bug_policy_info['unique-target-bugs']) == {'100001', '100002'}
        assert set(bug_policy_info['shared-bugs']) == {'100000'}

    def test_not_a_regression(self):
        src_name = 'not-a-regression'
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == set()
        assert set(bug_policy_info['unique-target-bugs']) == set()
        assert set(bug_policy_info['shared-bugs']) == {'123457'}

    def test_improvement(self):
        src_name = 'fixes-bug'
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == set()
        assert set(bug_policy_info['unique-target-bugs']) == {'123456'}
        assert set(bug_policy_info['shared-bugs']) == set()

    def test_regression_with_hint(self):
        src_name = 'regression'
        hints = ['ignore-rc-bugs 123458 regression/2.0']
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy, hints=hints)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.PASS_HINTED, src_name)
        assert set(bug_policy_info['ignored-bugs']['bugs']) == {'123458'}
        assert bug_policy_info['ignored-bugs']['issued-by'] == TEST_HINTER
        assert set(bug_policy_info['unique-source-bugs']) == set()
        assert set(bug_policy_info['unique-target-bugs']) == set()
        assert set(bug_policy_info['shared-bugs']) == set()

    def test_regression_but_fixes_more_bugs_bad_hint(self):
        src_name = 'regression-but-fixes-more-bugs'
        hints = ['ignore-rc-bugs 100000 regression-but-fixes-more-bugs/2.0']
        policy = initialize_policy('rc-bugs/basic', RCBugPolicy, hints=hints)
        bug_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert set(bug_policy_info['unique-source-bugs']) == {'100003'}
        assert set(bug_policy_info['unique-target-bugs']) == {'100001', '100002'}
        assert set(bug_policy_info['ignored-bugs']['bugs']) == {'100000'}
        assert bug_policy_info['ignored-bugs']['issued-by'] == TEST_HINTER
        assert set(bug_policy_info['shared-bugs']) == set()


class TestAgePolicy(unittest.TestCase):

    DEFAULT_MIN_DAYS = {
        'emergency': 0,
        'critical': 0,
        'high': 2,
        'medium': 5,
        'low': 10,
    }

    def test_missing_age_file(self):
        age_file = os.path.join(POLICY_DATA_BASE_DIR, 'age', 'missing-age-file', 'age-policy-dates')
        assert not os.path.exists(age_file)

        try:
            src_name = 'unlisted-source-package'
            policy = initialize_policy('age/missing-age-file', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS)
            age_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
            assert os.path.exists(age_file)
            assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
            assert age_policy_info['current-age'] == 0
        finally:
            if os.path.exists(age_file):
                os.unlink(age_file)

    def test_age_new(self):
        src_name = 'unlisted-source-package'
        policy = initialize_policy('age/basic', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS)
        age_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
        assert age_policy_info['current-age'] == 0

    def test_age_urgented(self):
        src_name = 'unlisted-source-package'
        policy = initialize_policy('age/basic', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS,
                                   hints=['urgent unlisted-source-package/2.0'])
        age_policy_info = apply_src_policy(policy, PolicyVerdict.PASS_HINTED, src_name)
        assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
        assert age_policy_info['current-age'] == 0
        assert age_policy_info['age-requirement-reduced']['new-requirement'] == 0
        assert age_policy_info['age-requirement-reduced']['changed-by'] == TEST_HINTER

    def test_age_old_version_aged(self):
        src_name = 'out-of-date-version'
        policy = initialize_policy('age/basic', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS)
        age_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
        assert age_policy_info['current-age'] == 0

    def test_age_almost_aged(self):
        src_name = 'almost-aged-properly'
        policy = initialize_policy('age/basic', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS)
        age_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
        assert age_policy_info['current-age'] == 4

    def test_age_aged_properly(self):
        src_name = 'aged-properly'
        policy = initialize_policy('age/basic', AgePolicy, TestAgePolicy.DEFAULT_MIN_DAYS)
        age_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert age_policy_info['age-requirement'] == TestAgePolicy.DEFAULT_MIN_DAYS[DEFAULT_URGENCY]
        assert age_policy_info['current-age'] == 5


class TestPiupartsPolicy(unittest.TestCase):

    def test_passes(self):
        src_name = 'pass'
        policy = initialize_policy('piuparts/basic', PiupartsPolicy)
        piu_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert piu_policy_info['test-results'] == 'pass'
        assert piu_policy_info['piuparts-test-url'] == 'https://piuparts.debian.org/sid/source/p/pass.html'

    def test_regression(self):
        src_name = 'regression'
        policy = initialize_policy('piuparts/basic', PiupartsPolicy)
        piu_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert piu_policy_info['test-results'] == 'regression'
        assert piu_policy_info['piuparts-test-url'] == 'https://piuparts.debian.org/sid/source/r/regression.html'

    def test_regression_hinted(self):
        src_name = 'regression'
        hints = ['ignore-piuparts regression/2.0']
        policy = initialize_policy('piuparts/basic', PiupartsPolicy, hints=hints)
        piu_policy_info = apply_src_policy(policy, PolicyVerdict.PASS_HINTED, src_name)
        assert piu_policy_info['test-results'] == 'regression'
        assert piu_policy_info['piuparts-test-url'] == 'https://piuparts.debian.org/sid/source/r/regression.html'
        assert piu_policy_info['ignored-piuparts']['issued-by'] == TEST_HINTER

    def test_not_tested_yet(self):
        src_name = 'not-tested-yet'
        policy = initialize_policy('piuparts/basic', PiupartsPolicy)
        piu_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert piu_policy_info['test-results'] == 'waiting-for-test-results'
        assert piu_policy_info['piuparts-test-url'] == 'https://piuparts.debian.org/sid/source/n/not-tested-yet.html'

    def test_failed_not_regression(self):
        src_name = 'failed-not-regression'
        policy = initialize_policy('piuparts/basic', PiupartsPolicy)
        piu_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert piu_policy_info['test-results'] == 'failed'
        assert piu_policy_info['piuparts-test-url'] == 'https://piuparts.debian.org/sid/source/f/failed-not-regression.html'


pkg1 = BinaryPackageId('pkg', '1.0', ARCH)
pkg2 = BinaryPackageId('pkg', '2.0', ARCH)
inter = BinaryPackageId('inter', '1.0', ARCH)
broken1 = BinaryPackageId('broken', '1.0', ARCH)
broken2 = BinaryPackageId('broken', '2.0', ARCH)
dummy = BinaryPackageId('dummy', '1', ARCH)

builder = new_pkg_universe_builder()
builder.new_package(pkg1).in_testing()
builder.new_package(pkg2).not_in_testing()
simple_universe, simple_inst_tester = builder.build()

builder_breaks = new_pkg_universe_builder()
builder_breaks.new_package(broken1).in_testing()
builder_breaks.new_package(broken2).not_in_testing()
builder_breaks.new_package(inter).in_testing().depends_on_any_of(broken1, broken2)
builder_breaks.new_package(pkg1).depends_on(inter).in_testing()
builder_breaks.new_package(pkg2).depends_on(inter).not_in_testing().conflicts_with(broken1)
breaks_universe, breaks_inst_tester = builder_breaks.build()


class TestAutopkgtestPolicy(unittest.TestCase):
    import apt_pkg
    apt_pkg.init()

    def read_amqp(self):
        with open(self.amqp.replace('file://', ''), 'r+') as f:
            amqp = f.read()
        return amqp

    def setUp(self):
        self.amqp = 'file://' + tempfile.NamedTemporaryFile(mode='w', delete=False).name

    def tearDown(self):
        os.unlink(self.amqp.replace('file://', ''))

    def test_pass_to_pass(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-pass',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'PASS'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert len(amqp) == 0

    def test_pass_to_fail(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-fail',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than=1,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'REGRESSION'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_pass_to_fail_no_retrigger(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-fail',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than='',
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'REGRESSION'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert len(amqp) == 0

    def test_pass_to_neutral(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-neutral',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'NEUTRAL'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert len(amqp) == 0

    def test_new(self):
        src_name = 'pkg'
        builder_new = new_pkg_universe_builder()
        builder_new.new_package(pkg2).not_in_testing()
        builder_new.new_package(dummy).in_testing()
        new_universe, new_inst_tester = builder_new.build()
        policy = initialize_policy(
            'autopkgtest/new',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=new_universe,
            inst_tester=new_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING-ALWAYSFAIL'
        assert autopkgtest_policy_info[src_name][ARCH][1] == 'status/pending'
        assert autopkgtest_policy_info[src_name][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_pass_to_new(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-new',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING'
        assert autopkgtest_policy_info[src_name][ARCH][1] == 'status/pending'
        assert autopkgtest_policy_info[src_name][ARCH][2] == 'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_fail_to_new(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/fail-to-new',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING-ALWAYSFAIL'
        assert autopkgtest_policy_info[src_name][ARCH][1] == 'status/pending'
        assert autopkgtest_policy_info[src_name][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_neutral_to_new(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/neutral-to-new',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING'
        assert autopkgtest_policy_info[src_name][ARCH][1] == 'status/pending'
        assert autopkgtest_policy_info[src_name][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_neutral_to_fail(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/neutral-to-fail',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than=1,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'REGRESSION'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0"]}'

    def test_neutral_to_fail_pending_retest(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/neutral-to-fail-pending-retest',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than=1,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_PERMANENTLY, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'REGRESSION'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert len(amqp) == 0

    def test_pass_to_new_with_breaks(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-new-with-breaks',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=breaks_universe,
            inst_tester=breaks_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING'
        assert autopkgtest_policy_info[src_name][ARCH][1] == 'status/pending'
        assert autopkgtest_policy_info[src_name][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:' + src_name + ' {"triggers": ["' + src_name + '/2.0 broken/2.0"]}'

    def test_remember_old_test_result(self):
        src_name = 'broken'
        policy = initialize_policy(
            'autopkgtest/remember-old-test-result',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            pkg_universe=breaks_universe,
            inst_tester=breaks_inst_tester,
            adt_baseline='reference')
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'PASS'
        amqp = self.read_amqp()
        assert amqp[0:-1] == 'debci-testing-amd64:inter {"triggers": ["' + src_name + '/2.0"]}'

    def test_fail_to_fail(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/fail-to-fail',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than=1,
            adt_baseline='reference',
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'ALWAYSFAIL'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert len(amqp) == 0

    def test_reference_too_old(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/fail-to-fail',
            AutopkgtestPolicy,
            adt_amqp=self.amqp,
            adt_retry_older_than=1,
            adt_baseline='reference',
            adt_reference_max_age=1,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.PASS, src_name)
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][0] == 'ALWAYSFAIL'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][1] == \
            'data/autopkgtest/testing/amd64/' + src_name[0] + '/' + src_name + '/2/log.gz'
        assert autopkgtest_policy_info[src_name + '/2.0'][ARCH][2] == \
            'packages/' + src_name[0] + '/' + src_name + '/testing/amd64'
        amqp = self.read_amqp()
        assert 'migration-reference/0' in amqp


class TestAutopkgtestAMQP(unittest.TestCase):

    def setUp(self):
        # amqplib is not necessarily installed, and we don't want to talk to a
        # real AMQP server anyway
        self.amqp = MagicMock()
        self.amqp.Connection.side_effect = lambda *args, **kwargs: MagicMock()
        modules = patch.dict(sys.modules, {'amqplib': MagicMock(client_0_8=self.amqp),
                                           'amqplib.client_0_8': self.amqp})
        modules.start()
        self.addCleanup(modules.stop)
        fake_clock(self)
        self.policy = initialize_policy('autopkgtest/pass-to-pass', AutopkgtestPolicy,
                                        adt_amqp='amqp://user:pass@localhost')
        self.con = self.policy.amqp_con

    def test_reconnect_on_connection_reset(self):
        self.con.channel.return_value.basic_publish.side_effect = \
            ConnectionResetError(104, 'Connection reset by peer')
        dead_sock = self.con.transport.sock

        assert self.policy.send_test_request('pkg', ARCH, ['pkg/2.0'])

        dead_sock.close.assert_called_once_with()
        assert self.con.transport.sock is None
        assert self.amqp.Connection.call_count == 2
        assert self.amqp.Connection.call_args[1]['connect_timeout'] == 30
        assert self.policy.amqp_con is not self.con
        self.policy.amqp_con.transport.sock.settimeout.assert_called_once_with(30)
        new_channel = self.policy.amqp_con.channel.return_value
        new_channel.basic_publish.assert_called_once_with(self.amqp.Message.return_value,
                                                          routing_key='debci-testing-' + ARCH)
        assert self.amqp.Message.call_args[0][0].startswith('pkg\n{"triggers": ["pkg/2.0"]')
        assert self.sleeps == []

    def test_retry_reconnecting(self):
        self.con.channel.return_value.basic_publish.side_effect = \
            ConnectionResetError(104, 'Connection reset by peer')
        flaky_con = MagicMock()
        flaky_con.channel.return_value.basic_publish.side_effect = BrokenPipeError(32, 'Broken pipe')
        flaky_sock = flaky_con.transport.sock
        good_con = MagicMock()
        self.amqp.Connection.side_effect = [ConnectionRefusedError(111, 'Connection refused'),
                                            flaky_con, good_con]

        assert self.policy.send_test_request('pkg', ARCH, ['pkg/2.0'])

        assert self.sleeps == [5, 10]
        flaky_sock.close.assert_called_once_with()
        assert flaky_con.transport.sock is None
        assert self.policy.amqp_con is good_con
        good_con.channel.return_value.basic_publish.assert_called_once_with(
            self.amqp.Message.return_value, routing_key='debci-testing-' + ARCH)

    def test_give_up_when_reconnect_fails(self):
        self.con.channel.return_value.basic_publish.side_effect = BrokenPipeError(32, 'Broken pipe')
        # what amqplib raises when the connection is refused
        self.amqp.Connection.side_effect = UnboundLocalError("local variable 'msg' referenced before assignment")

        assert not self.policy.send_test_request('pkg', ARCH, ['pkg/2.0'])
        # we tried to reconnect for up to 3 minutes
        assert self.sleeps == [5, 10, 20, 40, 60]
        assert self.amqp.Connection.call_count == 1 + 6
        assert not self.policy.send_test_request('pkg', ARCH, ['pkg/2.0'])

        # we didn't try to reconnect again
        assert self.amqp.Connection.call_count == 1 + 6
        assert self.con.channel.return_value.basic_publish.call_count == 1

    def test_retry_connecting(self):
        self.amqp.Connection.side_effect = [UnboundLocalError("local variable 'msg' referenced before assignment"),
                                            MagicMock()]

        policy = initialize_policy('autopkgtest/pass-to-pass', AutopkgtestPolicy,
                                   adt_amqp=self.policy.options.adt_amqp)

        assert policy.amqp_channel is policy.amqp_con.channel.return_value
        assert self.sleeps == [5]

    def test_unsent_request_holds_back(self):
        src_name = 'pkg'
        policy = initialize_policy(
            'autopkgtest/pass-to-new',
            AutopkgtestPolicy,
            adt_amqp=self.policy.options.adt_amqp,
            adt_show_irrelevant=False,
            pkg_universe=simple_universe,
            inst_tester=simple_inst_tester)
        # ignore the pending tests left behind by other tests
        policy.pending_tests = {}
        con = policy.amqp_con
        con.channel.return_value.basic_publish.side_effect = BrokenPipeError(32, 'Broken pipe')
        self.amqp.Connection.side_effect = UnboundLocalError("local variable 'msg' referenced before assignment")

        autopkgtest_policy_info = apply_src_policy(policy, PolicyVerdict.REJECTED_TEMPORARILY, src_name)

        con.channel.return_value.basic_publish.assert_called_once()
        assert autopkgtest_policy_info[src_name][ARCH][0] == 'RUNNING'
        # not pending, so that the next run requests it
        assert ARCH not in policy.pending_tests.get(src_name + '/2.0', {}).get(src_name, [])


class TestAutopkgtestDownloadRetry(unittest.TestCase):

    URL = 'https://objectstorage.example.com/results.tar'

    def setUp(self):
        fake_clock(self)
        self.policy = initialize_policy('autopkgtest/pass-to-pass', AutopkgtestPolicy,
                                        adt_amqp='file:///dev/null')
        urlopen = patch('britney2.policies.autopkgtest.urlopen')
        self.urlopen = urlopen.start()
        self.addCleanup(urlopen.stop)

    def test_retry_transient_errors(self):
        self.urlopen.side_effect = [
            URLError(ConnectionRefusedError(111, 'Connection refused')),
            HTTPError(self.URL, 503, 'Service Unavailable', {}, None),
            http_response(200, read_error=http.client.IncompleteRead(b'partial')),
            http_response(200, b'results'),
        ]

        assert self.policy.download_retry(self.URL) == (200, b'results')
        assert self.sleeps == [5, 10, 20]
        self.urlopen.assert_called_with(self.URL, timeout=30)

    def test_no_retry_on_permanent_errors(self):
        for error in (HTTPError(self.URL, 404, 'Not Found', {}, None),
                      URLError(FileNotFoundError(2, 'No such file or directory'))):
            self.urlopen.side_effect = error
            with self.assertRaises(URLError) as cm:
                self.policy.download_retry(self.URL)
            assert cm.exception is error
        assert self.urlopen.call_count == 2
        assert self.sleeps == []

    def test_give_up_after_3_minutes(self):
        def time_out(url, timeout):
            self.now += timeout
            raise socket.timeout('timed out')
        self.urlopen.side_effect = time_out

        with self.assertRaises(IOError):
            self.policy.download_retry(self.URL)
        assert self.urlopen.call_count == 4
        assert self.sleeps == [5, 10, 20]
        assert self.now <= 3 * 60

    def test_broken_responses_raised_as_ioerror(self):
        # the only errors the fetch_*() callers handle
        self.urlopen.side_effect = http.client.BadStatusLine('')

        with self.assertRaises(IOError) as cm:
            self.policy.download_retry(self.URL)
        assert isinstance(cm.exception.reason, http.client.BadStatusLine)
        assert self.sleeps == [5, 10, 20, 40, 60]


def result_tar(testpkg_version, triggers):
    '''Return the contents of a result.tar of a passed test'''
    tar_bytes = io.BytesIO()
    with tarfile.open(None, 'w', tar_bytes) as tar:
        for name, contents in (('exitcode', '0'),
                               ('testpkg-version', testpkg_version),
                               ('testinfo.json', json.dumps({'custom_environment': ['ADT_TEST_TRIGGERS=' + triggers]}))):
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            tar.addfile(info, io.BytesIO(contents.encode()))
    return tar_bytes.getvalue()


class TestAutopkgtestFetchFailures(unittest.TestCase):

    SWIFT_URL = 'https://objectstorage.example.com'

    def setUp(self):
        fake_clock(self)
        self.policy = initialize_policy('autopkgtest/pass-to-pass', AutopkgtestPolicy,
                                        adt_amqp='file:///dev/null')
        self.policy.options.adt_swift_url = self.SWIFT_URL
        urlopen = patch('britney2.policies.autopkgtest.urlopen')
        self.urlopen = urlopen.start()
        self.addCleanup(urlopen.stop)
        # results are fetched once per src/arch and process, not per policy
        done = patch.object(AutopkgtestPolicy.fetch_swift_results, '_done', set())
        done.start()
        self.addCleanup(done.stop)

    def test_no_request_when_fetching_fails(self):
        self.urlopen.side_effect = URLError(ConnectionRefusedError(111, 'Connection refused'))

        with patch.object(self.policy, 'send_test_request') as send_test_request:
            self.policy.pkg_test_request('foo', ARCH, ['bar/1'])

        # the results we could not fetch might already cover it
        send_test_request.assert_not_called()
        assert ('bar/1', 'foo', ARCH) in self.policy.unsent_requests
        with tempfile.TemporaryDirectory() as state_dir:
            for name in ('results_cache_file', 'pending_tests_file', 'fetch_failures_file'):
                setattr(self.policy, name, os.path.join(state_dir, name))
            self.policy.save_state(None)
            with open(self.policy.fetch_failures_file) as f:
                assert list(json.load(f)) == ['foo/' + ARCH]

    def test_stop_at_failed_result(self):
        runs = ['testing/amd64/f/foo/20261007_00000%i_abcde@' % i for i in (1, 2, 3)]
        self.urlopen.side_effect = [
            http_response(200, '\n'.join(runs).encode()),
            http_response(200, result_tar('foo 1', 'bar/1')),
            HTTPError(self.SWIFT_URL, 403, 'Forbidden', {}, None),
        ]

        self.policy.fetch_swift_results(self.SWIFT_URL, 'foo', ARCH)

        # the next run lists the results after the last one we fetched
        assert self.urlopen.call_count == 3
        assert self.policy.latest_run_for_package('foo', ARCH) == '20261007_000001_abcde@'
        assert list(self.policy.fetch_failures) == ['foo/' + ARCH]

    @patch('britney2.policies.autopkgtest.MAX_CONSECUTIVE_FETCH_FAILURES', 2)
    def test_stop_fetching_when_it_keeps_failing(self):
        self.urlopen.side_effect = [HTTPError(self.SWIFT_URL, 403, 'Forbidden', {}, None),
                                    http_response(204),
                                    HTTPError(self.SWIFT_URL, 403, 'Forbidden', {}, None),
                                    HTTPError(self.SWIFT_URL, 403, 'Forbidden', {}, None)]

        for src in ('a', 'b', 'c'):
            self.policy.fetch_swift_results(self.SWIFT_URL, src, ARCH)

        assert not self.policy.max_fetch_failures_hit
        self.policy.fetch_swift_results(self.SWIFT_URL, 'd', ARCH)
        assert self.policy.max_fetch_failures_hit
        self.policy.fetch_swift_results(self.SWIFT_URL, 'e', ARCH)

        # b succeeded, so it took c and d failing in a row to give up on e
        assert self.urlopen.call_count == 4
        assert sorted(self.policy.fetch_failures) == [src + '/' + ARCH for src in ('a', 'c', 'd', 'e')]
        assert self.policy.fetch_failures['e/' + ARCH] == 'not fetched, as fetching results kept failing'


if __name__ == '__main__':
    unittest.main()
