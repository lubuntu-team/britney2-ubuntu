import os
import time

from collections import defaultdict

from britney2 import SuiteClass
from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


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
        super().__init__(
            "update-excuse",
            options,
            suite_info,
            {SuiteClass.PRIMARY_SOURCE_SUITE},
        )
        self.filename = os.path.join(options.unstable, "ExcuseBugs")

    def initialise(self, britney):
        super().initialise(britney)
        self.excuse_bugs = defaultdict(list)  # srcpkg -> [(bug, date), ...]

        self.logger.info(
            "Loading user-supplied excuse bug data from %s" % self.filename
        )
        try:
            for line in open(self.filename):
                ln = line.split()
                if len(ln) != 3:
                    self.logger.warning(
                        "ExcuseBugs, ignoring malformed line %s" % line,
                    )
                    continue
                try:
                    self.excuse_bugs[ln[0]].append((ln[1], int(ln[2])))
                except ValueError:
                    self.logger.error(
                        'ExcuseBugs, unable to parse "%s"' % line
                    )
        except FileNotFoundError:
            self.logger.info(
                "ExcuseBugs, file %s not found, no bugs will be recorded",
                self.filename,
            )

    def apply_src_policy_impl(
        self,
        excuse_bugs_info,
        item,
        source_data_tdist,
        source_data_srcdist,
        excuse,
    ):
        source_name = item.package
        excuse_bug = self.excuse_bugs[source_name]

        for bug, date in excuse_bug:
            excuse_bugs_info[bug] = date
            excuse.addinfo(
                'Also see <a href="https://launchpad.net/bugs/%s">bug %s</a> last updated on %s'
                % (bug, bug, time.asctime(time.gmtime(date)))
            )

        return PolicyVerdict.PASS
