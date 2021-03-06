# Copyright (C) 2012 Science and Technology Facilities Council.
# Copyright (C) 2015-2016 East Asian Observatory.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function

from crab.util.crontab import parse_crontab, write_crontab
from crab.util.statuspattern import check_status_patterns


class CrabStore:
    def get_jobs(self, host=None, user=None, **kwargs):
        """Fetches a list of all of the cron jobs,
        excluding deleted jobs by default.

        Optionally filters by host or username if these parameters are
        supplied.  Other keyword arguments (e.g. include_deleted,
        crabid, command, without_crabid) are passed to the
        _get_jobs method."""

        with self.lock as c:
            return self._get_jobs(c, host, user, **kwargs)

    def delete_job(self, id_):
        """Mark a job as deleted."""
        with self.lock as c:
            self._delete_job(c, id_)

    def undelete_job(self, id_):
        """Remove deletion mark from a job."""
        with self.lock as c:
            self._update_job(c, id_)

    def update_job(self, id_, **kwargs):
        """Updates job information.

        Keyword arguments are passed on to the private _update_job method,
        and can include: crabid, command, time, timezone."""
        with self.lock as c:
            self._update_job(c, id_, **kwargs)

    def log_start(self, host, user, crabid, command):
        """Inserts a job start record into the database.

        Returns a dictionary including a boolean value indicating
        whether the job inhibit setting is active or not."""

        data = {'inhibit': False}

        with self.lock as c:
            id_ = self._check_job(c, host, user, crabid, command)

            self._log_start(c, id_, command)

            # Read the job configuration in order to determine whether
            # this job is currently inhibited.
            config = self._get_job_config(c, id_)

            if config is not None and config['inhibit']:
                data['inhibit'] = True

        return data

    def log_finish(self, host, user, crabid, command, status,
                   stdout=None, stderr=None):
        """Inserts a job finish record into the database.

        The output will be passed to the write_job_output method,
        unless both stdout and stderr are empty."""

        with self.lock as c:
            id_ = self._check_job(c, host, user, crabid, command)

            # Fetch the configuration so that we can check the status.
            config = self._get_job_config(c, id_)
            if config is not None:
                status = check_status_patterns(
                    status, config,
                    '\n'.join((x for x in (stdout, stderr)
                               if x is not None)))

            finishid = self._log_finish(c, id_, command, status)

        if stdout or stderr:
            # If a crabid was not specified, check whether the job
            # actually has one.  This is to avoid sending misleading
            # parameters to write_job_output, which can cause the
            # file-based output store to default to a numeric directory name.
            if crabid is None:
                info = self.get_job_info(id_)
                crabid = info['crabid']

            self.write_job_output(finishid, host, user, id_, crabid,
                                  stdout, stderr)

    def get_job_config(self, id_):
        """Retrieve configuration data for a job by ID number."""

        with self.lock as c:
            return self._get_job_config(c, id_)

    def write_job_output(self, finishid, host, user, id_, crabid,
                         stdout, stderr):
        """Writes the job output to the store.

        This will use the outputstore's corresponding method if it is defined,
        otherwise it writes to this store."""

        if self.outputstore is not None:
            return self.outputstore.write_job_output(
                finishid, host, user, id_, crabid, stdout, stderr)

        with self.lock as c:
            return self._write_job_output(
                c, finishid, host, user, id_, crabid, stdout, stderr)

    def get_job_output(self, finishid, host, user, id_, crabid):
        """Fetches the standard output and standard error for the
        given finish ID.

        The result is returned as a two element list.  Returns a pair of empty
        strings if no output is found.

        This will use the outputstore's corresponding method if it is defined,
        otherwise it reads from this store."""

        if self.outputstore is not None:
            return self.outputstore.get_job_output(
                finishid, host, user, id_, crabid)

        with self.lock as c:
            return self._get_job_output(
                c, finishid, host, user, id_, crabid)

    def get_crontab(self, host, user):
        """Fetches the job entries for a particular host and user and builds
        a crontab style representation.

        Please see crab.util.crontab.write_crontab for more details
        of how the crontab is constructed.
        """

        jobs = self.get_jobs(host, user)

        return write_crontab(jobs)

    def save_crontab(self, host, user, crontab, timezone=None,
                     allow_filter=True):
        """Takes a list of crontab lines and uses them to update the job records.

        It looks for the CRABID and CRON_TZ variables, but otherwise
        ignores everything except command lines.  It also checks for commands
        starting with a CRABID= definition, but otherwise inserts them
        into the database as is.

        If "allow_filter" is True (as is the default) then cron jobs are
        skipped if they have a specified user name or client host name
        which does not match the given host or user name.

        Returns a list of warning strings."""

        # Save the raw crontab.
        self.write_raw_crontab(host, user, crontab)

        # Prepare set of existing job ID numbers.
        idset = set()
        for job in self.get_jobs(host, user):
            idset.add(job['id'])

        # Parse the crontab.
        (jobs, warning) = parse_crontab(crontab, timezone=timezone)

        # Iterate over the supplied cron jobs, removing each
        # job from the idset set as we encounter it.
        idsaved = set()
        with self.lock as c:
            for job in jobs:
                if allow_filter:
                    vars_ = job['vars']

                    vars_hostname = vars_.get('CRABCLIENTHOSTNAME')
                    if (vars_hostname is not None) and (vars_hostname != host):
                        warning.append(
                            'Skipped job for other hostname: ' + job['rule'])
                        continue

                    vars_username = vars_.get('CRABUSERNAME')
                    if (vars_username is not None) and (vars_username != user):
                        warning.append(
                            'Skipped job for other user: ' + job['rule'])
                        continue

                id_ = self._check_job(
                    c, host, user, job['crabid'],
                    job['command'], job['time'], job['timezone'])

                if id_ in idsaved:
                    warning.append(
                        'Indistinguishable duplicated job: ' + job['rule'])
                else:
                    idsaved.add(id_)

                idset.discard(id_)

            # Set any jobs remaining in the id set to deleted
            # because we did not see them in the current crontab
            for id_ in idset:
                self._delete_job(c, id_)

        return warning

    def check_job(self, *args, **kwargs):
        """Ensure that a job exists in the store.

        Acquires the lock and then calls the private _check_job method."""

        with self.lock as c:
            return self._check_job(c, *args, **kwargs)

    def _check_job(self, c, host, user, crabid, command,
                   time=None, timezone=None):
        """Ensure that a job exists in the store.

        Tries to find (and update if necessary) the corresponding job.
        If it is not found, the job is stored as a new entry.

        In either case, the job's ID number is returned.

        This is a private method because the lock must be acquired
        prior to calling it."""

        id_ = None

        # We know the crabid, so use it to search

        if crabid is not None:
            jobs = self._get_jobs(c, host, user, include_deleted=True,
                                  crabid=crabid)

            if jobs:
                job = jobs[0]
                id_ = job['id']

                if (job['deleted'] is None and
                        command == job['command'] and
                        (time is None or time == job['time']) and
                        (timezone is None or timezone == job['timezone'])):
                    pass

                else:
                    self._update_job(c, id_, None, command, time, timezone)

            else:
                # Need to check if the job already existed without
                # a job ID, in which case we update it to add the job ID.

                jobs = self._get_jobs(c, host, user, include_deleted=True,
                                      command=command, without_crabid=True)
                if jobs:
                    job = jobs[0]
                    id_ = job['id']

                    self._update_job(c, id_, crabid, None, time, timezone)

                else:
                    id_ = self._insert_job(c, host, user, crabid, time,
                                           command, timezone)

        # We don't know the crabid, so we must search by command.
        # In general we can't distinguish multiple copies of the same
        # command running at different times.
        # Such jobs should be given job IDs, or combined using
        # time ranges / steps.

        else:
            jobs = self._get_jobs(c, host, user, include_deleted=True,
                                  command=command)

            if jobs:
                job = jobs[0]
                id_ = job['id']

                if (job['deleted'] is None and
                        (time is None or time == job['time']) and
                        (timezone is None or timezone == job['timezone'])):
                    pass

                else:
                    self._update_job(c, id_, None, None, time, timezone)

            else:
                id_ = self._insert_job(c, host, user, crabid,
                                       time, command, timezone)

        if id_ is None:
            raise CrabError('store error: failed to identify job')

        return id_

    def write_raw_crontab(self, host, user, crontab):
        if self.outputstore is not None and hasattr(self.outputstore,
                                                    'write_raw_crontab'):
            return self.outputstore.write_raw_crontab(host, user, crontab)

        with self.lock as c:
            return self._write_raw_crontab(c, host, user, crontab)

    def get_raw_crontab(self, host, user):
        if self.outputstore is not None and hasattr(self.outputstore,
                                                    'get_raw_crontab'):
            return self.outputstore.get_raw_crontab(host, user)

        with self.lock as c:
            return self._get_raw_crontab(c, host, user)
