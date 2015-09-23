# Copyright 2013 Answers for AWS LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from cache import Memorize
from exceptions import *

import boto
from boto import ec2

import time

__all__ = ('GraffitiMonkey', 'Logging')
log = logging.getLogger(__name__)


class GraffitiMonkey(object):
    def __init__(self, region, profile, instance_tags_to_propagate, volume_tags_to_propagate, dryrun, append, volumes_to_tag, snapshots_to_tag, novolumes, nosnapshots):
        # This list of tags associated with an EC2 instance to propagate to
        # attached EBS volumes
        self._instance_tags_to_propagate = instance_tags_to_propagate

        # This is a list of tags associated with a volume to propagate to
        # a snapshot created from the volume
        self._volume_tags_to_propagate = volume_tags_to_propagate

        # The region to operate in
        self._region = region

        # The profile to use
        self._profile = profile

        # Whether this is a dryrun
        self._dryrun = dryrun

        # If we are appending tags
        self._append = append

        # Volumes we will tag
        self._volumes_to_tag = volumes_to_tag

        # Snapshots we will tag
        self._snapshots_to_tag = snapshots_to_tag

        # If we process volumes
        self._novolumes = novolumes

        # If we process snapshots
        self._nosnapshots = nosnapshots

        log.info("Starting Graffiti Monkey")
        log.info("Options: dryrun %s, append %s, novolumes %s, nosnapshots %s", self._dryrun, self._append, self._novolumes, self._nosnapshots)
        log.info("Connecting to region %s using profile %s", self._region, self._profile)
        try:
            self._conn = ec2.connect_to_region(self._region, profile_name=self._profile)
        except boto.exception.NoAuthHandlerFound:
            raise GraffitiMonkeyException('No AWS credentials found - check your credentials')
        except boto.provider.ProfileNotFoundError:
            log.info("Connecting to region %s using default credentials", self._region)
            try:
                self._conn = ec2.connect_to_region(self._region)
            except boto.exception.NoAuthHandlerFound:
                raise GraffitiMonkeyException('No AWS credentials found - check your credentials')




    def propagate_tags(self):
        ''' Propagates tags by copying them from EC2 instance to EBS volume, and
        then to snapshot '''

        if not self._novolumes:
            self.tag_volumes()

        if not self._nosnapshots:
            self.tag_snapshots()

    def tag_volumes(self):
        ''' Gets a list of volumes, and then loops through them tagging
        them '''

        storage_counter = 0
        volumes   = []
        instances = []

        if self._volumes_to_tag:
            log.info('Using volume list from cli/config file')

            volumes = self._conn.get_all_volumes(
                    filters = [{ 'volume-id': self._volumes_to_tag }]
                    )
            volume_ids = [v.id for v in volumes]

            ''' We can't trust the volume list from the config file so we
            test the status of each volume and remove any that raise an exception '''
            for volume_id in self._volumes_to_tag:
                if volume_id not in volume_ids
                    log.info('Volume %s does not exist and will not be tagged', volume_id)
                    self._volumes_to_tag.remove(volume_id)

        else:
            log.info('Getting list of all volumes')
            volumes = self._conn.get_all_volumes()

        if not volumes:
            log.info('No volumes found')
            return True

        ''' Fetching all the relevant instances up-front is more efficient than
        trying to query them one at a time '''
        all_instance_ids = set(v.attach_data.instance_id for v in volumes)
        reservation = self._conn.get_all_instances(
                filters: [{'instance-id': all_instance_ids}]
                )

        # Make this a dict, since we'll need to lookup by instance id
        instances = { i.id: i for i in reservation.instances }

        log.debug('Volume list >%s<', volumes)
        total_vols = len(volumes)
        log.info('Found %d volume(s)', total_vols)
        this_vol = 0
        for volume in volumes:
            this_vol += 1
            storage_counter += volume.size
            log.info ('Processing volume %d of %d total volumes', this_vol, total_vols)

            if volume.status != 'in-use':
                log.debug('Skipping %s as it is not attached to an EC2 instance, so there is nothing to propagate', volume.id)
                continue

            for attempt in range(5):
                try:
                    self.tag_volume(volume, instances)
                except boto.exception.EC2ResponseError, e:
                    log.error("Encountered Error %s on volume %s", e.error_code, volume.id)
                    break
                except boto.exception.BotoServerError, e:
                    log.error("Encountered Error %s on volume %s, waiting %d seconds then retrying", e.error_code, volume.id, attempt)
                    time.sleep(attempt)
                else:
                    break
            else:
                log.error("Encountered Error %s on volume %s, %d retries failed, continuing", e.error_code, volume.id, attempt)
                continue

        log.info('Processed a total of {0} GB of AWS Volumes'.format(storage_counter))
        log.info('Completed processing all volumes')


    def tag_volume(self, volume, instances):
        ''' Tags a specific volume '''

        instance_id = None
        if volume.attach_data.instance_id:
            instance_id = volume.attach_data.instance_id
        device = None
        if volume.attach_data.device:
            device = volume.attach_data.device

        instance_tags = instances[instance_id].tags

        tags_to_set = {}
        if self._append:
            tags_to_set = volume.tags
        for tag_name in self._instance_tags_to_propagate:
            log.debug('Trying to propagate instance tag: %s', tag_name)
            if tag_name in instance_tags:
                value = instance_tags[tag_name]
                tags_to_set[tag_name] = value

        # Additional tags
        tags_to_set['instance_id'] = instance_id
        tags_to_set['device'] = device

        if self._dryrun:
            log.info('DRYRUN: Volume %s would have been tagged %s', volume.id, tags_to_set)
        else:
            self._set_resource_tags(volume, tags_to_set)
        return True


    def tag_snapshots(self):
        ''' Gets a list of snapshots, and then loops through them tagging
        them '''

        snapshots = []
        if self._snapshots_to_tag:
            log.info('Using snapshot list from cli/config file')

            log.info('Using volume list from cli/config file')

            snapshots = self._conn.get_all_snapshots(
                    owner = 'self',
                    filters = {
                        'snapshot-id': self._snapshots_to_tag,
                        }
                    )
            snapshot_ids = [s.id for s in snapshots]

            ''' We can't trust the snapshot list from the config file so we
            test the status of each and remove any that raise an exception '''
            for snapshot_id in self._snapshots_to_tag:
                if volume_id not in volume_ids
                    log.info('Snapshot %s does not exist and will not be tagged', snapshot_id)
                    self._snapshots_to_tag.remove(snapshot)
        else:
            log.info('Getting list of all snapshots')
            snapshots = self._conn.get_all_snapshots(owner='self')

        if not snapshots:
            log.info('No snapshots found')
            return True

        ''' Fetching all the relevant volumes up-front is more efficient than
        trying to query them one at a time '''
        all_volume_ids = set(s.volume_id for s in snapshots)
        volumes = self._conn.get_all_volumes(
                filters: [{'volume-id': all_volume_ids}]
                )

        # Make this a dict, since we'll need to lookup by volume id
        volumes = { v.id: v for v in volumes }

        log.debug('Snapshot list >%s<', snapshots)
        total_snaps = len(snapshots)
        log.info('Found %d snapshot(s)', total_snaps)
        this_snap = 0

        for snapshot in snapshots:
            this_snap += 1
            log.info ('Processing snapshot %d of %d total snapshots', this_snap, total_snaps)
            for attempt in range(5):
                try:
                    self.tag_snapshot(snapshot, volumes)
                except boto.exception.EC2ResponseError, e:
                    log.error("Encountered Error %s on snapshot %s", e.error_code, snapshot.id)
                    break
                except boto.exception.BotoServerError, e:
                    log.error("Encountered Error %s on snapshot %s, waiting %d seconds then retrying", e.error_code, snapshot.id, attempt)
                    time.sleep(attempt)
                else:
                    break
            else:
                log.error("Encountered Error %s on snapshot %s, %d retries failed, continuing", e.error_code, snapshot.id, attempt)
                continue
        log.info('Completed processing all snapshots')

    def tag_snapshot(self, snapshot, volumes):
        ''' Tags a specific snapshot '''

        volume_id = snapshot.volume_id
