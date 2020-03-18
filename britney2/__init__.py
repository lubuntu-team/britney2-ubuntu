import logging
from collections import namedtuple
from enum import Enum, unique


class DependencyType(Enum):
    DEPENDS = ('Depends', 'depends', 'dependency')
    # BUILD_DEPENDS includes BUILD_DEPENDS_ARCH
    BUILD_DEPENDS = ('Build-Depends(-Arch)', 'build-depends', 'build-dependency')
    BUILD_DEPENDS_INDEP = ('Build-Depends-Indep', 'build-depends-indep', 'build-dependency (indep)')
    BUILT_USING = ('Built-Using', 'built-using', 'built-using')
    # Pseudo dependency where Breaks/Conflicts effectively become a inverted dependency.  E.g.
    # p Depends on q plus q/2 breaks p/1 implies that p/2 must migrate before q/2 can migrate
    # (or they go at the same time).
    # - can also happen with version ranges
    IMPLICIT_DEPENDENCY = ('Implicit dependency', 'implicit-dependency', 'implicit-dependency')

    def __str__(self):
        return self.value[0]

    def get_reason(self):
        return self.value[1]

    def get_description(self):
        return self.value[2]


@unique
class SuiteClass(Enum):

    TARGET_SUITE = (False, False)
    PRIMARY_SOURCE_SUITE = (True, True)
    ADDITIONAL_SOURCE_SUITE = (True, False)

    @property
    def is_source(self):
        return self.value[0]

    @property
    def is_target(self):
        return not self.is_source

    @property
    def is_primary_source(self):
        return self is SuiteClass.PRIMARY_SOURCE_SUITE

    @property
    def is_additional_source(self):
        return self is SuiteClass.ADDITIONAL_SOURCE_SUITE


class Suite(object):

    def __init__(self, suite_class, name, path, suite_short_name=None):
        self.suite_class = suite_class
        self.name = name
        self.path = path
        self.suite_short_name = suite_short_name if suite_short_name else ''
        self.sources = {}
        self._binaries = {}
        self.provides_table = {}
        self._all_binaries_in_suite = None

    @property
    def excuses_suffix(self):
        return self.suite_short_name

    @property
    def binaries(self):
        # TODO some callers modify this structure, which doesn't invalidate
        # the self._all_binaries_in_suite cache
        return self._binaries

    @binaries.setter
    def binaries(self, binaries):
        self._binaries = binaries
        self._all_binaries_in_suite = None

    @property
    def all_binaries_in_suite(self):
        if not self._all_binaries_in_suite:
            self._all_binaries_in_suite = \
                {x.pkg_id: x for a in self._binaries for x in self._binaries[a].values()}
        return self._all_binaries_in_suite

    def any_of_these_are_in_the_suite(self, pkgs):
        """Test if at least one package of a given set is in the suite

        :param pkgs: A set of BinaryPackageId
        :return: True if any of the packages in pkgs are currently in the suite
        """
        return not self.all_binaries_in_suite.keys().isdisjoint(pkgs)

    def is_pkg_in_the_suite(self, pkg_id):
        """Test if the package of is in testing

        :param pkg_id: A BinaryPackageId
        :return: True if the pkg is currently in the suite
        """
        return pkg_id in self.all_binaries_in_suite

    def which_of_these_are_in_the_suite(self, pkgs):
        """Iterate over all packages that are in the suite

        :param pkgs: An iterable of package ids
        :return: An iterable of package ids that are in the suite
        """
        yield from (x for x in pkgs if x in self.all_binaries_in_suite)

    def is_cruft(self, pkg):
        """Check if the package is cruft in the suite

        :param pkg: BinaryPackage to check
                    Note that this package is assumed to be in the suite
        """
        newest_src_in_suite = self.sources[pkg.source]
        return pkg.source_version != newest_src_in_suite.version


