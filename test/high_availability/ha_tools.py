# Copyright 2009-2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for testing high availability in PyMongo."""

import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time

import pymongo

home = os.environ.get('HOME')
default_dbpath = os.path.join(home, 'data', 'pymongo_high_availability')
dbpath = os.environ.get('DBPATH', default_dbpath)
default_logpath = os.path.join(home, 'log', 'pymongo_high_availability')
logpath = os.environ.get('LOGPATH', default_logpath)
hostname = os.environ.get('HOSTNAME', socket.gethostname())
port = int(os.environ.get('DBPORT', 27017))
mongod = os.environ.get('MONGOD', 'mongod')
mongos = os.environ.get('MONGOS', 'mongos')
set_name = os.environ.get('SETNAME', 'repl0')
use_greenlets = bool(os.environ.get('GREENLETS'))

nodes = {}
routers = {}
cur_port = port


def kill_members(members, sig, hosts=nodes):
    for member in members:
        try:
            proc = hosts[member]['proc']
            # Not sure if cygwin makes sense here...
            if sys.platform in ('win32', 'cygwin'):
                os.kill(proc.pid, signal.CTRL_C_EVENT)
            else:
                os.kill(proc.pid, sig)
        except OSError:
            pass  # already dead


def kill_all_members():
    kill_members(nodes.keys(), 2, nodes)
    kill_members(routers.keys(), 2, routers)


def wait_for(proc, port_num):
    trys = 0
    while proc.poll() is None and trys < 160:
        trys += 1
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                s.connect((hostname, port_num))
                return True
            except (IOError, socket.error):
                time.sleep(0.25)
        finally:
            s.close()

    kill_all_members()
    return False


def start_replica_set(members, fresh=True):
    global cur_port

    if fresh:
        if os.path.exists(dbpath):
            try:
                shutil.rmtree(dbpath)
            except OSError:
                pass

    cur_port = port

    for i in xrange(len(members)):
        cur_port = cur_port + i
        host = '%s:%d' % (hostname, cur_port)
        members[i].update({'_id': i, 'host': host})
        path = os.path.join(dbpath, 'db' + str(i))
        if not os.path.exists(path):
            os.makedirs(path)
        member_logpath = os.path.join(logpath, 'db' + str(i) + '.log')
        if not os.path.exists(os.path.dirname(member_logpath)):
            os.makedirs(os.path.dirname(member_logpath))
        cmd = [mongod,
               '--dbpath', path,
               '--port', str(cur_port),
               '--replSet', set_name,
               '--nojournal', '--oplogSize', '64',
               '--logappend', '--logpath', member_logpath]
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        nodes[host] = {'proc': proc, 'cmd': cmd}
        res = wait_for(proc, cur_port)
        if not res:
            return None
    config = {'_id': set_name, 'members': members}
    primary = members[0]['host']
    c = pymongo.Connection(primary, use_greenlets=use_greenlets)
    try:
        c.admin.command('replSetInitiate', config)
    except pymongo.errors.OperationFailure:
        # Already initialized from a previous run?
        pass

    expected_arbiters = 0
    for member in members:
        if member.get('arbiterOnly'):
            expected_arbiters += 1
    expected_secondaries = len(members) - expected_arbiters - 1

    # Wait for 8 minutes for replica set to come up
    patience = 8
    for _ in range(patience * 60 / 2):
        time.sleep(2)
        try:
            if (get_primary() and
                len(get_secondaries()) == expected_secondaries and
                len(get_arbiters()) == expected_arbiters):
                break
        except pymongo.errors.AutoReconnect:
            # Keep waiting
            pass
    else:
        kill_all_members()
        raise Exception(
            "Replica set still not initalized after %s minutes" % patience)
    return primary, set_name


