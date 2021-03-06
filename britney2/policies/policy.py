import json
import os
import time
from abc import abstractmethod
from collections import defaultdict
from urllib.parse import quote

import apt_pkg

from britney2.hints import Hint, split_into_one_hint_per_package
from britney2.utils import ensuredir


class PolicyVerdict:
    """"""
    """
    The migration item passed the policy.
    """
    PASS = 1
    """
    The policy was completely overruled by a hint.
    """
    PASS_HINTED = 2
    """
    The migration item did not pass the policy, but the failure is believed
    to be temporary
    """
    REJECTED_TEMPORARILY = 3
    """
    The migration item did not pass the policy and the failure is believed
    to be uncorrectable (i.e. a hint or a new version is needed)
    """
    REJECTED_PERMANENTLY = 4

    @property
    def is_rejected(self):
        return True if self.name.startswith('REJECTED') else False


class BasePolicy(object):

    def __init__(self, policy_id, options, suite_info, applicable_suites):
        """The BasePolicy constructor

        :param policy_id An string identifying the policy.  It will
        determine the key used for the excuses.yaml etc.

        :param options The options member of Britney with all the
        config options.

        :param applicable_suites A set of suite names where this
        policy applies.
        """
        self.policy_id = policy_id
        self.options = options
        self.suite_info = suite_info
        self.applicable_suites = applicable_suites
        self.hints = None

    # FIXME: use a proper logging framework
    def log(self, msg, type="I"):
        """Print info messages according to verbosity level

        An easy-and-simple log method which prints messages to the standard
        output. The type parameter controls the urgency of the message, and
        can be equal to `I' for `Information', `W' for `Warning' and `E' for
        `Error'. Warnings and errors are always printed, and information is
        printed only if verbose logging is enabled.
        """
        if self.options.verbose or type in ("E", "W"):
            print("%s: [%s] - %s" % (type, time.asctime(), msg))

    def register_hints(self, hint_parser):  # pragma: no cover
        """Register new hints that this policy accepts

        :param hint_parser: An instance of HintParser (see HintParser.register_hint_type)
        """
        pass

    def initialise(self, britney):  # pragma: no cover
        """Called once to make the policy initialise any data structures

        This is useful for e.g. parsing files or other "heavy do-once" work.

        :param britney This is the instance of the "Britney" class.
        """
        pass

    def save_state(self, britney):  # pragma: no cover
        """Called once at the end of the run to make the policy save any persistent data

        Note this will *not* be called for "dry-runs" as such runs should not change
        the state.

        :param britney This is the instance of the "Britney" class.
        """
        pass

    def apply_policy(self, general_policy_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        if self.policy_id not in general_policy_info:
            general_policy_info[self.policy_id] = pinfo = {}
        else:
            pinfo = general_policy_info[self.policy_id]
        return self.apply_policy_impl(pinfo, suite, source_name, source_data_tdist, source_data_srcdist, excuse)

    @abstractmethod
    def apply_policy_impl(self, policy_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):  # pragma: no cover
        """Apply a policy on a given source migration

        Britney will call this method on a given source package, when
        Britney is considering to migrate it from the given source
        suite to the target suite.  The policy will then evaluate the
        the migration and then return a verdict.

        :param policy_info A dictionary of all policy results.  The
        policy can add a value stored in a key related to its name.
        (e.g. policy_info['age'] = {...}).  This will go directly into
        the "excuses.yaml" output.

        :param suite The name of the suite from where the source is
        migrating from.

        :param source_data_tdist Information about the source package
        in the target distribution (e.g. "testing").  This is the
        data structure in Britney.sources['testing'][source_name]

        :param source_data_srcdist Information about the source
        package in the source distribution (e.g. "unstable" or "tpu").
        This is the data structure in
        Britney.sources[suite][source_name]

        :return A Policy Verdict (e.g. PolicyVerdict.PASS)
        """
        pass


class SimplePolicyHint(Hint):

    def __init__(self, user, hint_type, policy_parameter, packages):
        super().__init__(user, hint_type, packages)
        self._policy_parameter = policy_parameter

    def __eq__(self, other):
        if self.type != other.type or self._policy_parameter != other._policy_parameter:
            return False
        return super.__eq__(other)

    def str(self):
        return '%s %s %s' % (self._type, str(self._policy_parameter), ' '.join(x.name for x in self._packages))


class AgeDayHint(SimplePolicyHint):

    @property
    def days(self):
        return self._policy_parameter


class IgnoreRCBugHint(SimplePolicyHint):

    @property
    def ignored_rcbugs(self):
        return self._policy_parameter


def simple_policy_hint_parser_function(class_name, converter):
    def f(hints, who, hint_name, policy_parameter, *args):
        for package in args:
            hints.add_hint(class_name(who, hint_name, converter(policy_parameter), package))
    return f


class AgePolicy(BasePolicy):
    """Configurable Aging policy for source migrations

    The AgePolicy will let packages stay in the source suite for a pre-defined
    amount of days before letting migrate (based on their urgency, if any).

    The AgePolicy's decision is influenced by the following:

    State files:
     * ${STATE_DIR}/age-policy-urgencies: File containing urgencies for source
       packages. Note that urgencies are "sticky" and the most "urgent" urgency
       will be used (i.e. the one with lowest age-requirements).
       - This file needs to be updated externally, if the policy should take
         urgencies into consideration.  If empty (or not updated), the policy
         will simply use the default urgency (see the "Config" section below)
       - In Debian, these values are taken from the .changes file, but that is
         not a requirement for Britney.
     * ${STATE_DIR}/age-policy-dates: File containing the age of all source
       packages.
       - The policy will automatically update this file.
    Config:
     * DEFAULT_URGENCY: Name of the urgency used for packages without an urgency
       (or for unknown urgencies).  Will also  be used to set the "minimum"
       aging requirements for packages not in the target suite.
     * MINDAYS_<URGENCY>: The age-requirements in days for packages with the
       given urgency.
       - Commonly used urgencies are: low, medium, high, emergency, critical
    Hints:
     * urgent <source>/<version>: Disregard the age requirements for a given
       source/version.
     * age-days X <source>/<version>: Set the age requirements for a given
       source/version to X days.  Note that X can exceed the highest
       age-requirement normally given.

    """

    def __init__(self, options, suite_info, mindays):
        super().__init__('age', options, suite_info, {'unstable'})
        self._min_days = mindays
        if options.default_urgency not in mindays:  # pragma: no cover
            raise ValueError("Missing age-requirement for default urgency (MINDAYS_%s)" % options.default_urgency)
        self._min_days_default = mindays[options.default_urgency]
        self._date_now = int(time.time())
        self._dates = {}
        self._urgencies = {}

    def register_hints(self, hint_parser):
        hint_parser.register_hint_type('age-days', simple_policy_hint_parser_function(AgeDayHint, int), min_args=2)
        hint_parser.register_hint_type('urgent', split_into_one_hint_per_package)

    def initialise(self, britney):
        super().initialise(britney)
        self._read_dates_file()
        self._read_urgencies_file(britney)

    def save_state(self, britney):
        super().save_state(britney)
        self._write_dates_file()

    def apply_policy_impl(self, age_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        # retrieve the urgency for the upload, ignoring it if this is a NEW package (not present in testing)
        urgency = self._urgencies.get(source_name, self.options.default_urgency)

        if urgency not in self._min_days:
            age_info['unknown-urgency'] = urgency
            urgency = self.options.default_urgency

        if not source_data_tdist:
            if self._min_days[urgency] < self._min_days_default:
                age_info['urgency-reduced'] = {
                    'from': urgency,
                    'to': self.options.default_urgency,
                }
                urgency = self.options.default_urgency

        if source_name not in self._dates:
            self._dates[source_name] = (source_data_srcdist.version, self._date_now)
        elif self._dates[source_name][0] != source_data_srcdist.version:
            self._dates[source_name] = (source_data_srcdist.version, self._date_now)

        days_old = (self._date_now - self._dates[source_name][1]) / 60 / 60 / 24
        min_days = self._min_days[urgency]
        age_info['age-requirement'] = min_days
        age_info['current-age'] = days_old

        for age_days_hint in self.hints.search('age-days', package=source_name,
                                               version=source_data_srcdist.version):
            new_req = age_days_hint.days
            age_info['age-requirement-reduced'] = {
                'new-requirement': new_req,
                'changed-by': age_days_hint.user
            }
            min_days = new_req

        res = PolicyVerdict.PASS

        if days_old < min_days:
            urgent_hints = self.hints.search('urgent', package=source_name,
                                             version=source_data_srcdist.version)
            if urgent_hints:
                age_info['age-requirement-reduced'] = {
                    'new-requirement': 0,
                    'changed-by': urgent_hints[0].user
                }
                res = PolicyVerdict.PASS_HINTED
            else:
                res = PolicyVerdict.REJECTED_TEMPORARILY

        # update excuse
        age_hint = age_info.get('age-requirement-reduced', None)
        age_min_req = age_info['age-requirement']
        if age_hint:
            new_req = age_hint['new-requirement']
            who = age_hint['changed-by']
            if new_req:
                excuse.addhtml("Overriding age needed from %d days to %d by %s" % (
                    age_min_req, new_req, who))
            else:
                excuse.addhtml("Too young, but urgency pushed by %s" % who)
        excuse.setdaysold(age_info['current-age'], age_min_req)

        return res

    def _read_dates_file(self):
        """Parse the dates file"""
        dates = self._dates
        fallback_filename = os.path.join(self.suite_info['testing'].path, 'Dates')
        using_new_name = False
        try:
            filename = os.path.join(self.options.state_dir, 'age-policy-dates')
            if not os.path.exists(filename) and os.path.exists(fallback_filename):
                filename = fallback_filename
            else:
                using_new_name = True
        except AttributeError:
            if os.path.exists(fallback_filename):
                filename = fallback_filename
            else:
                raise RuntimeError("Please set STATE_DIR in the britney configuration")

        try:
            with open(filename, encoding='utf-8') as fd:
                for line in fd:
                    # <source> <version> <date>
                    l = line.split()
                    if len(l) != 3:
                        continue
                    try:
                        dates[l[0]] = (l[1], int(l[2]))
                    except ValueError:
                        pass
        except IOError:
            if not using_new_name:
                # If we using the legacy name, then just give up
                raise
            self.log("%s does not appear to exist.  Creating it" % filename)
            with open(filename, mode='x', encoding='utf-8'):
                pass

    def _read_urgencies_file(self, britney):
        urgencies = self._urgencies
        min_days_default = self._min_days_default
        fallback_filename = os.path.join(self.suite_info['testing'].path, 'Urgency')
        try:
            filename = os.path.join(self.options.state_dir, 'age-policy-urgencies')
            if not os.path.exists(filename) and os.path.exists(fallback_filename):
                filename = fallback_filename
        except AttributeError:
            filename = fallback_filename

        if not os.path.exists(filename):
            self.log("%s missing; using default for all packages" % filename)
            return

        with open(filename, errors='surrogateescape', encoding='ascii') as fd:
            for line in fd:
                # <source> <version> <urgency>
                l = line.split()
                if len(l) != 3:
                    continue

                # read the minimum days associated with the urgencies
                urgency_old = urgencies.get(l[0], None)
                mindays_old = self._min_days.get(urgency_old, 1000)
                mindays_new = self._min_days.get(l[2], min_days_default)

                # if the new urgency is lower (so the min days are higher), do nothing
                if mindays_old <= mindays_new:
                    continue

                # if the package exists in testing and it is more recent, do nothing
                tsrcv = britney.sources['testing'].get(l[0], None)
                if tsrcv and apt_pkg.version_compare(tsrcv.version, l[1]) >= 0:
                    continue

                # if the package doesn't exist in unstable or it is older, do nothing
                usrcv = britney.sources['unstable'].get(l[0], None)
                if not usrcv or apt_pkg.version_compare(usrcv.version, l[1]) < 0:
                    continue

                # update the urgency for the package
                urgencies[l[0]] = l[2]

    def _write_dates_file(self):
        dates = self._dates
        try:
            directory = self.options.state_dir
            basename = 'age-policy-dates'
            old_file = os.path.join(self.suite_info['testing'].path, 'Dates')
        except AttributeError:
            directory = self.suite_info['testing'].path
            basename = 'Dates'
            old_file = None
        filename = os.path.join(directory, basename)
        ensuredir(directory)
        filename_tmp = os.path.join(directory, '%s_new' % basename)
        with open(filename_tmp, 'w', encoding='utf-8') as fd:
            for pkg in sorted(dates):
                version, date = dates[pkg]
                fd.write("%s %s %d\n" % (pkg, version, date))
        os.rename(filename_tmp, filename)
        if old_file is not None and os.path.exists(old_file):
            self.log("Removing old age-policy-dates file %s" % old_file)
            os.unlink(old_file)


class RCBugPolicy(BasePolicy):
    """RC bug regression policy for source migrations

    The RCBugPolicy will read provided list of RC bugs and block any
    source upload that would introduce a *new* RC bug in the target
    suite.

    The RCBugPolicy's decision is influenced by the following:

    State files:
     * ${STATE_DIR}/rc-bugs-unstable: File containing RC bugs for packages in
       the source suite.
       - This file needs to be updated externally.
     * ${STATE_DIR}/rc-bugs-testing: File containing RC bugs for packages in
       the target suite.
       - This file needs to be updated externally.
    """

    def __init__(self, options, suite_info):
        super().__init__('rc-bugs', options, suite_info, {'unstable'})
        self._bugs = {}

    def register_hints(self, hint_parser):
        f = simple_policy_hint_parser_function(IgnoreRCBugHint, lambda x: frozenset(x.split(',')))
        hint_parser.register_hint_type('ignore-rc-bugs',
                                       f,
                                       min_args=2)

    def initialise(self, britney):
        super().initialise(britney)
        fallback_unstable = os.path.join(self.suite_info['unstable'].path, 'BugsV')
        fallback_testing = os.path.join(self.suite_info['testing'].path, 'BugsV')
        try:
            filename_unstable = os.path.join(self.options.state_dir, 'rc-bugs-unstable')
            filename_testing = os.path.join(self.options.state_dir, 'rc-bugs-testing')
            if not os.path.exists(filename_unstable) and not os.path.exists(filename_testing) and \
               os.path.exists(fallback_unstable) and os.path.exists(fallback_testing):
                filename_unstable = fallback_unstable
                filename_testing = fallback_testing
        except AttributeError:
            filename_unstable = fallback_unstable
            filename_testing = fallback_testing
        self._bugs['unstable'] = self._read_bugs(filename_unstable)
        self._bugs['testing'] = self._read_bugs(filename_testing)

    def apply_policy_impl(self, rcbugs_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        bugs_t = set()
        bugs_u = set()

        for src_key in (source_name, 'src:%s' % source_name):
            if source_data_tdist and src_key in self._bugs['testing']:
                bugs_t.update(self._bugs['testing'][src_key])
            if src_key in self._bugs['unstable']:
                bugs_u.update(self._bugs['unstable'][src_key])

        for pkg, _, _ in source_data_srcdist.binaries:
            if pkg in self._bugs['unstable']:
                bugs_u |= self._bugs['unstable'][pkg]
        if source_data_tdist:
            for pkg, _, _ in source_data_tdist.binaries:
                if pkg in self._bugs['testing']:
                    bugs_t |= self._bugs['testing'][pkg]

        # If a package is not in testing, it has no RC bugs per
        # definition.  Unfortunately, it seems that the live-data is
        # not always accurate (e.g. live-2011-12-13 suggests that
        # obdgpslogger had the same bug in testing and unstable,
        # but obdgpslogger was not in testing at that time).
        # - For the curious, obdgpslogger was removed on that day
        #   and the BTS probably had not caught up with that fact.
        #   (https://tracker.debian.org/news/415935)
        assert not bugs_t or source_data_tdist, "%s had bugs in testing but is not in testing" % source_name

        success_verdict = PolicyVerdict.PASS

        for ignore_hint in self.hints.search('ignore-rc-bugs', package=source_name,
                                             version=source_data_srcdist.version):
            ignored_bugs = ignore_hint.ignored_rcbugs

            # Only handle one hint for now
            if 'ignored-bugs' in rcbugs_info:
                self.log("Ignoring ignore-rc-bugs hint from %s on %s due to another hint from %s" % (
                    ignore_hint.user, source_name, rcbugs_info['ignored-bugs']['issued-by']
                ))
                continue
            if not ignored_bugs.isdisjoint(bugs_u):
                bugs_u -= ignored_bugs
                bugs_t -= ignored_bugs
                rcbugs_info['ignored-bugs'] = {
                    'bugs': sorted(ignored_bugs),
                    'issued-by': ignore_hint.user
                }
                success_verdict = PolicyVerdict.PASS_HINTED
            else:
                self.log("Ignoring ignore-rc-bugs hint from %s on %s as none of %s affect the package" % (
                    ignore_hint.user, source_name, str(ignored_bugs)
                ))

        rcbugs_info['shared-bugs'] = sorted(bugs_u & bugs_t)
        rcbugs_info['unique-source-bugs'] = sorted(bugs_u - bugs_t)
        rcbugs_info['unique-target-bugs'] = sorted(bugs_t - bugs_u)

        # update excuse
        new_bugs = rcbugs_info['unique-source-bugs']
        old_bugs = rcbugs_info['unique-target-bugs']
        excuse.setbugs(old_bugs, new_bugs)
        if new_bugs:
            excuse.addhtml("%s <a href=\"https://bugs.debian.org/cgi-bin/pkgreport.cgi?" \
                           "src=%s&sev-inc=critical&sev-inc=grave&sev-inc=serious\" " \
                           "target=\"_blank\">has new bugs</a>!" % (source_name, quote(source_name)))
            excuse.addhtml("Updating %s introduces new bugs: %s" % (source_name, ", ".join(
                ["<a href=\"https://bugs.debian.org/%s\">#%s</a>" % (quote(a), a) for a in new_bugs])))

        if old_bugs:
            excuse.addhtml("Updating %s fixes old bugs: %s" % (source_name, ", ".join(
                ["<a href=\"https://bugs.debian.org/%s\">#%s</a>" % (quote(a), a) for a in old_bugs])))
        if new_bugs and len(old_bugs) > len(new_bugs):
            excuse.addhtml("%s introduces new bugs, so still ignored (even "
                           "though it fixes more than it introduces, whine at debian-release)" % source_name)

        if not bugs_u or bugs_u <= bugs_t:
            return success_verdict
        return PolicyVerdict.REJECTED_PERMANENTLY

    def _read_bugs(self, filename):
        """Read the release critical bug summary from the specified file

        The file contains rows with the format:

        <package-name> <bug number>[,<bug number>...]

        The method returns a dictionary where the key is the binary package
        name and the value is the list of open RC bugs for it.
        """
        bugs = {}
        if not os.path.exists(filename):
            self.log("%s missing; skipping bug-based processing" % filename)
            return bugs

        self.log("Loading RC bugs data from %s" % filename)
        for line in open(filename, encoding='ascii'):
            l = line.split()
            if len(l) != 2:
                self.log("Malformed line found in line %s" % (line), type='W')
                continue
            pkg = l[0]
            if pkg not in bugs:
                bugs[pkg] = set()
            bugs[pkg].update(l[1].split(","))
        return bugs


class PiupartsPolicy(BasePolicy):

    def __init__(self, options, suite_info):
        super().__init__('piuparts', options, suite_info, {'unstable'})
        self._piuparts = {
            'unstable': None,
            'testing': None,
        }

    def register_hints(self, hint_parser):
        hint_parser.register_hint_type('ignore-piuparts', split_into_one_hint_per_package)

    def initialise(self, britney):
        super().initialise(britney)
        try:
            filename_unstable = os.path.join(self.options.state_dir, 'piuparts-summary-unstable.json')
            filename_testing = os.path.join(self.options.state_dir, 'piuparts-summary-testing.json')
        except AttributeError as e:  # pragma: no cover
            raise RuntimeError("Please set STATE_DIR in the britney configuration") from e
        self._piuparts['unstable'] = self._read_piuparts_summary(filename_unstable, keep_url=True)
        self._piuparts['testing'] = self._read_piuparts_summary(filename_testing, keep_url=False)

    def apply_policy_impl(self, piuparts_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        if source_name in self._piuparts['testing']:
            testing_state = self._piuparts['testing'][source_name][0]
        else:
            testing_state = 'X'
        if source_name in self._piuparts['unstable']:
            unstable_state, url = self._piuparts['unstable'][source_name]
        else:
            unstable_state = 'X'
            url = None
        url_html = "(no link yet)"
        if url is not None:
            url_html = '<a href="{0}">{0}</a>'.format(url)

        if unstable_state == 'P':
            # Not a regression
            msg = 'Piuparts tested OK - {0}'.format(url_html)
            result = PolicyVerdict.PASS
            piuparts_info['test-results'] = 'pass'
        elif unstable_state == 'F':
            if testing_state != unstable_state:
                piuparts_info['test-results'] = 'regression'
                msg = 'Rejected due to piuparts regression - {0}'.format(url_html)
                result = PolicyVerdict.REJECTED_PERMANENTLY
            else:
                piuparts_info['test-results'] = 'failed'
                msg = 'Ignoring piuparts failure (Not a regression) - {0}'.format(url_html)
                result = PolicyVerdict.PASS
        elif unstable_state == 'W':
            msg = 'Waiting for piuparts test results (stalls testing migration) - {0}'.format(url_html)
            result = PolicyVerdict.REJECTED_TEMPORARILY
            piuparts_info['test-results'] = 'waiting-for-test-results'
        else:
            msg = 'Cannot be tested by piuparts (not a blocker) - {0}'.format(url_html)
            piuparts_info['test-results'] = 'cannot-be-tested'
            result = PolicyVerdict.PASS

        if url is not None:
            piuparts_info['piuparts-test-url'] = url
        excuse.addhtml(msg)

        if result in [PolicyVerdict.REJECTED_PERMANENTLY, PolicyVerdict.REJECTED_TEMPORARILY]:
            for ignore_hint in self.hints.search('ignore-piuparts',
                                                 package=source_name,
                                                 version=source_data_srcdist.version):
                piuparts_info['ignored-piuparts'] = {
                    'issued-by': ignore_hint.user
                }
                result = PolicyVerdict.PASS_HINTED
                excuse.addhtml("Ignoring piuparts issue as requested by {0}".format(ignore_hint.user))
                break

        return result

    def _read_piuparts_summary(self, filename, keep_url=True):
        summary = {}
        if not os.path.exists(filename):
            self.log("%s missing; skipping piuparts processing" % filename)
            return summary

        self.log("Loading piuparts report from {0}".format(filename))
        with open(filename) as fd:
            if os.fstat(fd.fileno()).st_size < 1:
                return summary
            data = json.load(fd)
        try:
            if data['_id'] != 'Piuparts Package Test Results Summary' or data['_version'] != '1.0':  # pragma: no cover
                raise ValueError('Piuparts results in {0} does not have the correct ID or version'.format(filename))
        except KeyError as e:  # pragma: no cover
            raise ValueError('Piuparts results in {0} is missing id or version field'.format(filename)) from e
        for source, suite_data in data['packages'].items():
            if len(suite_data) != 1:  # pragma: no cover
                raise ValueError('Piuparts results in {0}, the source {1} does not have exactly one result set'.format(
                    filename, source
                ))
            item = next(iter(suite_data.values()))
            state, _, url = item
            if not keep_url:
                keep_url = None
            summary[source] = (state, url)

        return summary


class LPBlockBugPolicy(BasePolicy):
    """block-proposed Launchpad bug policy for source migrations

    This policy will read an user-supplied "Blocks" file from the unstable
    directory (provided by an external script) with rows of the following
    format:

        <source-name> <bug> <date>

    The dates are expressed as the number of seconds from the Unix epoch
    (1970-01-01 00:00:00 UTC).
    """
    def __init__(self, options, suite_info):
        super().__init__('block-bugs', options, suite_info, {'unstable'})

    def initialise(self, britney):
        super().initialise(britney)
        self.blocks = {}  # srcpkg -> [(bug, date), ...]

        filename = os.path.join(self.options.unstable, "Blocks")
        self.log("Loading user-supplied block data from %s" % filename)
        for line in open(filename):
            l = line.split()
            if len(l) != 3:
                self.log("Blocks, ignoring malformed line %s" % line, type='W')
                continue
            try:
                self.blocks.setdefault(l[0], [])
                self.blocks[l[0]].append((l[1], int(l[2])))
            except ValueError:
                self.log("Blocks, unable to parse \"%s\"" % line, type='E')

    def apply_policy_impl(self, block_bugs_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        try:
            blocks = self.blocks[source_name]
        except KeyError:
            return PolicyVerdict.PASS

        for bug, date in blocks:
            block_bugs_info[bug] = date
            excuse.addhtml("Not touching package as requested in <a href=\"https://launchpad.net/bugs/%s\">bug %s</a> on %s" %
                           (bug, bug, time.asctime(time.gmtime(date))))
        excuse.addreason('block')

        return PolicyVerdict.REJECTED_PERMANENTLY


class LPExcuseBugsPolicy(BasePolicy):
    """update-excuse Launchpad bug policy to link to a bug report, does not prevent migration

    This policy will read an user-supplied "ExcuseBugs" file from the unstable
    directory (provided by an external script) with rows of the following
    format:

        <source-name> <bug> <date>

    The dates are expressed as the number of seconds from the Unix epoch
    (1970-01-01 00:00:00 UTC).
    """
    def __init__(self, options, suite_info):
        super().__init__('update-excuse', options, suite_info, {'unstable'})

    def initialise(self, britney):
        super().initialise(britney)
        self.excuse_bugs = defaultdict(list)  # srcpkg -> [(bug, date), ...]

        filename = os.path.join(self.options.unstable, "ExcuseBugs")
        self.log("Loading user-supplied excuse bug data from %s" % filename)
        try:
            for line in open(filename):
                l = line.split()
                if len(l) != 3:
                    self.log("ExcuseBugs, ignoring malformed line %s" % line, type='W')
                    continue
                try:
                    self.excuse_bugs[l[0]].append((l[1], int(l[2])))
                except ValueError:
                    self.log("ExcuseBugs, unable to parse \"%s\"" % line, type='E')
        except FileNotFoundError:
            self.log("ExcuseBugs, data file not found, no bugs will be recorded")

    def apply_policy_impl(self, excuse_bugs_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        excuse_bug = self.excuse_bugs[source_name]

        for bug, date in excuse_bug:
            excuse_bugs_info[bug] = date
            excuse.addhtml("Also see <a href=\"https://launchpad.net/bugs/%s\">bug %s</a> last updated on %s" %
                           (bug, bug, time.asctime(time.gmtime(date))))

        return PolicyVerdict.PASS