class TargetSuite(Suite):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inst_tester = None
        logger_name = ".".join((self.__class__.__module__, self.__class__.__name__))
        self._logger = logging.getLogger(logger_name)

    def is_installable(self, pkg_id):
        """Determine whether the given package can be installed in the suite

        :param pkg_id: A BinaryPackageId
        :return: True if the pkg is currently installable in the suite
        """
        return self.inst_tester.is_installable(pkg_id)

    def add_binary(self, pkg_id):
        """Add a binary package to the suite

        If the package is not known, this method will throw an
        KeyError.

        :param pkg_id The id of the package
        """

        # TODO The calling code currently manually updates the contents of
        # target_suite.binaries when this is called. It would probably make
        # more sense to do that here instead
        self.inst_tester.add_binary(pkg_id)
        self._all_binaries_in_suite = None

    def remove_binary(self, pkg_id):
        """Remove a binary from the suite

        :param pkg_id The id of the package
        If the package is not known, this method will throw an
        KeyError.
        """

        # TODO The calling code currently manually updates the contents of
        # target_suite.binaries when this is called. It would probably make
        # more sense to do that here instead
        self.inst_tester.remove_binary(pkg_id)
        self._all_binaries_in_suite = None

    def check_suite_source_pkg_consistency(self, comment):
        sources_t = self.sources
        binaries_t = self.binaries
        logger = self._logger
        issues_found = False

        logger.info("check_target_suite_source_pkg_consistency %s", comment)

        for arch in binaries_t:
            for pkg_name in binaries_t[arch]:
                pkg = binaries_t[arch][pkg_name]
                src = pkg.source

                if src not in sources_t:  # pragma: no cover
                    issues_found = True
                    logger.error("inconsistency found (%s): src %s not in target, target has pkg %s with source %s" % (
                        comment, src, pkg_name, src))

        for src in sources_t:
            source_data = sources_t[src]
            for pkg_id in source_data.binaries:
                binary, _, parch = pkg_id
                if binary not in binaries_t[parch]:  # pragma: no cover
                    issues_found = True
                    logger.error("inconsistency found (%s): binary %s from source %s not in binaries_t[%s]" % (
                        comment, binary, src, parch))

        if issues_found:  # pragma: no cover
            raise AssertionError("inconsistencies found in target suite")


class Suites(object):

    def __init__(self, target_suite, source_suites):
        self._suites = {}
        self._by_name_or_alias = {}
        self.target_suite = target_suite
        self.source_suites = source_suites
        self._suites[target_suite.name] = target_suite
        self._by_name_or_alias[target_suite.name] = target_suite
        if target_suite.suite_short_name:
            self._by_name_or_alias[target_suite.suite_short_name] = target_suite
        for suite in source_suites:
            self._suites[suite.name] = suite
            self._by_name_or_alias[suite.name] = suite
            if suite.suite_short_name:
                self._by_name_or_alias[suite.suite_short_name] = suite

    @property
    def primary_source_suite(self):
        return self.source_suites[0]

    @property
    def by_name_or_alias(self):
        return self._by_name_or_alias

    @property
    def additional_source_suites(self):
        return self.source_suites[1:]

    def __getitem__(self, item):
        return self._suites[item]

    def __len__(self):
        return len(self.source_suites) + 1

    def __contains__(self, item):
        return item in self._suites

    def __iter__(self):
        # Sources first (as we will rely on this for loading data in the old live-data tests)
        yield from self.source_suites
        yield self.target_suite


class SourcePackage(object):

    __slots__ = ['source', 'version', 'section', 'binaries', 'maintainer', 'is_fakesrc', 'build_deps_arch',
                 'build_deps_indep', 'testsuite', 'testsuite_triggers']

    def __init__(self, source, version, section, binaries, maintainer, is_fakesrc, build_deps_arch,
                 build_deps_indep, testsuite, testsuite_triggers):
        self.source = source
        self.version = version
        self.section = section
        self.binaries = binaries
        self.maintainer = maintainer
        self.is_fakesrc = is_fakesrc
        self.build_deps_arch = build_deps_arch
        self.build_deps_indep = build_deps_indep
        self.testsuite = testsuite
        self.testsuite_triggers = testsuite_triggers

    def __getitem__(self, item):
        return getattr(self, self.__slots__[item])


class PackageId(namedtuple(
    'PackageId',
        [
            'package_name',
            'version',
            'architecture',
        ])):
    """Represent a source or binary package"""

    def __init__(self, package_name, version, architecture):
        assert self.architecture != 'all', "all not allowed for PackageId (%s)" % (self.name)

    def __repr__(self):
        return ('PID(%s)' % (self.name))

    @property
    def name(self):
        if self.architecture == "source":
            return ('%s/%s' % (self.package_name, self.version))
        else:
            return ('%s/%s/%s' % (self.package_name, self.version, self.architecture))

    @property
    def uvname(self):
        if self.architecture == "source":
            return ('%s' % (self.package_name))
        else:
            return ('%s/%s' % (self.package_name, self.architecture))


class BinaryPackageId(PackageId):
    """Represent a binary package"""

    def __init__(self, package_name, version, architecture):
        assert self.architecture != 'source', "Source not allowed for BinaryPackageId (%s)" % (self.name)
        super().__init__(package_name, version, architecture)

    def __repr__(self):
        return ('BPID(%s)' % (self.name))


BinaryPackage = namedtuple('BinaryPackage', [
    'version',
    'section',
    'source',
    'source_version',
    'architecture',
    'multi_arch',
    'depends',
    'conflicts',
    'provides',
    'is_essential',
    'pkg_id',
    'builtusing',
])
