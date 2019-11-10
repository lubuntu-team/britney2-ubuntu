import code
import collections


class SubInterpreterExit(SystemExit):
    pass


def fuzzy_match(string, collection, thing_being_matched):
    if len(collection) < 1:
        raise ValueError("No valid candidates to match!?")
    matches = collection
    if string:
        if string in collection:
            return string
        matches = [x for x in collection if string in x]
    if not matches:
        raise ValueError("No matches for %s (%s), possible valid values are: %s" % (
            string, thing_being_matched, sorted(collection)))
    if string is not None and len(matches) > 1:
        matches = [x for x in collection if x.starts(string)]
    if len(matches) > 1:
        if string is None:
            raise ValueError("More than one item and no search criteria for %s, possible valid values are: %s" % (
                thing_being_matched, sorted(matches)))
        raise ValueError("Too many matches for %s (%s), matching candidates are: %s" % (
            string, thing_being_matched, sorted(matches)))
    return next(iter(matches))


class ConsoleUtils(object):

    def __init__(self, britney):
        self._britney = britney
        self._pkgid_cache = collections.defaultdict(list)
        self._build_cache()

    def _build_cache(self):
        for pkg_id in self._britney.all_binaries:
            self._pkgid_cache[pkg_id.package_name].append(pkg_id)

    def pkg_id(self, package_name, package_version=None, package_architecture=None, *,
               fuzzy_match_version=True, fuzzy_match_arch=True):
        """Look up BinaryPackageID

        Example:
             pkg_id("lintian", "2.5", "amd64") -> BinaryPackageID("lintian", "2.5.10", "amd64")
        (Note that difference in version is intentional and a part of the fuzzy matching)

        :param package_name: Name of the package (e.g. "lintian")
        :param package_version: Version of the package (e.g. "2.5")
        :param package_architecture: Architecture of the package (note arch:all packages are always split
          in to a per-architecture package)
        :param fuzzy_match_version: If true, any version string is accepted as long as it uniquely identified
          the package.
        :param fuzzy_match_arch: If true, any architecture string is accepted as long as it uniquely identified
          the package.
        :return: Exactly one BinaryPackageId
        """
        if package_name not in self._pkgid_cache:
            raise ValueError("Unknown package name: %s" % package_name)

        package_candidates = self._pkgid_cache[package_name]
        unique_versions = {x.version for x in package_candidates}

        real_version = package_version
        if package_version is None or package_version not in unique_versions:
            if not fuzzy_match_version:
                if unique_versions is None:
                    raise ValueError("unique_versions cannot be None when fuzzy_match_version is False")
                raise ValueError("Unknown version %s, valid options are: %s" % (
                    package_version, sorted(unique_versions)))
            real_version = fuzzy_match(package_architecture, unique_versions, 'package_version')

        unique_architectures = {x.architecture for x in package_candidates if x.version == real_version}

        real_architecture = package_architecture
        if package_architecture is None or package_architecture not in unique_architectures:
            if not fuzzy_match_arch:
                if package_architecture is None:
                    raise ValueError("package_architecture cannot be None when fuzzy_match_arch is False")
                raise ValueError("Unknown architecture %s" % package_architecture)
            real_architecture = fuzzy_match(package_architecture, unique_architectures, 'package_architecture')

        match = [x for x in package_candidates if x.version == real_version and x.architecture == real_architecture]
        if len(match) != 1:
            if not match:
                raise ValueError("Package %s, version %s (%s) is not available on architecture %s (%s)" %
                                 package_name, package_version, real_version, package_architecture, real_architecture)
            raise ValueError("The terms %s, %s (%s) and %s (%s) did not result in a unique package!?  All matches: %s" %
                             package_name, package_version, real_version, package_architecture, real_architecture,
                             sorted(match))

        return match[0]


def console_quit():
    raise SubInterpreterExit()


def run_python_console(britney_obj):
    console_utils = ConsoleUtils(britney_obj)
    console_locals = {
        'britney': britney_obj,
        '__name__': '__console__',
        '__doc__': None,
        'all_bin_pkg_ids': britney_obj.all_binaries.keys(),
        'pkg_id': console_utils.pkg_id,
        'quit': console_quit,
        'exit': console_quit,
    }
    console = code.InteractiveConsole(locals=console_locals)
    banner = """\
Interactive python (REPL) shell in britney.

Locals available
 * britney: Instance of the Britney object.
 * all_bin_pkg_ids: Set of all BinaryPackageIDs
 * pkg_id: Lookup a BinaryPackageID
 * quit()/exit(): leave this REPL console.
"""
    try:
        console.interact(banner=banner, exitmsg='')
    except SubInterpreterExit:
        pass