#        if volume_id == '':
#            log.debug('Skipping %s as it does not have volume information', snapshot.id)
#            continue

        volume_tags = volumes[volume_id].tags

        tags_to_set = {}
        if self._append:
            tags_to_set = snapshot.tags
        for tag_name in self._volume_tags_to_propagate:
            log.debug('Trying to propagate volume tag: %s', tag_name)
            if tag_name in volume_tags:
                tags_to_set[tag_name] = volume_tags[tag_name]

        if self._dryrun:
            log.info('DRYRUN: Snapshot %s would have been tagged %s', snapshot.id, tags_to_set)
        else:
            self._set_resource_tags(snapshot, tags_to_set)
        return True



    def _set_resource_tags(self, resource, tags):
        ''' Sets the tags on the given AWS resource '''

        if not isinstance(resource, ec2.ec2object.TaggedEC2Object):
            msg = 'Resource %s is not an instance of TaggedEC2Object' % resource
            raise GraffitiMonkeyException(msg)

        delta_tags = {}

        for tag_key, tag_value in tags.iteritems():
            if not tag_key in resource.tags or resource.tags[tag_key] != tag_value:
                delta_tags[tag_key] = tag_value

        if len(delta_tags) == 0:
            return

        resource.add_tags(delta_tags)


class Logging(object):
    # Logging formats
    _log_simple_format = '%(asctime)s [%(levelname)s] %(message)s'
    _log_detailed_format = '%(asctime)s [%(levelname)s] [%(name)s(%(lineno)s):%(funcName)s] %(message)s'

    def configure(self, verbosity = None):
        ''' Configure the logging format and verbosity '''

        # Configure our logging output
        if verbosity >= 2:
            logging.basicConfig(level=logging.DEBUG, format=self._log_detailed_format, datefmt='%F %T')
        elif verbosity >= 1:
            logging.basicConfig(level=logging.INFO, format=self._log_detailed_format, datefmt='%F %T')
        else:
            logging.basicConfig(level=logging.INFO, format=self._log_simple_format, datefmt='%F %T')

        # Configure Boto's logging output
        if verbosity >= 4:
            logging.getLogger('boto').setLevel(logging.DEBUG)
        elif verbosity >= 3:
            logging.getLogger('boto').setLevel(logging.INFO)
        else:
            logging.getLogger('boto').setLevel(logging.CRITICAL)
