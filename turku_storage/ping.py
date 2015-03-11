#!/usr/bin/env python

# Turku backups - storage module
# Copyright 2015 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
# SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import sys
import subprocess
import datetime
import re
import logging
import tempfile
import time
import shutil
from utils import load_config, acquire_lock, api_call, random_weighted, get_latest_snapshot, get_snapshots_to_delete


class StoragePing():
    def __init__(self, uuid, config_dir='/etc/turku-storage'):
        self.arg_uuid = uuid

        self.config = load_config(config_dir)
        for k in ('name', 'secret'):
            if k not in self.config:
                raise Exception('Incomplete config')

        self.logger = logging.getLogger(self.config['name'])
        self.logger.setLevel(logging.DEBUG)

        self.lh_console = logging.StreamHandler()
        self.lh_console_formatter = logging.Formatter('[%(asctime)s %(name)s] %(levelname)s: %(message)s')
        self.lh_console.setFormatter(self.lh_console_formatter)
        self.lh_console.setLevel(logging.ERROR)
        self.logger.addHandler(self.lh_console)

        self.lh_master = logging.FileHandler(self.config['log_file'])
        self.lh_master_formatter = logging.Formatter('[%(asctime)s ' + self.arg_uuid + ' %(process)s] %(levelname)s: %(message)s')
        self.lh_master.setFormatter(self.lh_master_formatter)
        self.lh_master.setLevel(logging.DEBUG)
        self.logger.addHandler(self.lh_master)

    def run_logging(self, args, loglevel=logging.DEBUG, cwd=None, env=None, return_output=False):
        self.logger.log(loglevel, 'Running: %s' % repr(args))
        t = tempfile.NamedTemporaryFile()
        self.logger.log(loglevel, '(Command output is in %s until written here at the end)' % t.name)
        returncode = subprocess.call(args, cwd=cwd, env=env, stdout=t, stderr=t)
        t.flush()
        t.seek(0)
        out = ''
        for line in t:
            if return_output:
                out = out + line
            self.logger.log(loglevel, line.rstrip('\n'))
        t.close()
        self.logger.log(loglevel, 'Return code: %d' % returncode)
        if return_output:
            return (returncode, out)
        else:
            return returncode

    def process_ping(self):
        jsonin = ''
        while True:
            l = sys.stdin.readline()
            if (l == '.\n') or (not l):
                break
            jsonin = jsonin + l
        try:
            j = json.loads(jsonin)
        except ValueError:
            raise Exception('Invalid input JSON')

        lock = acquire_lock(os.path.join(self.config['lock_dir'], 'turku-storage-ping-%s.lock' % self.arg_uuid))

        if 'port' not in j:
            raise Exception('Port required')
        forwarded_port = int(j['port'])

        verbose = False
        if 'verbose' in j and j['verbose']:
            verbose = True
        if verbose:
            self.lh_console.setLevel(logging.INFO)

        if 'action' in j and j['action'] == 'restore':
            self.logger.info('Restore mode active on port %d.  Good luck.' % forwarded_port)
            subprocess.call(['/bin/cat'])
            return

        api_out = {
            'name': self.config['name'],
            'secret': self.config['secret'],
            'machine_uuid': self.arg_uuid,
        }
        api_reply = api_call(self.config['api_url'], 'storage_ping_checkin', api_out)

        machine = api_reply['machine']
        scheduled_sources = api_reply['scheduled_sources']
        if len(scheduled_sources) > 0:
            self.logger.info('Sources to back up: %s' % ', '.join([s['name'] for s in scheduled_sources]))
        else:
            self.logger.info('No sources to back up now')
        for s in scheduled_sources:
            time_begin = time.time()
            snapshot_mode = self.config['snapshot_mode']
            if snapshot_mode == 'link-dest':
                if 'large_rotating_files' in s and s['large_rotating_files']:
                    snapshot_mode = 'none'
                if 'large_modifying_files' in s and s['large_modifying_files']:
                    snapshot_mode = 'none'

            var_machines = os.path.join(self.config['var_dir'], 'machines')
            if not os.path.exists(var_machines):
                os.makedirs(var_machines)

            if os.path.islink(os.path.join(var_machines, machine['uuid'])):
                machine_dir = os.readlink(os.path.join(var_machines, machine['uuid']))
            else:
                weights = {}
                for dir in self.config['storage_dir']:
                    try:
                        sv = os.statvfs(dir)
                        weights[dir] = (sv.f_bsize * sv.f_bavail / 1048576)
                    except OSError:
                        continue
                chosen_storage_dir = random_weighted(weights)
                if not chosen_storage_dir:
                    raise Exception('Cannot find a suitable storage directory')
                machine_dir = os.path.join(chosen_storage_dir, machine['uuid'])
                os.symlink(machine_dir, os.path.join(var_machines, machine['uuid']))
            if not os.path.exists(machine_dir):
                os.makedirs(machine_dir)

            machine_symlink = machine['unit_name']
            if 'service_name' in machine and machine['service_name']:
                machine_symlink = machine['service_name'] + '-'
            if 'environment_name' in machine and machine['environment_name']:
                machine_symlink = machine['environment_name'] + '-'
            machine_symlink = machine_symlink.replace('/', '_')
            if os.path.exists(os.path.join(var_machines, machine_symlink)):
                if os.path.islink(os.path.join(var_machines, machine_symlink)):
                    if not os.readlink(os.path.join(var_machines, machine_symlink)) == machine['uuid']:
                        os.symlink(machine['uuid'], os.path.join(var_machines, machine_symlink))
            else:
                os.symlink(machine['uuid'], os.path.join(var_machines, machine_symlink))

            self.logger.info('Begin: %s %s' % (machine['unit_name'], s['name']))

            rsync_args = ['rsync', '--archive', '--compress', '--numeric-ids', '--delete', '--delete-excluded']
            rsync_args.append('--verbose')

            if snapshot_mode == 'attic':
                rsync_args.append('--inplace')
                dest_dir = os.path.join(machine_dir, s['name'])
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
            elif snapshot_mode == 'link-dest':
                snapshot_dir = os.path.join(machine_dir, '%s.snapshots' % s['name'])
                if not os.path.exists(snapshot_dir):
                    os.makedirs(snapshot_dir)
                dirs = [d for d in os.listdir(snapshot_dir) if os.path.isdir(os.path.join(snapshot_dir, d))]
                base_snapshot = get_latest_snapshot(dirs)
                if base_snapshot:
                    rsync_args.append('--link-dest=%s' % os.path.join(snapshot_dir, base_snapshot))
            else:
                rsync_args.append('--inplace')
                dest_dir = os.path.join(machine_dir, s['name'])
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)

            filter_file = None
            filter_data = ''
            if 'filter' in s:
                for filter in s['filter']:
                    if filter.startswith('merge') or filter.startswith(':'):
                        # Do not allow local merges
                        continue
                    filter_data += '%s\n' % filter
            if 'exclude' in s:
                for exclude in s['exclude']:
                    filter_data += '- %s\n' % exclude
            if filter_data:
                filter_file = tempfile.NamedTemporaryFile()
                filter_file.write(filter_data)
                filter_file.flush()
                rsync_args.append('--filter=merge %s' % filter_file.name)

            rsync_args.append('rsync://%s@127.0.0.1:%d/%s/' % (s['username'], forwarded_port, s['name']))

            if snapshot_mode == 'link-dest':
                rsync_args.append('%s/' % os.path.join(snapshot_dir, 'working'))
            else:
                rsync_args.append('%s/' % dest_dir)

            rsync_env = {
                'RSYNC_PASSWORD': s['password']
            }
            returncode = self.run_logging(rsync_args, env=rsync_env)
            if returncode in (0, 24):
                success = True
            else:
                success = False
            if filter_file:
                filter_file.close()

            snapshot_name = None
            summary_output = None
            if success:
                if snapshot_mode == 'attic':
                    snapshot_name = datetime.datetime.now().isoformat()
                    attic_dir = '%s.attic' % dest_dir
                    if not os.path.exists(attic_dir):
                        attic_args = ['attic', 'init', attic_dir]
                        self.run_logging(attic_args)
                    attic_args = ['attic', 'create', '--numeric-owner', '%s::%s' % (attic_dir, snapshot_name), '.']
                    self.run_logging(attic_args, cwd=dest_dir)
                    if 'retention' in s:
                        attic_snapshots = re.findall('^([\w\.\-\:]+)', subprocess.check_output(['attic', 'list', attic_dir]), re.M)
                        to_delete = get_snapshots_to_delete(s['retention'], attic_snapshots)
                        for snapshot in to_delete:
                            attic_args = ['attic', 'delete', '%s::%s' % (attic_dir, snapshot)]
                            self.run_logging(attic_args)
                    attic_args = ['attic', 'info', '%s::%s' % (attic_dir, snapshot_name)]
                    (ret, summary_output) = self.run_logging(attic_args, return_output=True)
                elif snapshot_mode == 'link-dest':
                    summary_output = ''
                    if base_snapshot:
                        summary_output = summary_output + 'Base snapshot: %s\n' % base_snapshot
                    snapshot_name = datetime.datetime.now().isoformat()
                    os.rename(os.path.join(snapshot_dir, 'working'), os.path.join(snapshot_dir, snapshot_name))
                    if os.path.exists(os.path.join(snapshot_dir, 'latest')):
                        if os.path.islink(os.path.join(snapshot_dir, 'latest')):
                            os.symlink(snapshot_name, os.path.join(snapshot_dir, 'latest'))
                    else:
                        os.symlink(snapshot_name, os.path.join(snapshot_dir, 'latest'))
                    if 'retention' in s:
                        dirs = [d for d in os.listdir(snapshot_dir) if os.path.isdir(os.path.join(snapshot_dir, d))]
                        to_delete = get_snapshots_to_delete(s['retention'], dirs)
                        for snapshot in to_delete:
                            shutil.rmtree(os.path.join(snapshot_dir, snapshot))
                            summary_output = summary_output + 'Removed old snapshot: %s\n' % snapshot
            else:
                summary_output = 'rsync exited with return code %d' % returncode

            time_end = time.time()
            api_out = {
                'name': self.config['name'],
                'secret': self.config['secret'],
                'machine_uuid': self.arg_uuid,
                'source_name': s['name'],
                'success': success,
                'backup_data': {
                    'snapshot': snapshot_name,
                    'summary': summary_output,
                    'time_begin': time_begin,
                    'time_end': time_end,
                },
            }
            api_reply = api_call(self.config['api_url'], 'storage_ping_source_update', api_out)

            self.logger.info('End: %s %s' % (machine['unit_name'], s['name']))

        self.logger.info('Done')
        lock.close()

    def main(self):
        try:
            return self.process_ping()
        except Exception as e:
            self.logger.exception(e.message)
            return 1


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config-dir', '-c', type=str, default='/etc/turku-storage')
    parser.add_argument('uuid')
    return parser.parse_args()


def main(argv):
    args = parse_args()
    sys.exit(StoragePing(args.uuid, config_dir=args.config_dir).main())
