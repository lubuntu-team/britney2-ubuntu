#!/usr/bin/env python3

# Copyright (C) 2020 Simon Quigley <tsimonq2@lubuntu.me>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
from jenkinsapi.jenkins import Jenkins
from britney2.policies.policy import BasePolicy, PolicyVerdict


class JenkinsPassPolicy(BasePolicy):
    """Ensure packages cannot migrate if their Jenkins job failed

    Using the Jenkins API (with API_{SITE,USER,KEY} env vars), if the job
    belonging to this package has failed for some reason, ensure the package
    cannot migrate
    """

    def __init__(self, options, suite_info):
        super().__init__("jenkins-pass", options, suite_info, {"unstable"})
        self.filename = os.path.join(options.unstable, "JenkinsPass")

        # Authenticate to Jenkins with the given env vars
        api_site = getenv("API_SITE")
        api_user = getenv("API_USER")
        api_key = getenv("API_KEY")
        for envvar in [api_site, api_user, api_key]:
            if not envvar:
                raise ValueError("API_SITE, API_USER, and API_KEY must be",
                                 "defined")
        self.jenkins = Jenkins(api_site, username=api_user, password=api_key)

        self.britney = None

    def initialise(self, britney):
        """Load cached source ppa data"""
        super().initialise(britney)
        self.britney = britney

    def save_state(self):
        pass

    def match_jobname_package(self, pkg, version):
        """Match the job name in Jenkins to the package"""

        # Ensure that the job format is specified as an env var
        # Also, substitute in values
        job_format = getenv("JOB_FORMAT")
        if not job_format:
            raise ValueError("JOB_FORMAT not defined")
        elif not "RELEASE" in job_format:
            raise ValueError("RELEASE not in JOB_FORMAT")
        elif not "PACKAGE" in job_format:
            raise ValueError("PACKAGE not in JOB_FORMAT")
        else:
            job_format = job_format.replace("RELEASE", self.options.series)
            job_format = job_format.replace("PACKAGE", pkg)

        # Check if job exists on server, if not return None
        # For reference, s_jobs is in the following format:
        # ('JOBNAME', <jenkinsapi.job.Job JOBNAME>)
        job = None
        for s_job in self.jenkins.get_jobs():
            if s_job[0] == job_format:
                job = s_job[1]
                break
        if not job:
            return None

        # Get the last build done for the given job
        # If it exists but hasn't been built yet, return None
        # Otherwise, return the Build instance so we can check if it's valid
        job_build = job.get_last_build_or_none()
        if not job_build:
            return None
        else:
            return job_build

    def is_job_successful(self, pkg, version):
        """Check if the Jenkins job for this package was successful"""

        # Get the Job instance, and if it doesn't exist, just allow the
        # package to migrate without issue
        job = self.match_jobname_package(pkg, version)
        if not job:
            return True

        # The Jenkins API has a built-in function to check this, so just
        # return the value it gives
        return job.is_good()

    def apply_policy_impl(self, sourceppa_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        """Reject package if the associated Jenkins job has failed"""
        accept = excuse.is_valid
        britney_excuses = self.britney.excuses
        version = source_data_srcdist.version

        # If the job was successful or doesn't exist, allow it to migrate
        # If not, reject it
        if self.is_job_successful(source_name, version):
            return PolicyVerdict.PASS
        else:
            return PolicyVerdict.REJECTED_PERMANENTLY