def create_sharded_cluster(num_routers=3):
    global cur_port

    # Start a config server
    cur_port = port
    configdb_host = '%s:%d' % (hostname, cur_port)
    path = os.path.join(dbpath, 'configdb')
    if not os.path.exists(path):
        os.makedirs(path)
    configdb_logpath = os.path.join(logpath, 'configdb.log')
    cmd = [mongod,
           '--dbpath', path,
           '--port', str(cur_port),
           '--nojournal', '--logappend',
           '--logpath', configdb_logpath]
    proc = subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    nodes[configdb_host] = {'proc': proc, 'cmd': cmd}
    res = wait_for(proc, cur_port)
    if not res:
        return None

    # ...and a shard server
    cur_port = cur_port + 1
    shard_host = '%s:%d' % (hostname, cur_port)
    path = os.path.join(dbpath, 'shard1')
    if not os.path.exists(path):
        os.makedirs(path)
    db_logpath = os.path.join(logpath, 'shard1.log')
    cmd = [mongod,
           '--dbpath', path,
           '--port', str(cur_port),
           '--nojournal', '--logappend',
           '--logpath', db_logpath]
    proc = subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    nodes[shard_host] = {'proc': proc, 'cmd': cmd}
    res = wait_for(proc, cur_port)
    if not res:
        return None

    # ...and a few mongos instances
    cur_port = cur_port + 1
    for i in xrange(num_routers):
        cur_port = cur_port + i
        host = '%s:%d' % (hostname, cur_port)
        mongos_logpath = os.path.join(logpath, 'mongos' + str(i) + '.log')
        cmd = [mongos,
               '--port', str(cur_port),
               '--logappend',
               '--logpath', mongos_logpath,
               '--configdb', configdb_host]
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        routers[host] = {'proc': proc, 'cmd': cmd}
        res = wait_for(proc, cur_port)
        if not res:
            return None

    # Add the shard
    conn = pymongo.Connection(host)
    try:
        conn.admin.command({'addshard': shard_host})
    except pymongo.errors.OperationFailure:
        # Already configured.
        pass

    return get_mongos_seed_list()


# Connect to a random member
def get_connection():
    return pymongo.Connection(nodes.keys(), slave_okay=True, use_greenlets=use_greenlets)


def get_mongos_seed_list():
    members = routers.keys()
    return ','.join(members)


def kill_mongos(host):
    kill_members([host], 2, hosts=routers)
    return host


def restart_mongos(host):
    restart_members([host], True)


def get_members_in_state(state):
    status = get_connection().admin.command('replSetGetStatus')
    members = status['members']
    return [k['name'] for k in members if k['state'] == state]


def get_primary():
    try:
        primaries = get_members_in_state(1)
        assert len(primaries) <= 1
        if primaries:
            return primaries[0]
    except pymongo.errors.AutoReconnect:
        pass

    return None


def get_random_secondary():
    secondaries = get_members_in_state(2)
    if len(secondaries):
        return random.choice(secondaries)
    return None


def get_secondaries():
    return get_members_in_state(2)


def get_arbiters():
    return get_members_in_state(7)


def get_passives():
    return get_connection().admin.command('ismaster').get('passives', [])


def get_hosts():
    return get_connection().admin.command('ismaster').get('hosts', [])


def get_hidden_members():
    # Both 'hidden' and 'slaveDelay'
    secondaries = get_secondaries()
    readers = get_hosts() + get_passives()
    for member in readers:
        try:
            secondaries.remove(member)
        except:
            # Skip primary
            pass
    return secondaries


def get_tags(member):
    config = get_connection().local.system.replset.find_one()
    for m in config['members']:
        if m['host'] == member:
            return m.get('tags', {})

    raise Exception('member %s not in config' % repr(member))


def kill_primary(sig=2):
    primary = get_primary()
    kill_members([primary], sig)
    return primary


def kill_secondary(sig=2):
    secondary = get_random_secondary()
    kill_members([secondary], sig)
    return secondary


def kill_all_secondaries(sig=2):
    secondaries = get_secondaries()
    kill_members(secondaries, sig)
    return secondaries


def stepdown_primary():
    primary = get_primary()
    if primary:
        c = pymongo.Connection(primary, use_greenlets=use_greenlets)
        # replSetStepDown causes mongod to close all connections
        try:
            c.admin.command('replSetStepDown', 20)
        except:
            pass


def restart_members(members, router=False):
    restarted = []
    for member in members:
        if router:
            cmd = routers[member]['cmd']
        else:
            cmd = nodes[member]['cmd']
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        if router:
            routers[member]['proc'] = proc
        else:
            nodes[member]['proc'] = proc
        res = wait_for(proc, int(member.split(':')[1]))
        if res:
            restarted.append(member)
    return restarted
